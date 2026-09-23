#!/usr/bin/env python3
"""Incremental media discovery. Existing public records and review queues are retained.

One entrypoint replaces the former destructive harvest/enhance/seed chain. Network
failures are recorded and retried on later runs; never turn them into empty data.
"""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import requests
import yaml

from media_postprocess import mirror_image, parse_date_value, is_blocked_record, usable_image_url
from media_translation import MediaTranslator, update_translation_state

OUT = Path('data/media')
QUEUE = Path('data/admin_queue')
CONFIG = Path(os.environ.get('MEDIA_SOURCES_YAML', 'config/media_sources.yml'))
FETCH_DEADLINE = None
HOST_FAILURES = {}
HOST_FAILURE_LIMIT = 3
TRACKING = re.compile(r'^(utm_|fbclid$|gclid$|yclid$)', re.I)
STATIC = re.compile(r'\.(?:css|js|png|jpe?g|gif|svg|webp|pdf|docx?|xlsx?|zip)(?:$|\?)', re.I)
SURNAME = r'(?:байгутлин(?:а|у|ым|е)?|baigutlin|baygutlin)'
GIVEN = r'(?:дани{1,2}л(?:а|у|ом|е)?|dani{1,2}l)'
PATRONYMIC = r'(?:расулович(?:а|у|ем|е)?|rasulovich)'
INITIALS = r'[дd]\.\s*[рr]\.'
NAME = re.compile(r'\b(?:' + SURNAME + r'\s+(?:' + GIVEN + r'\b|' + INITIALS + r')|' + GIVEN + r'(?:\s+' + PATRONYMIC + r'|\s+r\.)?\s+' + SURNAME + r'\b|' + INITIALS + r'\s*' + SURNAME + r'\b)', re.I)
SURNAME_ONLY = re.compile(r'\b' + SURNAME + r'\b', re.I)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def date_day(value):
    parsed = parse_date_value(value)
    try:
        return datetime.fromisoformat(parsed[:10]).date().isoformat() if parsed else None
    except (TypeError, ValueError):
        return None  # An unknown/malformed date cannot justify dropping a URL.


def discovery_cutoff(cfg, state):
    initial = (datetime.now(timezone.utc) - timedelta(days=int(cfg.get('initial_lookback_days', 90)))).date().isoformat()
    return state.setdefault('initial_cutoff', initial)


def candidate_priority(candidate, cfg):
    hint = clean(candidate.get('title')) + ' ' + unquote(candidate['url']).replace('-', ' ').replace('_', ' ')
    date = date_day(candidate.get('published_at')) or date_day(candidate.get('sitemap_lastmod')) or ''
    return (candidate.get('source') == 'institutional_news_listing',
            score_record('', hint, candidate['url'], cfg), bool(date), date,
            not bool(candidate.get('last_attempt_at')))


def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(str(value or ''))).strip()


def canonical(url):
    p = urlparse(str(url or '').strip())
    host = p.netloc.lower().removeprefix('www.')
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.search(k)))
    return urlunparse(('https', host, unquote(p.path).rstrip('/'), '', query, '')) if host else ''


def read_json(path, default):
    if not path.exists():
        return default
    # Corrupt committed data must fail the run, not silently become an empty file.
    return json.loads(path.read_text(encoding='utf-8'))


def payload_records(payload):
    """Accept the original site's arrays as well as the pipeline envelope."""
    records = payload if isinstance(payload, list) else payload.get('records', [])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError('invalid_media_records')
    return records


def normalize_legacy_record(original):
    """Add pipeline aliases without changing reviewed fields or stable IDs."""
    record = dict(original)
    aliases = {'title': ('title_ru', 'title_en'),
               'description': ('description_ru', 'description_en'),
               'source_name': ('source_name_ru', 'source_name_en'),
               'published_at': ('date',)}
    for field, options in aliases.items():
        if not record.get(field):
            value = next((record[key] for key in options if record.get(key)), None)
            if value:
                record[field] = value
    return record


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + '.tmp')
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    staged.replace(path)


