#!/usr/bin/env python3
"""Prefer configured official WoS APIs and retain independently verified data.

Each transport receives its own copy of the last published provider checkpoint.
Only atomic, current observations can enter the shared publication candidate.
"""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from publish_refresh import merge_observed_rows, provider_payloads
from harvest_wos_api import API_REASONS
from report_safety import sanitize
from source_health import (
    component_state, is_verified, load_checkpoint, materialize_checkpoint, now,
    observation_time, source_result, write_checkpoint,
)

SCRIPTS = Path(__file__).resolve().parent
API_KEYS = ('WOS_STARTER_API_KEY', 'WOS_RESEARCHER_API_KEY')
COMPONENTS = ('metrics', 'publications')
API_TIMEOUT = 180
BROWSER_TIMEOUT = 900
SAFE_REASONS = API_REASONS | frozenset({
    'completed', 'nonzero_exit', 'transport_failed', 'transport_timeout', 'transport_execution_failed',
    'missing_atomic_checkpoint', 'missing_current_component_report', 'invalid_metrics_payload',
    'invalid_publications_payload', 'incomplete_components', 'not_attempted',
    'human_verification_required', 'challenge_observation_incomplete', 'challenge_observation_budget_invalid',
    'vpn_verification_failed', 'vpn_route_mismatch', 'credentials_missing', 'login_form_changed',
    'unexpected_login_origin', 'login_submit_changed', 'login_not_confirmed', 'username_configuration_invalid',
    'username_input_not_applied', 'orcid_submit_changed', 'wos_login_not_confirmed',
    'publication_scope_control_missing', 'publication_scope_not_confirmed', 'profile_total_missing',
    'publication_total_changed', 'pagination_limit', 'incomplete_pagination', 'profile_metrics_missing',
    'wrong_author_profile', 'session_expired', 'profile_not_authenticated_or_changed', 'rate_limited',
    'invalid_session_checkpoint', 'profile_records_not_ready', 'publication_list_scroll_failed',
    'pagination_in_progress', 'wos_sso_state_capture_failed', 'wos_sso_state_restore_failed',
    'display_unavailable', 'filesystem_permission_denied', 'crashpad_initialization_failed',
    'browser_executable_missing', 'browser_library_missing', 'browser_sandbox_failed', 'storage_full',
    'python_dependency_missing', 'browser_process_crashed', 'browser_process_closed', 'browser_initialization_failed',
})


def api_configured():
    return any(os.environ.get(name, '').strip() for name in API_KEYS)


def transport_plan():
    return ([name for name, key in (
        ('api_researcher', 'WOS_RESEARCHER_API_KEY'), ('api_starter', 'WOS_STARTER_API_KEY'),
    ) if os.environ.get(key, '').strip()] + ['browser'])


def browser_only():
    # Keep the existing no-key and maintenance behavior, including session work.
    from harvest_wos_authenticated import main
    return main()


def _safe_reason(value, default='transport_failed'):
    return value if isinstance(value, str) and value in SAFE_REASONS else default


