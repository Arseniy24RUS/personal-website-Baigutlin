#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote
import csv
import hashlib
import json
import os
import re
import time
from typing import Any

import requests
from build_public_data import usable_source_pages

DATA = Path('data')
PUBLIC = DATA / 'public'
PUBLICATIONS_JSON = PUBLIC / 'publications.json'
PUBLICATIONS_TSV = PUBLIC / 'publications.tsv'
REPORT_JSON = DATA / 'audit' / 'publication_metadata_enrichment_report.json'
CROSSREF_CACHE = DATA / 'curation' / 'crossref_metadata_cache.json'
ELIBRARY_DETAILS_JSON = DATA / 'elibrary' / 'item_details.json'

CROSSREF_MAILTO = os.environ.get('CROSSREF_MAILTO', 'd0nik1996@mail.ru')
CROSSREF_DELAY_SEC = float(os.environ.get('CROSSREF_DELAY_SEC', '0.2'))

ACRONYMS = {
    'рф': 'РФ', 'ран': 'РАН', 'ринц': 'РИНЦ', 'вак': 'ВАК', 'рудн': 'РУДН', 'фнисц': 'ФНИСЦ',
    'ранхигс': 'РАНХиГС', 'рнф': 'РНФ', 'рффи': 'РФФИ', 'гис': 'ГИС', 'дпо': 'ДПО',
    'еаэс': 'ЕАЭС', 'снг': 'СНГ', 'ссср': 'СССР', 'esg': 'ESG', 'gis': 'GIS', 'doi': 'DOI',
    'кмнр': 'КМНР', 'кмнс': 'КМНС', 'оон': 'ООН', 'рцни': 'РЦНИ', 'wos': 'WoS', 'scopus': 'Scopus'
}

PROPER_REPLACEMENTS = [
    ('россия', 'Россия'), ('россии', 'России'), ('россию', 'Россию'), ('россией', 'Россией'),
    ('российской федерации', 'Российской Федерации'), ('республика тыва', 'Республика Тыва'),
    ('республики тыва', 'Республики Тыва'), ('республике тыва', 'Республике Тыва'),
    ('тыва', 'Тыва'), ('тувы', 'Тувы'), ('северного казахстана', 'Северного Казахстана'),
    ('казахстана', 'Казахстана'), ('евразийского макрорегиона', 'Евразийского макрорегиона'),
    ('челябинской области', 'Челябинской области'), ('московского региона', 'Московского региона'),
    ('севастополя', 'Севастополя'), ('чувашии', 'Чувашии'), ('уральских', 'уральских'),
    ('л. л. рыбаковского', 'Л. Л. Рыбаковского'), ('рыбаковского', 'Рыбаковского'),
]

RU_LAT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'i','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya'
}


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


def is_mostly_upper(value: str) -> bool:
    letters = re.findall(r'[A-Za-zА-Яа-яЁё]', value or '')
    if len(letters) < 8:
        return False
    upper = [x for x in letters if x.upper() == x and x.lower() != x]
    return len(upper) / max(1, len(letters)) > 0.65


def capitalize_first_letter(value: str) -> str:
    for i, ch in enumerate(value):
        if ch.isalpha():
            return value[:i] + ch.upper() + value[i + 1:]
    return value


def replace_word_case(text: str, source: str, target: str) -> str:
    return re.sub(rf'(?<![\wА-Яа-яЁё]){re.escape(source)}(?![\wА-Яа-яЁё])', target, text, flags=re.I)


