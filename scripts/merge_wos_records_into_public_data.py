#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
from build_public_data import is_fresh, normalize_health, citation_is_new, mark_citation_observation
from source_health import component_state
from typing import Any

DATA = Path('data')
PUBLICATIONS_JSON = DATA / 'public' / 'publications.json'
PROFILE_JSON = DATA / 'public' / 'profile.json'
WOS_PROFILE_JSON = DATA / 'wos' / 'profile_metrics.json'
REPORT_JSON = DATA / 'audit' / 'wos_public_merge_report.json'


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def clean(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def has_cyrillic(value: Any) -> bool:
    return bool(re.search(r'[А-Яа-яЁё]', str(value or '')))


def has_latin(value: Any) -> bool:
    return bool(re.search(r'[A-Za-z]', str(value or '')))


def norm_title(value: Any) -> str:
    text = clean(value).lower().replace('ё', 'е')
    text = text.replace('’', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'[^a-zа-я0-9]+', ' ', text).strip()


def norm_doi(value: Any) -> str:
    doi = clean(value).lower()
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi)
    return doi.rstrip('.,;')


def add_source(pub: dict, source: str) -> None:
    sources = pub.get('sources') or []
    if isinstance(sources, str):
        sources = [x.strip() for x in sources.split(',') if x.strip()]
    if source not in sources:
        sources.append(source)
    pub['sources'] = sources


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, '', []):
            return value
    return None


def set_missing(pub: dict, key: str, value: Any) -> bool:
    if value in (None, '', []):
        return False
    if pub.get(key) in (None, '', []):
        pub[key] = value
        return True
    return False


def set_lang_field(pub: dict, base: str, value: Any, prefer: str | None = None) -> bool:
    value = clean(value)
    if not value:
        return False
    lang = prefer
    if lang not in {'ru', 'en'}:
        lang = 'ru' if has_cyrillic(value) else 'en' if has_latin(value) else None
    changed = False
    if lang:
        changed = set_missing(pub, f'{base}_{lang}', value) or changed
    changed = set_missing(pub, base, value) or changed
    return changed