def _clone_baseline(root, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for relative in ('data/wos', 'data/snapshots/wos'):
        source = root / relative
        if source.is_dir():
            shutil.copytree(source, destination / relative)


def run_transport(transport, root):
    """Never relay subprocess output, exceptions, request URLs or credentials."""
    env = dict(os.environ)
    env['WOS_PROFILE_OUT'] = str(root / 'data/wos/profile_metrics.json')
    env['WOS_HARVEST_REPORT'] = str(root / 'data/wos/harvest_report.json')
    if transport.startswith('api'):
        script, timeout = 'harvest_wos_api.py', API_TIMEOUT
        if transport in {'api_researcher', 'api_starter'}:
            provider = transport.removeprefix('api_')
            env['WOS_API_PROVIDER'] = provider
            env.pop('WOS_STARTER_API_KEY' if provider == 'researcher' else 'WOS_RESEARCHER_API_KEY', None)
        for name in ('WOS_ORCID_USERNAME', 'WOS_ORCID_PASSWORD', 'BROWSER_SESSION_KEY',
                     'BROWSER_SESSION_INPUT_WOS', 'BROWSER_SESSION_OUTBOX'):
            env.pop(name, None)
    else:
        script, timeout = 'harvest_wos_authenticated.py', BROWSER_TIMEOUT
        for name in API_KEYS:
            env.pop(name, None)
    try:
        result = subprocess.run([sys.executable, str(SCRIPTS / script)], cwd=root, env=env,
                                capture_output=True, timeout=timeout)
        return result.returncode, 'completed' if result.returncode == 0 else 'nonzero_exit'
    except subprocess.TimeoutExpired:
        return 124, 'transport_timeout'
    except OSError:
        return 1, 'transport_execution_failed'


def _current(state, started):
    return (isinstance(state, dict) and state.get('status') in {'success', 'partial', 'blocked', 'error'}
            and observation_time(state.get('attempted_at')) >= observation_time(started))


def _verified(state, started):
    return (_current(state, started) and is_verified(state)
            and observation_time(state.get('last_success_at')) >= observation_time(started))


def _failed_snapshot(previous, started, reason):
    report, payloads = previous
    failed = source_result(report, reason=reason, count=len(payloads['publications']), attempted_at=started)
    failed['components'] = {
        name: source_result(component_state(report, name), reason=reason,
                            count=component_state(report, name).get('record_count', 0), attempted_at=started)
        for name in COMPONENTS
    }
    return {'report': failed, 'payloads': copy.deepcopy(payloads), 'started': started}


def read_attempt(root, previous, started, code, reason):
    checkpoint = load_checkpoint(root / 'data/wos/collection_checkpoint.json')
    if not checkpoint or not _current(checkpoint['report'], started) or checkpoint == {
        'schema': 'provider_collection/v1', 'report': previous[0], 'payloads': previous[1],
    }:
        return _failed_snapshot(previous, started, reason if code else 'missing_atomic_checkpoint')
    report, payloads = copy.deepcopy(checkpoint['report']), copy.deepcopy(checkpoint['payloads'])
    for name in COMPONENTS:
        state = component_state(report, name)
        if not _current(state, started) or (state.get('status') == 'success' and not _verified(state, started)):
            state = source_result(component_state(previous[0], name), reason='missing_current_component_report',
                                  count=component_state(previous[0], name).get('record_count', 0), attempted_at=started)
            payloads[name] = copy.deepcopy(previous[1][name])
        else:
            state = dict(state)
            if state.get('reason'):
                state['reason'] = _safe_reason(state['reason'])
        if name == 'metrics' and _verified(state, started):
            summary = payloads['metrics'].get('summary', {})
            if not isinstance(summary, dict) or not all(type(summary.get(key)) in (int, float) and math.isfinite(summary[key]) and summary[key] >= 0
                       for key in ('publications', 'citations', 'h_index')):
                state = source_result(component_state(previous[0], name), reason='invalid_metrics_payload', attempted_at=started)
                payloads[name] = copy.deepcopy(previous[1][name])
        if name == 'publications' and any(not isinstance(row, dict) for row in payloads['publications']):
            state = source_result(component_state(previous[0], name), reason='invalid_publications_payload', attempted_at=started)
            payloads['publications'] = copy.deepcopy(previous[1]['publications'])
        report.setdefault('components', {})[name] = state
    return {'report': report, 'payloads': payloads, 'started': started}


def _merge_fields(previous, incoming):
    result = copy.deepcopy(previous)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_fields(result[key], value)
        elif value not in (None, '', [], {}) or key in {'retained_metric_fields', 'retained_citation_fields'}:
            result[key] = copy.deepcopy(value)
    return result


def _has_metric_observation(attempt):
    metrics = attempt['payloads']['metrics']
    stamps = metrics.get('metric_observed_at', {})
    summary = metrics.get('summary', {})
    if not isinstance(stamps, dict) or not isinstance(summary, dict):
        return False
    return any(observation_time(stamps.get(key)) >= observation_time(attempt['started'])
               and type(summary.get(key)) in (int, float) and math.isfinite(summary[key]) and summary[key] >= 0
               for key in ('publications', 'citations', 'h_index'))


def _fresh_rows(attempt):
    return [row for row in attempt['payloads']['publications']
            if observation_time(row.get('observed_at')) >= observation_time(attempt['started'])]


def compose(previous, attempts, started):
    """Choose successful API components first; failed browser work cannot revoke them."""
    old_report, old_payloads = previous
    components, selected = {}, {}
    for name in COMPONENTS:
        chosen = next((attempt for attempt in attempts
                       if _verified(component_state(attempt['report'], name), attempt['started'])), None)
        if chosen is None:
            chosen = next((attempt for attempt in attempts
                           if (_has_metric_observation(attempt) if name == 'metrics' else bool(_fresh_rows(attempt)))), None)
        chosen = chosen or attempts[-1]
        selected[name] = chosen
        state = copy.deepcopy(component_state(chosen['report'], name))
        state['transport'] = chosen['transport']
        components[name] = state

    metrics_attempt = selected['metrics']
    metrics_state = components['metrics']
    metrics = copy.deepcopy(old_payloads['metrics'])
    if _verified(metrics_state, metrics_attempt['started']) or _has_metric_observation(metrics_attempt):
        metrics = _merge_fields(metrics, metrics_attempt['payloads']['metrics'])
    if metrics_attempt['transport'] == 'browser' and _verified(metrics_state, metrics_attempt['started']):
        # Fresh browser counters must not inherit API calculation labels or
        # retained-field flags from the source's earlier checkpoint.
        for field in ('publications', 'citations', 'h_index'):
            metrics.setdefault('metric_methods', {})[field] = 'provider_profile'
            metrics.setdefault('api_metric_method', {}).pop(field, None)
            metrics.setdefault('metric_observed_at', {})[field] = metrics_state['last_success_at']
        metrics['retained_metric_fields'] = [field for field in metrics.get('retained_metric_fields', [])
                                              if field not in {'publications', 'citations', 'h_index'}]

    rows = copy.deepcopy(old_payloads['publications'])
    old_list = component_state(old_report, 'publications')
    # Fresh per-row stamps allow verified partial pages to survive even when
    # neither transport can finish the complete list.
    for attempt in attempts:
        state = component_state(attempt['report'], 'publications')
        rows = merge_observed_rows(rows, _fresh_rows(attempt), old_list, state)
    components['publications']['record_count'] = len(rows)
    current_rows = [row for row in rows if observation_time(row.get('observed_at')) >= observation_time(started)]
    if current_rows:
        components['publications']['observed_count'] = len(current_rows)
        components['publications']['last_observation_at'] = max(row['observed_at'] for row in current_rows)

    complete = all(is_verified(components[name]) for name in COMPONENTS)
    successful = any(is_verified(state) for state in components.values()) or bool(current_rows) or _has_metric_observation(metrics_attempt)
    failed = next((state for state in components.values() if not is_verified(state)), {})
    report = source_result(old_report, status='success' if complete else 'partial' if successful else failed.get('status', 'error'),
                           count=len(rows), reason=None if complete else failed.get('reason') or 'incomplete_components', attempted_at=started)
    report.update(components=components, researcher_id=os.environ.get('WOS_RESEARCHER_ID', 'AAN-4717-2020'),
                  transport_attempts={attempt['transport']: attempt['metadata'] for attempt in attempts})
    # Keep the original browser diagnostic evidence in the normal report shape.
    # It is not used as authority for an independently successful API component.
    for attempt in attempts:
        if attempt['transport'] == 'browser':
            for key in ('authentication', 'session_restore', 'session_checkpoint', 'verification_evidence',
                        'profile_diagnostics', 'profile_entry_observation', 'authentication_evidence', 'initialization'):
                if key in attempt['report']:
                    report[key] = copy.deepcopy(attempt['report'][key])
    payloads = {'metrics': metrics, 'publications': rows, 'details': copy.deepcopy(old_payloads.get('details', {}))}
    return sanitize(report), sanitize(payloads)


def _metadata(attempt, code, outcome):
    report = attempt['report']
    states = [component_state(report, name) for name in COMPONENTS]
    complete = all(_verified(state, attempt['started']) for state in states)
    partial = any(_verified(state, attempt['started']) for state in states) or _has_metric_observation(attempt) or bool(_fresh_rows(attempt))
    metadata = {'attempted_at': attempt['started'], 'exit_code': code,
                'status': 'success' if complete else 'partial' if partial else 'blocked' if report.get('status') == 'blocked' else 'error',
                'reason': _safe_reason(report.get('reason'), outcome) if report.get('reason') else outcome,
                'components': {}}
    for name in COMPONENTS:
        state = component_state(report, name)
        value = {'status': state.get('status'), 'complete': _verified(state, attempt['started'])}
        if state.get('reason'):
            value['reason'] = _safe_reason(state['reason'])
        if type(state.get('record_count')) is int:
            value['record_count'] = state['record_count']
        metadata['components'][name] = value
    if report.get('api_provider') in {'starter', 'researcher'}:
        metadata['provider'] = report['api_provider']
    endpoint_attempts = report.get('api_attempts')
    if isinstance(endpoint_attempts, list):
        metadata['attempts'] = []
        for endpoint in endpoint_attempts[:64]:
            if not isinstance(endpoint, dict) or endpoint.get('endpoint') not in {'profile', 'documents'}:
                continue
            item = {'endpoint': endpoint['endpoint']}
            if type(endpoint.get('http_status')) is int and 0 <= endpoint['http_status'] <= 599:
                item['http_status'] = endpoint['http_status']
            if endpoint.get('reason') in API_REASONS:
                item['reason'] = endpoint['reason']
            metadata['attempts'].append(item)
    return metadata


def dispatch(root):
    root = Path(root).resolve()
    previous = copy.deepcopy(provider_payloads(root, 'wos', 'harvest_report.json'))
    started = now()
    attempts = []
    temporary_root = os.environ.get('RUNNER_TEMP') or None
    with tempfile.TemporaryDirectory(prefix='wos-transports-', dir=temporary_root) as temporary:
        # Both transports start from the same baseline, even after the API result
        # is checkpointed to survive interruption while the browser is running.
        workspaces = {name: Path(temporary) / name for name in transport_plan()}
        for path in workspaces.values():
            _clone_baseline(root, path)
        for transport, path in workspaces.items():
            attempted = now()
            code, outcome = run_transport(transport, path)
            attempt = read_attempt(path, previous, attempted, code, outcome)
            attempt['transport'] = transport
            attempt['metadata'] = _metadata(attempt, code, outcome)
            attempts.append(attempt)
            report, payloads = compose(previous, attempts, started)
            checkpoint = root / 'data/wos/collection_checkpoint.json'
            write_checkpoint(checkpoint, report, payloads)
            materialize_checkpoint(checkpoint, 'wos')
            if report['complete']:
                break
    return report


def main():
    if (os.environ.get('BROWSER_SESSION_MAINTENANCE') == '1'
            or os.environ.get('WOS_AUTH_MODE', 'restore') != 'restore'
            or not api_configured()):
        return browser_only()
    report = dispatch(Path.cwd())
    print('WoS: ' + report['status'] + '; official API/browser component observations preserved.')
    return 0 if report['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