def fetch_text(url, accept='text/html,application/xml,text/xml,*/*', attempts=3):
    report = {'url': url, 'status': 'error'}
    host = urlparse(url).netloc.lower().removeprefix('www.')
    for attempt in range(attempts):
        if HOST_FAILURES.get(host, 0) >= HOST_FAILURE_LIMIT:
            return None, {**report, 'status': 'circuit_open', 'reason': 'repeated_host_failures'}
        remaining = FETCH_DEADLINE - time.monotonic() if FETCH_DEADLINE else 60
        if remaining <= 2:
            return None, {**report, 'status': 'budget_exhausted'}
        try:
            report['attempts'] = attempt + 1
            response = requests.get(url, timeout=(min(10, remaining / 2), min(25, remaining / 2)), headers={
                'User-Agent': 'Mozilla/5.0 personal-website-media-monitor/2.0',
                'Accept': accept, 'Accept-Language': 'en-US,en;q=0.8' if urlparse(url).netloc == 'news.google.com' else 'ru,en;q=0.8'}, stream=True)
            report.update(http_status=response.status_code, final_url=response.url)
            if response.status_code != 200:
                response.close()
                report['status'] = 'http_error'
                if response.status_code in (401, 403, 429, 500, 502, 503, 504):
                    HOST_FAILURES[host] = HOST_FAILURES.get(host, 0) + 1
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            else:
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    if FETCH_DEADLINE and time.monotonic() >= FETCH_DEADLINE:
                        response.close()
                        return None, {**report, 'status': 'budget_exhausted'}
                    size += len(chunk)
                    if size > 6_000_000:
                        response.close()
                        return None, {**report, 'status': 'too_large'}
                    chunks.append(chunk)
                encoding = response.encoding
                if not encoding or encoding.lower() == 'iso-8859-1':
                    encoding = 'utf-8'
                text = b''.join(chunks).decode(encoding, errors='replace')
                response.close()
                HOST_FAILURES.pop(host, None)
                return text, {**report, 'status': 'ok', 'bytes': size}
        except requests.RequestException as exc:
            HOST_FAILURES[host] = HOST_FAILURES.get(host, 0) + 1
            report['reason'] = type(exc).__name__  # Do not store response bodies or headers.
        if attempt < attempts - 1:
            remaining = FETCH_DEADLINE - time.monotonic() if FETCH_DEADLINE else 60
            time.sleep(min(2 ** attempt, max(0, remaining - 2)))
    return None, report


def source_slices(sources):
    """Reserve time for each configured source inside its channel's allocation."""
    global FETCH_DEADLINE
    sources = list(sources)
    channel_deadline = FETCH_DEADLINE
    try:
        for index, source in enumerate(sources):
            if channel_deadline is not None:
                started = time.monotonic()
                FETCH_DEADLINE = started + max(0, channel_deadline - started) / (len(sources) - index)
            yield source
    finally:
        FETCH_DEADLINE = channel_deadline


def score_record(text, title, url, cfg, force_publish=False):
    if force_publish:
        return 1.0
    # Identity and context must occur in the article, never in the navigation.
    body = clean(title + ' ' + text).lower().replace('ё', 'е')
    terms = [term.lower().replace('ё', 'е') for term in cfg.get('context_terms', ['физик', 'челгу', 'материаловед', 'heusler'])]
    context = any(re.search(r'\b' + re.escape(term) + r'\b', body) if len(term) <= 4 else term in body for term in terms)
    if NAME.search(body):
        return 0.95 if context else 0.6
    return 0.45 if SURNAME_ONLY.search(body) and context else 0.2 if SURNAME_ONLY.search(body) else 0.0


