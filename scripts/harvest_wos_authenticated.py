#!/usr/bin/env python3
"""Collect WoS through its ordinary ORCID login and rendered pagination.

No stale cookies, browser fingerprint overrides, access-control workarounds or
public session diagnostics. A snapshot fallback is reported as a failed live
attempt, not as a successful login.
"""
from __future__ import annotations

import json
import copy
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from parse_wos_author_profile import parse_wos_author_profile_html
from provider_auth import AuthFailure, login_wos, assert_no_challenge, verify_browser_egress, visible, browser_initialization_diagnostics, safe_browser_diagnostics, safe_wos_login_evidence, wos_authenticated, provider_host
from source_health import read_json, write_json, source_result, merge_records, now, snapshot_time, component_state, load_checkpoint, write_checkpoint, materialize_checkpoint

RESEARCHER_ID = os.environ.get('WOS_RESEARCHER_ID', 'AAN-4717-2020')
PROFILE_URL = f'https://www.webofscience.com/wos/author/record/{RESEARCHER_ID}'
OUT = Path(os.environ.get('WOS_PROFILE_OUT', 'data/wos/profile_metrics.json'))
REPORT = Path(os.environ.get('WOS_HARVEST_REPORT', 'data/wos/harvest_report.json'))
WAIT_SEC = int(os.environ.get('WOS_BROWSER_WAIT_SEC', '180'))
AUTH_MODES = frozenset({'restore', 'fresh_orcid'})


class CheckpointWriteError(RuntimeError):
    pass


def record_key(record):
    return record.get('wos_uid') or record.get('doi') or (record.get('title'), record.get('year'))


def safe_profile_diagnostics(page):
    """Expose parsed counts/schema, never source HTML, text or record values."""
    try:
        data = parse_wos_author_profile_html(page.content(), RESEARCHER_ID)
        records = data.get('records', [])
        summary = data.get('summary', {})
        keys = ('publications', 'citations', 'h_index', 'total_documents', 'indexed_publications', 'core_collection_publications')
        schema_fields = {'source', 'source_url', 'researcher_id', 'generated_at', 'page_title', 'summary', 'summary_metrics', 'core_collection_metrics', 'records_count_on_page', 'records'}
        record_fields = {'source', 'wos_uid', 'document_type', 'title', 'title_en', 'authors_raw', 'venue', 'venue_en', 'publisher', 'year', 'volume', 'issue', 'pages', 'doi', 'url', 'metadata_raw', 'dedupe_fingerprint', 'sources'}
        numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
        return {
            'parsed_record_count': len(records),
            'summary': {key: summary.get(key) if numeric(summary.get(key)) else None for key in keys},
            'schema_fields': sorted(schema_fields.intersection(data)),
            'record_fields': sorted(record_fields.intersection(key for record in records[:5] if isinstance(record, dict) for key in record)),
            'summary_metric_count': len(data.get('summary_metrics', {})),
            'core_metric_count': len(data.get('core_collection_metrics', {})),
        }
    except Exception:
        return {'parser_failed': True}


def safe_record_dom_diagnostics(page):
    """Only counts and geometry: no DOM text, attribute values or record URLs."""
    try:
        observed = page.locator('app-record').evaluate_all("""elements => {
            const first = elements[0];
            const rect = first ? first.getBoundingClientRect() : null;
            const integer = value => Math.round(Math.max(-1000000, Math.min(1000000, value)));
            return {
                app_record_count: elements.length,
                nonempty_app_record_count: elements.filter(el => (el.innerText || '').trim().length > 0).length,
                record_title_link_count: elements.reduce((count, el) => count + el.querySelectorAll('app-summary-title a[data-ta="summary-record-title-link"]').length, 0),
                record_shadow_root_count: elements.filter(el => el.shadowRoot !== null).length,
                first_record_top: rect ? integer(rect.top) : null,
                first_record_height: rect ? integer(rect.height) : null,
                viewport_height: innerHeight,
                first_record_intersects_viewport: rect ? Number(rect.top < innerHeight && rect.bottom >= 0 && rect.left < innerWidth && rect.right >= 0) : 0
            };
        }""")
        fields = {'app_record_count', 'nonempty_app_record_count', 'record_title_link_count',
                  'record_shadow_root_count', 'first_record_top', 'first_record_height',
                  'viewport_height', 'first_record_intersects_viewport'}
        return {key: value for key, value in observed.items()
                if key in fields and (value is None or type(value) in (int, float) and math.isfinite(value))}
    except Exception:
        return {'dom_observation_failed': True}


