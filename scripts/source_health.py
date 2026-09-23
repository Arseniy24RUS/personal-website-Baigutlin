"""Small, credential-free source reports and atomic last-good persistence."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def snapshot_time(path):
    """Legacy generated_at means rebuild time; use the actual capture filename."""
    match = re.search(r'(\d{8}T\d{6}Z)', str(path or ''))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def source_result(previous=None, *, status='error', count=0, reason=None, attempted_at=None):
    previous = previous or {}
    attempted_at = attempted_at or now()
    success = status == 'success'
    return {
        'status': status,
        'attempted_at': attempted_at,
        'last_success_at': attempted_at if success else previous.get('last_success_at'),
        'origin': 'live' if success else 'snapshot',
        'complete': success,
        'record_count': count,
        'reason': reason,
    }


def component_state(report, name):
    """Legacy all-or-nothing reports apply to both core components only."""
    report = report if isinstance(report, dict) else {}
    if isinstance(report.get('components'), dict):
        return report['components'].get(name) or {}
    return report if name in ('metrics', 'publications') else {}


def is_verified(state):
    return (isinstance(state, dict) and state.get('status') == 'success'
            and state.get('origin') == 'live' and state.get('complete') is True
            and bool(state.get('last_success_at')))


def observation_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return float('-inf')


def load_checkpoint(path):
    value = read_json(path, {})
    if not isinstance(value, dict) or value.get('schema') != 'provider_collection/v1':
        return None
    payloads = value.get('payloads')
    if (not isinstance(value.get('report'), dict) or not isinstance(payloads, dict)
            or not isinstance(payloads.get('metrics'), dict)
            or not isinstance(payloads.get('publications'), list)
            or not isinstance(payloads.get('details', {}), dict)):
        return None
    return value


def write_checkpoint(path, report, payloads):
    """One atomic commit of observations and the reports that authenticate them."""
    write_json(path, {'schema': 'provider_collection/v1', 'report': report, 'payloads': payloads})


def materialize_checkpoint(path, provider):
    checkpoint = load_checkpoint(path)
    if checkpoint is None:
        return False
    directory = Path(path).parent
    payloads = checkpoint['payloads']
    if provider == 'elibrary':
        write_json(directory / 'profile_metrics.json', payloads['metrics'])
        write_json(directory.parent / 'processed/elibrary_publications.json', payloads['publications'])
        write_json(directory / 'item_details.json', payloads.get('details', {}))
        report_name = 'browser_fetch_report.json'
    elif provider == 'wos':
        write_json(directory / 'profile_metrics.json', {**payloads['metrics'], 'records': payloads['publications'],
                                                       'records_count_on_page': len(payloads['publications'])})
        report_name = 'harvest_report.json'
    else:
        raise ValueError('Unsupported checkpoint provider')
    write_json(directory / report_name, checkpoint['report'])
    return True


def merge_records(previous, fresh, key):
    """Add/update verified records, never delete old ones or replace values by null."""
    result = {key(row): dict(row) for row in previous if key(row)}
    for row in fresh:
        identifier = key(row)
        if not identifier:
            continue
        old = result.get(identifier, {})
        sources = sorted(set(old.get('sources') or []) | set(row.get('sources') or []))
        result[identifier] = {**old, **{k: v for k, v in row.items() if v not in (None, '', [], {})}}
        if sources:
            result[identifier]['sources'] = sources
    return list(result.values())
