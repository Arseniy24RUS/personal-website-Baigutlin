#!/usr/bin/env python3
"""Enrich publication records with English titles.

Priority for title_en:
1. Existing published title_en, preserved verbatim
2. Scopus
3. OpenAlex
4. Crossref
5. ORCID or existing Latin title
6. cached machine translation in data/curation/publication_title_translations.json
7. new offline Argos Translate ru->en translation, saved to the cache

The script is best-effort. Models are provisioned before collection; this script
uses a bounded offline worker and keeps existing text when no model is available.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
import re
import sys
from typing import Any
from translation_runtime import BoundedArgosTranslator

DATA = Path('data')
PUBLIC = DATA / 'public'
CURATION = DATA / 'curation'
PUBLICATIONS_JSON = PUBLIC / 'publications.json'
PUBLICATIONS_TSV = PUBLIC / 'publications.tsv'
TRANSLATIONS_JSON = CURATION / 'publication_title_translations.json'
REPORT_JSON = DATA / 'audit' / 'publication_title_translation_report.json'

SOURCE_PRIORITY = ['existing_publication', 'scopus', 'openalex_api', 'crossref_api', 'orcid_public_api', 'existing_latin', 'cached_machine_translation', 'argos_translate_ru_en']


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


def latin_value(value: Any) -> str:
    value = clean(value)
    return value if value and not has_cyrillic(value) else ''


def sentence_case(title: str) -> str:
    title = clean(title)
    if not title:
        return ''
    letters = re.sub(r'[^A-Za-zА-Яа-яЁё]', '', title)
    capitals = re.sub(r'[^A-ZА-ЯЁ]', '', title)
    if len(title) > 12 and len(capitals) > max(5, len(letters) / 2):
        title = title.lower()
    return title[:1].upper() + title[1:]


def record_key(pub: dict) -> str:
    if pub.get('elibrary_item_id'):
        return f"elibrary:{pub['elibrary_item_id']}"
    if pub.get('doi'):
        return 'doi:' + clean(pub['doi']).lower().replace('https://doi.org/', '')
    raw = '|'.join([clean(pub.get('title_ru') or pub.get('title')), str(pub.get('year') or ''), clean(pub.get('authors_raw') or '')])
    return 'sha256:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def cache_payload() -> dict:
    payload = read_json(TRANSLATIONS_JSON, {})
    if isinstance(payload, dict) and 'items' in payload:
        return payload
    if isinstance(payload, dict):
        return {'generated_at': None, 'items': payload}
    return {'generated_at': None, 'items': {}}


def source_title_from_open_sources(pub: dict, source_name: str) -> str:
    for rec in pub.get('open_sources') or []:
        if rec.get('source') != source_name:
            continue
        title = latin_value(rec.get('title'))
        if title:
            return title
        raw = rec.get('raw') or {}
        raw_title = (((raw.get('title') or {}).get('title') or {}).get('value')) if isinstance(raw.get('title'), dict) else None
        title = latin_value(raw_title)
        if title:
            return title
    return ''


def source_venue_from_open_sources(pub: dict, source_name: str) -> str:
    for rec in pub.get('open_sources') or []:
        if rec.get('source') != source_name:
            continue
        venue = latin_value(rec.get('venue'))
        if venue:
            return venue
        raw = rec.get('raw') or {}
        journal_title = raw.get('journal-title') or {}
        if isinstance(journal_title, dict):
            venue = latin_value(journal_title.get('value'))
            if venue:
                return venue
        container = raw.get('container-title')
        if isinstance(container, list) and container:
            venue = latin_value(container[0])
            if venue:
                return venue
    return ''


def official_title_candidate(pub: dict) -> tuple[str, str]:
    if clean(pub.get('title_en')):
        return pub['title_en'], pub.get('title_en_source') or 'existing_publication'
    scopus_title = latin_value((pub.get('scopus') or {}).get('title'))
    if scopus_title:
        return sentence_case(scopus_title), 'scopus'
    for source_name in ['openalex_api', 'crossref_api', 'orcid_public_api']:
        title = source_title_from_open_sources(pub, source_name)
        if title:
            return sentence_case(title), source_name
    existing = latin_value(pub.get('title_en')) or latin_value(pub.get('title'))
    if existing:
        return sentence_case(existing), 'existing_latin'
    return '', ''


def official_venue_candidate(pub: dict) -> tuple[str, str]:
    if clean(pub.get('venue_en')):
        return pub['venue_en'], pub.get('venue_en_source') or 'existing_publication'
    scopus = pub.get('scopus') or {}
    venue = latin_value(scopus.get('journal_or_source') or scopus.get('source_title'))
    if venue:
        return venue, 'scopus'
    for source_name in ['openalex_api', 'crossref_api', 'orcid_public_api']:
        venue = source_venue_from_open_sources(pub, source_name)
        if venue:
            return venue, source_name
    existing = latin_value(pub.get('venue_en')) or latin_value(pub.get('venue'))
    if existing:
        return existing, 'existing_latin'
    return '', ''


class ArgosTranslator(BoundedArgosTranslator):
    def translate(self, text: str) -> str:
        return sentence_case(clean(super().translate(text)))


def update_publications_tsv(publications: list[dict]) -> None:
    fields = [
        'number', 'year', 'rinc_citations', 'scopus_citations', 'title', 'title_ru', 'title_en', 'title_en_source',
        'authors', 'venue', 'venue_ru', 'venue_en', 'venue_en_source', 'pages', 'doi', 'url', 'sources'
    ]
    with PUBLICATIONS_TSV.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(fields)
        for pub in publications:
            scopus_citations = pub.get('scopus_citations')
            if scopus_citations is None:
                scopus_citations = (pub.get('scopus') or {}).get('cited_by_count', '')
            writer.writerow([
                pub.get('number') or '',
                pub.get('year') or '',
                pub.get('rinc_citations', 0),
                scopus_citations if scopus_citations is not None else '',
                pub.get('title') or '',
                pub.get('title_ru') or pub.get('title') or '',
                pub.get('title_en') or '',
                pub.get('title_en_source') or '',
                pub.get('authors_raw') or pub.get('authors') or '',
                pub.get('venue') or '',
                pub.get('venue_ru') or pub.get('venue') or '',
                pub.get('venue_en') or '',
                pub.get('venue_en_source') or '',
                pub.get('pages') or '',
                pub.get('doi') or '',
                pub.get('url') or '',
                ','.join(pub.get('sources') or []) if isinstance(pub.get('sources'), list) else pub.get('sources') or '',
            ])


def main() -> int:
    publications = read_json(PUBLICATIONS_JSON, None)
    if not isinstance(publications, list):
        print(f'{PUBLICATIONS_JSON} is missing or invalid', file=sys.stderr)
        return 1

    cache = cache_payload()
    items = cache.setdefault('items', {})
    translator = ArgosTranslator()
    stats = {'official': 0, 'preserved_existing': 0, 'cached_machine_translation': 0, 'new_machine_translation': 0, 'unresolved': 0, 'venue_enriched': 0}

    for pub in publications:
        if not pub.get('title_ru'):
            pub['title_ru'] = pub.get('title') or ''
        title_en, title_source = official_title_candidate(pub)
        key = record_key(pub)
        cache_item = items.get(key) or {}
        if not title_en and latin_value(cache_item.get('title_en')):
            title_en = sentence_case(cache_item['title_en'])
            title_source = cache_item.get('title_en_source') or 'cached_machine_translation'
            stats['cached_machine_translation'] += 1
        elif title_en:
            stats['preserved_existing' if clean(pub.get('title_en')) else 'official'] += 1
        elif has_cyrillic(pub.get('title_ru') or pub.get('title')):
            translated = translator.translate(pub.get('title_ru') or pub.get('title') or '')
            if translated and not has_cyrillic(translated):
                title_en = translated
                title_source = 'argos_translate_ru_en'
                items[key] = {
                    'title_ru': pub.get('title_ru') or pub.get('title') or '',
                    'title_en': title_en,
                    'title_en_source': title_source,
                    'created_at': cache_item.get('created_at') or now(),
                    'updated_at': now(),
                    'review_status': cache_item.get('review_status') or 'machine',
                }
                stats['new_machine_translation'] += 1
            else:
                stats['unresolved'] += 1
        else:
            stats['unresolved'] += 1

        if title_en:
            pub['title_en'] = title_en
            pub['title_en_source'] = title_source

        venue_en, venue_source = official_venue_candidate(pub)
        if venue_en:
            if not clean(pub.get('venue_en')):
                stats['venue_enriched'] += 1
            pub['venue_en'] = venue_en
            pub['venue_en_source'] = venue_source

    cache['generated_at'] = now()
    cache['schema'] = 'publication_title_translations/v1'
    translator.close()
    write_json(TRANSLATIONS_JSON, cache)
    write_json(PUBLICATIONS_JSON, publications)
    update_publications_tsv(publications)
    write_json(REPORT_JSON, {'generated_at': now(), 'stats': stats, 'argos_status': translator.status, 'source_priority': SOURCE_PRIORITY})
    print(json.dumps({'stats': stats, 'argos_status': translator.status}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