def record_read_failure(page, reason):
    failure = AuthFailure(reason)
    failure.profile_diagnostics = {**safe_profile_diagnostics(page), **safe_record_dom_diagnostics(page)}
    return failure


def read_records(page, previous_keys=None):
    deadline = time.monotonic() + WAIT_SEC
    scrolled_to_records = False
    while time.monotonic() < deadline:
        assert_no_challenge(page)
        data = parse_wos_author_profile_html(page.content(), RESEARCHER_ID)
        records = data.get('records', [])
        if not records and not previous_keys and expected_publications(data) == 0:
            return data
        if records and (not previous_keys or {record_key(r) for r in records} - previous_keys):
            return data
        if not records and not scrolled_to_records:
            cards = page.locator('app-record')
            if cards.count():
                # WoS leaves even zero-height shells empty until their first
                # IntersectionObserver target enters the viewport. Use the same
                # ordinary scroll as a reader; do not change or synthesize DOM.
                scrolled_to_records = True
                remaining_ms = int(max(1, (deadline - time.monotonic()) * 1000))
                try:
                    cards.first.scroll_into_view_if_needed(timeout=min(5000, remaining_ms))
                except Exception:
                    raise record_read_failure(page, 'publication_list_scroll_failed') from None
        page.wait_for_timeout(1000)
    raise record_read_failure(page, 'profile_records_not_ready')


def expected_publications(data):
    value = data.get('summary', {}).get('core_collection_publications')
    return value if value is not None else data.get('summary', {}).get('publications')


def select_core_collection(page):
    """The official metric total must describe the publication list's active scope."""
    assert_no_challenge(page)
    controls = page.get_by_role('button', name=re.compile(r'^Web of Science Core Collection(?:\s*\(\s*\d+\s*\))?$', re.I))
    control = next((controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible()), None)
    if control is None:
        raise AuthFailure('publication_scope_control_missing')
    selected = lambda: 'selected-round-chip' in (control.get_attribute('class') or '').split() or control.get_attribute('aria-pressed') == 'true' or control.get_attribute('aria-selected') == 'true'
    if not selected():
        control.click(timeout=15000)
    deadline = time.monotonic() + min(WAIT_SEC, 30)
    while time.monotonic() < deadline:
        assert_no_challenge(page)
        if selected():
            return
        page.wait_for_timeout(500)
    raise AuthFailure('publication_scope_not_confirmed')


def collect_publications(page, *, on_batch=None):
    data = read_records(page)
    records = merge_records([], data['records'], record_key)
    expected = expected_publications(data)
    stamp = now()
    for row in records:
        row['observed_at'] = stamp
    if on_batch:
        on_batch(records, expected is not None and len(records) == int(expected))
    if expected is None:
        raise AuthFailure('profile_total_missing')
    for _ in range(1000):
        if len(records) == int(expected):
            break
        keys = {record_key(r) for r in records}
        next_button = visible(page, ['button[data-ta="next-page-button"]', 'button[aria-label*="Next Page" i]', 'button[aria-label*="Следующая" i]'])
        if next_button is None or not next_button.is_enabled():
            break
        next_button.click()
        batch = read_records(page, keys)
        if expected_publications(batch) not in (None, expected):
            raise AuthFailure('publication_total_changed')
        for row in batch['records']:
            row['observed_at'] = now()
        records = merge_records(records, batch['records'], record_key)
        if on_batch:
            on_batch(batch['records'], len(records) == int(expected))
    else:
        raise AuthFailure('pagination_limit')
    if len(records) != int(expected):
        raise AuthFailure('incomplete_pagination')
    data['records'] = records
    return data