def article_meta(raw, url):
    soup = BeautifulSoup(raw, 'html.parser')
    for tag in soup.select('script, style, noscript, nav, footer, aside, .sidebar, .menu'):
        tag.decompose()
    host = urlparse(url).netloc.lower().removeprefix('www.')
    if host == 'csu.ru':
        # CSU uses <article> for unrelated recommendation cards, not the story.
        # Keep this scope strict: a changed template must remain pending.
        body = soup.select_one('.post .text-content')
        heading = soup.select_one('.post .post-head__title')
        date_node = soup.select_one('.post .post-head__date')
        if not body or not heading:
            return None
    else:
        # Prefer a scoped article over an outer layout containing other news.
        body = next((node for selector in ('article', '.news-detail__content', '.news-detail',
                     '.page-detail-content', '.page-detail', '.article-body', '.entry-content',
                     '.body-text', 'main', '.content') if (node := soup.select_one(selector))), None)
        heading = (body.select_one('h1, h2') if body else None) or soup.select_one('h1')
        date_node = soup.select_one('time, [itemprop="datePublished"], .news-detail__date, .news-date-time, .page-detail-nib__item--date')
    date = parse_date_value(date_node.get('datetime') or date_node.get('content') or date_node.get_text(' ')) if date_node else None
    if not body:
        return None  # A changed template cannot become an automatic false positive.
    def meta(name):
        tag = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
        return clean(tag.get('content')) if tag else ''
    title = clean(heading.get_text(' ')) if heading else meta('og:title') or clean(soup.title.get_text(' ') if soup.title else '')
    text = clean(body.get_text(' '))
    if len(text) < 60 or not title:
        return None
    paragraphs = [clean(p.get_text(' ')) for p in body.select('p') if len(clean(p.get_text(' '))) >= 50]
    description = next((p for p in paragraphs if SURNAME_ONLY.search(p)), paragraphs[0] if paragraphs else text)
    if len(description) > 360:
        description = description[:357].rsplit(' ', 1)[0] + '…'
    image = meta('og:image') or meta('twitter:image')
    if not usable_image_url(image):
        image = next((urljoin(url, im.get('src') or im.get('data-src')) for im in body.select('img') if usable_image_url(im.get('src') or im.get('data-src'))), None)
    return {'title': title, 'description': description, 'text': text,
            'published_at': date or parse_date_value(meta('article:published_time')),
            'image': urljoin(url, image) if image else None}


def parse_listing(raw, source, cutoff):
    soup = BeautifulSoup(raw, 'html.parser')
    selector = source['link_selector']
    anchors = soup.select(selector)
    if not anchors:
        raise ValueError('listing_template_changed')
    items, seen = [], set()
    allowed = re.compile(source.get('url_allow_regex', '.'))
    for anchor in anchors:
        container = anchor.find_parent(class_=source['item_class']) if source.get('item_class') else anchor.parent
        date_node = container.select_one(source['date_selector']) if container else None
        date = parse_date_value(date_node.get_text(' ')) if date_node else None
        if date and date[:10] < cutoff:
            continue
        href = anchor.get('href', '').strip()
        if not href:
            continue
        url = urljoin(source['url'], href)
        key = canonical(url)
        if key == canonical(source['url']) or key in seen or not allowed.search(url) or STATIC.search(url):
            continue
        if canonical(url) and urlparse(url).netloc == urlparse(source['url']).netloc:
            seen.add(key)
            items.append({'url': url, 'title': clean(anchor.get_text(' ')), 'published_at': date,
                          'source': 'institutional_news_listing', 'source_name': source['name'],
                          'source_name_en': source.get('name_en')})
    return items


def discover_listings(cfg, reports, state):
    items = []
    cutoff = discovery_cutoff(cfg, state)
    for source in source_slices(cfg.get('listing_sources', [])):
        raw, report = fetch_text(source['url'])
        if raw:
            try:
                found = parse_listing(raw, source, cutoff)
                items.extend(found)
                report['record_count'] = len(found)
            except ValueError as exc:
                report.update(status='parse_error', reason=str(exc))
        reports.append(report)
    return items


def unwrap_google_news_link(link):
    qs = parse_qs(urlparse(link).query)
    for key in ('url', 'u'):
        if qs.get(key):
            return qs[key][0]
    # Older RSS URLs contain the original URL as a protobuf string.
    try:
        token = urlparse(link).path.rsplit('/', 1)[-1]
        decoded = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
        match = re.search(rb'https?://[^\x00-\x20\x7f-\xff]+', decoded)
        if match:
            return match.group().decode('utf-8')
    except (ValueError, UnicodeError):
        pass
    return link


