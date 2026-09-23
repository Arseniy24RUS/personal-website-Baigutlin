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


def text_after_label(text: str, label: str) -> str:
    m = re.search(rf'{re.escape(label)}\s*[:\-]?\s*([^\n\r]+)', text, flags=re.I)
    return clean(m.group(1)) if m else ''


def parse_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text('\n', strip=True)
    flat = clean(text)
    parsed: dict[str, Any] = {}

    doi = re.search(r'10\.\d{4,9}/[^\s<>,;"\']+', flat, flags=re.I)
    if doi:
        parsed['doi'] = doi.group(0).rstrip('.,;').lower()

    isbn = re.search(r'ISBN\s*[:\-]?\s*([0-9Xx\- ]{10,20})', flat, flags=re.I)
    if isbn:
        parsed['isbn'] = clean(isbn.group(1))
    issn = re.search(r'ISSN\s*[:\-]?\s*([0-9Xx]{4}\-?[0-9Xx]{4})', flat, flags=re.I)
    if issn:
        parsed['issn'] = clean(issn.group(1))

    for label, key in [
        ('Журнал', 'venue'), ('Название журнала', 'venue'), ('Сборник', 'venue'), ('Книга', 'book_title'),
        ('Издательство', 'publisher'), ('Издатель', 'publisher'), ('Место издания', 'place'),
        ('Год издания', 'year'), ('Год', 'year'), ('Том', 'volume'), ('Номер', 'issue'),
        ('Выпуск', 'issue'), ('Страницы', 'pages'), ('DOI', 'doi')
    ]:
        value = text_after_label(text, label)
        if value and key not in parsed:
            parsed[key] = value

    m = re.search(r'(?:^|[\s.])Т\.\s*([0-9IVXLCА-Яа-яA-Za-z.-]+)', flat)
    if m:
        parsed.setdefault('volume', m.group(1).strip(' .'))
    m = re.search(r'№\s*([0-9A-Za-zА-Яа-я/().-]+)', flat)
    if m:
        parsed.setdefault('issue', m.group(1).strip(' .'))
    m = re.search(r'[СC]\.\s*([0-9]+\s*[-–—]\s*[0-9]+|[0-9]+)', flat)
    if m:
        parsed.setdefault('pages', clean(m.group(1).replace('—', '-').replace('–', '-').replace(' ', '')))
    m = re.search(r'\b((?:19|20)\d{2})\b', flat)
    if m:
        parsed.setdefault('year', int(m.group(1)))

    # eLibrary item pages usually contain the bibliographic block as plain text; keep a short raw tail for audits.
    parsed['raw_text_excerpt'] = flat[:2500]
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