def collect_profile(page, previous):
    """Compatibility API; independent component collection uses collect_from_page."""
    data = collect_publications(page)
    if any(data.get('summary', {}).get(key) is None for key in ('publications', 'citations', 'h_index')):
        raise AuthFailure('profile_metrics_missing')
    # Preserve withdrawn or temporarily hidden old works while refreshing new data.
    data['records'] = merge_records(previous.get('records', []), data['records'], record_key)
    data['records_count_on_page'] = len(data['records'])
    data['source'] = 'web_of_science_authenticated_orcid'
    # Missing metrics are not genuine zero, and must not replace the last value.
    for field in ('summary', 'summary_metrics', 'core_collection_metrics'):
        data[field] = {**previous.get(field, {}), **{k: v for k, v in data.get(field, {}).items() if v is not None}}
    return data


def explicitly_logged_out(page):
    host = urlparse(page.url).hostname or ''
    if any(provider_host(host, domain) for domain in ('clarivate.com', 'orcid.org')):
        return visible(page, ['input[type="password"]']) is not None
    if provider_host(host, 'webofscience.com'):
        for role in ('button', 'link'):
            controls = page.get_by_role(role, name=re.compile(r'^\s*(?:Sign in|Войти)\s*$', re.I))
            if any(controls.nth(i).is_visible() for i in range(controls.count())):
                return True
    return False


def target_profile_html(page, target=RESEARCHER_ID, *, profile_entry_wait_seconds=10.0):
    path = f'/wos/author/record/{target}'
    if urlparse(page.url).path.rstrip('/') != path:
        page.goto(f'https://www.webofscience.com{path}', wait_until='domcontentloaded', timeout=90000)
    deadline = time.monotonic() + WAIT_SEC
    entry_deadline = time.monotonic() + profile_entry_wait_seconds

    def guard():
        # The one profile-entry window survives an initially clear scan and
        # later menu-triggered rendering; it is never restarted by another frame.
        options = {}
        if profile_entry_wait_seconds != 10.0 and time.monotonic() < entry_deadline:
            options = {'passive_wait_seconds': profile_entry_wait_seconds, 'passive_deadline': entry_deadline}
        observation = assert_no_challenge(page, **options)
        if profile_entry_wait_seconds != 10.0 and isinstance(observation, dict):
            page._profile_entry_observation = observation
        return observation

    while time.monotonic() < deadline:
        guard()
        address = urlparse(page.url)
        if provider_host(address.hostname or '', 'webofscience.com') and wos_authenticated(page):
            # Account-menu rendering may navigate while authorization is checked.
            address = urlparse(page.url)
            if not provider_host(address.hostname or '', 'webofscience.com') or address.path.rstrip('/') != path:
                raise AuthFailure('wrong_author_profile')
            # The parser's researcher_id argument is not identity evidence.
            # Require the requested ResearcherID in the rendered profile itself.
            if re.search(r'(?<![A-Z0-9-])' + re.escape(str(target)) + r'(?![A-Z0-9-])', page.locator('body').inner_text()):
                # Opening the account menu may reveal a new challenge after
                # the initial scan. Readiness is required at the return boundary.
                if guard():
                    # A passive wait can include navigation or session expiry.
                    # Prove origin, target and authorization again after it.
                    continue
                return page.content()
        elif explicitly_logged_out(page):
            raise AuthFailure('session_expired')
        page.wait_for_timeout(1000)
    raise AuthFailure('profile_not_authenticated_or_changed')