def resolve_original(link):
    candidate = unwrap_google_news_link(link)
    if urlparse(candidate).netloc != 'news.google.com':
        return candidate, None
    raw, report = fetch_text(candidate)
    final = report.get('final_url', '')
    if final and not urlparse(final).netloc.endswith(('google.com', 'googleusercontent.com', 'gstatic.com')):
        return final, report
    if urlparse(final).netloc == 'consent.google.com':
        return None, {'url': link, 'status': 'consent_required'}
    if raw:
        soup = BeautifulSoup(raw, 'html.parser')
        nodes = soup.select('a[rel="nofollow"], link[rel="canonical"], a[data-n-au]')
        for node in nodes:
            url = node.get('data-n-au') or node.get('href') or ''
            host = urlparse(url).netloc
            if url.startswith(('https://', 'http://')) and host and not host.endswith(('google.com', 'googleusercontent.com', 'gstatic.com')):
                return url, report
        # Current Google News wrappers expose a signed public article lookup.
        # Protocol reference: SSujitX/google-news-url-decoder new_decoderv1.py.
        attributes = soup.select_one('[data-n-a-sg][data-n-a-ts]')
        if attributes:
            try:
                context = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                            None, None, None, None, None, 0, 1],
                           "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0]
                request = ['garturlreq', context, urlparse(link).path.rsplit('/', 1)[-1],
                           int(attributes['data-n-a-ts']), attributes['data-n-a-sg']]
                envelope = [[['Fbv4je', json.dumps(request)]]]
                response = requests.post('https://news.google.com/_/DotsSplashUi/data/batchexecute',
                                         data={'f.req': json.dumps(envelope)}, timeout=(10, 25))
                response.raise_for_status()
                for line in response.text.splitlines():
                    if not line.startswith('[['):
                        continue
                    for row in json.loads(line):
                        if len(row) > 2 and row[0] == 'wrb.fr' and row[1] == 'Fbv4je':
                            decoded = json.loads(row[2])
                            original = decoded[1]
                            if isinstance(original, str) and original.startswith(('https://', 'http://')) and not urlparse(original).netloc.endswith(('google.com', 'googleusercontent.com', 'gstatic.com')):
                                return original, {**report, 'status': 'ok', 'resolution': 'public_article_lookup'}
            except (requests.RequestException, ValueError, TypeError, IndexError, KeyError):
                pass  # Persist the unresolved candidate and retry next run.
    return None, {**report, 'status': 'unresolved_original'}


def discover_rss(cfg, reports, state):
    items = []
    feeds = [(query, lang, country) for query in cfg.get('queries', []) for lang, country in [('ru', 'RU'), ('en', 'US')]]
    offset = int(state.get('rss_cursor', {}).get('next_index', 0)) % max(1, len(feeds))
    for index in list(range(offset, len(feeds))) + list(range(offset)):
        query, lang, country = feeds[index]
        url = 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': lang, 'gl': country, 'ceid': country + ':' + lang})
        raw, report = fetch_text(url)
        # A slow but reachable feed must not monopolize the first slot forever.
        # Skips do not count as attempts; the next run resumes at the next query.
        if report.get('attempts') or report.get('status') not in ('budget_exhausted', 'circuit_open'):
            state['rss_cursor'] = {'next_index': (index + 1) % len(feeds), 'attempted_at': now()}
        if raw:
            try:
                root = ET.fromstring(raw)
                for node in root.findall('.//item'):
                    items.append({'url': node.findtext('link'), 'title': clean(node.findtext('title')),
                                  'published_at': parse_date_value(node.findtext('pubDate')),
                                  'source': 'google_news_rss', 'source_name': node.findtext('source'), 'query': query})
            except ET.ParseError:
                report.update(status='parse_error', reason='invalid_rss')
        reports.append(report)
    return items


def fetch_sitemap_urls(url, limit, allow_regex=None, max_sitemaps=12, state=None, include_metadata=False):
    backlog = state.setdefault('sitemap_pending', {}) if state is not None else {}
    cutoff = (state or {}).get('initial_cutoff')
    queue, visited, urls, reports = list(dict.fromkeys(backlog.get(url, []) + [url])), set(), [], []
    failed = []
    rx = re.compile(allow_regex) if allow_regex else None
    while queue and len(visited) < max_sitemaps:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        raw, report = fetch_text(current)
        reports.append(report)
        if not raw:
            failed.append(current)
            continue
        try:
            root = ET.fromstring(raw)
            kind = root.tag.rsplit('}', 1)[-1]
            if kind not in ('sitemapindex', 'urlset'):
                raise ET.ParseError()
            for entry in root:
                loc = next((clean(n.text) for n in entry if n.tag.rsplit('}', 1)[-1] == 'loc'), '')
                if not loc:
                    continue
                if kind == 'sitemapindex':
                    if urlparse(loc).netloc == urlparse(url).netloc:
                        queue.append(loc)
                elif (not rx or rx.search(loc)) and not STATIC.search(loc):
                    modified = next((clean(n.text) for n in entry if n.tag.rsplit('}', 1)[-1] == 'lastmod'), '')
                    day = date_day(modified)
                    if cutoff and day and day < cutoff:
                        report['excluded_before_cutoff'] = report.get('excluded_before_cutoff', 0) + 1
                        continue
                    urls.append((day or '', loc))
        except ET.ParseError:
            report.update(status='parse_error', reason='invalid_sitemap')
    # Keep the entire bounded discovery result in the durable backlog. limit is
    # an article processing budget, never a truncation of already found links.
    if queue:
        reports.append({'url': url, 'status': 'partial', 'reason': 'sitemap_budget'})
    backlog[url] = list(dict.fromkeys(queue + failed))
    unique = {}
    for modified, loc in sorted(urls, reverse=True):
        unique.setdefault(loc, {'url': loc, 'sitemap_lastmod': modified or None})
    return (list(unique.values()) if include_metadata else list(unique)), reports