def smart_ru_title(title: str) -> str:
    title = clean(title)
    if not title:
        return ''
    if not has_cyrillic(title):
        if is_mostly_upper(title):
            title = title.lower()
        return capitalize_first_letter(title)
    result = title.lower() if is_mostly_upper(title) else title
    result = re.sub(r'\s+([:;,.!?])', r'\1', result)
    result = re.sub(r'([:;,.!?])([^\s])', r'\1 \2', result)
    result = re.sub(r'\s*[-–—]\s*', ' — ', result)
    result = re.sub(r'\s+', ' ', result).strip()
    result = capitalize_first_letter(result)
    for source, target in sorted(PROPER_REPLACEMENTS, key=lambda x: -len(x[0])):
        result = replace_word_case(result, source, target)
    for source, target in ACRONYMS.items():
        result = replace_word_case(result, source, target)
    result = re.sub(r'(?<![А-ЯA-Z])\b([а-яёa-z])\.\s*([а-яёa-z])\.', lambda m: f'{m.group(1).upper()}. {m.group(2).upper()}.', result)
    result = re.sub(r'\bг\.\s*([а-яё])', lambda m: 'г. ' + m.group(1).upper(), result)
    return result


def normalize_doi(value: Any) -> str:
    doi = clean(value)
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi, flags=re.I).strip()
    return doi.rstrip('.,;').lower()


def page_range(value: Any) -> str:
    value = clean(value)
    value = re.sub(r'^[СC]\.?\s*', '', value)
    value = value.replace('—', '-').replace('–', '-')
    value = re.sub(r'\s*-\s*', '–', value)
    return value


def parse_elibrary_metadata(raw: str) -> dict[str, Any]:
    raw = clean(raw)
    out: dict[str, Any] = {}
    if not raw:
        return out
    m = re.search(r'(?:^|[\s.])Т\.\s*([0-9IVXLCА-Яа-яA-Za-z.-]+)', raw)
    if m:
        out['volume'] = m.group(1).strip(' .')
    m = re.search(r'№\s*([0-9A-Za-zА-Яа-я/().-]+)', raw)
    if m:
        out['issue'] = m.group(1).strip(' .')
    m = re.search(r'[СC]\.\s*([0-9]+\s*[-–—]\s*[0-9]+|[0-9]+)', raw)
    if m:
        out['pages'] = page_range(m.group(1))
    doi = re.search(r'10\.\d{4,9}/[^\s]+', raw, flags=re.I)
    if doi:
        out['doi'] = normalize_doi(doi.group(0))
    return out


def item_id_from_pub(pub: dict) -> str:
    if pub.get('elibrary_item_id'):
        return str(pub['elibrary_item_id'])
    m = re.search(r'id=(\d+)', str(pub.get('url') or ''))
    return m.group(1) if m else ''


def load_elibrary_item_details() -> dict:
    payload = read_json(ELIBRARY_DETAILS_JSON, {})
    return payload.get('items', {}) if isinstance(payload, dict) and isinstance(payload.get('items'), dict) else {}


def merge_elibrary_item_details(pub: dict, details: dict) -> int:
    item_id = item_id_from_pub(pub)
    parsed = ((details.get(item_id) or {}).get('parsed') or {}) if item_id else {}
    added = 0
    for src, dst in [
        ('venue', 'venue'), ('publisher', 'publisher'), ('place', 'place'), ('book_title', 'book_title'),
        ('volume', 'volume'), ('issue', 'issue'), ('pages', 'pages'), ('doi', 'doi'), ('isbn', 'isbn'), ('issn', 'issn')
    ]:
        value = parsed.get(src)
        if dst == 'pages' and not usable_source_pages(value):
            continue
        if value not in (None, '', []) and not pub.get(dst):
            pub[dst] = normalize_doi(value) if dst == 'doi' else page_range(value) if dst == 'pages' else value
            added += 1
    if parsed and not pub.get('elibrary_item_detail'):
        pub['elibrary_item_detail'] = {k: v for k, v in parsed.items() if k != 'raw_text_excerpt'}
    return added


def crossref_cache_payload() -> dict:
    payload = read_json(CROSSREF_CACHE, {})
    if isinstance(payload, dict) and 'items' in payload:
        return payload
    return {'generated_at': None, 'items': payload if isinstance(payload, dict) else {}}


