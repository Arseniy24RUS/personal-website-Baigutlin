#!/usr/bin/env python3
"""Isolated refresh, mandatory validation and explicit provider health.

Collectors never write into the checkout that will be published. Public data
is promoted only after successful schema/content/retention checks.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from report_safety import sanitize, sanitize_public_tree
from source_health import component_state, is_verified, load_checkpoint, materialize_checkpoint, write_checkpoint

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ('open', 'harvest_open_sources.py', 'data/open/harvest_report.json', 360),
    ('media', 'harvest_media_mentions.py', 'data/media/harvest_report.json', 1200),
    ('elibrary', 'harvest_elibrary_browser.py', 'data/elibrary/browser_fetch_report.json', 900),
    ('wos', 'harvest_wos.py', 'data/wos/harvest_report.json', 1380),
    ('scopus', 'harvest_scopus.py', 'data/scopus/scopus_author_57211062810_access_report.json', 360),
]
DERIVED = [
    ('build_public_data.py', 120),
    ('merge_wos_records_into_public_data.py', 120),
    ('translate_publication_titles.py', 240),
    ('enrich_publication_metadata.py', 300),
    ('sanitize_publication_references.py', 120),
]
OPTIONAL_DERIVED = {
    'translate_publication_titles.py': (
        'data/public/publications.json', 'data/public/publications.tsv',
        'data/curation/publication_title_translations.json',
        'data/audit/publication_title_translation_report.json',
    ),
    'enrich_publication_metadata.py': (
        'data/public/publications.json', 'data/public/publications.tsv',
        'data/curation/crossref_metadata_cache.json',
        'data/audit/publication_metadata_enrichment_report.json',
    ),
}


def derive(stage: Path):
    """Optional network enrichment is transactional; core generation must pass."""
    steps = []
    report_path = stage / 'data/audit/derived_steps.json'
    for script, timeout in DERIVED:
        optional = script in OPTIONAL_DERIVED
        backup = {name: (stage / name).read_bytes() if (stage / name).exists() else None
                  for name in OPTIONAL_DERIVED.get(script, ())}
        step = {'script': script, 'optional': optional, 'attempted_at': now(), 'status': 'running'}
        steps.append(step)
        write(report_path, {'steps': steps})
        print(f'Building: {script}', flush=True)
        code, reason = run(script, stage, timeout)
        if optional and not code:
            try:
                if not isinstance(read(stage / 'data/public/publications.json'), list):
                    raise ValueError('Publication list required.')
                for name in backup:
                    if name.endswith('.json') and (stage / name).exists():
                        read(stage / name)
            except (ValueError, TypeError):
                code, reason = 1, 'invalid_enrichment_output'
        if code and optional:
            for name, content in backup.items():
                path = stage / name
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
        step.update(status='error' if code else 'success', exit_code=code,
                    reason=reason, completed_at=now())
        write(report_path, {'steps': steps})
        if code and not optional:
            raise RuntimeError(f'Mandatory derived-data step failed: {script} ({reason}).')
        if code:
            print(f'Optional enrichment unavailable: {script} ({reason}); collected data retained.', flush=True)

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize(value), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def audit_directory(scope='portfolio'):
    return 'data/it/audit' if scope == 'it' else 'data/audit'


def prepare(destination: Path, scope='portfolio'):
    if destination.exists():
        raise RuntimeError('Staging destination must be a new directory.')
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    destination.mkdir(parents=True)
    for name in filter(None, tracked):
        source = ROOT / name
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    audit = destination / audit_directory(scope)
    if scope == 'it' and audit.exists():
        shutil.rmtree(audit)
    write(audit / 'refresh_run.json', {'attempted_at': now(), 'base_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'state': 'collecting', 'scope': scope})
    # A failed run must not export yesterday's validation as its own evidence.
    for name in ('collector_steps.json', 'derived_steps.json', 'refresh_pipeline_audit.json',
                 'retention_report.json', 'translation_model_setup.json',
                 'publication_title_translation_report.json', 'publication_metadata_enrichment_report.json'):
        (audit / name).unlink(missing_ok=True)
    print('Isolated source and content snapshot prepared.')

def run(script, cwd, timeout, args=()):
    try:
        # Subprocess output is intentionally not copied to public reports. Older
        # parsers can include HTML, response headers or a session URL in errors.
        result = subprocess.run([sys.executable, str(cwd / 'scripts' / script), *args], cwd=cwd,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return result.returncode, 'completed' if result.returncode == 0 else 'nonzero_exit'
    except subprocess.TimeoutExpired:
        return 124, 'timeout'

def current_observation(report, previous, attempted):
    """A process exit code is not evidence of a new provider observation."""
    if not isinstance(report, dict) or report == previous:
        return False
    try:
        started = datetime.fromisoformat(attempted.replace('Z', '+00:00'))
        observed = datetime.fromisoformat(report['attempted_at'].replace('Z', '+00:00'))
        if observed < started:
            return False
        if report.get('status') == 'success':
            succeeded = datetime.fromisoformat(report['last_success_at'].replace('Z', '+00:00'))
            return (succeeded >= started and report.get('origin') == 'live'
                    and report.get('complete') is True)
        return report.get('status') in ('partial', 'blocked', 'error')
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def collect_it(stage: Path):
    """Public GitHub discovery has no dependency on scientific source sessions."""
    from it_resources import validate_it_resources
    audit = stage / audit_directory('it')
    attempted = now()
    state = read(audit / 'refresh_run.json', {})
    state.update(state='collecting', scope='it', selected_sources=['github'])
    write(audit / 'refresh_run.json', state)
    before = read(audit / 'harvest_report.json', {})
    code, reason = run('harvest_it_resources.py', stage, 2400, ('--root', str(stage)))
    report = read(audit / 'harvest_report.json', {})
    if not current_observation(report, before, attempted) or (code and report.get('status') == 'success'):
        report = {**report, 'status': 'error', 'attempted_at': attempted,
                  'last_success_at': before.get('last_success_at'), 'origin': 'snapshot',
                  'complete': False, 'reason': reason if code else 'missing_current_source_report'}
    write(audit / 'harvest_report.json', report)
    write(audit / 'collector_steps.json', {'attempted_at': attempted, 'steps': [
        {'source': 'github', 'exit_code': code, 'reason': reason, 'attempted_at': attempted}]})
    issues = validate_it_resources(stage)
    if issues:
        write(audit / 'validation_report.json', {'status': 'error', 'issues': issues})
        raise RuntimeError('IT candidate failed structural validation.')
    state.update(state='ready', completed_at=now())
    write(audit / 'refresh_run.json', state)
    print(f'GitHub discovery: {report.get("status")}; safe IT candidate ready.', flush=True)


def collect(stage: Path, only: str, scope='portfolio'):
    if scope == 'it':
        if only:
            raise RuntimeError('--only is not supported for IT collection.')
        return collect_it(stage)
    if os.environ.get('HOME_VPN_REQUIRED') != '1':
        raise RuntimeError('Production collection requires the verified home tunnel.')
    steps = []
    known = {s[0] for s in SOURCES}
    wanted = {name.strip() for name in only.split(',') if name.strip()} if only else known
    if not wanted or wanted - known:
        raise RuntimeError('Select only known collection sources.')
    state = read(stage / 'data/audit/refresh_run.json', {})
    state.update(state='collecting', selected_sources=sorted(wanted))
    write(stage / 'data/audit/refresh_run.json', state)
    for source, script, report_path, timeout in SOURCES:
        if source not in wanted:
            continue
        attempted = now()
        before = read(stage / report_path, {})
        code, reason = run(script, stage, timeout)
        checkpoint_path = stage / 'data' / source / 'collection_checkpoint.json'
        checkpoint = load_checkpoint(checkpoint_path) if source in ('elibrary', 'wos') else None
        recovered = checkpoint and current_observation(checkpoint['report'], before, attempted)
        if recovered:
            # The atomic envelope may have been committed just before a timeout
            # or interruption during legacy-file materialization.
            materialize_checkpoint(checkpoint_path, source)
        report = read(stage / report_path, {})
        current = current_observation(report, before, attempted)
        if not current or (code and report.get('status') == 'success'):
            reason = reason if code else 'missing_current_source_report'
            report.update(status='error', attempted_at=attempted,
                          last_success_at=before.get('last_success_at'), origin='snapshot',
                          complete=False, reason=reason, record_count=before.get('record_count'))
        component_names = set((report.get('components') or {})) | set((before.get('components') or {}))
        if component_names:
            components = report.setdefault('components', {})
            for name in component_names:
                prior = component_state(before, name)
                state_component = components.get(name) or {}
                uncommitted = bool(code and source in ('elibrary', 'wos') and not recovered)
                if uncommitted or not current_observation(state_component, prior, attempted):
                    state_component = {**state_component, 'status': 'error', 'attempted_at': attempted,
                                       'last_success_at': prior.get('last_success_at'), 'origin': 'snapshot',
                                       'complete': False, 'record_count': prior.get('record_count'),
                                       'reason': 'missing_atomic_checkpoint' if uncommitted else 'missing_current_component_report'}
                components[name] = state_component
            core = [components.get(name, {}) for name in ('metrics', 'publications')]
            if not all(is_verified(item) for item in core):
                report.update(status='partial' if any(is_verified(item) for item in core) else
                              (report.get('status') if report.get('status') in ('blocked', 'error') else 'error'),
                              complete=False, reason=report.get('reason') or 'incomplete_components')
        stale_providers = False
        for key, provider in report.get('providers', {}).items() if isinstance(report.get('providers'), dict) else []:
            previous = (before.get('providers') or {}).get(key, {})
            if not current_observation(provider, previous, attempted):
                stale_providers = True
                provider.update(status='error', attempted_at=attempted,
                                last_success_at=previous.get('last_success_at'), origin='snapshot',
                                complete=False, reason='missing_current_source_report')
        if stale_providers and report.get('status') == 'success':
            any_success = any(p.get('status') == 'success' for p in report['providers'].values())
            report.update(status='partial' if any_success else 'error', complete=False,
                          origin='snapshot', last_success_at=before.get('last_success_at'),
                          reason='one_or_more_providers_not_current')
        write(stage / report_path, report)
        if recovered:
            write_checkpoint(checkpoint_path, report, checkpoint['payloads'])
        steps.append({'source': source, 'exit_code': code, 'reason': reason, 'attempted_at': attempted})
        print(f'{source}: {report.get("status", "error")} (exit {code})', flush=True)
    write(stage / 'data/audit/collector_steps.json', {'attempted_at': now(), 'steps': steps})
    derive(stage)
    sanitize_public_tree(stage)
    code, _ = run('audit_refresh_pipeline.py', stage, 120)
    if code not in (0, 2):
        raise RuntimeError('Refreshed data failed structural audit.')
    state = read(stage / 'data/audit/refresh_run.json', {})
    state.update(state='ready', completed_at=now(), selected_sources=sorted(wanted))
    write(stage / 'data/audit/refresh_run.json', state)
    print('Candidate data prepared; publication still requires retention and UI checks.')

def promote(stage: Path, destination: Path = ROOT, scope='portfolio'):
    if read(stage / audit_directory(scope) / 'refresh_run.json', {}).get('state') != 'ready':
        raise RuntimeError('Only a completely built candidate can be promoted.')
    from it_resources import merge_it_resources
    if scope == 'it':
        merge_it_resources(stage, destination)
        print('Only IT additions copied; published cards and scientific data retained.')
        return
    # Merge before copying the rest of data, so a stale scientific snapshot
    # cannot overwrite cards added by the independent IT workflow.
    merge_it_resources(stage, destination)
    for name in ('data', 'assets/media/mentions', 'assets/craftum'):
        source = stage / name
        if source.exists():
            shutil.copytree(source, destination / name, dirs_exist_ok=True,
                            ignore=(lambda directory, names: ['it'] if Path(directory) == stage / 'data' else []))
    sanitize_public_tree(destination)
    print('Candidate copied; previous files were retained.')

def health(root: Path = ROOT, scope='portfolio'):
    if scope == 'it':
        audit = root / audit_directory(scope)
        report = read(audit / 'harvest_report.json', {})
        ready = read(audit / 'refresh_run.json', {}).get('state') == 'ready'
        healthy = ready and report.get('status') == 'success' and report.get('complete') is True and report.get('origin') == 'live'
        result = {'healthy': healthy, 'status': report.get('status', 'error'),
                  'pending': report.get('pending', 0), 'reason': report.get('reason')}
        write(audit / 'source_health_check.json', result)
        text = ('### IT resource discovery\n\n' +
                ('PASS' if healthy else 'NEEDS ATTENTION — published resources preserved') +
                f'; pending: {result["pending"]}; reason: {result["reason"] or "none"}\n')
        print(text)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as output:
                output.write(text)
        return 0 if healthy else 2
    profile = read(root / 'data/public/profile.json', {})
    sources = profile.get('source_health', {})
    run_state = read(root / 'data/audit/refresh_run.json', {})
    selected = set(run_state.get('selected_sources') or (s[0] for s in SOURCES))
    results = {}
    observations = {}
    for key in ('elibrary', 'wos', 'scopus'):
        if key in selected:
            state = sources.get(key, {})
            observations[key] = state
            results[key] = all(is_verified(component_state(state, name)) for name in ('metrics', 'publications'))
    if 'media' in selected:
        media = read(root / 'data/media/harvest_report.json', {})
        observations['media'] = media
        results['media'] = (media.get('status') in ('success', 'partial') and media.get('origin') == 'live'
                            and bool(media.get('last_success_at'))
                            and media.get('required_sources_ok', media.get('status') == 'success') is True)
    if 'open' in selected:
        opening = read(root / 'data/open/harvest_report.json', {})
        observations['open'] = opening
        results['open'] = (opening.get('status') == 'success' and opening.get('origin') == 'live'
                           and opening.get('complete') is True and bool(opening.get('last_success_at')))
    if run_state.get('state') not in (None, 'ready'):
        results['pipeline_completed'] = False
    details = {key: {field: value.get(field) for field in ('status', 'origin', 'complete', 'record_count', 'pending', 'reason')}
               for key, value in observations.items()}
    for key, value in observations.items():
        if isinstance(value.get('components'), dict):
            details[key]['components'] = value['components']
    report = {'checked_at': now(), 'healthy': all(results.values()), 'checks': results, 'details': details}
    write(root / 'data/audit/source_health_check.json', report)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    lines = ['### Source collection', '', '| Source | Availability | Coverage / reason |', '|---|---|---|']
    for key, ok in results.items():
        detail = details.get(key, {})
        coverage = 'complete' if detail.get('complete') else (detail.get('reason') or 'incomplete')
        if detail.get('pending'):
            coverage += f'; pending: {detail["pending"]}'
        lines.append(f'| {key} | {"PASS" if ok else "NEEDS ATTENTION — previous data preserved"} | {coverage} |')
        for name, component in detail.get('components', {}).items():
            observed = component.get('last_success_at') or 'no verified date'
            component_coverage = 'complete' if is_verified(component) else component.get('reason') or 'incomplete'
            lines.append(f'| {key} / {name} | {"PASS" if is_verified(component) else "NEEDS ATTENTION"} | {component_coverage}; last verified: {observed} |')
    enrichment = read(root / 'data/audit/derived_steps.json', {}).get('steps', [])
    incomplete = [step for step in enrichment if step.get('status') != 'success']
    if incomplete:
        lines += ['', 'Optional enrichment requiring attention:']
        lines += [f'- {step["script"]}: {step.get("reason", "not completed")}; collected data retained.' for step in incomplete if step.get('optional')]
    print('\n'.join(lines))
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    return 0 if report['healthy'] else 2

def diagnostics(stage: Path, destination: Path, scope='portfolio'):
    if scope == 'it':
        source = stage / audit_directory(scope)
        allowed = ('refresh_run.json', 'harvest_report.json', 'collector_steps.json',
                   'retention_report.json', 'validation_report.json', 'source_health_check.json',
                   'live_probe.json',
                   'translation_model_setup_ru_en.json', 'translation_model_setup_en_ru.json')
        for name in allowed:
            if (source / name).is_file():
                write(destination / audit_directory(scope) / name, read(source / name))
        print('Only sanitized IT diagnostic JSON exported.')
        return
    state = read(stage / 'data/audit/refresh_run.json', {})
    selected = set(state.get('selected_sources', []))
    paths = [item[2] for item in SOURCES if item[0] in selected] + ['data/audit/refresh_run.json', 'data/audit/collector_steps.json', 'data/audit/derived_steps.json', 'data/audit/translation_model_setup.json', 'data/audit/publication_title_translation_report.json', 'data/audit/publication_metadata_enrichment_report.json', 'data/audit/refresh_pipeline_audit.json', 'data/audit/retention_report.json']
    exported, omitted = [], []
    for name in paths:
        path = stage / name
        if path.exists():
            payload = read(path, {})
            observed = payload.get('attempted_at') or payload.get('generated_at')
            if observed and observed < state.get('attempted_at', ''):
                omitted.append(name)
                continue
            write(destination / name, payload)
            exported.append(name)
    write(destination / 'data/audit/diagnostic_manifest.json', {
        'attempted_at': state.get('attempted_at'), 'state': state.get('state'),
        'selected_sources': sorted(selected), 'exported': exported,
        'omitted_previous_run_reports': omitted,
    })
    print('Only sanitized diagnostic JSON exported; sessions and raw responses excluded.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'collect', 'promote', 'health', 'diagnostics'])
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--only', default='')
    parser.add_argument('--scope', choices=['portfolio', 'it'], default='portfolio')
    args = parser.parse_args()
    if args.command == 'prepare': prepare(args.stage.resolve(), args.scope)
    elif args.command == 'collect': collect(args.stage.resolve(), args.only, args.scope)
    elif args.command == 'promote': promote(args.stage.resolve(), scope=args.scope)
    elif args.command == 'diagnostics': diagnostics(args.stage.resolve(), args.output.resolve(), args.scope)
    else: return health(args.stage.resolve() if args.stage else ROOT, args.scope)
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Exceptions here are generated internally, never raw provider errors.
        print(f'Refresh stopped: {type(exc).__name__}: {sanitize(str(exc))}', file=sys.stderr)
        sys.exit(1)