def discover_sitemaps(cfg, reports, state):
    items = []
    discovery_cutoff(cfg, state)
    for source in source_slices(cfg.get('sitemap_sources', [])):
        urls, source_reports = fetch_sitemap_urls(source['sitemap_url'], int(source.get('max_urls', 100)), source.get('url_allow_regex'), state=state, include_metadata=True)
        reports.extend(source_reports)
        items.extend({**item, 'source': 'sitemap_scan', 'source_name': source['name'],
                      'source_name_en': source.get('name_en')} for item in urls)
    return items


def discover_sites(cfg, reports, state):
    # Generic institutional scan remains bounded, with article extraction required.
    items = []
    for source in source_slices(cfg.get('site_scan_sources', [])):
        allowed = re.compile(source.get('url_allow_regex', '.'))
        pending = [(url, 0) for url in source.get('start_urls', [])]
        visited = set()
        while pending and len(visited) < int(source.get('max_pages', 12)):
            url, depth = pending.pop(0)
            if canonical(url) in visited:
                continue
            visited.add(canonical(url))
            raw, report = fetch_text(url)
            reports.append(report)
            if not raw:
                continue
            soup = BeautifulSoup(raw, 'html.parser')
            for anchor in soup.select('a[href]'):
                link = urljoin(url, anchor['href'])
                if urlparse(link).netloc != urlparse(url).netloc or not allowed.search(link) or STATIC.search(link):
                    continue
                if is_blocked_record({'url': link}):
                    continue
                items.append({'url': link, 'source': 'institutional_site_scan', 'source_name': source['name'],
                              'source_name_en': source.get('name_en')})
                if depth < int(source.get('max_depth', 0)):
                    pending.append((link, depth + 1))
    return items


def discover_telegram(cfg, reports, state):
    items = []
    for source in source_slices(cfg.get('telegram_channels', [])):
        url = 'https://t.me/s/' + source['channel']
        raw, report = fetch_text(url)
        reports.append(report)
        if not raw:
            continue
        soup = BeautifulSoup(raw, 'html.parser')
        for post in soup.select('.tgme_widget_message')[-int(source.get('max_latest_posts', 50)):]:
            body = post.select_one('.tgme_widget_message_text')
            if not body or not post.get('data-post'):
                continue
            text = clean(body.get_text(' '))
            date = post.select_one('time')
            items.append({'url': 'https://t.me/' + post['data-post'], 'source': 'telegram_channel_scan',
                          'source_name': source['channel'], 'title': text[:110],
                          'published_at': date.get('datetime') if date else None,
                          '_meta': {'title': text[:110], 'description': text[:360], 'text': text}})
    return items


def build_record(candidate, meta, cfg):
    url = candidate['url']
    confidence = score_record(meta.get('text', ''), meta.get('title', ''), url, cfg, candidate.get('force_publish', False))
    rec = {k: v for k, v in candidate.items() if not k.startswith('_')}
    rec.update({k: v for k, v in meta.items() if k != 'text' and v})
    rec.update(id=candidate.get('id') or hashlib.sha256(canonical(url).encode()).hexdigest()[:16],
               confidence=confidence, status='published' if confidence >= float(cfg.get('auto_publish_threshold', .75)) else 'low_confidence',
               domain=urlparse(url).netloc.removeprefix('www.'), harvested_at=now())
    for field in ('title', 'description'):
        value = rec.get(field, '')
        lang = 'ru' if re.search('[А-Яа-яЁё]', value) else 'en'
        if value:
            rec.setdefault(field + '_' + lang, value)
    rec['language'] = 'ru' if re.search('[А-Яа-яЁё]', rec.get('title', '')) else 'en'
    return rec