def authenticated_page(context, session_info, target=RESEARCHER_ID, *, fresh_context=None):
    if session_info.get('status') == 'invalid':
        raise AuthFailure('invalid_session_checkpoint')

    def verify(page):
        try:
            return target_profile_html(page, target, profile_entry_wait_seconds=60.0)
        except Exception as exc:
            failure = exc if isinstance(exc, AuthFailure) else AuthFailure(type(exc).__name__)
            failure.diagnostics = safe_browser_diagnostics(context)
            login_evidence = safe_wos_login_evidence(getattr(page, '_wos_login_evidence', None))
            if login_evidence:
                failure.authentication_evidence = safe_wos_login_evidence({
                    **login_evidence, **(failure.authentication_evidence or {}),
                })
            entry_observation = getattr(page, '_profile_entry_observation', None)
            if isinstance(entry_observation, dict):
                failure.profile_entry_observation = entry_observation
            if failure.reason in {'profile_not_authenticated_or_changed', 'wrong_author_profile', 'TimeoutError'}:
                failure.profile_diagnostics = safe_profile_diagnostics(page)
            raise failure from None

    if session_info.get('status') == 'restored':
        page = context.new_page()
        try:
            verify(page)
            return page, 'existing_session_verified'
        except AuthFailure as exc:
            if exc.reason != 'session_expired':
                raise
            if fresh_context is None:
                page.close()
                raise
            context = fresh_context()
    page = login_wos(context, f'https://www.webofscience.com/wos/author/record/{target}', WAIT_SEC)
    verify(page)
    return page, 'fresh_orcid_login'


def read_profile_metrics(page, target=RESEARCHER_ID):
    html = target_profile_html(page, target)
    deadline = time.monotonic() + WAIT_SEC
    while time.monotonic() < deadline:
        assert_no_challenge(page)
        data = parse_wos_author_profile_html(html, target)
        if all(data.get('summary', {}).get(key) is not None for key in ('publications', 'citations', 'h_index')):
            return {key: value for key, value in data.items() if key not in {'records', 'records_count_on_page'}}
        page.wait_for_timeout(1000)
        html = page.content()
    raise AuthFailure('profile_metrics_missing')


