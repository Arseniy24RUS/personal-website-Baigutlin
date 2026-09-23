#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DATA = Path('data')
ITEMS_JSON = Path(os.environ.get('ELIBRARY_ITEMS_OUT', 'data/processed/elibrary_publications.json'))
PUBLIC_JSON = Path(os.environ.get('ELIBRARY_PUBLIC_FALLBACK_JSON', 'data/public/publications.json'))
DETAILS_JSON = Path(os.environ.get('ELIBRARY_ITEM_DETAILS_OUT', 'data/elibrary/item_details.json'))
REPORT_JSON = Path(os.environ.get('ELIBRARY_ITEM_DETAILS_REPORT', 'data/elibrary/item_details_report.json'))
SNAPSHOT_DIR = Path(os.environ.get('ELIBRARY_ITEM_DETAILS_SNAPSHOT_DIR', 'data/snapshots/elibrary/item_details'))
COOKIE = os.environ.get('ELIBRARY_COOKIE', '').strip()
LIMIT = int(os.environ.get('ELIBRARY_ITEM_DETAILS_LIMIT', '25'))
DELAY_SEC = float(os.environ.get('ELIBRARY_ITEM_DETAILS_DELAY_SEC', '1.5'))
UA = os.environ.get('ELIBRARY_USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 YaBrowser/26.4.0.0 Safari/537.36')


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


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


def item_id_from_pub(pub: dict) -> str:
    if pub.get('elibrary_item_id'):
        return str(pub['elibrary_item_id'])
    url = str(pub.get('url') or '')
    m = re.search(r'id=(\d+)', url)
    return m.group(1) if m else ''


def needs_details(pub: dict, cached: dict, *, at: datetime | None = None) -> bool:
    item_id = item_id_from_pub(pub)
    if not item_id:
        return False
    if item_id not in cached:
        return True
    entry = cached.get(item_id) or {}
    parsed = entry.get('parsed') or {}
    # ISBN, DOI, volume, issue etc. are optional and their absence is not a
    # failed extraction. A successful page observation is cached as a whole.
    if not parsed or entry.get('status') in {'error', 'blocked'}:
        return True
    try:
        fetched_at = datetime.fromisoformat(str(entry.get('fetched_at', '')).replace('Z', '+00:00'))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    at = at or datetime.now(timezone.utc)
    # Recently published works may receive metadata corrections more often.
    try:
        recent = int(pub.get('year') or 0) >= at.year - 1
    except (TypeError, ValueError):
        recent = False
    max_age_days = 30 if recent else 90
    age_seconds = (at - fetched_at).total_seconds()
    return age_seconds < 0 or age_seconds >= max_age_days * 86400


DETAIL_LABELS = {
    'Название журнала': 'venue', 'Журнал': 'venue', 'Название сборника': 'venue',
    'Сборник': 'venue', 'В сборнике': 'venue', 'Книга': 'book_title',
    'Издательство': 'publisher', 'Издатель': 'publisher', 'Место издания': 'place',
    'Год издания': 'year', 'Год': 'year', 'Том': 'volume', 'Номер': 'issue',
    'Выпуск': 'issue', 'Страницы': 'pages', 'DOI': 'doi', 'ISBN': 'isbn', 'ISSN': 'issn',
}
LABEL_PATTERN = '|'.join(re.escape(label) for label in sorted(DETAIL_LABELS, key=len, reverse=True))
NON_BIBLIOGRAPHIC_SECTION = re.compile(
    r'^(?:АННОТАЦИЯ|ABSTRACT|КЛЮЧЕВЫЕ СЛОВА|KEYWORDS|СПИСОК ЛИТЕРАТУРЫ|'
    r'ЛИТЕРАТУРА|REFERENCES|БЛАГОДАРНОСТИ|ACKNOWLEDGMENTS?|ФИНАНСИРОВАНИЕ|'
    r'ИСТОЧНИКИ ФИНАНСИРОВАНИЯ|ГРАНТЫ)(?:\s*:|\s*$)', re.I)


def text_after_label(text: str, label: str) -> str:
    """A whole metadata label, never a substring of a menu or abstract word."""
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    for index, line in enumerate(lines):
        match = re.fullmatch(rf'{re.escape(label)}(?:\s*:\s*(.*))?', line, flags=re.I)
        if not match:
            continue
        value = clean(match.group(1))
        if not value:
            following = lines[index + 1:]  # Table cells often put the value on its own line.
            value = next((line for line in following if line != ':'), '')
        if re.match(rf'^(?:{LABEL_PATTERN})(?:\s*:|\s*$)', value, re.I):
            return ''
        return value
    return ''