def merge_records(existing, incoming):
    # Existing IDs, URLs, manual text/translations and cached images win. New
    # metadata only fills gaps; reviewed edits are made explicitly in the data.
    merged = {canonical(r.get('url')) or r['id']: dict(r) for r in existing}
    ids = {r.get('id'): k for k, r in merged.items() if r.get('id')}
    for record in incoming:
        key = ids.get(record.get('id')) or canonical(record.get('url')) or record.get('id')
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(record)
        else:
            for field, value in record.items():
                if value is not None and value != '' and merged[key].get(field) in (None, ''):
                    merged[key][field] = value
        if merged[key].get('id'):
            ids[merged[key]['id']] = key
        if merged[key].get('translation_state'):
            # A concurrent merge may fill the last missing translation. This
            # operational state must follow the merged fields, not old metadata.
            update_translation_state(merged[key])
    return sorted(merged.values(), key=lambda r: (r.get('published_at') or '', r.get('title') or ''), reverse=True)


def merge_discovery_state(existing, incoming):
    """Reconcile an Actions candidate with checkpoints newly committed to main.

    Successful processing wins over pending attempts. Keep every remaining
    candidate and sitemap continuation; repeated discovery is harmless and avoids
    losing work when the snapshots were collected concurrently.
    """
    result = {'processed': {}, 'pending': {}, 'sitemap_pending': {}}
    for state in (existing or {}, incoming or {}):
        cursor = state.get('rss_cursor')
        if cursor and cursor.get('attempted_at', '') >= result.get('rss_cursor', {}).get('attempted_at', ''):
            result['rss_cursor'] = dict(cursor)
        cutoff = state.get('initial_cutoff')
        if cutoff:
            result['initial_cutoff'] = min(cutoff, result.get('initial_cutoff', cutoff))
        for key, value in state.get('processed', {}).items():
            old = result['processed'].get(key, {})
            if value.get('processed_at', '') >= old.get('processed_at', ''):
                result['processed'][key] = dict(value)
        for key, value in state.get('pending', {}).items():
            old = result['pending'].get(key, {})
            if value.get('last_attempt_at', '') >= old.get('last_attempt_at', ''):
                result['pending'][key] = {**old, **value}
            else:
                result['pending'][key] = {**value, **old}
        for source, urls in state.get('sitemap_pending', {}).items():
            result['sitemap_pending'][source] = list(dict.fromkeys(result['sitemap_pending'].get(source, []) + urls))
    for key in result['processed']:
        result['pending'].pop(key, None)
    return result


def seed_records(cfg):
    corpus = payload_records(read_json(OUT / 'known_mentions_corpus.json', {}))
    corpus += payload_records(read_json(OUT / 'seed.json', []))
    records = []
    for seed in list(cfg.get('seed_urls', [])) + corpus:
        if not seed.get('url') or not seed.get('force_publish', True):
            continue
        fields = normalize_legacy_record(seed)
        fields.update(source=seed.get('source_type', 'known_media_seed'), force_publish=True,
                      seed_metadata_locked=True, published_at=seed.get('date') or seed.get('published_at'),
                      source_name=fields.get('source_name') or urlparse(seed['url']).netloc.removeprefix('www.'))
        fields['description'] = fields.get('description') or seed.get('context') or ''
        if not is_blocked_record(fields):
            records.append(build_record(fields, {}, cfg))
    return records