def extract_crossref_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return {}
    def first_list(name):
        val = message.get(name)
        return val[0] if isinstance(val, list) and val else None
    issued = message.get('issued') or message.get('published-print') or message.get('published-online') or {}
    year = None
    parts = issued.get('date-parts') if isinstance(issued, dict) else None
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        year = parts[0][0]
    return {
        'source': 'crossref_api', 'doi': normalize_doi(message.get('DOI')), 'type': message.get('type'),
        'title': first_list('title'), 'subtitle': first_list('subtitle'), 'container_title': first_list('container-title'),
        'publisher': message.get('publisher'), 'volume': message.get('volume'), 'issue': message.get('issue'),
        'page': page_range(message.get('page')), 'year': year, 'issn': message.get('ISSN'), 'isbn': message.get('ISBN'), 'url': message.get('URL'),
    }


def fetch_crossref_by_doi(doi: str, cache: dict, stats: dict) -> dict:
    doi = normalize_doi(doi)
    if not doi:
        return {}
    items = cache.setdefault('items', {})
    if doi in items:
        stats['crossref_cache_hit'] += 1
        return items[doi].get('metadata') or {}
    url = f'https://api.crossref.org/works/{quote(doi, safe="")}'
    headers = {'User-Agent': f'personal-website metadata enricher (mailto:{CROSSREF_MAILTO})'}
    try:
        res = requests.get(url, headers=headers, timeout=25)
        if res.status_code == 200:
            metadata = extract_crossref_message((res.json() or {}).get('message') or {})
            items[doi] = {'fetched_at': now(), 'status': 200, 'metadata': metadata}
            stats['crossref_fetched'] += 1
            if CROSSREF_DELAY_SEC:
                time.sleep(CROSSREF_DELAY_SEC)
            return metadata
        items[doi] = {'fetched_at': now(), 'status': res.status_code, 'metadata': {}}
        stats['crossref_failed'] += 1
    except Exception as exc:
        items[doi] = {'fetched_at': now(), 'status': 'error', 'error': repr(exc), 'metadata': {}}
        stats['crossref_failed'] += 1
    return {}


def set_if_missing(pub: dict, key: str, value: Any) -> bool:
    if value in (None, '', []):
        return False
    if key in ('pages', 'page') and not usable_source_pages(value):
        return False
    if pub.get(key) in (None, '', []):
        pub[key] = page_range(value) if key == 'pages' else value
        return True
    return False


def mark_generated_reference(pub: dict, key: str) -> None:
    pub[key + '_source'] = 'generated'
    pub[key + '_generated_sha256'] = hashlib.sha256(pub[key].encode('utf-8')).hexdigest()


def reference_is_generated(pub: dict, key: str) -> bool:
    """A manual edit invalidates provenance even when its source label remains."""
    return (pub.get(key + '_source') == 'generated' and bool(pub.get(key))
            and pub.get(key + '_generated_sha256') == hashlib.sha256(pub[key].encode('utf-8')).hexdigest())


def authors_gost(raw: str) -> str:
    return clean(raw).rstrip('.')


def initials_ru(name: str) -> str:
    return re.sub(r'([А-ЯЁA-Z])\.?(?=\s|$)', r'\1.', clean(name))


def translit(value: str) -> str:
    out = []
    for ch in clean(value):
        low = ch.lower()
        tr = RU_LAT.get(low)
        if tr is None:
            out.append(ch)
        else:
            out.append(tr.capitalize() if ch != low else tr)
    return ''.join(out)


def author_to_apa(part: str) -> str:
    part = clean(part)
    if not part:
        return ''
    if re.search(r'и др\.?|et al\.?', part, flags=re.I):
        return 'et al.'
    part = translit(part)
    m = re.match(r'^(.+?)\s+([A-Z](?:\.?\s*[A-Z]\.?)+)$', part)
    if m:
        initials = re.sub(r'([A-Z])\.?', r'\1. ', m.group(2)).strip()
        return f'{m.group(1).rstrip(".")}, {initials}'
    m = re.match(r'^(.+?),\s*(.+)$', part)
    if m:
        initials = re.sub(r'([A-Z])\.?', r'\1. ', m.group(2)).strip()
        return f'{m.group(1).rstrip(".")}, {initials}'
    return part


