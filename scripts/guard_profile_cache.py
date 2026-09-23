#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import os
import re
from export_profile_env import public_profile_identifiers

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

PROFILE_YAML = Path(os.environ.get('PROFILE_YAML', 'config/profile.yml'))
DATA = Path('data')
REPORT = DATA / 'audit' / 'profile_cache_guard_report.json'


def clean(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def load_profile() -> dict:
    if yaml and PROFILE_YAML.exists():
        data = yaml.safe_load(PROFILE_YAML.read_text(encoding='utf-8')) or {}
        return data.get('profile') or {}
    raise ValueError('Profile configuration or PyYAML is unavailable; cached data was not changed')


def ids_match(expected: dict, actual: dict, keys: list[str]) -> bool:
    compared = 0
    matched = 0
    for key in keys:
        exp = clean(expected.get(key))
        got = clean(actual.get(key))
        if got and not exp:
            return False
        if exp and got:
            compared += 1
            if exp == got:
                matched += 1
            else:
                return False
    return compared == 0 or matched > 0


def unlink(path: Path, report: dict, reason: str) -> None:
    """Legacy name: report incompatible data without deleting any publication."""
    if not path.exists():
        return
    report.setdefault('errors', []).append({'path': str(path), 'reason': reason,
                                           'action': 'preserved; explicit profile migration required'})


def main() -> int:
    profile = load_profile()
    ids = profile.get('identifiers') or {}
    report = {'expected_identifiers': ids, 'removed': [], 'skipped': [], 'errors': []}

    public_profile = read_json(DATA / 'public/profile.json', {})
    public_ids = public_profile_identifiers(public_profile) if isinstance(public_profile, dict) else {}
    if isinstance(public_ids, dict) and public_ids and not ids_match(ids, public_ids, ['elibrary_authorid', 'orcid', 'scopus_author_id', 'wos_researcher_id']):
        for path in [DATA / 'public/publications.json', DATA / 'public/publications.tsv', DATA / 'public/profile.json']:
            unlink(path, report, 'public_profile_identifiers_do_not_match_config')
        for path in [DATA / 'open/open_publications.json', DATA / 'open/harvest_report.json']:
            unlink(path, report, 'open_sources_cached_for_previous_profile')

    expected_elib = clean(ids.get('elibrary_authorid'))
    elib_metrics = read_json(DATA / 'elibrary/metrics.json', {})
    cached_elib = clean(elib_metrics.get('authorid') if isinstance(elib_metrics, dict) else '')
    if expected_elib and cached_elib and cached_elib != expected_elib:
        for path in [
            DATA / 'elibrary/metrics.json',
            DATA / 'elibrary/publications.tsv',
            DATA / 'elibrary/profile_metrics.json',
            DATA / 'elibrary/items_fetch_report.json',
            DATA / 'elibrary/browser_fetch_report.json',
            DATA / 'processed/elibrary_publications.json',
        ]:
            unlink(path, report, 'elibrary_authorid_cache_mismatch')

    elib_profile = read_json(DATA / 'elibrary/profile_metrics.json', {})
    cached_profile_elib = clean(elib_profile.get('authorid') if isinstance(elib_profile, dict) else '')
    if expected_elib and cached_profile_elib and cached_profile_elib != expected_elib:
        for path in [DATA / 'elibrary/profile_metrics.json', DATA / 'processed/elibrary_publications.json']:
            unlink(path, report, 'elibrary_profile_authorid_cache_mismatch')

    expected_wos = clean(ids.get('wos_researcher_id'))
    wos_profile = read_json(DATA / 'wos/profile_metrics.json', {})
    cached_wos = clean(wos_profile.get('researcher_id') if isinstance(wos_profile, dict) else '')
    if expected_wos and cached_wos and cached_wos != expected_wos:
        for path in [DATA / 'wos/profile_metrics.json', DATA / 'wos/harvest_report.json']:
            unlink(path, report, 'wos_researcher_id_cache_mismatch')

    report['status'] = 'error' if report['errors'] else 'success'
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
