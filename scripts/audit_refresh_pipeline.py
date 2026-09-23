#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import re
from build_public_data import is_fresh, load_source_health

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

DATA = Path('data')
REPORT = DATA / 'audit' / 'refresh_pipeline_audit.json'


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def clean(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def load_profile_config() -> dict:
    path = Path('config/profile.yml')
    if not path.exists() or not yaml:
        return {}
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    return data.get('profile') or {}


def sources_of(pub: dict) -> set[str]:
    sources = pub.get('sources') or []
    if isinstance(sources, str):
        sources = [x.strip() for x in sources.split(',') if x.strip()]
    return {str(x) for x in sources if x}


def metric(profile: dict, source: str, key: str):
    try:
        return (((profile.get('scientometrics') or {}).get('sources') or {}).get(source) or {}).get(key)
    except Exception:
        return None


def has_bad_reference(pub: dict) -> bool:
    text = ' '.join(str(pub.get(k) or '') for k in ['gost_ru', 'apa_en'])
    return bool(re.search(r'//\s*[КЫ]\.|—\s*Т\.\s*[КЫ]\.|,\s*[КЫ](?:\(|,|\.)', text))


def main() -> int:
    profile_cfg = load_profile_config()
    cfg_ids = profile_cfg.get('identifiers') or {}
    public_profile = read_json(DATA / 'public' / 'profile.json', {})
    publications = read_json(DATA / 'public' / 'publications.json', [])
    wos_profile = read_json(DATA / 'wos' / 'profile_metrics.json', {})
    wos_report = read_json(DATA / 'wos' / 'harvest_report.json', {})
    elib_report = read_json(DATA / 'elibrary' / 'browser_fetch_report.json', {})
    enrichment_report = read_json(DATA / 'audit' / 'publication_metadata_enrichment_report.json', {})

    ids_public = public_profile.get('identifiers') or {}
    issues: list[dict] = []
    warnings: list[dict] = []

    for key in ['elibrary_authorid', 'orcid', 'scopus_author_id', 'wos_researcher_id']:
        cfg = clean(cfg_ids.get(key))
        pub = clean(ids_public.get(key))
        if cfg and pub and cfg != pub:
            issues.append({'code': 'identifier_mismatch', 'key': key, 'config': cfg, 'public_profile': pub})

    if not isinstance(publications, list) or not publications:
        issues.append({'code': 'no_publications', 'message': 'data/public/publications.json is empty or invalid'})
        publications = []

    source_counts = {
        'elibrary': sum(1 for p in publications if 'elibrary' in sources_of(p) or p.get('elibrary_item_id')),
        'scopus': sum(1 for p in publications if 'scopus' in sources_of(p)),
        'wos': sum(1 for p in publications if 'wos' in sources_of(p) or p.get('wos_records') or p.get('wos_uid')),
        'open': sum(1 for p in publications if any(s in sources_of(p) for s in ['orcid_public_api', 'openalex_author_api', 'crossref_orcid_api', 'open_api'])),
    }

    expected_wos_id = clean(cfg_ids.get('wos_researcher_id'))
    actual_wos_id = clean(wos_profile.get('researcher_id'))
    wos_records = wos_profile.get('records') if isinstance(wos_profile, dict) else []
    if expected_wos_id and actual_wos_id and expected_wos_id != actual_wos_id:
        issues.append({'code': 'wos_researcher_id_mismatch', 'config': expected_wos_id, 'wos_profile': actual_wos_id})
    if expected_wos_id and not isinstance(wos_records, list):
        issues.append({'code': 'wos_records_invalid', 'message': 'data/wos/profile_metrics.json records is not a list'})
        wos_records = []
    elif expected_wos_id and len(wos_records) == 0:
        warnings.append({'code': 'wos_records_empty', 'message': 'WoS metrics may exist, but no publication records are currently normalized.'})
    if not isinstance(wos_records, list):
        wos_records = []

    wos_publications_metric = metric(public_profile, 'wos', 'publications')
    wos_citations_metric = metric(public_profile, 'wos', 'citations')
    wos_h_metric = metric(public_profile, 'wos', 'h_index')
    if expected_wos_id and wos_publications_metric is None:
        warnings.append({'code': 'wos_publications_metric_missing', 'message': 'WoS publications metric is missing from data/public/profile.json'})
    if expected_wos_id and len(wos_records) and int(wos_publications_metric or 0) < len(wos_records):
        warnings.append({'code': 'wos_metric_less_than_records', 'metric': wos_publications_metric, 'records': len(wos_records)})
    if public_profile.get('wos_records_count') != len(wos_records):
        warnings.append({'code': 'wos_profile_public_count_differs', 'public_profile_count': public_profile.get('wos_records_count'), 'wos_records': len(wos_records)})

    weak_wos_statuses = []
    for section in ['browser', 'direct_wosnx']:
        block = wos_report.get(section) if isinstance(wos_report, dict) else None
        if isinstance(block, dict):
            status = block.get('status')
            route = block.get('route')
            if status and status not in {'ok', 'partial_records'}:
                weak_wos_statuses.append({'section': section, 'route': route, 'status': status, 'excerpt': clean(block.get('excerpt'))[:160]})
    if weak_wos_statuses and len(wos_records) > 0:
        warnings.append({'code': 'wos_live_fallback_used', 'message': 'Live WoS harvest was weak, but preserved records are available.', 'details': weak_wos_statuses})
    elif weak_wos_statuses and len(wos_records) == 0:
        issues.append({'code': 'wos_live_failed_no_records', 'details': weak_wos_statuses})

    bad_refs = [p.get('title') or p.get('title_ru') or p.get('title_en') for p in publications if has_bad_reference(p)]
    if bad_refs:
        warnings.append({'code': 'bad_reference_text_detected', 'count': len(bad_refs), 'sample': bad_refs[:10]})

    missing_reference = [p.get('title') or p.get('title_ru') or p.get('title_en') for p in publications if not (p.get('gost_ru') or p.get('apa_en'))]
    if missing_reference:
        warnings.append({'code': 'missing_ready_references', 'count': len(missing_reference), 'sample': missing_reference[:10]})

    checks = {
        'publications_count': len(publications),
        'canonical_publications_count': public_profile.get('canonical_publications_count'),
        'source_counts': source_counts,
        'wos_records_count': len(wos_records),
        'wos_public_profile_records_count': public_profile.get('wos_records_count'),
        'wos_enriched_publications_count': public_profile.get('wos_enriched_publications_count'),
        'wos_auto_added_publications_count': public_profile.get('wos_auto_added_publications_count'),
        'wos_metrics': {
            'publications': wos_publications_metric,
            'citations': wos_citations_metric,
            'h_index': wos_h_metric,
        },
        'elibrary_browser_status': elib_report.get('status') if isinstance(elib_report, dict) else None,
        'reference_enrichment_stats': (enrichment_report.get('stats') if isinstance(enrichment_report, dict) else None),
    }
    health = load_source_health(cfg_ids, public_profile.get('source_health'))
    degraded = {name: state for name, state in health.items() if not is_fresh(state)}
    for name, state in degraded.items():
        warnings.append({'code': 'source_not_fresh', 'provider': name, **state})
    # Structural safety and successful live collection are separate assertions.
    status = 'error' if issues else 'partial' if degraded else 'success'
    payload = {'generated_at': now(), 'status': status, 'checks': checks,
               'source_health': health, 'warnings': warnings, 'issues': issues}
    write_json(REPORT, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if issues else 2 if degraded else 0


if __name__ == '__main__':
    raise SystemExit(main())