def authors_apa(raw: str) -> str:
    parts = [author_to_apa(x) for x in re.split(r',\s*', raw or '') if clean(x)]
    if not parts:
        return 'Baigutlin, D. R.'
    if any(x == 'et al.' for x in parts):
        return (next((x for x in parts if x != 'et al.'), parts[0]) or 'Baigutlin, D. R.') + ' et al.'
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] + ' & ' + parts[1]
    return ', '.join(parts[:-1]) + ', & ' + parts[-1]


def title_en(pub: dict) -> str:
    for value in [pub.get('title_en'), (pub.get('scopus') or {}).get('title')]:
        if clean(value) and not has_cyrillic(value):
            return clean(value)
    return translit(pub.get('title_ru_display') or pub.get('title_ru') or pub.get('title') or '')


def venue_en(pub: dict) -> str:
    for value in [pub.get('venue_en'), (pub.get('scopus') or {}).get('journal_or_source'), (pub.get('scopus') or {}).get('source_title'), (pub.get('crossref_metadata') or {}).get('container_title')]:
        if clean(value) and not has_cyrillic(value):
            return clean(value)
    return translit(pub.get('venue_ru') or pub.get('venue') or pub.get('publisher') or '')


def format_gost(pub: dict) -> str:
    authors = authors_gost(pub.get('authors_raw') or pub.get('authors') or '')
    title = pub.get('title_ru_display') or pub.get('title_ru') or pub.get('title') or ''
    venue = pub.get('venue_ru') or pub.get('venue') or pub.get('book_title') or ''
    year = pub.get('year') or ''
    volume = pub.get('volume') or ''
    issue = pub.get('issue') or ''
    pages = page_range(pub.get('pages') or pub.get('page') or '')
    publisher = pub.get('publisher') or ''
    place = pub.get('place') or ''
    doi = normalize_doi(pub.get('doi'))
    url = clean(pub.get('url'))
    text = ''
    if authors:
        text += authors + '. '
    if title:
        text += title.rstrip('.') + '.'
    if venue:
        text += f' // {venue.rstrip(".")}.'
    elif publisher:
        place_part = f'{place}: ' if place else ''
        text += f' — {place_part}{publisher.rstrip(".")}.'
    if year:
        text += f' — {year}.'
    if volume:
        text += f' — Т. {volume}.'
    if issue:
        text += f' — № {issue}.'
    if pages:
        text += f' — С. {pages}.'
    if doi:
        text += f' — DOI: {doi}.'
    elif url:
        text += f' — URL: {url}.'
    return re.sub(r'\s+', ' ', text).replace('..', '.').strip()


def format_apa(pub: dict) -> str:
    authors = authors_apa(pub.get('authors_en') or pub.get('authors_raw') or pub.get('authors') or '')
    year = pub.get('year') or 'n.d.'
    title = title_en(pub).rstrip('.')
    venue = venue_en(pub)
    volume = clean(pub.get('volume'))
    issue = clean(pub.get('issue'))
    pages = page_range(pub.get('pages') or pub.get('page') or '')
    doi = normalize_doi(pub.get('doi'))
    url = clean(pub.get('url'))
    out = f'{authors} ({year}). {title}.'
    if venue:
        out += f' {venue}'
        if volume:
            out += f', {volume}'
            if issue:
                out += f'({issue})'
        elif issue:
            out += f', ({issue})'
        if pages:
            out += f', {pages}'
        out += '.'
    if doi:
        out += f' https://doi.org/{doi}'
    elif url:
        out += f' {url}'
    return re.sub(r'\s+', ' ', out).strip()


