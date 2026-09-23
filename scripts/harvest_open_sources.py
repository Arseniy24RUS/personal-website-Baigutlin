#!/usr/bin/env python3
"""Harvest open bibliographic sources for a static scientist portfolio.

Inputs:
  config/profile.yml with identifiers.orcid and optional display names.

Outputs:
  data/open/orcid_works.json
  data/open/openalex_author.json
  data/open/openalex_works.json
  data/open/crossref_works.json
  data/open/open_publications.json
  data/open/harvest_report.json

The script is deliberately best-effort: every source is saved independently and
one failing provider must not break the whole GitHub Pages refresh workflow.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
import json
import html
import copy
import os
import re
import time
import urllib.request
import urllib.error
from source_health import read_json, source_result, write_json

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ROOT = Path('.')
OUT = ROOT / 'data' / 'open'
OUT.mkdir(parents=True, exist_ok=True)
PROFILE = Path(os.environ.get('PROFILE_YAML', 'config/profile.yml'))
CONTACT = os.environ.get('OPENALEX_MAILTO', 'd0nik1996@mail.ru')


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_profile():
    if not PROFILE.exists() or yaml is None:
        return {}
    return yaml.safe_load(PROFILE.read_text(encoding='utf-8')) or {}


def get_json(url, headers=None, timeout=45):
    headers = headers or {}
    headers.setdefault('User-Agent', 'scientist-portfolio-harvester/0.2 (mailto:d0nik1996@mail.ru)')
    headers.setdefault('Accept', 'application/json')
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp), {'status': 'ok', 'http_status': resp.status}
        except urllib.error.HTTPError as exc:
            reason = {'status': 'error', 'http_status': exc.code, 'reason': f'http_{exc.code}'}
            if exc.code not in {429, 500, 502, 503, 504}:
                return None, reason
        except (OSError, ValueError):
            reason = {'status': 'error', 'reason': 'network_or_invalid_json'}
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None, reason


def save(path, payload):
    write_json(path, payload)


def normalize_title(s):
    text = html.unescape(str(s or ''))
    text = re.sub(r'<[^>]+>|-=/?SUB=-', '', text, flags=re.I)
    for command, symbol in [('alpha', 'α'), ('beta', 'β'), ('gamma', 'γ'), ('delta', 'δ')]:
        text = re.sub(r'\\+' + command + r'\b', symbol, text)
        text = re.sub(r'\$' + command + r'\$', symbol, text)
    text = re.sub(r'\\+(?:mathrm|textrm|text|mathbf|mathit)\s*', '', text)
    text = re.sub(r'[$_{}]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def work_title_key(value):
    """Equivalent math markup/chemical spacing must not create another work."""
    return re.sub(r'[^a-zа-яёα-ω0-9]', '', normalize_title(value).casefold())


def publication_type(record):
    kind = record.get('publication_type') or record.get('type') or ''
    doi = doi_norm(record.get('doi')) or ''
    venue = record.get('venue') or (record.get('journal-title') or {}).get('value') or (((record.get('primary_location') or {}).get('source') or {}).get('display_name')) or ''
    if doi.startswith('10.48550/arxiv.') or kind in {'preprint', 'posted-content'} or str(venue).strip().casefold() == 'arxiv':
        return 'preprint'
    return {'article': 'journal-article', 'proceedings-article': 'conference-paper'}.get(kind, kind) or None


def work_url_key(value):
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(str(value or ''))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.query, '')) if parsed.netloc else ''


def compatible_work_versions(first, second):
    """A title match cannot collapse two distinct DOI versions or a preprint."""
    a, b = doi_norm(first.get('doi')), doi_norm(second.get('doi'))
    if a and b and a != b:
        return False
    first_type, second_type = publication_type(first), publication_type(second)
    if (first_type == 'preprint') != (second_type == 'preprint'):
        return False
    return True


def same_open_work(first, second):
    a, b = doi_norm(first.get('doi')), doi_norm(second.get('doi'))
    if a and b:
        return a == b
    if not compatible_work_versions(first, second):
        return False
    urls_a = {work_url_key(first.get(key)) for key in ('url', 'landing_page_url')} - {''}
    urls_b = {work_url_key(second.get(key)) for key in ('url', 'landing_page_url')} - {''}
    if urls_a & urls_b:
        return True
    return bool(work_title_key(first.get('title'))) and (
        work_title_key(first.get('title')), str(first.get('year') or '')
    ) == (work_title_key(second.get('title')), str(second.get('year') or ''))


def doi_norm(doi):
    if not doi:
        return None
    doi = str(doi).strip()
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi, flags=re.I)
    return doi.lower() or None


def normalize_orcid_works(payload, orcid):
    out = []
    for group in (payload or {}).get('group', []) or []:
        summaries = group.get('work-summary') or []
        for w in summaries:
            title = (((w.get('title') or {}).get('title') or {}).get('value'))
            pdate = w.get('publication-date') or {}
            year = ((pdate.get('year') or {}).get('value'))
            ext = ((w.get('external-ids') or {}).get('external-id')) or []
            doi = None
            for e in ext:
                if (e.get('external-id-type') or '').lower() == 'doi':
                    doi = e.get('external-id-value')
                    break
            out.append({
                'source': 'orcid_public_api',
                'orcid': orcid,
                'title': normalize_title(title),
                'year': int(year) if str(year or '').isdigit() else None,
                'doi': doi_norm(doi),
                'type': w.get('type'),
                'publication_type': publication_type({'type': w.get('type'), 'doi': doi, 'journal-title': w.get('journal-title')}),
                'venue': (w.get('journal-title') or {}).get('value'),
                'url': (w.get('url') or {}).get('value'),
                'put_code': w.get('put-code'),
                'raw': w,
            })
    return out


def normalize_openalex_works(payload, author_id=None, orcid=None):
    out = []
    for w in (payload or {}).get('results', []) or []:
        authorships = w.get('authorships') or []
        if (author_id or orcid) and not any(
            (author_id and (item.get('author') or {}).get('id') == author_id)
            or (orcid and str((item.get('author') or {}).get('orcid') or '').rstrip('/').endswith('/' + orcid))
            for item in authorships
        ):
            continue
        loc = w.get('primary_location') or {}
        source = loc.get('source') or {}
        bibliography = w.get('biblio') or {}
        pages = '-'.join(str(bibliography[key]) for key in ('first_page', 'last_page') if bibliography.get(key))
        out.append({
            'source': 'openalex_api',
            'openalex_id': w.get('id'),
            'title': normalize_title(w.get('display_name')),
            'year': w.get('publication_year'),
            'doi': doi_norm(w.get('doi')),
            'authors_raw': ', '.join(normalize_title(item.get('raw_author_name') or (item.get('author') or {}).get('display_name')) for item in authorships if item.get('raw_author_name') or (item.get('author') or {}).get('display_name')),
            'type': w.get('type'),
            'publication_type': publication_type(w),
            'volume': bibliography.get('volume'), 'issue': bibliography.get('issue'), 'pages': pages or None,
            'url': w.get('id'),
            'landing_page_url': loc.get('landing_page_url'),
            'pdf_url': ((loc.get('pdf_url') or '') or None),
            'venue': source.get('display_name'),
            'cited_by_count': w.get('cited_by_count'),
            'is_oa': (w.get('open_access') or {}).get('is_oa'),
            'oa_status': (w.get('open_access') or {}).get('oa_status'),
            'raw': w,
        })
    return out


def normalize_crossref_works(payload):
    out = []
    for w in ((payload or {}).get('message') or {}).get('items', []) or []:
        title = (w.get('title') or [None])[0]
        pub = (w.get('container-title') or [None])[0]
        year = None
        for key in ['published-print', 'published-online', 'published', 'created']:
            parts = (((w.get(key) or {}).get('date-parts') or [[None]])[0])
            if parts and parts[0]:
                year = parts[0]
                break
        out.append({
            'source': 'crossref_api',
            'title': normalize_title(title),
            'year': year,
            'doi': doi_norm(w.get('DOI')),
            'url': w.get('URL'),
            'venue': pub,
            'type': w.get('type'),
            'publication_type': publication_type(w),
            'authors_raw': ', '.join(normalize_title(' '.join(filter(None, (author.get('given'), author.get('family')))) or author.get('name')) for author in w.get('author') or [] if author.get('given') or author.get('family') or author.get('name')),
            'volume': w.get('volume'), 'issue': w.get('issue'), 'pages': w.get('page'),
            'publisher': w.get('publisher'),
            'is_referenced_by_count': w.get('is-referenced-by-count'),
            'raw': w,
        })
    return out


def enrich_orcid_contributors(records, *, max_requests=12, budget_seconds=90):
    """Fill omitted summary contributors from bounded public work-detail reads."""
    cache_path = OUT / 'orcid_work_details.json'
    cache = read_json(cache_path, {}) or {}
    items = cache.setdefault('items', {})
    attempted = 0
    deadline = time.monotonic() + budget_seconds
    for record in records:
        if record.get('authors_raw'):
            continue
        provider = (record.get('provider_records') or {}).get('orcid_public_api') or record
        orcid, put_code = provider.get('orcid'), provider.get('put_code')
        if not re.fullmatch(r'\d{4}-\d{4}-\d{4}-[\dX]{4}', str(orcid or '')) or not str(put_code or '').isdigit():
            continue
        key = f'{orcid}/{put_code}'
        known = items.get(key) or {}
        if not known.get('authors_raw') and attempted < max_requests and time.monotonic() < deadline:
            attempted += 1
            payload, diagnostic = get_json(f'https://pub.orcid.org/v3.0/{orcid}/work/{put_code}',
                                           timeout=max(1, min(10, deadline - time.monotonic())))
            if isinstance(payload, dict):
                contributors = (payload.get('contributors') or {}).get('contributor') or []
                authors = [normalize_title((item.get('credit-name') or {}).get('value')) for item in contributors
                           if (item.get('credit-name') or {}).get('value')
                           and (item.get('contributor-attributes') or {}).get('contributor-role') in (None, 'author')]
                known = {'authors_raw': ', '.join(authors), 'observed_at': now(),
                         'status': 'success' if authors else 'contributors_not_provided'}
                items[key] = known
        if known.get('authors_raw'):
            record['authors_raw'] = known['authors_raw']
            record['authors_source'] = 'orcid_public_work_detail'
    cache.update(schema='orcid-work-contributors/v1')
    save(cache_path, cache)
    return records


def fetch_cursor_pages(base_url, params, provider):
    """Keep partial pages out of the last-good cache."""
    params = dict(params)
    if provider == 'crossref':
        # Since 2026-08-24 Crossref rejects cursor + published/issued sorting.
        # https://community.crossref.org/t/16246
        params.pop('sort', None)
        params.pop('order', None)
    cursor = '*'
    seen = set()
    collected = []
    record_ids = set()
    first = None
    while cursor and cursor not in seen:
        seen.add(cursor)
        payload, diagnostic = get_json(base_url + '?' + urlencode({**params, 'cursor': cursor}))
        if payload is None:
            return None, {**diagnostic, 'pages_completed': len(seen) - 1}
        block = payload if provider == 'openalex' else payload.get('message', {})
        key = 'results' if provider == 'openalex' else 'items'
        batch = block.get(key)
        if not isinstance(batch, list):
            return None, {'reason': 'unexpected_schema'}
        if first is None:
            first = payload
        identities = [row.get('id') if provider == 'openalex' else str(row.get('DOI', '')).lower() for row in batch]
        if any(identity and identity in record_ids for identity in identities):
            return None, {'reason': 'duplicate_pagination'}
        record_ids.update(identity for identity in identities if identity)
        collected.extend(batch)
        meta = payload.get('meta', {}) if provider == 'openalex' else block
        total = meta.get('count') if provider == 'openalex' else meta.get('total-results')
        next_cursor = meta.get('next_cursor') if provider == 'openalex' else meta.get('next-cursor')
        if total is not None and len(collected) >= int(total):
            break
        if not batch:
            if total is not None and len(collected) < int(total):
                return None, {'reason': 'incomplete_pagination'}
            break
        if not next_cursor or next_cursor in seen:
            return None, {'reason': 'incomplete_pagination'}
        cursor = next_cursor
    if first is None:
        return None, {'reason': 'empty_response'}
    if provider == 'openalex':
        first['results'] = collected
    else:
        collected.sort(key=lambda row: tuple(((row.get('published') or row.get('issued') or {}).get('date-parts') or [[]])[0]), reverse=True)
        first['message']['items'] = collected
    return first, {'status': 'ok', 'pages': len(seen)}


def dedupe_records(records):
    merged = []
    # Prefer an identified journal version when an untyped/DOI-less duplicate
    # also appears. A preprint's distinct DOI always remains a separate work.
    for original in sorted(records, key=lambda row: (publication_type(row) == 'preprint', not bool(row.get('doi')))):
        record = copy.deepcopy(original)
        previous = next((row for row in merged if same_open_work(row, record)), None)
        if previous is None:
            previous = {}
            merged.append(previous)
        sources = set(previous.get('sources') or []) | set(record.get('sources') or [])
        if record.get('source'):
            sources.add(record['source'])
        # Retain each provider's observations rather than discard later DOI matches.
        observations = dict(previous.get('provider_records') or {})
        for provider, observation in (record.get('provider_records') or {}).items():
            observations.setdefault(provider, {key: value for key, value in observation.items() if key != 'provider_records'})
        observations.setdefault(record['source'], {key: value for key, value in record.items() if key != 'provider_records'})
        for field, value in record.items():
            if previous.get(field) in (None, '', [], {}) and value not in (None, '', [], {}):
                previous[field] = value
        previous.update(sources=sorted(sources), provider_records=observations)
    return merged


def main():
    profile = read_profile()
    ids = ((profile.get('profile') or {}).get('identifiers') or {}) if profile else {}
    orcid = ids.get('orcid') or os.environ.get('ORCID_ID')
    old_report = read_json(OUT / 'harvest_report.json', {})
    report = {'generated_at': now(), 'providers': {}}
    all_records = []

    def store_provider(name, filename, payload, diagnostic, normalizer=None):
        previous = (old_report.get('providers') or {}).get(name, {})
        cached = read_json(OUT / filename)
        valid = payload is not None
        selected = payload if valid else cached
        rows = normalizer(selected) if normalizer and selected else []
        state = source_result(previous, status='success' if valid else 'error', count=len(rows), reason=None if valid else diagnostic.get('reason', 'fetch_failed'))
        if not valid and not state['last_success_at'] and cached:
            state['last_success_at'] = cached.get('last_success_at') or cached.get('generated_at')
        report['providers'][name] = {**state, 'http_status': diagnostic.get('http_status')}
        if valid:
            payload['last_success_at'] = state['last_success_at']
            save(OUT / filename, payload)
        all_records.extend(rows)
        return selected

    if orcid:
        # ORCID /works returns all public groups, not a paginated first page.
        payload, rep = get_json(f'https://pub.orcid.org/v3.0/{orcid}/works')
        if payload is not None and not isinstance(payload.get('group'), list):
            payload, rep = None, {'reason': 'unexpected_schema'}
        store_provider('orcid', 'orcid_works.json', payload, rep, lambda data: normalize_orcid_works(data, orcid))

        author_url = 'https://api.openalex.org/authors/' + quote('https://orcid.org/' + orcid, safe='') + '?' + urlencode({'mailto': CONTACT})
        author, rep = get_json(author_url)
        if author is not None and (not author.get('id') or (author.get('orcid') and not str(author['orcid']).rstrip('/').endswith('/' + orcid))):
            author, rep = None, {'reason': 'unexpected_schema'}
        author = store_provider('openalex_author', 'openalex_author.json', author, rep)
        filt = 'authorships.author.id:' + author['id'] if author and author.get('id') else 'authorships.author.orcid:' + orcid
        works, rep = fetch_cursor_pages('https://api.openalex.org/works', {'filter': filt, 'per-page': 200, 'sort': 'publication_date:desc', 'mailto': CONTACT}, 'openalex')
        store_provider('openalex_works', 'openalex_works.json', works, rep,
                       lambda payload: normalize_openalex_works(payload, (author or {}).get('id'), orcid))

        works, rep = fetch_cursor_pages('https://api.crossref.org/works', {'filter': 'orcid:' + orcid, 'rows': 1000, 'sort': 'published', 'order': 'desc', 'mailto': CONTACT}, 'crossref')
        store_provider('crossref', 'crossref_works.json', works, rep, normalize_crossref_works)
    else:
        report['reason'] = 'orcid_identifier_missing'

    # Preserve records even if a provider has withdrawn a DOI from its response.
    previous_records = (read_json(OUT / 'open_publications.json', {}) or {}).get('records', [])
    current = dedupe_records([*all_records, *previous_records])
    enrich_orcid_contributors(current)
    save(OUT / 'open_publications.json', {'generated_at': now(), 'records': current})
    complete = bool(report['providers']) and all(p['complete'] for p in report['providers'].values())
    report.update(source_result(old_report, status='success' if complete else 'partial', count=len(current), reason=None if complete else 'one_or_more_providers_unavailable'))
    report['records_total_before_dedupe'] = len(all_records)
    report['records_total_after_dedupe'] = len(current)
    save(OUT / 'harvest_report.json', report)
    print(json.dumps({'open_records': len(current), 'status': report['status']}, ensure_ascii=False))
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
