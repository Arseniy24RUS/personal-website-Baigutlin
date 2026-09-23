#!/usr/bin/env python3
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import argparse
import hashlib
import json
import re


def clean(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').replace('\xa0', ' ')).strip()


def parse_number(value):
    match = re.search(r'\d+(?:[,.]\d+)?', str(value or ''))
    if not match:
        return None
    raw = match.group(0).replace(',', '.')
    return float(raw) if '.' in raw else int(raw)


def parse_wos_researcher_profile_html(html: str, *, source_file_sha256: str | None = None) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    blocks = []
    for div in soup.select('.wat-author-metric-inline-block'):
        values = [clean(node.get_text(' ')) for node in div.select('.wat-author-metric')]
        descriptor = clean(' '.join(node.get_text(' ') for node in div.select('.wat-author-metric-descriptor')))
        full = clean(div.get_text(' '))
        if values or descriptor:
            blocks.append({'values': values, 'descriptor': descriptor, 'full': full})

    # Web of Science currently renders author metrics through Angular markup. The block order is stable
    # in the saved researcher profile page: H-Index, publications, citations, citing articles,
    # citations without self-citation, citing articles without self-citation, patents, policy documents.
    def value_at(index):
        if index < len(blocks) and blocks[index].get('values'):
            return parse_number(blocks[index]['values'][0])
        return None

    return {
        'source': 'wos_saved_html',
        'source_url': 'https://www.webofscience.com/wos/author/record/AAN-4717-2020',
        'researcher_id': 'AAN-4717-2020',
        'author_name': 'Danil Baigutlin',
        'snapshot_generated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'source_file_sha256': source_file_sha256,
        'summary': {
            'h_index': value_at(0),
            'publications': value_at(1),
            'citations': value_at(2),
            'citing_articles': value_at(3),
            'citations_without_self': value_at(4),
            'citing_articles_without_self': value_at(5),
            'patent_citations': value_at(6),
            'citing_patents': value_at(7),
            'policy_citations': value_at(8),
            'citing_policy_documents': value_at(9),
        },
        'raw_blocks': blocks[:20],
        'parser_note': 'Parsed from user-saved Web of Science researcher profile HTML. Live WoS harvesting should use the official WoS API when approved.',
    }


def parse_file(path: str) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    html = raw.decode('utf-8', errors='replace')
    return parse_wos_researcher_profile_html(html, source_file_sha256=hashlib.sha256(raw).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('html')
    parser.add_argument('--out', default='data/wos/profile_metrics.json')
    args = parser.parse_args()
    data = parse_file(args.html)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Parsed WoS researcher profile metrics -> {out}")
    print(json.dumps(data['summary'], ensure_ascii=False, indent=2))