def update_publications_tsv(publications: list[dict]) -> None:
    fields = [
        'number', 'year', 'rinc_citations', 'scopus_citations', 'title', 'title_ru', 'title_ru_display', 'title_en', 'title_en_source',
        'authors', 'venue', 'venue_ru', 'venue_en', 'venue_en_source', 'volume', 'issue', 'pages', 'doi', 'url', 'gost_ru', 'apa_en', 'sources'
    ]
    with PUBLICATIONS_TSV.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(fields)
        for pub in publications:
            scopus_citations = pub.get('scopus_citations')
            if scopus_citations is None:
                scopus_citations = (pub.get('scopus') or {}).get('cited_by_count', '')
            writer.writerow([
                pub.get('number') or '', pub.get('year') or '', pub.get('rinc_citations', 0), scopus_citations if scopus_citations is not None else '',
                pub.get('title') or '', pub.get('title_ru') or pub.get('title') or '', pub.get('title_ru_display') or '', pub.get('title_en') or '', pub.get('title_en_source') or '',
                pub.get('authors_raw') or pub.get('authors') or '', pub.get('venue') or '', pub.get('venue_ru') or pub.get('venue') or '', pub.get('venue_en') or '', pub.get('venue_en_source') or '',
                pub.get('volume') or '', pub.get('issue') or '', pub.get('pages') or '', pub.get('doi') or '', pub.get('url') or '', pub.get('gost_ru') or '', pub.get('apa_en') or '',
                ','.join(pub.get('sources') or []) if isinstance(pub.get('sources'), list) else pub.get('sources') or '',
            ])


def main() -> int:
    publications = read_json(PUBLICATIONS_JSON, None)
    if not isinstance(publications, list):
        raise SystemExit(f'{PUBLICATIONS_JSON} is missing or invalid')
    item_details = load_elibrary_item_details()
    cache = crossref_cache_payload()
    stats = {'records': len(publications), 'titles_fixed': 0, 'title_fields_added': 0, 'elibrary_detail_fields_added': 0, 'metadata_fields_added': 0, 'gost_built': 0, 'apa_built': 0, 'crossref_cache_hit': 0, 'crossref_fetched': 0, 'crossref_failed': 0}
    for pub in publications:
        raw_title_ru = pub.get('title_ru') or pub.get('title') or ''
        # Published wording and identifiers may contain editorial corrections.
        # Enrichment fills gaps; a new provider observation cannot rewrite them.
        for field in ('title_ru', 'title_ru_display'):
            if set_if_missing(pub, field, raw_title_ru):
                stats['title_fields_added'] += 1
        stats['elibrary_detail_fields_added'] += merge_elibrary_item_details(pub, item_details)
        parsed = parse_elibrary_metadata(pub.get('metadata_raw') or '')
        for key, value in parsed.items():
            if set_if_missing(pub, key, value):
                stats['metadata_fields_added'] += 1
        doi = normalize_doi(pub.get('doi'))
        if doi:
            cr = fetch_crossref_by_doi(doi, cache, stats)
            if cr:
                pub['crossref_metadata'] = cr
                for src_key, dst_key in [('volume', 'volume'), ('issue', 'issue'), ('page', 'pages'), ('publisher', 'publisher'), ('type', 'publication_type')]:
                    if set_if_missing(pub, dst_key, cr.get(src_key)):
                        stats['metadata_fields_added'] += 1
                if not pub.get('venue_en') and cr.get('container_title') and not has_cyrillic(cr.get('container_title')):
                    pub['venue_en'] = cr.get('container_title')
                    pub['venue_en_source'] = 'crossref_api'
                    stats['metadata_fields_added'] += 1
        for field, formatter, statistic in (('gost_ru', format_gost, 'gost_built'), ('apa_en', format_apa, 'apa_built')):
            if set_if_missing(pub, field, formatter(pub)):
                mark_generated_reference(pub, field)
                stats[statistic] += 1
    cache['generated_at'] = now()
    cache['schema'] = 'crossref_metadata_cache/v1'
    write_json(CROSSREF_CACHE, cache)
    write_json(PUBLICATIONS_JSON, publications)
    update_publications_tsv(publications)
    write_json(REPORT_JSON, {'generated_at': now(), 'stats': stats})
    print(json.dumps({'stats': stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