def collect_from_page(page, target=RESEARCHER_ID, previous=None, previous_report=None, *, on_checkpoint=None, cv_export=None):
    payloads = copy.deepcopy(previous or {'metrics': {}, 'publications': [], 'details': {}})
    for key, default in [('metrics', {}), ('publications', []), ('details', {})]:
        payloads.setdefault(key, default)
    previous_report = previous_report or {}
    attempted = now()
    components = {key: source_result(component_state(previous_report, key), status='blocked', reason='not_attempted', attempted_at=attempted) for key in ('metrics', 'publications')}
    report = {}
    publication_transport = 'rendered_profile'
    cv_attempt = None

    def emit():
        complete = all(state.get('complete') for state in components.values())
        successful = any(state.get('complete') or state.get('observed_count') for state in components.values())
        failed = next((state for state in components.values() if not state.get('complete')), {})
        report.clear()
        report.update(source_result(previous_report, status='success' if complete else ('partial' if successful else failed.get('status', 'error')), count=len(payloads['publications']), reason=None if complete else failed.get('reason'), attempted_at=attempted))
        report.update(components=components, researcher_id=str(target), generated_at=now())
        report['publication_transport'] = publication_transport
        if cv_attempt is not None:
            report['cv_export'] = dict(cv_attempt)
        if on_checkpoint:
            try:
                on_checkpoint(copy.deepcopy(report), copy.deepcopy(payloads))
            except Exception as exc:
                raise CheckpointWriteError(type(exc).__name__) from None

    def fail(key, exc):
        prior = components[key]
        state = source_result(component_state(previous_report, key), status='blocked' if isinstance(exc, AuthFailure) else 'error', count=prior.get('record_count', 0), reason=getattr(exc, 'reason', type(exc).__name__), attempted_at=attempted)
        for field in ('observed_count', 'last_observation_at'):
            if prior.get(field):
                state[field] = prior[field]
                state['status'] = 'partial'
        for field in ('verification_evidence', 'profile_diagnostics'):
            if getattr(exc, field, None):
                state[field] = getattr(exc, field)
        components[key] = state
        emit()

    def save_metrics(data, *, observed_at=None):
        data = copy.deepcopy(data)
        observed = dict(data.get('summary', {}))
        old = payloads['metrics']
        for field in ('summary', 'summary_metrics', 'core_collection_metrics'):
            data[field] = {**payloads['metrics'].get(field, {}), **{key: value for key, value in data.get(field, {}).items() if value is not None}}
        components['metrics'] = source_result(component_state(previous_report, 'metrics'), status='success', count=1, attempted_at=attempted)
        components['metrics']['last_success_at'] = observed_at or now()
        data['last_success_at'] = components['metrics']['last_success_at']
        data['metric_observed_at'] = {key: data['last_success_at'] if observed.get(key) is not None else old.get('metric_observed_at', {}).get(key, component_state(previous_report, 'metrics').get('last_success_at')) for key in data['summary']}
        data['retained_metric_fields'] = [key for key in data['summary'] if observed.get(key) is None]
        payloads['metrics'] = data
        emit()

    try:
        save_metrics(read_profile_metrics(page, target))
    except Exception as exc:
        if isinstance(exc, CheckpointWriteError):
            raise
        fail('metrics', exc)
        if not isinstance(exc, AuthFailure) or exc.reason != 'profile_metrics_missing':
            return report, payloads

    observed = set()

    def batch(rows, complete):
        stamp = now()
        prior_rows = {record_key(row): row for row in payloads['publications']}
        fresh = []
        for row in rows:
            record = {**row, 'observed_at': row.get('observed_at') or stamp}
            prior = prior_rows.get(record_key(row), {})
            citation_stamps = dict(prior.get('citation_observed_at') or {})
            retained = set(prior.get('retained_citation_fields') or [])
            if row.get('wos_citations') is not None:
                citation_stamps['wos'] = record['observed_at']
                retained.discard('wos_citations')
            else:
                retained.add('wos_citations')
                previous_stamp = (citation_stamps['wos'] if 'wos' in citation_stamps else
                                  None if 'wos_citations' in (prior.get('retained_citation_fields') or [])
                                  else prior.get('observed_at'))
                if previous_stamp:
                    citation_stamps['wos'] = previous_stamp
            if citation_stamps:
                record['citation_observed_at'] = citation_stamps
            record['retained_citation_fields'] = sorted(retained)
            fresh.append(record)
        observed.update(record_key(row) for row in fresh)
        payloads['publications'] = merge_records(payloads['publications'], fresh, record_key)
        # merge_records keeps empty fields by design; a confirmed count must
        # nevertheless clear a previous metadata-only API observation's marker.
        fresh_by_key = {record_key(row): row for row in fresh}
        for row in payloads['publications']:
            if record_key(row) in fresh_by_key:
                row['retained_citation_fields'] = fresh_by_key[record_key(row)]['retained_citation_fields']
        components['publications'] = source_result(component_state(previous_report, 'publications'), status='success' if complete else 'partial', count=len(payloads['publications']), reason=None if complete else 'pagination_in_progress', attempted_at=attempted)
        components['publications'].update(observed_count=len(observed), last_observation_at=stamp)
        components['publications']['scope'] = 'web_of_science_core_collection'
        emit()

    if cv_export is not None:
        from parse_wos_cv import parse_wos_cv, CvParseError
        from wos_cv_export import CVExportError, CV_STAGES
        try:
            # Export follows the site's own UI and belongs to this login. The
            # profile metrics above have already been durably checkpointed.
            document = cv_export(page)
            if not isinstance(document, dict) or document.get('author', {}).get('rid') != str(target):
                raise AuthFailure('wrong_author_profile')
            result = parse_wos_cv(document, researcher_id=str(target), observed_at=now())
            cv_state, cv_payload = result['report'], result['payloads']
            cv_metrics = cv_payload['metrics']
            cv_core_total = cv_metrics.get('summary', {}).get('publications')
            profile_core_total = payloads['metrics'].get('summary', {}).get('publications')
            if components['metrics'].get('complete') and cv_core_total is not None and cv_core_total != profile_core_total:
                raise ValueError('cv_profile_count_changed')
            if not components['metrics'].get('complete') and component_state(cv_state, 'metrics').get('complete'):
                save_metrics(cv_metrics, observed_at=component_state(cv_state, 'metrics')['last_success_at'])
            complete = component_state(cv_state, 'publications').get('complete') is True
            cv_attempt = {'status': 'success' if complete else 'partial'}
            if not complete:
                cv_attempt['reason'] = component_state(cv_state, 'publications').get('reason')
            publication_transport = 'cv_export'
            batch(cv_payload['publications'], complete)
            if complete:
                return report, payloads
        except CheckpointWriteError:
            raise
        except AuthFailure as exc:
            cv_attempt = {'status': 'blocked', 'reason': exc.reason}
            fail('publications', exc)
            return report, payloads
        except (CvParseError, CVExportError) as exc:
            cv_attempt = {'status': 'error', 'reason': exc.reason}
            stage = getattr(exc, 'stage', None)
            if isinstance(stage, str) and stage in CV_STAGES:
                cv_attempt['stage'] = stage
        except Exception:
            # Raw export exceptions can include download URLs or CV sections.
            cv_attempt = {'status': 'error', 'reason': 'cv_export_unavailable'}

    try:
        if cv_export is not None:
            # Re-establish account, author and challenge checks after leaving
            # the export page; a blocked export never enters this fallback.
            target_profile_html(page, target)
        publication_transport = 'rendered_profile'
        select_core_collection(page)
        collect_publications(page, on_batch=batch)
    except Exception as exc:
        if isinstance(exc, CheckpointWriteError):
            raise
        fail('publications', exc)
    return report, payloads