def sanitize_detail_fields(parsed: dict) -> dict:
    """Validate shapes at cache/consumer boundaries without inventing metadata.

    Shape validation cannot distinguish a cited DOI from the work's own DOI;
    parse_detail_html therefore also requires explicit bibliographic labels.
    """
    result = {}
    for key, raw in parsed.items():
        value = clean(raw)
        if not value:
            continue
        valid = False
        if key in ('venue', 'book_title', 'publisher', 'place'):
            valid = len(value) >= 3 and len(re.findall(r'[A-Za-zА-Яа-яЁё]', value)) >= 2 and not value.endswith((':', ';'))
        elif key == 'doi':
            value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value, flags=re.I).rstrip('.,;').lower()
            valid = bool(re.fullmatch(r'10\.\d{4,9}/[^\s<>,;"\']+', value, re.I))
        elif key == 'year':
            valid = bool(re.fullmatch(r'(?:19|20)\d{2}', value))
        elif key in ('volume', 'issue'):
            # One range or supplement is plausible; multi-part grant numbers and
            # fragments of prose are not publication volume/issue identifiers.
            valid = bool(re.fullmatch(r'(?:[A-Za-zА-Яа-я]?\d{1,4}[A-Za-zА-Яа-я]?(?:[-/]\d{1,3})?(?:\(\d{1,4}\))?|[IVXLC]{1,8})', value))
        elif key == 'pages':
            value = re.sub(r'\s+', '', value).replace('–', '-').replace('—', '-')
            valid = bool(re.fullmatch(r'[A-Za-zА-Яа-я]?\d{1,12}(?:-[A-Za-zА-Яа-я]?\d{1,12})?', value))
        elif key == 'isbn':
            compact = re.sub(r'[\s-]', '', value)
            valid = bool(re.fullmatch(r'(?:\d{9}[\dXx]|\d{13})', compact))
        elif key == 'issn':
            valid = bool(re.fullmatch(r'\d{4}-?\d{3}[\dXx]', value))
        if valid:
            result[key] = value
    return result


def parse_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, nav, aside'):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    # Once abstract, funding or references start, bibliographic extraction ends.
    # Article text can contain DOI links, "Т. 13" and grant "№ 22-12-20032".
    metadata_lines = []
    for line in text.splitlines():
        if NON_BIBLIOGRAPHIC_SECTION.match(clean(line)):
            break
        metadata_lines.append(line)
    text = '\n'.join(metadata_lines)
    text = re.sub(rf'[^\S\n]+(?=(?:{LABEL_PATTERN})\s*:)', '\n', text, flags=re.I)
    parsed: dict[str, Any] = {}
    for name, key in {
        'citation_doi': 'doi', 'citation_journal_title': 'venue', 'citation_conference_title': 'venue',
        'citation_volume': 'volume', 'citation_issue': 'issue', 'citation_issn': 'issn',
        'citation_isbn': 'isbn', 'citation_publisher': 'publisher',
    }.items():
        node = soup.find('meta', attrs={'name': name})
        if node and node.get('content'):
            parsed.setdefault(key, node['content'])
    for label, key in DETAIL_LABELS.items():
        value = text_after_label(text, label)
        if value and key not in parsed:
            parsed[key] = value
    parsed = sanitize_detail_fields(parsed)
    parsed['raw_text_excerpt'] = clean(text)[:2500]
    return parsed


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru,en;q=0.9',
        'Referer': 'https://www.elibrary.ru/author_items.asp?authorid=1170779&pubrole=100&show_refs=1&pubcat=risc',
    })
    if COOKIE:
        s.headers.update({'Cookie': COOKIE})
    return s


def main() -> int:
    publications = read_json(ITEMS_JSON, [])
    if not isinstance(publications, list) or len(publications) < 10:
        publications = read_json(PUBLIC_JSON, [])
    if not isinstance(publications, list):
        publications = []
    payload = read_json(DETAILS_JSON, {})
    if not isinstance(payload, dict) or 'items' not in payload:
        payload = {'generated_at': None, 'items': payload if isinstance(payload, dict) else {}}
    cached = payload.setdefault('items', {})
    todo = [p for p in publications if isinstance(p, dict) and needs_details(p, cached)]
    report = {'generated_at': now(), 'records_seen': len(publications), 'candidates': len(todo), 'fetched': 0, 'failed': 0, 'skipped_due_limit': max(0, len(todo) - LIMIT), 'items': []}
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    s = session()
    for pub in todo[:LIMIT]:
        item_id = item_id_from_pub(pub)
        if not item_id:
            continue
        url = f'https://www.elibrary.ru/item.asp?id={item_id}'
        item_report = {'item_id': item_id, 'url': url, 'title': pub.get('title_ru') or pub.get('title')}
        try:
            res = s.get(url, timeout=45)
            item_report['status_code'] = res.status_code
            html = res.text or ''
            item_report['bytes'] = len(html.encode('utf-8', errors='replace'))
            if res.status_code == 200 and 'item.asp' in res.url and len(html) > 1000:
                snap = SNAPSHOT_DIR / f'item_{item_id}_{stamp()}.html'
                snap.write_text(html, encoding='utf-8', errors='replace')
                parsed = parse_detail_html(html)
                cached[item_id] = {'fetched_at': now(), 'url': url, 'snapshot_path': str(snap), 'parsed': parsed}
                item_report['parsed_keys'] = sorted(k for k, v in parsed.items() if v and k != 'raw_text_excerpt')
                report['fetched'] += 1
            else:
                item_report['error'] = 'unexpected_response'
                report['failed'] += 1
        except Exception as exc:
            item_report['error'] = repr(exc)
            report['failed'] += 1
        report['items'].append(item_report)
        if DELAY_SEC:
            time.sleep(DELAY_SEC)
    payload['generated_at'] = now()
    payload['schema'] = 'elibrary_item_details/v1'
    write_json(DETAILS_JSON, payload)
    write_json(REPORT_JSON, report)
    print(json.dumps({'details_cache_items': len(cached), 'fetched': report['fetched'], 'failed': report['failed'], 'candidates': report['candidates']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