def run(cfg, providers=None, max_articles=None, seeds_only=False, mirror=True, translate=True):
    global FETCH_DEADLINE
    started = time.monotonic()
    runtime_budget = int(cfg.get('max_runtime_seconds', 600))
    discovery_deadline = started + min(180, runtime_budget / 3)
    HOST_FAILURES.clear()  # A later weekly run must retry an unavailable host.
    OUT.mkdir(parents=True, exist_ok=True)
    current_payload = read_json(OUT / 'published.json', [])
    current = payload_records(current_payload)
    if isinstance(current_payload, list):
        current = [normalize_legacy_record(record) for record in current]
    old_report = read_json(OUT / 'harvest_report.json', {})
    state = read_json(OUT / 'discovery_state.json', {'processed': {}, 'pending': {}})
    cutoff = discovery_cutoff(cfg, state)
    processed, pending = state.setdefault('processed', {}), state.setdefault('pending', {})
    queue_payload = read_json(QUEUE / 'media_mentions.json', [])
    queue = payload_records(queue_payload)
    rejected = payload_records(read_json(OUT / 'rejected_or_low_confidence.json', []))
    queue = merge_records(queue, rejected)
    reports, incoming = [], seed_records(cfg)
    existing_urls = {canonical(r['url']) for r in current}
    funcs = {'listings': discover_listings, 'rss': discover_rss, 'sitemaps': discover_sitemaps,
             'sites': discover_sites, 'telegram': discover_telegram}
    selected = [] if seeds_only else providers or list(funcs)
    selected = list(dict.fromkeys(selected))
    if 'listings' in selected:
        selected.remove('listings')
        selected.insert(0, 'listings')
    # Required listings receive double weight. Every subsequent channel keeps a
    # reserved share even if Google or another earlier channel times out.
    weights = {'listings': 2}
    discovery_budgets = []
    for index, provider in enumerate(selected):
        channel_started = time.monotonic()
        weight = weights.get(provider, 1)
        allocation = max(0, discovery_deadline - channel_started) * weight / sum(weights.get(p, 1) for p in selected[index:])
        FETCH_DEADLINE = channel_started + allocation
        first_report = len(reports)
        try:
            for candidate in funcs[provider](cfg, reports, state):
                key = canonical(candidate.get('url'))
                if key and key not in processed and key not in existing_urls:
                    prior = pending.get(key, {})
                    if prior.get('source') == 'institutional_news_listing' and candidate.get('source') != prior['source']:
                        candidate = {**candidate, **{name: prior[name] for name in ('source', 'source_name', 'source_name_en', 'published_at', 'title') if prior.get(name)}}
                    pending[key] = {**pending.get(key, {}), **candidate}
        except Exception as exc:
            reports.append({'collector': provider, 'status': 'error', 'reason': type(exc).__name__})
        for report in reports[first_report:]:
            report.update(collector=provider, required=provider == 'listings')
        discovery_budgets.append({'collector': provider, 'budget_seconds': round(allocation, 2),
                                  'elapsed_seconds': round(time.monotonic() - channel_started, 2)})
    FETCH_DEADLINE = started + runtime_budget
    listing_urls = {canonical(source['url']) for source in cfg.get('listing_sources', [])}
    for key in list(pending):
        if key in listing_urls or not pending[key].get('url'):
            pending.pop(key)
    limit = max_articles if max_articles is not None else int(cfg.get('max_articles_per_run', 60))
    candidates = sorted(pending.items(), key=lambda kv: candidate_priority(kv[1], cfg), reverse=True)
    for key, candidate in candidates[:0 if seeds_only else limit]:
        url = candidate['url']
        if urlparse(url).netloc == 'news.google.com':
            original, report = resolve_original(url)
            if report:
                reports.append(report)
            if not original:
                candidate.update(last_attempt_at=now(), reason='unresolved_original')
                continue
            candidate['discovered_url'] = url
            candidate['url'] = url = original
        meta = candidate.pop('_meta', None)
        if not meta:
            raw, report = fetch_text(url)
            report.update(collector=candidate.get('source'), required=candidate.get('source') == 'institutional_news_listing')
            reports.append(report)
            if not raw:
                candidate.update(last_attempt_at=now(), reason=report['status'])
                continue
            meta = article_meta(raw, url)
            if not meta:
                candidate.update(last_attempt_at=now(), reason='article_template_changed')
                reports.append({'url': url, 'status': 'parse_error', 'reason': 'article_template_changed', 'required': candidate.get('source') == 'institutional_news_listing'})
                continue
        published_day = date_day(meta.get('published_at') or candidate.get('published_at'))
        if published_day and published_day < cutoff and not candidate.get('force_publish'):
            processed[key] = {'processed_at': now(), 'status': 'outside_lookback', 'published_at': published_day}
            pending.pop(key, None)
            continue
        record = build_record(candidate, meta, cfg)
        if record['status'] == 'published' and not is_blocked_record(record):
            incoming.append(record)
        elif record['confidence'] > 0:
            queue = merge_records(queue, [record])
        processed[key] = {'processed_at': now(), 'status': record['status'], 'confidence': record['confidence']}
        pending.pop(key, None)
    published = merge_records(current, incoming)
    translator = MediaTranslator(OUT / 'translation_cache.json') if translate else None
    post_reports = []
    for record in published:
        if translator:
            translator.enrich(record)
        # Never re-fetch/rewrite a previously cached image merely to touch its timestamp.
        image = record.get('image')
        if mirror and canonical(record['url']) not in existing_urls and image and not (image.startswith('assets/') and Path(image).exists()):
            post_reports.append({'id': record['id'], 'image': mirror_image(record, image, deadline=FETCH_DEADLINE)['status']})
    if translator:
        translator.save()
    published_urls = {canonical(r['url']) for r in published}
    queue = [r for r in queue if canonical(r['url']) not in published_urls]
    errors = [r for r in reports if r.get('status') != 'ok']
    stamp = now()
    complete = not errors and not pending and not seeds_only
    required_ok = 'listings' in selected and not any(r.get('required') for r in errors)
    report = {'status': 'success' if complete else 'partial' if selected else 'not_attempted',
              'attempted_at': stamp, 'last_success_at': stamp if required_ok else old_report.get('last_success_at'),
              'origin': 'live' if selected else 'snapshot', 'complete': complete,
              'record_count': len(published), 'reason': 'reviewed_seed_only' if seeds_only else 'source_failures_or_pending' if errors or pending else None,
              'required_sources_ok': required_ok,
              'published': len(published), 'new_records': len(published) - len(current),
              'low_confidence': len(queue), 'pending': len(pending), 'providers': reports,
              'discovery_budgets': discovery_budgets,
              'translation_pending': sum((r.get('translation_state') or {}).get('status') == 'pending' for r in published),
              'images': post_reports, 'translation': translator.status if translator else 'disabled'}
    # Assert identity-level retention before promoting any public file.
    for old in current:
        kept = next((r for r in published if r.get('id') == old.get('id') and r.get('url') == old.get('url')), None)
        if kept is None or any(kept.get(k) != v for k, v in old.items()
                               if k != 'translation_state' and v is not None and v != ''):
            raise ValueError('media_retention_failed')
    payload = {'generated_at': stamp, 'records': published}
    for filename in ('published.json', 'news_mentions.json', 'published-fallback.json'):
        write_json(OUT / filename, payload)
    write_json(OUT / 'rejected_or_low_confidence.json', {'generated_at': stamp, 'records': queue})
    write_json(QUEUE / 'media_mentions.json', queue)
    with (QUEUE / 'media_mentions.csv').open('w', encoding='utf-8-sig', newline='') as target:
        columns = ['id', 'confidence', 'title', 'source_name', 'domain', 'published_at', 'url', 'query']
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(queue)
    write_json(OUT / 'discovery_state.json', state)
    write_json(OUT / 'harvest_report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers', nargs='+', choices=['listings', 'rss', 'sitemaps', 'sites', 'telegram'])
    parser.add_argument('--max-articles', type=int)
    parser.add_argument('--seeds-only', action='store_true')
    parser.add_argument('--no-images', action='store_true')
    parser.add_argument('--no-translate', action='store_true', help='Testing only; requires --output-dir')
    parser.add_argument('--output-dir', type=Path, help='Isolated output directory for a test run')
    args = parser.parse_args()
    if args.no_translate and not args.output_dir:
        parser.error('--no-translate is testing-only and requires an isolated --output-dir')
    if args.output_dir:
        global OUT, QUEUE
        base = args.output_dir.resolve()
        if base == Path('.').resolve() or base == Path('data').resolve() or Path('data').resolve() in base.parents:
            parser.error('--output-dir must be outside the published data directory')
        OUT, QUEUE = base / 'media', base / 'admin_queue'
        args.no_images = True  # Isolated tests must not mutate repository assets.
    cfg = yaml.safe_load(CONFIG.read_text(encoding='utf-8'))['media_monitoring']
    report = run(cfg, args.providers, args.max_articles, args.seeds_only, not args.no_images, not args.no_translate)
    print(json.dumps({k: report[k] for k in ('status', 'record_count', 'new_records', 'pending', 'low_confidence')}, ensure_ascii=False))
    # Fresh-source availability is checked after safe promotion by the shared
    # workflow health gate; transport problems are never disguised as fresh data.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