def initial_browser_context(browser, options, mode='restore'):
    """Choose the initial login path once, before visiting any provider page."""
    if mode == 'fresh_orcid':
        return browser.new_context(**options), {'status': 'skipped', 'reason': 'fresh_orcid_login_requested'}
    if mode == 'restore':
        from browser_sessions import restore_context
        return restore_context(browser, 'wos', **options)
    raise AuthFailure('wos_auth_mode_invalid')


def main():
    from browser_sessions import checkpoint_session, create_wos_reauthentication_context, SessionError
    from wos_cv_export import fetch_wos_cv
    maintenance = os.environ.get('BROWSER_SESSION_MAINTENANCE') == '1'
    requested_mode = os.environ.get('WOS_AUTH_MODE', 'restore')
    authentication_mode = requested_mode if requested_mode in AUTH_MODES else 'invalid'
    checkpoint_path = REPORT.parent / 'collection_checkpoint.json'
    existing = load_checkpoint(checkpoint_path)
    previous = read_json(OUT, {})
    previous_report = existing['report'] if existing else read_json(REPORT, {})
    payloads = existing['payloads'] if existing else {'metrics': {key: value for key, value in previous.items() if key not in {'records', 'records_count_on_page'}}, 'publications': previous.get('records', []), 'details': {}}
    if not previous_report.get('last_success_at'):
        snapshots = sorted(Path('data/snapshots/wos').glob(f'author_profile_{RESEARCHER_ID}_????????T??????Z.html'))
        previous_report['last_success_at'] = previous.get('last_success_at') or snapshot_time(snapshots[-1] if snapshots else None)
    report, stage, session, restored = {}, 'initialization', {}, {}
    authentication = None
    page = None
    persisted = False
    saved_components = set()

    def persist(state, data):
        nonlocal persisted, session
        successful = {(name, result.get('last_success_at')) for name, result in state.get('components', {}).items() if result.get('status') == 'success' and result.get('complete')}
        if successful - saved_components:
            try:
                assert_no_challenge(page)
                authenticated = wos_authenticated(page)
            except Exception:
                authenticated = False
            session = checkpoint_session(context, 'wos', authenticated=authenticated, target_verified=True, target_id=RESEARCHER_ID, verified_page=page)
            saved_components.update(successful)
        state.update(authentication=authentication, authentication_mode=authentication_mode,
                     session_checkpoint=session, session_restore=restored)
        login_evidence = safe_wos_login_evidence(getattr(page, '_wos_login_evidence', None))
        if login_evidence:
            state['authentication_evidence'] = login_evidence
        entry_observation = getattr(page, '_profile_entry_observation', None)
        if isinstance(entry_observation, dict):
            state['profile_entry_observation'] = entry_observation
        write_checkpoint(checkpoint_path, state, data)
        persisted = True
        materialize_checkpoint(checkpoint_path, 'wos')

    try:
        if authentication_mode == 'invalid':
            stage = 'configuration'
            raise AuthFailure('wos_auth_mode_invalid')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {'headless': os.environ.get('WOS_BROWSER_HEADLESS', 'true').lower() not in {'0', 'false', 'no'}, 'args': ['--disable-dev-shm-usage', '--no-sandbox']}
            if os.environ.get('WOS_BROWSER_CHANNEL'):
                launch['channel'] = os.environ['WOS_BROWSER_CHANNEL']
            browser = playwright.chromium.launch(**launch)
            options = {'locale': 'en-US', 'timezone_id': 'Europe/Moscow', 'viewport': {'width': 1440, 'height': 1100}}
            context, restored = initial_browser_context(browser, options, authentication_mode)

            def replace_expired_context():
                nonlocal context
                try:
                    context = create_wos_reauthentication_context(browser, context, **options)
                except SessionError as exc:
                    raise AuthFailure(str(exc)) from None
                verify_browser_egress(context)
                return context

            stage = 'route_verification'
            verify_browser_egress(context)
            stage = 'login'
            page, authentication = authenticated_page(context, restored, fresh_context=replace_expired_context)
            session = checkpoint_session(context, 'wos', authenticated=True, target_verified=True, target_id=RESEARCHER_ID, verified_page=page)
            if maintenance:
                report = {'provider': 'wos', 'status': 'success' if session.get('status') == 'checkpointed' else 'error', 'reason': session.get('reason'), 'authentication': authentication, 'target_verified': True, 'session_checkpoint': session, 'session_restore': restored, 'attempted_at': now()}
            else:
                stage = 'collection'
                from provider_auth import wos_cv_export_allowed
                exporter = fetch_wos_cv if wos_cv_export_allowed(RESEARCHER_ID) else None
                report, payloads = collect_from_page(page, previous=payloads, previous_report=previous_report, on_checkpoint=persist, cv_export=exporter)
                report.update(authentication=authentication, session_checkpoint=session)
            entry_observation = getattr(page, '_profile_entry_observation', None)
            if isinstance(entry_observation, dict):
                report['profile_entry_observation'] = entry_observation
            try:
                context.close()
                browser.close()
            except Exception:
                report['cleanup_reason'] = 'browser_close_failed'
    except Exception as exc:
        init = browser_initialization_diagnostics(exc) if stage == 'initialization' else None
        reason = init['reason'] if init else getattr(exc, 'reason', type(exc).__name__)
        current = load_checkpoint(checkpoint_path) if persisted else None
        if maintenance:
            report = {'provider': 'wos', 'status': 'blocked' if isinstance(exc, AuthFailure) else 'error', 'reason': reason, 'attempted_at': now()}
        elif current:
            report, payloads = current['report'], current['payloads']
            report.update(status='partial', complete=False, reason=reason)
        else:
            report = source_result(previous_report, status='blocked' if isinstance(exc, AuthFailure) else 'error', count=len(payloads['publications']), reason=reason)
            report['components'] = {key: source_result(component_state(previous_report, key), status=report['status'], reason=reason) for key in ('metrics', 'publications')}
        report.update(stage=stage, session_checkpoint=session, session_restore=restored)
        for field in ('diagnostics', 'verification_evidence', 'authentication_evidence', 'profile_diagnostics', 'profile_entry_observation'):
            if getattr(exc, field, None):
                report[field] = getattr(exc, field)
        if init:
            report['initialization'] = init
    report['authentication_mode'] = authentication_mode
    login_evidence = safe_wos_login_evidence(getattr(page, '_wos_login_evidence', None))
    if login_evidence:
        report['authentication_evidence'] = login_evidence
    if maintenance:
        output = os.environ.get('BROWSER_SESSION_REPORT_DIR')
        if output:
            write_json(Path(output) / 'wos.json', report)
    else:
        persist(report, payloads)
    print(json.dumps(report))
    return 0 if (report.get('status') == 'success' if maintenance else report.get('complete')) else 2


if __name__ == '__main__':
    raise SystemExit(main())
