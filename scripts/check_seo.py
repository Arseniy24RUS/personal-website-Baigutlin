#!/usr/bin/env python3
"""Validate the existing bilingual site's metadata, assets and public JSON."""
from pathlib import Path
import json
import re
from media_translation import pending_translation_fields
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, unquote
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
PAGES = ('index.html', 'projects.html', 'publications.html', 'teaching.html', 'media.html', 'diplomas.html', 'it.html', 'materials.html', 'metrics.html')

ERRORS = []

def fail(message):
    ERRORS.append(message)

def read(path):
    return path.read_text(encoding='utf-8')

def check_media_english_localization() -> None:
    cyrillic = re.compile(r"[\u0400-\u04FF]")
    for name in ("published.json", "news_mentions.json"):
        path = ROOT / "data" / "media" / name
        if not path.exists():
            fail(f"Media data file is missing: data/media/{name}")
            continue
        try:
            records = (lambda value: value if isinstance(value, list) else value.get("records", []))(json.loads(read(path)))
        except json.JSONDecodeError:
            continue
        for index, record in enumerate(records, start=1):
            label = record.get("url") or record.get("id") or f"record #{index}"
            state = record.get('translation_state') or {}
            pending = set(pending_translation_fields(record))
            allowed_pending = (set(state.get('fields') or []) & pending
                               if state.get('status') == 'pending' and state.get('reason') else set())
            for key in ("title_en", "description_en"):
                value = str(record.get(key) or "").strip()
                if not value and key not in allowed_pending:
                    fail(f"Missing {key} in data/media/{name}: {label}")
                elif cyrillic.search(value):
                    fail(f"Cyrillic text found in {key} in data/media/{name}: {label}")
            source_en = str(record.get("source_name_en") or "").strip()
            source = str(record.get("source_name") or "")
            if cyrillic.search(source):
                if not source_en and 'source_name_en' not in allowed_pending:
                    fail(f"Missing source_name_en for Cyrillic source in data/media/{name}: {label}")
                elif cyrillic.search(source_en):
                    fail(f"Cyrillic text found in source_name_en in data/media/{name}: {label}")

def main():
    errors = ERRORS
    errors.clear()
    check_media_english_localization()
    domain = (ROOT / 'CNAME').read_text(encoding='utf-8').strip()
    base = 'https://' + domain
    expected = set()
    for lang in ('ru', 'en'):
        for name in PAGES:
            path = ROOT / ('en' if lang == 'en' else '') / name
            url = base + ('/en/' if lang == 'en' else '/') + (name if name != 'index.html' else '')
            expected.add(url)
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
            label = path.relative_to(ROOT).as_posix()
            def require(condition, reason):
                if not condition: errors.append(f'{label}: {reason}')
            require(soup.html and soup.html.get('lang') == lang, 'incorrect language')
            require(soup.title and soup.title.get_text(strip=True), 'missing title')
            description = soup.find('meta', attrs={'name': 'description'})
            require(description and description.get('content'), 'missing description')
            canonical = soup.find('link', rel='canonical')
            require(canonical and canonical.get('href') == url, 'incorrect canonical URL')
            robots = soup.find('meta', attrs={'name': 'robots'})
            require(robots and 'noindex' not in robots.get('content', ''), 'not indexable')
            alternates = {node.get('hreflang'): node.get('href') for node in soup.find_all('link', rel='alternate')}
            ru = base + '/' + (name if name != 'index.html' else '')
            en = base + '/en/' + (name if name != 'index.html' else '')
            require(alternates == {'ru': ru, 'en': en, 'x-default': ru}, 'incorrect language alternatives')
            types = set()
            for block in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(block.string or block.get_text())
                    for node in data.get('@graph', [data]):
                        value = node.get('@type', '')
                        types.update(value if isinstance(value, list) else [value])
                except (ValueError, TypeError, AttributeError): errors.append(f'{label}: invalid JSON-LD')
            require({'Person', 'ProfilePage', 'WebSite'} <= types if name == 'index.html' else bool(types & {'WebPage', 'ProfilePage', 'CollectionPage'}), 'missing structured metadata')
            for node in soup.select('script[src], link[rel=stylesheet], img[src]'):
                ref = node.get('src') or node.get('href')
                parsed = urlsplit(ref)
                if parsed.scheme or parsed.netloc or not parsed.path: continue
                local = ROOT / unquote(parsed.path).lstrip('/') if parsed.path.startswith('/') else path.parent / unquote(parsed.path)
                require(local.is_file(), f'missing local asset: {ref}')
    actual = {node.text for node in ET.parse(ROOT / 'sitemap.xml').findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')}
    if actual != expected: errors.append('Sitemap must contain the 18 canonical RU/EN pages')
    if f'Sitemap: {base}/sitemap.xml' not in (ROOT / 'robots.txt').read_text(encoding='utf-8'): errors.append('robots.txt must reference the canonical sitemap')
    for name in ('admin.html', 'en/admin.html', '404.html', 'en/404.html'):
        path = ROOT / name
        if path.exists():
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
            meta = soup.find('meta', attrs={'name': 'robots'})
            if not meta or 'noindex' not in meta.get('content', ''): errors.append(f'{name}: utility pages must be noindex')
    for path in (ROOT / 'data').rglob('*.json'):
        try: json.loads(path.read_text(encoding='utf-8'))
        except ValueError as exc: errors.append(f'{path.relative_to(ROOT)}: invalid JSON ({exc})')
    if errors:
        print('\n'.join(errors)); return 1
    print('SEO and public JSON checks passed (18 bilingual pages).'); return 0

if __name__ == '__main__':
    raise SystemExit(main())
