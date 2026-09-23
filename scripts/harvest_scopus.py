#!/usr/bin/env python3
"""
Harvest Scopus author/publication data for a portfolio site.

The script intentionally reads credentials from environment variables, because
GitHub Pages is static and any key placed into browser-side JavaScript becomes public.

Required:
  SCOPUS_API_KEY

Optional:
  SCOPUS_INST_TOKEN
  SCOPUS_AUTHOR_ID (default: 57211062810)
  SCOPUS_OUT_DIR (default: data/scopus)

Outputs:
  scopus_author_<id>_raw.json
  scopus_author_<id>_works.json
  scopus_author_<id>_metrics.json
  scopus_author_<id>_access_report.json
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from source_health import read_json, write_json, source_result, merge_records
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

API_BASE = "https://api.elsevier.com"

def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def request_json(path: str, api_key: str, inst_token: str | None = None, params: Dict[str, Any] | None = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    params = params or {}
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {
        "Accept": "application/json",
        "X-ELS-APIKey": api_key,
        "User-Agent": "scientist-portfolio-harvester/0.1",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    req = urllib.request.Request(url, headers=headers, method="GET")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp), {}
        except urllib.error.HTTPError as exc:
            result = {'_http_status': exc.code, '_reason': f'http_{exc.code}'}
            if exc.code not in {429, 500, 502, 503, 504}:
                return result, {}
        except (OSError, ValueError):
            result = {'_http_status': 0, '_reason': 'network_or_invalid_json'}
        if attempt < 2:
            time.sleep(2 ** attempt)
    return result, {}


def safe_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0

def optional_int(value):
    if isinstance(value, dict):
        value = value.get('$') if '$' in value else value.get('value')
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def flatten_author_profile(author_json: Dict[str, Any]) -> Dict[str, Any]:
    root = author_json.get("author-retrieval-response")
    if isinstance(root, list):
        root = root[0] if root else {}
    if not isinstance(root, dict):
        root = {}
    coredata = root.get("coredata") or {}
    profile = root.get("author-profile") or {}
    preferred = profile.get("preferred-name") or {}
    def metric(name):
        for candidate in (root.get(name), coredata.get(name), profile.get(name)):
            value = optional_int(candidate)
            if value is not None:
                return value
        return None
    return {
        "h_index": metric("h-index"),
        "method": "official_author_profile",
        "scopus_author_id": coredata.get("dc:identifier", "").replace("AUTHOR_ID:", "") or None,
        "eid": coredata.get("eid"),
        "name": " ".join([preferred.get("given-name", ""), preferred.get("surname", "")]).strip() or coredata.get("dc:title"),
        "document_count": metric("document-count"),
        "cited_by_count": metric("cited-by-count"),
        "citation_count": metric("citation-count"),
        "coauthor_count": metric("coauthor-count"),
        "raw_coredata": coredata,
    }

def fetch_author(api_key: str, author_id: str, inst_token: str | None = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    # ENHANCED may depend on entitlements; STANDARD fallback is attempted by caller if needed.
    return request_json(f"/content/author/author_id/{author_id}", api_key, inst_token, {"view": "ENHANCED"})

def fetch_author_standard(api_key: str, author_id: str, inst_token: str | None = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    return request_json(f"/content/author/author_id/{author_id}", api_key, inst_token, {"view": "STANDARD"})

def fetch_works(api_key: str, author_id: str, inst_token: str | None = None, count: int = 25) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, str]]]:
    entries: List[Dict[str, Any]] = []
    headers_seen: List[Dict[str, str]] = []
    start = 0
    first_payload: Dict[str, Any] = {}
    fields = ",".join([
        "dc:title",
        "dc:creator",
        "prism:publicationName",
        "prism:coverDate",
        "prism:doi",
        "citedby-count",
        "eid",
        "dc:identifier",
        "prism:aggregationType",
        "subtypeDescription",
        "openaccess",
    ])
    seen_starts = set()
    seen_entries = set()
    complete = False
    reason = None
    while start not in seen_starts:
        seen_starts.add(start)
        payload, headers = request_json(
            "/content/search/scopus", api_key, inst_token,
            {"query": f"AU-ID({author_id})", "count": count, "start": start,
             "view": "STANDARD", "field": fields},
        )
        headers_seen.append(headers)
        if start == 0:
            first_payload = payload
        if '_http_status' in payload:
            reason = payload.get('_reason', 'search_failed')
            break
        search = payload.get('search-results')
        if not isinstance(search, dict) or 'opensearch:totalResults' not in search:
            reason = 'unexpected_schema'
            break
        total = optional_int(search['opensearch:totalResults'])
        if total is None:
            reason = 'unexpected_schema'
            break
        batch = search.get('entry') or []
        if isinstance(batch, dict):
            batch = [batch]
        batch = [row for row in batch if isinstance(row, dict) and row.get('dc:title')]
        identities = [row.get('eid') or row.get('dc:identifier') or row.get('prism:doi') or row.get('dc:title') for row in batch]
        if any(identifier in seen_entries for identifier in identities):
            reason = 'duplicate_pagination'
            break
        seen_entries.update(identities)
        entries.extend(batch)
        if len(entries) >= total:
            complete = True
            break
        if not batch:
            reason = 'incomplete_pagination'
            break
        next_start = safe_int(search.get('opensearch:startIndex')) + (safe_int(search.get('opensearch:itemsPerPage')) or len(batch))
        if next_start <= start:
            reason = 'pagination_loop'
            break
        start = next_start
        time.sleep(0.2)
    first_payload['_collection'] = {'complete': complete, 'reason': reason, 'pages': len(seen_starts)}
    return entries, first_payload, headers_seen


def compute_h_index(citations: List[int]) -> int:
    citations = sorted([safe_int(c) for c in citations], reverse=True)
    h = 0
    for i, c in enumerate(citations, start=1):
        if c >= i:
            h = i
        else:
            break
    return h

def normalize_work(entry: Dict[str, Any]) -> Dict[str, Any]:
    authors = entry.get('author') or []
    if isinstance(authors, dict):
        authors = [authors]
    author_names = [str(author.get('authname') or ' '.join(filter(None, [author.get('given-name'), author.get('surname')]))).strip()
                    for author in authors if isinstance(author, dict)]
    return {
        "source": "scopus_api",
        "eid": entry.get("eid"),
        "scopus_id": (entry.get("dc:identifier") or "").replace("SCOPUS_ID:", "") or None,
        "title": entry.get("dc:title"),
        "creator": entry.get("dc:creator"),
        "authors_raw": ', '.join(name for name in author_names if name) or None,
        "journal_or_source": entry.get("prism:publicationName"),
        "cover_date": entry.get("prism:coverDate"),
        "year": (entry.get("prism:coverDate") or "")[:4] or None,
        "doi": entry.get("prism:doi"),
        "cited_by_count": optional_int(entry.get("citedby-count")),
        "aggregation_type": entry.get("prism:aggregationType"),
        "subtype": entry.get("subtypeDescription"),
        "openaccess": entry.get("openaccess"),
        "url": next((l.get("@href") for l in entry.get("link", []) if l.get("@ref") == "scopus"), None) if isinstance(entry.get("link"), list) else None,
        "raw": entry,
    }

def main() -> int:
    api_key = os.environ.get('SCOPUS_API_KEY', '').strip()
    inst_token = os.environ.get('SCOPUS_INST_TOKEN', '').strip() or None
    author_id = os.environ.get('SCOPUS_AUTHOR_ID', '57211062810').strip()
    out_dir = Path(os.environ.get('SCOPUS_OUT_DIR', 'data/scopus'))
    prefix = out_dir / f'scopus_author_{author_id}'
    def path(suffix):
        return prefix.with_name(prefix.name + '_' + suffix + '.json')
    old_report = read_json(path('access_report'), {})
    old_works = read_json(path('works'), {})
    if isinstance(old_works, dict):
        old_works = old_works.get('works', [])
    if not api_key:
        write_json(path('access_report'), source_result(old_report, count=len(old_works), status='blocked', reason='credentials_missing'))
        return 2

    # Record both entitlements independently; a restricted Author Retrieval must
    # not suppress a working Scopus Search subscription.
    author_attempts = {}
    author_payload = {}
    for view in ('STANDARD', 'ENHANCED'):
        candidate, _ = request_json(f'/content/author/author_id/{author_id}', api_key, inst_token, {'view': view})
        valid = '_http_status' not in candidate and bool(candidate.get('author-retrieval-response'))
        author_attempts[view] = {'status': 'success' if valid else 'error', 'http_status': candidate.get('_http_status', 200), 'reason': None if valid else candidate.get('_reason', 'unexpected_schema')}
        if valid:
            author_payload = candidate
    entries, search_payload, _ = fetch_works(api_key, author_id, inst_token)
    collection = search_payload.get('_collection', {})
    complete = collection.get('complete', False)
    fresh = [normalize_work(e) for e in entries]
    works = merge_records(old_works, fresh, lambda r: r.get('eid') or r.get('scopus_id') or r.get('doi')) if complete else old_works
    state = source_result(old_report, status='success' if complete else ('partial' if fresh else 'error'), count=len(works), reason=collection.get('reason'))
    report = {**state, 'generated_at': now_utc(), 'author_attempts': author_attempts,
              'author_profile_status': 200 if author_payload else author_attempts['STANDARD']['http_status'],
              'search_status': search_payload.get('_http_status', 200), 'search_pages': collection.get('pages')}
    if complete:
        citations = [w['cited_by_count'] for w in fresh]
        known_citations = all(c is not None for c in citations)
        profile = flatten_author_profile(author_payload) if author_payload else {}
        metrics = {'source': 'scopus_api', 'generated_at': now_utc(), 'last_success_at': state['last_success_at'],
                   'scopus_author_id': author_id, 'author_profile_status': report['author_profile_status'],
                   'search_status': 200, 'profile': profile, 'works_count_from_search': len(fresh),
                   'citation_sum_from_search': sum(citations) if known_citations else None,
                   'h_index_recomputed_from_retrieved_works': compute_h_index(citations) if known_citations else None,
                   'method': 'official_author_profile' if profile.get('h_index') is not None else 'calculated_from_complete_search',
                   'note': 'Search metrics are calculated from a complete Search response. Missing citation counts remain unknown.'}
        write_json(path('works'), works)
        write_json(path('metrics'), metrics)
        # Response headers and error bodies can contain private session material.
        safe_first = {k: v for k, v in search_payload.items() if not k.startswith('_')}
        write_json(path('raw'), {'author_payload': author_payload, 'search_first_payload': safe_first})
    write_json(path('access_report'), report)
    print(json.dumps({'status': state['status'], 'works': len(works), 'author_attempts': author_attempts}))
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
