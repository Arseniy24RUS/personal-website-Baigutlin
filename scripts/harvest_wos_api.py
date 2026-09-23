"""Official Clarivate APIs, with atomic independent component checkpoints.

Schemas: https://developer.clarivate.com/apis/wos-starter/swagger and
https://developer.clarivate.com/apis/wos-researcher/swagger (checked 2026-09-20).
No browser fallback, raw API responses, API links, keys or exception text are
written. The public bibliography is additive even after a complete API search.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import json
import os
import re
import time
from pathlib import Path

import requests

from source_health import (component_state, load_checkpoint, materialize_checkpoint,
                           now, read_json, source_result, write_checkpoint)

BASES = {'starter': 'https://api.clarivate.com/apis/wos-starter/v2',
         'researcher': 'https://api.clarivate.com/apis/wos-researcher'}
KEY_NAMES = {'starter': 'WOS_STARTER_API_KEY', 'researcher': 'WOS_RESEARCHER_API_KEY'}
CORE_FIELDS = ('publications', 'citations', 'h_index')
CORE_SCOPE = 'web_of_science_core_collection'
MAX_PAGES = 2000
PAGE_SIZE = 50
MAX_RETRY_WAIT = 10
API_REASONS = frozenset({
    'api_key_missing', 'api_provider_invalid', 'api_researcher_id_invalid',
    'api_identity_mismatch', 'api_schema_invalid', 'api_invalid_json',
    'api_http_401', 'api_http_403', 'api_http_404', 'api_http_429',
    'api_http_500', 'api_http_502', 'api_http_503', 'api_http_504', 'api_http_error',
    'api_redirect_refused', 'api_network_error', 'api_timeout', 'api_unexpected_error',
    'api_pagination_in_progress', 'api_pagination_mismatch', 'api_total_changed',
    'api_duplicate_uid', 'api_pagination_limit', 'api_citations_unavailable',
    'api_metrics_pending', 'api_empty_response', 'api_scope_mismatch', 'api_metric_mismatch',
})


class ApiFailure(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if reason in API_REASONS else 'api_unexpected_error'
        super().__init__(self.reason)


def nonnegative_int(value):
    # Swagger declares integers; bool and numeric-looking strings are not counts.
    return value if type(value) is int and 0 <= value <= 10**12 else None


def text(value):
    return html.unescape(re.sub(r'<[^>]*>', '', value)).strip()[:10000] if isinstance(value, str) else None


class Client:
    def __init__(self, provider, key, session):
        self.provider, self.key, self.session = provider, key, session
        self.attempts = []
        self.last_request_at = None

    def get(self, endpoint, params=None):
        for attempt in range(3):
            delay = 2 ** attempt
            try:
                if self.provider == 'starter' and self.last_request_at is not None:
                    remaining = 1.0 - (time.monotonic() - self.last_request_at)
                    if remaining > 0:
                        time.sleep(remaining)
                self.last_request_at = time.monotonic()
                response = self.session.get(
                    BASES[self.provider] + endpoint, params=params or {},
                    headers={'X-ApiKey': self.key, 'Accept': 'application/json'},
                    timeout=(10, 20), allow_redirects=False,
                )
                status = int(response.status_code)
                if len(self.attempts) < 64:
                    self.attempts.append({'endpoint': 'documents' if endpoint.endswith('/documents') else 'profile',
                                          'http_status': status if 100 <= status <= 599 else 0})
                if 300 <= status <= 399:
                    raise ApiFailure('api_redirect_refused')
                if status == 200:
                    try:
                        result = response.json()
                    except (ValueError, TypeError):
                        raise ApiFailure('api_invalid_json') from None
                    if not isinstance(result, dict):
                        raise ApiFailure('api_schema_invalid')
                    return result
                reason = f'api_http_{status}'
                reason = reason if reason in API_REASONS else 'api_http_error'
                if status not in {429, 500, 502, 503, 504}:
                    raise ApiFailure(reason)
                retry_after = response.headers.get('Retry-After', '')
                server_delay = None
                if str(retry_after).isdigit():
                    server_delay = int(retry_after)
                elif retry_after:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        server_delay = max(0, (date - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
                if server_delay is not None:
                    if server_delay > MAX_RETRY_WAIT:
                        # Do not retry earlier than the provider allows. A later
                        # scheduled run can retry without holding this collector.
                        raise ApiFailure(reason)
                    delay = max(delay, server_delay)
            except requests.Timeout:
                reason = 'api_timeout'
            except requests.RequestException:
                reason = 'api_network_error'
            if attempt < 2:
                time.sleep(delay)
        raise ApiFailure(reason)


def core_citations(record):
    values = record.get('citations')
    if not isinstance(values, list):
        return None
    core = [item.get('count') for item in values if isinstance(item, dict) and item.get('db') == 'WOS']
    # Missing/duplicate/invalid counters never mean an observed zero.
    return nonnegative_int(core[0]) if len(core) == 1 else None


def parse_document(record, provider, stamp):
    if not isinstance(record, dict):
        raise ApiFailure('api_schema_invalid')
    uid, title = record.get('uid'), text(record.get('title'))
    if not isinstance(uid, str) or not re.fullmatch(r'[A-Z][A-Z0-9]*:[A-Za-z0-9._-]{1,100}', uid) or not title:
        raise ApiFailure('api_schema_invalid')
    if not uid.startswith('WOS:'):
        if provider == 'starter':
            raise ApiFailure('api_scope_mismatch')
        return uid, None
    if not re.fullmatch(r'WOS:[A-Z0-9]+', uid):
        raise ApiFailure('api_schema_invalid')
    # WOS is Clarivate's documented Core database code. An explicitly different
    # collection must not silently override the UID-based scope assumption.
    if record.get('collection') not in (None, '', 'WOS'):
        raise ApiFailure('api_scope_mismatch')
    source, identifiers = record.get('source') or {}, record.get('identifiers') or {}
    if not isinstance(source, dict) or not isinstance(identifiers, dict):
        raise ApiFailure('api_schema_invalid')
    types = record.get('types') or []
    if not isinstance(types, list) or any(not isinstance(value, str) for value in types):
        raise ApiFailure('api_schema_invalid')
    row = {
        'source': f'web_of_science_{provider}_api', 'sources': ['wos'], 'wos_uid': uid,
        'title': title, 'document_type': '; '.join(types), 'venue': text(source.get('sourceTitle')),
        'year': nonnegative_int(source.get('publishYear')), 'volume': text(source.get('volume')),
        'issue': text(source.get('issue')), 'doi': text(identifiers.get('doi')),
        'url': 'https://www.webofscience.com/wos/woscc/full-record/' + uid,
        'observed_at': stamp, 'wos_citations': core_citations(record),
    }
    pages = source.get('pages')
    if isinstance(pages, dict):
        row['pages'] = text(pages.get('range'))
    for key in ('issn', 'eissn', 'isbn', 'eisbn'):
        row[key] = text(identifiers.get(key))
    names = record.get('names') or {}
    authors = names.get('authors', []) if isinstance(names, dict) else []
    if isinstance(authors, list):
        row['authors_raw'] = '; '.join(name for author in authors if isinstance(author, dict)
                                      if (name := text(author.get('displayName') or author.get('wosStandard'))))
    row['retained_citation_fields'] = ['wos_citations'] if row['wos_citations'] is None else []
    if row['wos_citations'] is not None:
        row['citation_observed_at'] = {'wos': stamp}
    return uid, row


def merge_rows(previous, fresh):
    result = copy.deepcopy(previous)
    index = {row.get('wos_uid'): index for index, row in enumerate(result) if isinstance(row, dict) and row.get('wos_uid')}
    for row in fresh:
        old = result[index[row['wos_uid']]] if row['wos_uid'] in index else {}
        merged = {**old, **{key: value for key, value in row.items() if value not in (None, '', [], {})}}
        merged['sources'] = sorted(set(old.get('sources') or []) | {'wos'})
        merged['retained_citation_fields'] = row['retained_citation_fields']
        if row.get('wos_citations') is None:
            prior_stamps = old.get('citation_observed_at') or {}
            prior_stamp = prior_stamps.get('wos') if 'wos' in prior_stamps else (
                old.get('observed_at') if 'wos_citations' not in (old.get('retained_citation_fields') or []) else None
            )
            if prior_stamp:
                merged['citation_observed_at'] = {**(old.get('citation_observed_at') or {}), 'wos': prior_stamp}
        else:
            merged['citation_observed_at'] = {**(old.get('citation_observed_at') or {}), **row['citation_observed_at']}
        if row['wos_uid'] in index:
            result[index[row['wos_uid']]] = merged
        else:
            index[row['wos_uid']] = len(result)
            result.append(merged)
    return result


def harvest(root, *, provider=None, session=None, researcher_id=None):
    """Write compatible WoS checkpoints and return a safe report; no browser use."""
    directory = Path(root) / 'data/wos'
    checkpoint_path = directory / 'collection_checkpoint.json'
    existing = load_checkpoint(checkpoint_path)
    previous = read_json(directory / 'profile_metrics.json', {})
    previous_report = existing['report'] if existing else read_json(directory / 'harvest_report.json', {})
    payloads = copy.deepcopy(existing['payloads']) if existing else {
        'metrics': {key: value for key, value in previous.items() if key not in {'records', 'records_count_on_page'}},
        'publications': previous.get('records', []), 'details': {},
    }
    attempted = now()
    provider = provider or ('researcher' if os.environ.get(KEY_NAMES['researcher']) else 'starter')
    target = researcher_id or os.environ.get('WOS_RESEARCHER_ID', 'AAN-4717-2020')
    components = {name: source_result(component_state(previous_report, name), status='blocked',
                                     reason='api_metrics_pending' if name == 'metrics' else 'api_pagination_in_progress',
                                     count=1 if name == 'metrics' and payloads['metrics'] else len(payloads['publications']) if name == 'publications' else 0,
                                     attempted_at=attempted) for name in ('metrics', 'publications')}
    report = {'transport': 'api', 'api_provider': provider if provider in BASES else 'unknown',
              'identity_verified': False, 'components': components}
    metric_values, methods, seen, current_core = {}, {}, set(), {}
    raw_metrics = {}
    client = None
    owned_session = False

    def emit():
        complete = all(state['status'] == 'success' for state in components.values())
        partial = bool(metric_values) or bool(seen)
        failure = next((state['reason'] for state in components.values() if state.get('reason')), None)
        report.update(source_result(previous_report, status='success' if complete else 'partial' if partial else
                                    'error' if any(state['status'] == 'error' for state in components.values()) else 'blocked',
                                    count=len(payloads['publications']), reason=None if complete else failure, attempted_at=attempted))
        if client:
            report['api_attempts'] = list(client.attempts)
        write_checkpoint(checkpoint_path, report, payloads)
        materialize_checkpoint(checkpoint_path, 'wos')

    def update_metrics(values, reason='api_metrics_pending', new_methods=None):
        metric_values.update(values)
        methods.update(new_methods or {})
        old = payloads['metrics']
        summary = {**old.get('summary', {}), **metric_values}
        complete = all(key in metric_values for key in CORE_FIELDS)
        components['metrics'] = source_result(component_state(previous_report, 'metrics'),
            status='success' if complete else 'partial' if metric_values else 'blocked', count=1 if summary else 0,
            reason=None if complete else reason, attempted_at=attempted)
        if metric_values:
            components['metrics'].update(observed_count=len(metric_values), last_observation_at=attempted, scope=CORE_SCOPE)
        payloads['metrics'] = {**old, 'source': f'web_of_science_{provider}_api',
            'source_url': f'https://www.webofscience.com/wos/author/record/{target}', 'researcher_id': target,
            'summary': summary, 'api_metrics': raw_metrics, 'metric_methods': {**old.get('metric_methods', {}), **methods},
            'metric_observed_at': {key: attempted if key in metric_values else old.get('metric_observed_at', {}).get(key, component_state(previous_report, 'metrics').get('last_success_at')) for key in summary},
            'retained_metric_fields': [key for key in summary if key not in metric_values]}
        if metric_values:
            payloads['metrics']['generated_at'] = attempted
        if complete:
            payloads['metrics']['last_success_at'] = attempted
        emit()

    def fail(name, reason):
        prior = components[name]
        partial = bool(metric_values) if name == 'metrics' else bool(seen)
        blocked = reason in {'api_key_missing', 'api_http_401', 'api_http_403', 'api_http_404', 'api_http_429'}
        state = source_result(component_state(previous_report, name), status='partial' if partial else 'blocked' if blocked else 'error',
                              count=prior['record_count'], reason=reason, attempted_at=attempted)
        for key in ('observed_count', 'last_observation_at', 'scope', 'api_indexed_count', 'core_record_count'):
            if key in prior:
                state[key] = prior[key]
        components[name] = state

    try:
        if provider not in BASES:
            raise ApiFailure('api_provider_invalid')
        key = os.environ.get(KEY_NAMES[provider], '').strip()
        if not key:
            raise ApiFailure('api_key_missing')
        if not isinstance(target, str) or not re.fullmatch(r'[A-Z]{1,3}-\d{4}-\d{4}', target):
            raise ApiFailure('api_researcher_id_invalid')
        if session is None:
            session, owned_session = requests.Session(), True
        client = Client(provider, key, session)
        if provider == 'researcher':
            profile = client.get(f'/researchers/{target}')
            ids = profile.get('ids')
            if not isinstance(ids, dict) or not isinstance(ids.get('rids'), list) or target not in ids['rids']:
                raise ApiFailure('api_identity_mismatch')
            report['identity_verified'] = True
            metrics = profile.get('metricsAllTime')
            if not isinstance(metrics, dict):
                raise ApiFailure('api_schema_invalid')
            documents = metrics.get('documents') or {}
            for field in ('hIndex', 'totalTimesCited', 'totalCitingPublications', 'totalTimesCitedWithoutSelf'):
                if nonnegative_int(metrics.get(field)) is not None:
                    raw_metrics[field] = metrics[field]
            if isinstance(documents, dict) and nonnegative_int(documents.get('count')) is not None:
                raw_metrics['documents_count'] = documents['count']
            # Only hIndex explicitly has Core scope in the official schema.
            values = {'h_index': raw_metrics['hIndex']} if 'hIndex' in raw_metrics else {}
            update_metrics(values, new_methods={'h_index': 'researcher_api_official_core_hindex'} if values else {})

        total, limit = None, None
        endpoint = '/documents' if provider == 'starter' else f'/researchers/{target}/documents'
        for page in range(1, MAX_PAGES + 1):
            params = {'page': page, 'limit': PAGE_SIZE}
            params.update({'db': 'WOS', 'q': f'AI={target}'} if provider == 'starter' else {'nonIndexed': 'false'})
            data = client.get(endpoint, params)
            metadata, hits = data.get('metadata'), data.get('hits')
            if not isinstance(metadata, dict) or not isinstance(hits, list):
                raise ApiFailure('api_schema_invalid')
            page_total, page_limit = nonnegative_int(metadata.get('total')), nonnegative_int(metadata.get('limit'))
            if page_total is None or type(metadata.get('page')) is not int or metadata['page'] != page or not page_limit or page_limit > PAGE_SIZE:
                raise ApiFailure('api_pagination_mismatch')
            if total is not None and page_total != total:
                raise ApiFailure('api_total_changed')
            if limit is not None and page_limit != limit:
                raise ApiFailure('api_pagination_mismatch')
            total, limit = page_total, page_limit
            report['identity_verified'] = True  # Fixed AI query or verified RID route.
            batch, page_error = [], None
            for item in hits:
                try:
                    uid, row = parse_document(item, provider, attempted)
                    if uid in seen:
                        page_error = 'api_duplicate_uid'
                        continue
                    seen.add(uid)
                    if row:
                        batch.append(row)
                        current_core[uid] = row
                except ApiFailure as exc:
                    page_error = exc.reason
            payloads['publications'] = merge_rows(payloads['publications'], batch)
            components['publications'] = source_result(component_state(previous_report, 'publications'), status='partial',
                count=len(payloads['publications']), reason='api_pagination_in_progress', attempted_at=attempted)
            components['publications'].update(observed_count=len(current_core), last_observation_at=attempted,
                scope=CORE_SCOPE, api_indexed_count=total, core_record_count=len(current_core))
            emit()
            if page_error:
                raise ApiFailure(page_error)
            if len(hits) != min(limit, max(0, total - (page - 1) * limit)) or len(seen) > total:
                raise ApiFailure('api_pagination_mismatch')
            if len(seen) == total:
                break
        else:
            raise ApiFailure('api_pagination_limit')
        if total == 0 and payloads['publications'] and (provider != 'researcher' or raw_metrics.get('documents_count') != 0):
            raise ApiFailure('api_empty_response')
        if not current_core and payloads['publications'] and not (
            provider == 'researcher' and total == 0 and raw_metrics.get('documents_count') == 0 and raw_metrics.get('hIndex') == 0
        ):
            raise ApiFailure('api_empty_response')
        if raw_metrics.get('hIndex', 0) > len(current_core):
            raise ApiFailure('api_metric_mismatch')
        components['publications'].update(status='success', complete=True, origin='live', reason=None, last_success_at=attempted)
        emit()  # A later metrics failure must not revoke the complete list.
        values = {'publications': len(current_core), 'core_collection_publications': len(current_core)}
        if provider == 'researcher':
            values['indexed_publications'] = total
        calculated_method = f'{provider}_api_calculated_complete_core'
        metric_methods = {'publications': calculated_method}
        counts = [row['wos_citations'] for row in current_core.values()]
        if all(value is not None for value in counts):
            values['citations'] = sum(counts)
            recomputed_h = max((rank for rank, count in enumerate(sorted(counts, reverse=True), 1) if count >= rank), default=0)
            if 'h_index' in metric_values and metric_values['h_index'] != recomputed_h:
                # Different Core totals/citation snapshots cannot jointly prove
                # a complete fresh metric set, even if each endpoint responded.
                raise ApiFailure('api_metric_mismatch')
            if 'h_index' not in metric_values:
                values['h_index'] = recomputed_h
                metric_methods['h_index'] = calculated_method
            metric_methods['citations'] = calculated_method
            # Numeric agreement with raw API totals does not establish their
            # Core scope; publication/citation methods remain calculated.
        update_metrics(values, reason='api_citations_unavailable', new_methods=metric_methods)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, ApiFailure) else 'api_unexpected_error'
        for name in ('metrics', 'publications'):
            if components[name]['status'] != 'success':
                fail(name, reason)
        emit()
    finally:
        if owned_session:
            session.close()
    return report


def main():
    report = harvest(Path.cwd(), provider=os.environ.get('WOS_API_PROVIDER') or None)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('status') == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