def pub_indexes(publications: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for pub in publications:
        for rec in pub.get('wos_records') or []:
            uid = clean(rec.get('wos_uid'))
            if uid:
                idx[f'wos:{uid}'] = pub
        if pub.get('wos_uid'):
            idx[f"wos:{clean(pub.get('wos_uid'))}"] = pub
        doi = norm_doi(pub.get('doi'))
        if doi:
            idx[f'doi:{doi}'] = pub
        for title_key in [pub.get('title'), pub.get('title_en'), pub.get('title_ru')]:
            title = norm_title(title_key)
            if title:
                idx.setdefault(f'title:{title}', pub)
                idx.setdefault(f'title-year:{title}|{pub.get("year") or ""}', pub)
    return idx


def find_target(record: dict, idx: dict[str, dict]) -> dict | None:
    uid = clean(record.get('wos_uid'))
    if uid and f'wos:{uid}' in idx:
        return idx[f'wos:{uid}']
    doi = norm_doi(record.get('doi'))
    if doi and f'doi:{doi}' in idx:
        return idx[f'doi:{doi}']
    title = norm_title(record.get('title_en') or record.get('title'))
    year = record.get('year') or ''
    if title and f'title-year:{title}|{year}' in idx:
        return idx[f'title-year:{title}|{year}']
    if title and f'title:{title}' in idx:
        return idx[f'title:{title}']
    return None


def append_unique_record(pub: dict, record: dict) -> None:
    existing = pub.setdefault('wos_records', [])
    uid = clean(record.get('wos_uid'))
    doi = norm_doi(record.get('doi'))
    for item in existing:
        if uid and clean(item.get('wos_uid')) == uid:
            item.update({k: v for k, v in record.items() if v not in (None, '', [])})
            return
        if doi and norm_doi(item.get('doi')) == doi:
            item.update({k: v for k, v in record.items() if v not in (None, '', [])})
            return
    existing.append(record)


def enrich_existing(pub: dict, record: dict, fresh: bool = False) -> int:
    changed = 0
    add_source(pub, 'wos')
    append_unique_record(pub, record)
    if set_missing(pub, 'wos_uid', record.get('wos_uid')): changed += 1
    if set_missing(pub, 'doi', norm_doi(record.get('doi'))): changed += 1
    if set_missing(pub, 'url', record.get('url')): changed += 1
    if set_missing(pub, 'authors_raw', record.get('authors_raw')): changed += 1
    if set_missing(pub, 'venue', record.get('venue') or record.get('venue_en')): changed += 1
    if set_missing(pub, 'venue_en', record.get('venue_en') or record.get('venue')): changed += 1
    if set_missing(pub, 'volume', record.get('volume')): changed += 1
    if set_missing(pub, 'issue', record.get('issue')): changed += 1
    if set_missing(pub, 'pages', record.get('pages')): changed += 1
    if set_missing(pub, 'issn', record.get('issn')): changed += 1
    if set_missing(pub, 'eissn', record.get('eissn')): changed += 1
    if set_missing(pub, 'isbn', record.get('isbn')): changed += 1
    if citation_is_new(record, pub, 'wos', fresh) and record.get('wos_citations') is not None:
        if pub.get('wos_citations') != record['wos_citations']:
            pub['wos_citations'] = record['wos_citations']
            changed += 1
        mark_citation_observation(pub, record, 'wos')
    elif set_missing(pub, 'wos_citations', record.get('wos_citations')):
        changed += 1
    if set_missing(pub, 'references_count', record.get('references_count')): changed += 1
    if set_missing(pub, 'publication_type', record.get('document_type')): changed += 1
    if set_lang_field(pub, 'title', record.get('title_en') or record.get('title'), 'en'): changed += 1
    return changed


def auto_record(record: dict) -> dict:
    title = clean(record.get('title_en') or record.get('title'))
    doi = norm_doi(record.get('doi'))
    raw_for_key = '|'.join([title.lower(), str(record.get('year') or ''), doi or clean(record.get('wos_uid'))])
    fp = hashlib.sha256(raw_for_key.encode('utf-8')).hexdigest()[:16]
    return {
        'source': 'wos_free_view_auto',
        'number': None,
        'elibrary_item_id': None,
        'year': record.get('year'),
        'rinc_citations': None,
        'title': title,
        'title_en': title,
        'title_en_source': 'web_of_science',
        'title_ru': title if has_cyrillic(title) else '',
        'authors_raw': record.get('authors_raw') or '',
        'venue': record.get('venue') or record.get('venue_en'),
        'venue_en': record.get('venue_en') or record.get('venue'),
        'venue_en_source': 'web_of_science',
        'volume': record.get('volume'),
        'issue': record.get('issue'),
        'pages': record.get('pages'),
        'doi': doi,
        'url': record.get('url'),
        'issn': record.get('issn'),
        'eissn': record.get('eissn'),
        'isbn': record.get('isbn'),
        'wos_uid': record.get('wos_uid'),
        'wos_citations': record.get('wos_citations'),
        'references_count': record.get('references_count'),
        'publication_type': record.get('document_type'),
        'sources': ['wos'],
        'wos_records': [record],
        'dedupe_fingerprint': fp,
        'auto_accept_reason': 'author-scoped Web of Science ResearcherID record',
    }


def sort_key(pub: dict) -> tuple:
    year = pub.get('year')
    try:
        year_num = int(year)
    except Exception:
        year_num = 0
    number = pub.get('number')
    try:
        number_num = int(number)
    except Exception:
        number_num = 999999
    return (-year_num, number_num, clean(pub.get('title') or pub.get('title_en') or pub.get('title_ru')).lower())


def main() -> int:
    publications = json.loads(PUBLICATIONS_JSON.read_text(encoding='utf-8'))
    if not isinstance(publications, list):
        raise SystemExit(f'{PUBLICATIONS_JSON} is missing or invalid')
    profile = read_json(PROFILE_JSON, {})
    wos_profile = read_json(WOS_PROFILE_JSON, {})
    health = normalize_health(read_json(DATA / 'wos/harvest_report.json', {}))
    records = wos_profile.get('records') or []
    if not isinstance(records, list):
        records = []

    idx = pub_indexes(publications)
    enriched = 0
    added = 0
    changed_fields = 0
    unmatched = []
    matched = []

    for record in records:
        if not isinstance(record, dict) or not clean(record.get('title_en') or record.get('title')):
            continue
        target = find_target(record, idx)
        if target:
            changed_fields += enrich_existing(target, record, fresh=is_fresh(component_state(health, 'publications')))
            enriched += 1
            matched.append({'title': record.get('title_en') or record.get('title'), 'doi': record.get('doi'), 'wos_uid': record.get('wos_uid')})
        else:
            rec = auto_record(record)
            publications.append(rec)
            added += 1
            unmatched.append({'title': rec.get('title_en') or rec.get('title'), 'doi': rec.get('doi'), 'wos_uid': rec.get('wos_uid')})
            idx = pub_indexes(publications)

    publications.sort(key=sort_key)
    write_json(PUBLICATIONS_JSON, publications)

    if isinstance(profile, dict):
        profile['generated_at'] = now()
        profile['canonical_publications_count'] = len(publications)
        profile['wos_records_count'] = len(records)
        profile['wos_enriched_publications_count'] = enriched
        profile['wos_auto_added_publications_count'] = added
        profile['wos_public_merge_report'] = {
            'generated_at': now(),
            'records_seen': len(records),
            'enriched': enriched,
            'added': added,
            'changed_fields': changed_fields,
        }
        write_json(PROFILE_JSON, profile)

    report = {
        'generated_at': now(),
        'records_seen': len(records),
        'publications_after_merge': len(publications),
        'enriched': enriched,
        'added': added,
        'changed_fields': changed_fields,
        'matched_sample': matched[:20],
        'auto_added': unmatched,
    }
    write_json(REPORT_JSON, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
