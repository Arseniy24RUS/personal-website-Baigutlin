#!/usr/bin/env python3
"""Publish validated candidate data, recombining with newer main on a race."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from source_health import component_state, load_checkpoint, write_checkpoint, materialize_checkpoint

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATHS = ('data', 'assets/media/mentions', 'assets/craftum', 'assets/it/thumbs')
IT_PATHS = ('data/it', 'assets/it/thumbs')


def publication_paths(scope='portfolio'):
    return IT_PATHS if scope == 'it' else PORTFOLIO_PATHS


def synchronize_published(source, destination, scope):
    for name in publication_paths(scope):
        if (source / name).exists():
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True)


def allowed_it_path(name):
    return (name.startswith('data/it/') and not name.startswith('data/it/audit/')) or name.startswith('assets/it/thumbs/')


def assert_it_scope(path, baseline):
    """A scoped publication cannot carry unrelated candidate changes to main."""
    names = set(git('diff', '--name-only', baseline, '--', '.', cwd=path).splitlines())
    names.update(git('ls-files', '--others', '--exclude-standard', cwd=path).splitlines())
    forbidden = sorted(name for name in names if not allowed_it_path(name))
    if forbidden:
        raise RuntimeError('IT publication attempted to modify paths outside its scope: ' + ', '.join(forbidden))


def copy_candidate(candidate, destination, scope='portfolio'):
    """Always merge published IT cards, even when main has not moved."""
    from it_resources import merge_it_resources
    merge_it_resources(candidate, destination)
    if scope == 'it':
        return
    for name in ('data', 'assets/media/mentions', 'assets/craftum'):
        if (candidate / name).exists():
            shutil.copytree(candidate / name, destination / name, dirs_exist_ok=True,
                            ignore=lambda directory, names: ['it'] if Path(directory) == candidate / 'data' else [])

def git(*args, cwd=ROOT):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()

def read(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def records(value):
    return value if isinstance(value, list) else value.get('records', [])

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return float('-inf')


def merge_source_report(previous, incoming, record_count=None):
    """Attempt diagnostics and successful observations have separate clocks."""
    reports = [report for report in (previous, incoming) if report]
    if not reports:
        return {}
    # Reports have second resolution. On a tie, retain failure rather than claim
    # that an indistinguishable success superseded it.
    latest = max(reports, key=lambda report: (
        timestamp(report.get('attempted_at') or report.get('generated_at')),
        report.get('status') != 'success' or report.get('complete') is not True,
    ))
    merged = dict(latest)
    successes = [report.get('last_success_at') for report in reports if report.get('last_success_at')]
    merged['last_success_at'] = max(successes, key=timestamp) if successes else None
    observations = [report.get('last_observation_at') for report in reports if report.get('last_observation_at')]
    if observations:
        merged['last_observation_at'] = max(observations, key=timestamp)
    names = set().union(*(report.get('components', {}).keys() for report in reports))
    if names:
        merged['components'] = {name: merge_source_report(component_state(previous, name), component_state(incoming, name))
                                for name in names}
    if record_count is not None:
        merged['record_count'] = record_count
    return merged


def provider_payloads(root, provider, report_name):
    directory = root / 'data' / provider
    report = read(directory / report_name, {})
    checkpoint = load_checkpoint(directory / 'collection_checkpoint.json')
    if checkpoint and timestamp(checkpoint['report'].get('attempted_at')) >= timestamp(report.get('attempted_at')):
        return checkpoint['report'], checkpoint['payloads']
    if provider == 'elibrary':
        payloads = {'metrics': read(directory / 'profile_metrics.json', {}),
                    'publications': read(root / 'data/processed/elibrary_publications.json', []),
                    'details': read(directory / 'item_details.json', {})}
    elif provider == 'wos':
        payload = read(directory / 'profile_metrics.json', {})
        payloads = {'metrics': {k: v for k, v in payload.items() if k not in ('records', 'records_count_on_page')},
                    'publications': payload.get('records', []), 'details': {}}
    else:
        rows = read(directory / 'scopus_author_57211062810_works.json', [])
        payloads = {'metrics': read(directory / 'scopus_author_57211062810_metrics.json', {}),
                    'publications': rows.get('works', []) if isinstance(rows, dict) else rows, 'details': {}}
    return report, payloads


def source_row_key(row):
    for key in ('elibrary_item_id', 'wos_uid', 'eid', 'doi', 'id'):
        if row.get(key):
            return key, str(row[key]).lower()
    return 'title', str(row.get('title', '')).lower(), str(row.get('year', ''))


def merge_observed_rows(previous, incoming, previous_state, incoming_state):
    """Union verified partial pages; never erase a row missing from a response."""
    from build_public_data import (index_source_aliases, match_source_aliases,
                                   remember_source_aliases, citation_observation)
    output = copy.deepcopy(previous)
    keys = {source_row_key(row): i for i, row in enumerate(output)}
    aliases = {}
    for position, row in enumerate(output):
        index_source_aliases(aliases, row, position)
    new_snapshot = timestamp(incoming_state.get('last_success_at')) > timestamp(previous_state.get('last_success_at'))
    for row in incoming:
        key = source_row_key(row)
        observed = timestamp(row.get('observed_at'))
        if not new_snapshot and observed == float('-inf'):
            continue
        position, ambiguous = match_source_aliases(aliases, row)
        if ambiguous:
            continue
        if position is None and key[0] == 'title':
            position = keys.get(key)
        if position is None:
            keys[key] = len(output)
            output.append(copy.deepcopy(row))
            index_source_aliases(aliases, output[-1], len(output) - 1)
            continue
        old = output[position]
        prior = timestamp(old.get('observed_at') or previous_state.get('last_success_at'))
        newer = observed > prior if row.get('observed_at') else new_snapshot
        old_citation = old.get('wos_citations')
        old_stamp = citation_observation(old, 'wos')
        if (not old_stamp and 'wos' not in (old.get('citation_observed_at') or {})
                and 'wos_citations' not in (old.get('retained_citation_fields') or [])):
            old_stamp = previous_state.get('last_success_at')
        retained = 'wos_citations' in (row.get('retained_citation_fields') or [])
        explicit = row.get('citation_observed_at') or {}
        new_stamp = (explicit.get('wos') if 'wos' in explicit else
                     None if retained else row.get('observed_at') or incoming_state.get('last_success_at'))
        remember_source_aliases(old, row)
        def enrich(target, values):
            for field, value in values.items():
                if target is old and field in {'wos_citations', 'citation_observed_at', 'retained_citation_fields',
                                                'wos_uid_aliases', 'doi_aliases'}:
                    continue
                if target is old and field in {'id', 'elibrary_item_id', 'wos_uid', 'eid', 'doi', 'source'} and target.get(field):
                    continue
                if field == 'sources':
                    existing = target.get(field) or []
                    existing = [existing] if isinstance(existing, str) else existing
                    additions = [value] if isinstance(value, str) else value or []
                    target[field] = list(dict.fromkeys([*existing, *additions]))
                    continue
                if isinstance(value, dict) and isinstance(target.get(field), dict):
                    enrich(target[field], value)
                elif value not in (None, '', [], {}) and (newer or target.get(field) in (None, '')):
                    target[field] = copy.deepcopy(value)
        enrich(old, row)
        sources = old.get('sources') or []
        sources = [sources] if isinstance(sources, str) else sources
        if row.get('source') or old.get('source'):
            old['sources'] = list(dict.fromkeys([*sources, *filter(None, (old.get('source'), row.get('source')))]))
        stamps = old.setdefault('citation_observed_at', {}) if old.get('citation_observed_at') or explicit else {}
        for provider, stamp in explicit.items():
            if provider != 'wos' and timestamp(stamp) > timestamp(stamps.get(provider)):
                stamps[provider] = stamp
        has_citation = row.get('wos_citations') is not None
        accept_citation = has_citation and (
            old_citation is None or timestamp(new_stamp) > timestamp(old_stamp))
        if accept_citation:
            old['wos_citations'] = copy.deepcopy(row['wos_citations'])
            if timestamp(new_stamp) > float('-inf'):
                old.setdefault('citation_observed_at', {})['wos'] = new_stamp
        elif old_citation is not None and timestamp(old_stamp) > float('-inf'):
            # Anchor retained counts before the metadata clock advances again.
            old.setdefault('citation_observed_at', {}).setdefault('wos', old_stamp)
        if newer and 'retained_citation_fields' in row:
            old['retained_citation_fields'] = copy.deepcopy(row['retained_citation_fields'])
        if has_citation and not retained and timestamp(new_stamp) >= timestamp(old_stamp):
            if 'retained_citation_fields' in old or 'retained_citation_fields' in row:
                old['retained_citation_fields'] = [field for field in old.get('retained_citation_fields', [])
                                                   if field != 'wos_citations']
        index_source_aliases(aliases, old, position)
        keys.setdefault(source_row_key(old), position)
    return output


def merge_provider(candidate, destination, provider, report_name):
    previous, old_payloads = provider_payloads(destination, provider, report_name)
    incoming, new_payloads = provider_payloads(candidate, provider, report_name)
    if not previous and not incoming:
        return {}
    old_metrics, new_metrics = component_state(previous, 'metrics'), component_state(incoming, 'metrics')
    newer_metrics = timestamp(new_metrics.get('last_success_at')) > timestamp(old_metrics.get('last_success_at'))
    payloads = {'metrics': new_payloads['metrics'] if newer_metrics else old_payloads['metrics']}
    old_list, new_list = component_state(previous, 'publications'), component_state(incoming, 'publications')
    payloads['publications'] = merge_observed_rows(old_payloads['publications'], new_payloads['publications'], old_list, new_list)
    old_details, new_details = old_payloads.get('details', {}), new_payloads.get('details', {})
    detail_rows = lambda payload: [dict(row, id=key) for key, row in payload.get('items', {}).items()]
    details = merge_observed_rows(detail_rows(old_details), detail_rows(new_details),
                                  component_state(previous, 'details'), component_state(incoming, 'details'))
    payloads['details'] = {**old_details, 'items': {row['id']: {k: v for k, v in row.items() if k != 'id'} for row in details}} if details or old_details or new_details else {}
    report = merge_source_report(previous, incoming, len(payloads['publications']))
    if report.get('components'):
        report['components'].setdefault('publications', {})['record_count'] = len(payloads['publications'])
    directory = destination / 'data' / provider
    if provider in ('elibrary', 'wos'):
        if previous.get('components') or incoming.get('components'):
            write_checkpoint(directory / 'collection_checkpoint.json', report, payloads)
            materialize_checkpoint(directory / 'collection_checkpoint.json', provider)
        elif provider == 'elibrary':
            write(directory / 'profile_metrics.json', payloads['metrics'])
            write(destination / 'data/processed/elibrary_publications.json', payloads['publications'])
            write(directory / 'item_details.json', payloads['details'])
        else:
            # Preserve legacy payload shape for compatibility with existing snapshots.
            write(directory / 'profile_metrics.json', {**payloads['metrics'], 'records': payloads['publications']})
    else:
        write(directory / 'scopus_author_57211062810_metrics.json', payloads['metrics'])
        write(directory / 'scopus_author_57211062810_works.json', payloads['publications'])
    write(directory / report_name, report)
    return report


def copy_snapshot(source, destination, incoming_is_newer, excluded=()):
    if not source.exists():
        return
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_file() and relative.as_posix() not in excluded and (incoming_is_newer or not target.exists()):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def merge_open_sources(candidate, destination):
    from harvest_open_sources import (
        normalize_orcid_works, normalize_openalex_works, normalize_crossref_works,
        dedupe_records, doi_norm, normalize_title,
    )
    output = destination / 'data/open'
    previous = read(output / 'harvest_report.json', {})
    incoming = read(candidate / 'data/open/harvest_report.json', {})
    if not previous and not incoming and not (candidate / 'data/open').exists():
        return
    prior_aggregate = read(output / 'open_publications.json', {})
    incoming_aggregate = read(candidate / 'data/open/open_publications.json', {})
    providers, observed = {}, []
    files = {'orcid': 'orcid_works.json', 'openalex_author': 'openalex_author.json',
             'openalex_works': 'openalex_works.json', 'crossref': 'crossref_works.json'}
    for provider, filename in files.items():
        old = (previous.get('providers') or {}).get(provider, {})
        new = (incoming.get('providers') or {}).get(provider, {})
        target = output / filename
        source = candidate / 'data/open' / filename
        if source.exists() and (not target.exists() or timestamp(new.get('last_success_at')) > timestamp(old.get('last_success_at'))):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        payload = read(target, None)
        rows = None
        if payload is not None:
            if provider == 'orcid':
                identifier = str(payload.get('path', '')).strip('/').split('/')[0] or None
                rows = normalize_orcid_works(payload, identifier)
            elif provider == 'openalex_works':
                rows = normalize_openalex_works(payload)
            elif provider == 'crossref':
                rows = normalize_crossref_works(payload)
            else:
                rows = []  # Author metadata is not an additional publication.
            observed.extend(rows)
        state = merge_source_report(old, new, len(rows) if rows is not None else None)
        if state:
            providers[provider] = state
    aggregate = dedupe_records(observed)
    def identity(row):
        return doi_norm(row.get('doi')) or (normalize_title(row.get('title')).lower(), row.get('year'))
    seen = {identity(row) for row in aggregate}
    for row in records(prior_aggregate) + records(incoming_aggregate):
        if identity(row) not in seen:
            aggregate.append(row)
            seen.add(identity(row))
    report = merge_source_report(previous, incoming, len(aggregate))
    report['providers'] = providers
    report['records_total_after_dedupe'] = len(aggregate)
    report['records_total_before_dedupe'] = len(observed)
    if providers and any(p.get('status') != 'success' or p.get('complete') is not True for p in providers.values()):
        report['complete'] = False
        if report.get('status') == 'success':
            report.update(status='partial', origin='snapshot', reason='concurrent_provider_failure')
    write(output / 'harvest_report.json', report)
    write(output / 'open_publications.json', {'generated_at': report.get('attempted_at') or report.get('generated_at'), 'records': aggregate})


def seed_newer_metric_baseline(destination, source_reports):
    """Adopt verified newer values while leaving the newer failure report intact.

    The public builder normally retains previous values on a failed attempt. A
    race can bring in an intermediate successful snapshot that was absent from
    main, so advance that previous-value baseline before the ordinary rebuild.
    """
    from build_public_data import build_scientometrics
    path = destination / 'data/public/profile.json'
    profile = read(path, {})
    old_metrics = profile.get('scientometrics') or {}
    states = {}
    names = {'elibrary': 'rinc', 'scopus': 'scopus', 'wos': 'wos'}
    for provider, state in source_reports.items():
        state = component_state(state, 'metrics')
        old = (old_metrics.get('sources') or {}).get(names[provider], {})
        if timestamp(state.get('last_success_at')) > timestamp(old.get('last_success_at')):
            states[provider] = {**state, 'status': 'success', 'origin': 'live', 'complete': True}
    if not states:
        return
    calculated = build_scientometrics(
        [], read(destination / 'data/elibrary/profile_metrics.json', {}),
        read(destination / 'data/scopus/scopus_author_57211062810_metrics.json', {}),
        read(destination / 'data/wos/profile_metrics.json', {}), health=states, previous=old_metrics,
    )
    baseline = dict(old_metrics)
    baseline['sources'] = dict(old_metrics.get('sources') or {})
    for provider in states:
        baseline['sources'][names[provider]] = calculated['sources'][names[provider]]
    profile['scientometrics'] = baseline
    write(path, profile)


def apply_newer_citation_observations(publications, destination, source_reports):
    """Use only verified snapshot epochs, independent of later attempt failure."""
    import build_public_data as builder
    profile = read(destination / 'data/public/profile.json', {})
    previous_data = builder.DATA
    try:
        builder.DATA = destination / 'data'
        for provider, report in source_reports.items():
            column = 'rinc' if provider == 'elibrary' else provider
            prior = ((profile.get('source_health') or {}).get(provider) or
                     ((profile.get('scientometrics') or {}).get('sources') or {}).get(column) or {})
            report = component_state(report, 'publications')
            prior = component_state(prior, 'publications')
            fresh = timestamp(report.get('last_success_at')) > timestamp(prior.get('last_success_at'))
            for row in publications:
                field = {'elibrary': 'rinc_citations', 'wos': 'wos_citations', 'scopus': 'scopus'}[provider]
                if row.get(field) is not None and prior.get('last_success_at'):
                    row.setdefault('citation_observed_at', {}).setdefault(provider, prior['last_success_at'])
            if provider == 'elibrary':
                observations = {str(row.get('elibrary_item_id')): row
                                for row in read(destination / 'data/processed/elibrary_publications.json', [])
                                if row.get('elibrary_item_id') and row.get('rinc_citations') is not None}
                for row in publications:
                    key = str(row.get('elibrary_item_id'))
                    if key in observations and builder.citation_is_new(observations[key], row, 'elibrary', fresh):
                        row['rinc_citations'] = observations[key]['rinc_citations']
                        builder.mark_citation_observation(row, observations[key], 'elibrary')
            elif provider == 'scopus':
                works = read(destination / 'data/scopus/scopus_author_57211062810_works.json', [])
                builder.merge_scopus(publications, works.get('works', []) if isinstance(works, dict) else works, fresh=fresh)
            else:
                payload = read(destination / 'data/wos/profile_metrics.json', {})
                builder.merge_wos(publications, payload.get('records', []), fresh=fresh)
    finally:
        builder.DATA = previous_data
    return publications

def recombine(candidate, destination, scope='portfolio'):
    from it_resources import merge_it_resources
    merge_it_resources(candidate, destination)
    if scope == 'it':
        return
    from harvest_media_mentions import merge_records, merge_discovery_state, canonical as normalize_url
    from build_public_data import merge_publication_sets
    report_names = {'scopus': 'scopus_author_57211062810_access_report.json', 'elibrary': 'browser_fetch_report.json', 'wos': 'harvest_report.json'}
    merged_reports = {}
    for provider, filename in report_names.items():
        report = merge_provider(candidate, destination, provider, filename)
        if report:
            merged_reports[provider] = report
    merge_open_sources(candidate, destination)
    if (candidate / 'data/audit').exists():
        shutil.copytree(candidate / 'data/audit', destination / 'data/audit', dirs_exist_ok=True)
    public_path = destination / 'data/public/publications.json'
    merged_publications = merge_publication_sets(read(public_path, []), read(candidate / 'data/public/publications.json', []))
    write(public_path, apply_newer_citation_observations(merged_publications, destination, merged_reports))
    seed_newer_metric_baseline(destination, merged_reports)
    for name in ('published.json', 'news_mentions.json', 'published-fallback.json'):
        path = destination / 'data/media' / name
        previous = read(path, {})
        incoming = read(candidate / 'data/media' / name, {})
        merged = merge_records(records(previous), records(incoming))
        if isinstance(incoming, list):
            payload = merged
        else:
            payload = dict(incoming, records=merged)
        write(path, payload)
    current_state = read(destination / 'data/media/discovery_state.json', {})
    incoming_state = read(candidate / 'data/media/discovery_state.json', {})
    combined_state = merge_discovery_state(current_state, incoming_state)
    write(destination / 'data/media/discovery_state.json', combined_state)
    for name in ('harvest_report.json', 'live_discovery_smoke.json'):
        incoming = read(candidate / 'data/media' / name, {})
        existing = read(destination / 'data/media' / name, {})
        if incoming and (incoming.get('attempted_at') or '') >= (existing.get('attempted_at') or ''):
            write(destination / 'data/media' / name, incoming)
    for name in ('media_mentions.json', 'publications.json'):
        path = destination / 'data/admin_queue' / name
        old = read(path, [])
        new = read(candidate / 'data/admin_queue' / name, [])
        by_key = {str(row.get('url') or row.get('doi') or row.get('id') or json.dumps(row, sort_keys=True)): row for row in new}
        by_key.update({str(row.get('url') or row.get('doi') or row.get('id') or json.dumps(row, sort_keys=True)): row for row in old})
        write(path, list(by_key.values()))
    published = records(read(destination / 'data/media/published.json', {}))
    published_urls = {normalize_url(row.get('url', '')) for row in published}
    queue = merge_records(
        read(destination / 'data/admin_queue/media_mentions.json', []),
        records(read(destination / 'data/media/rejected_or_low_confidence.json', {})),
    )
    queue = [row for row in queue if normalize_url(row.get('url', '')) not in published_urls]
    write(destination / 'data/admin_queue/media_mentions.json', queue)
    write(destination / 'data/media/rejected_or_low_confidence.json', {'records': queue})
    report_path = destination / 'data/media/harvest_report.json'
    report = read(report_path, {})
    report.update(record_count=len(published), published=len(published), pending=len(combined_state.get('pending', {})))
    if report['pending']:
        report['complete'] = False
        if report.get('status') == 'success':
            report.update(status='partial', reason='concurrent_pending_backlog')
    write(report_path, report)
    for name in ('assets/media/mentions', 'assets/craftum'):
        if (candidate / name).exists():
            for source in (candidate / name).rglob('*'):
                target = destination / name / source.relative_to(candidate / name)
                if source.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    # Current curated bibliographic fields and gallery/RISS content are kept.
    for script in ('build_public_data.py', 'merge_wos_records_into_public_data.py', 'sanitize_publication_references.py', 'report_safety.py'):
        subprocess.run([sys.executable, str(destination / 'scripts' / script)], cwd=destination, check=True)
    audit = subprocess.run([sys.executable, str(destination / 'scripts/audit_refresh_pipeline.py')], cwd=destination)
    if audit.returncode not in (0, 2):
        raise RuntimeError('Recombined data failed structural audit.')

def validate(path, baseline, scope='portfolio'):
    # Runtime diagnostics are not public IT catalog content and must not create
    # timestamp-only commits. The caller exports stage diagnostics separately.
    arguments = [sys.executable, 'scripts/validate_retention.py', '--baseline-ref', baseline,
                 '--scope', scope]
    if scope != 'it':
        arguments += ['--report', 'data/audit/retention_report.json']
    subprocess.run(arguments, cwd=path, check=True)
    if scope == 'it' or (path / 'data/it/resources.json').exists():
        from it_resources import validate_it_resources
        if validate_it_resources(path):
            raise RuntimeError('IT catalog failed validation.')
    subprocess.run([sys.executable, 'scripts/check_seo.py'], cwd=path, check=True)
    git('diff', '--check', cwd=path)
    if scope == 'it':
        assert_it_scope(path, baseline)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--scope', choices=['portfolio', 'it'], default='portfolio')
    args = parser.parse_args()
    baseline = git('rev-parse', args.baseline_ref)
    validate(ROOT, baseline, args.scope)
    result_sha, changed = baseline, False
    for attempt in range(3):
        git('fetch', 'origin', 'main')
        latest = git('rev-parse', 'origin/main')
        changed_code = git('diff', '--name-only', baseline, latest, '--', 'scripts', 'config', '.github', '*.html', 'assets/*.js', 'assets/*.css', 'data/it/config.json')
        if changed_code:
            raise RuntimeError('Code/configuration changed during collection. Rerun against latest main; candidate retained.')
        with tempfile.TemporaryDirectory(prefix='portfolio-publish-') as directory:
            target = Path(directory) / 'checkout'
            git('worktree', 'add', '--detach', str(target), latest)
            try:
                if latest == baseline:
                    copy_candidate(ROOT, target, args.scope)
                else:
                    recombine(ROOT, target, args.scope)
                validate(target, latest, args.scope)
                git('config', 'user.name', 'github-actions[bot]', cwd=target)
                git('config', 'user.email', 'github-actions[bot]@users.noreply.github.com', cwd=target)
                paths = [name for name in publication_paths(args.scope) if (target / name).exists()]
                if paths:
                    git('add', *paths, cwd=target)
                if not git('diff', '--cached', '--name-only', cwd=target):
                    result_sha = latest
                    synchronize_published(target, ROOT, args.scope)
                    break
                message = 'chore: add discovered IT resources' if args.scope == 'it' else 'chore: refresh validated portfolio data'
                git('commit', '-m', message, cwd=target)
                result_sha = git('rev-parse', 'HEAD', cwd=target)
                push = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=target, capture_output=True, text=True)
                if push.returncode == 0:
                    # Verification must compare the data actually pushed after any remerge.
                    synchronize_published(target, ROOT, args.scope)
                    changed = True
                    break
                if attempt == 2:
                    raise RuntimeError('Main kept changing; safe publication aborted without a forced push.')
            finally:
                git('worktree', 'remove', '--force', str(target))
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f'changed={str(changed).lower()}\nsha={result_sha}\n')
    print(f'Validated data publication: {result_sha}; changed={changed}')

if __name__ == '__main__':
    main()
