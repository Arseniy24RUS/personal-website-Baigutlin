#!/usr/bin/env python3
from pathlib import Path
from bs4 import BeautifulSoup
import re, json, hashlib, argparse


def clean(s):
    return re.sub(r'\s+', ' ', (s or '').replace('\xa0', ' ')).strip()


def parse_elibrary_author_items(path: str):
    html = Path(path).read_text(encoding='utf-8', errors='replace')
    soup = BeautifulSoup(html, 'html.parser')
    pubs = []
    for row in soup.select('tr[id^=arw]'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        left, mid = cells[0], cells[1]
        cit_cell = cells[2] if len(cells) > 2 else None
        row_id = row.get('id') or ''
        item_id = row_id[3:] if row_id.startswith('arw') else None
        number_m = re.search(r'(\d+)', clean(left.get_text(' ')))
        number = int(number_m.group(1)) if number_m else len(pubs) + 1
        title_a = mid.find('a', href=re.compile(r'item\.asp\?id='))
        title = clean(title_a.get_text(' ')) if title_a else ''
        author_i = mid.find('i')
        authors_raw = clean(author_i.get_text(' ')) if author_i else ''
        venue_a = next((a for a in mid.find_all('a', href=True) if 'contents.asp' in a['href']), None)
        venue = clean(venue_a.get_text(' ')) if venue_a else None
        full = clean(mid.get_text(' '))
        meta = full
        if title and meta.startswith(title):
            meta = clean(meta[len(title):])
        if authors_raw and meta.startswith(authors_raw):
            meta = clean(meta[len(authors_raw):])
        meta = clean(re.sub(r'Версии:.*$', '', meta))
        year_m = re.search(r'(?:^|[\s,.])((?:19|20)\d{2})(?:[.\s,]|$)', meta)
        year = int(year_m.group(1)) if year_m else None
        pages = None
        pages_m = re.search(r'С\.\s*([0-9]+\s*[-–]\s*[0-9]+|[0-9]+)', meta)
        if pages_m:
            pages = clean(pages_m.group(1).replace('–', '-').replace(' ', ''))
        doi = None
        doi_m = re.search(r'10\.\d{4,9}/[^\s]+', meta, flags=re.I)
        if doi_m:
            doi = doi_m.group(0).rstrip('.,;')
        cit_txt = clean(cit_cell.get_text(' ')) if cit_cell else ''
        citations = int(cit_txt) if cit_txt.isdigit() else None
        norm_title = re.sub(r'[^a-zа-я0-9]+', ' ', title.lower(), flags=re.I).strip()
        fingerprint = hashlib.sha256('|'.join([norm_title, str(year or ''), venue or '', authors_raw]).encode('utf-8')).hexdigest()[:16]
        pubs.append({
            'source': 'elibrary_rinc_saved_html',
            'number': number,
            'elibrary_item_id': item_id,
            'url': f'https://www.elibrary.ru/item.asp?id={item_id}' if item_id else None,
            'title': title,
            'title_ru': title,
            'authors_raw': authors_raw,
            'venue': venue,
            'venue_ru': venue,
            'year': year,
            'pages': pages,
            'doi': doi,
            'metadata_raw': meta,
            'rinc_citations': citations,
            'dedupe_fingerprint': fingerprint,
            'sources': ['elibrary'],
        })
    return pubs


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('html')
    ap.add_argument('--out', default='elibrary_publications.json')
    args = ap.parse_args()
    data = parse_elibrary_author_items(args.html)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Parsed {len(data)} publications -> {args.out}')
