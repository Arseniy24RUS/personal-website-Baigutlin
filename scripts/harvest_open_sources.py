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
    return re.sub(r'\s+', ' ', (s or '').strip())


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
                'url': (w.get('url') or {}).get('value'),
                'put_code': w.get('put-code'),
                'raw': w,
            })
    return out


def normalize_openalex_works(payload):
    out = []
    for w in (payload or {}).get('results', []) or []:
        loc = w.get('primary_location') or {}
        source = loc.get('source') or {}
        out.append({
            'source': 'openalex_api',
            'openalex_id': w.get('id'),
            'title': normalize_title(w.get('display_name')),
            'year': w.get('publication_year'),
            'doi': doi_norm(w.get('doi')),
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
            'publisher': w.get('publisher'),
            'is_referenced_by_count': w.get('is-referenced-by-count'),
            'raw': w,
        })
    return out


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
    merged = {}
    for record in records:
        key = ('doi', doi_norm(record.get('doi'))) if record.get('doi') else ('title_year', normalize_title(record.get('title')).lower(), record.get('year'))
        previous = merged.get(key, {})
        sources = set(previous.get('sources') or []) | set(record.get('sources') or [])
        if record.get('source'):
            sources.add(record['source'])
        # Retain each provider's observations rather than discard later DOI matches.
        observations = dict(previous.get('provider_records') or {})
        observations[record['source']] = record
        merged[key] = {**record, **previous, 'sources': sorted(sources), 'provider_records': observations}
    return list(merged.values())


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
        if author is not None and not author.get('id'):
            author, rep = None, {'reason': 'unexpected_schema'}
        author = store_provider('openalex_author', 'openalex_author.json', author, rep)
        filt = 'authorships.author.id:' + author['id'] if author and author.get('id') else 'authorships.author.orcid:' + orcid
        works, rep = fetch_cursor_pages('https://api.openalex.org/works', {'filter': filt, 'per-page': 200, 'sort': 'publication_date:desc', 'mailto': CONTACT}, 'openalex')
        store_provider('openalex_works', 'openalex_works.json', works, rep, normalize_openalex_works)

        works, rep = fetch_cursor_pages('https://api.crossref.org/works', {'filter': 'orcid:' + orcid, 'rows': 1000, 'sort': 'published', 'order': 'desc', 'mailto': CONTACT}, 'crossref')
        store_provider('crossref', 'crossref_works.json', works, rep, normalize_crossref_works)
    else:
        report['reason'] = 'orcid_identifier_missing'

    # Preserve records even if a provider has withdrawn a DOI from its response.
    previous_records = (read_json(OUT / 'open_publications.json', {}) or {}).get('records', [])
    current = dedupe_records(all_records)
    identities = {(doi_norm(r.get('doi')) or (normalize_title(r.get('title')).lower(), r.get('year'))) for r in current}
    for row in previous_records:
        identity = doi_norm(row.get('doi')) or (normalize_title(row.get('title')).lower(), row.get('year'))
        if identity not in identities:
            current.append(row)
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
