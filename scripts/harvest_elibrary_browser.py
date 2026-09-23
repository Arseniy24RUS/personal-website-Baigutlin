#!/usr/bin/env python3
"""Authenticated eLibrary profile, all list pages and item details in one context."""
from __future__ import annotations

import json
import copy
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

from parse_elibrary_author_profile import parse_elibrary_author_profile_html
from parse_elibrary_author_items import parse_elibrary_author_items
from harvest_elibrary_item_details import parse_detail_html, needs_details, item_id_from_pub
from provider_auth import AuthFailure, login_elibrary, assert_no_challenge, elibrary_authenticated, verify_browser_egress, browser_initialization_diagnostics, safe_browser_diagnostics
from source_health import read_json, write_json, source_result, merge_records, now, snapshot_time, load_checkpoint, write_checkpoint, materialize_checkpoint, component_state

AUTHOR_ID = os.environ.get('ELIBRARY_AUTHOR_ID', '1170779')
PROFILE_URL = f'https://elibrary.ru/author_profile.asp?id={AUTHOR_ID}'
ITEMS_URL = f'https://elibrary.ru/author_items.asp?authorid={AUTHOR_ID}&pubrole=100&show_refs=1&pubcat=risc'
PROFILE_OUT = Path(os.environ.get('ELIBRARY_PROFILE_OUT', 'data/elibrary/profile_metrics.json'))
ITEMS_OUT = Path(os.environ.get('ELIBRARY_ITEMS_OUT', 'data/processed/elibrary_publications.json'))
DETAILS_OUT = Path(os.environ.get('ELIBRARY_ITEM_DETAILS_OUT', 'data/elibrary/item_details.json'))
REPORT = Path(os.environ.get('ELIBRARY_BROWSER_REPORT', 'data/elibrary/browser_fetch_report.json'))
WAIT_SEC = int(os.environ.get('ELIBRARY_BROWSER_WAIT_SEC', '90'))


class CheckpointWriteError(RuntimeError):
    pass


def wait_ready(page, selector):
    deadline = time.monotonic() + WAIT_SEC
    while time.monotonic() < deadline:
        assert_no_challenge(page)
        if page.locator(selector).count() or (selector == 'tr[id^="arw"]' and list_total(page.content()) == 0):
            if not elibrary_authenticated(page):
                raise AuthFailure('session_expired')
            return page.content()
        page.wait_for_timeout(1000)
    raise AuthFailure('page_structure_changed')


def list_total(html):
    text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    match = re.search(r'(?:Всего\s+)?найден(?:о|а|ы)?\s*:?\s*([\d\s]+)\s+публикац', text, re.I)
    return int(re.sub(r'\s+', '', match.group(1))) if match else None


def parse_items(html, temp):
    path = Path(temp) / 'items.html'
    path.write_text(html, encoding='utf-8')
    return parse_elibrary_author_items(str(path))


def collect_items(page, temp, *, target=AUTHOR_ID, on_batch=None):
    url = f'https://elibrary.ru/author_items.asp?authorid={target}&pubrole=100&show_refs=1&pubcat=risc'
    page.goto(url, wait_until='domcontentloaded', timeout=90000)
    result = []
    seen = set()
    total = None
    for number in range(1, 1001):
        html = wait_ready(page, 'tr[id^="arw"]')
        batch = parse_items(html, temp)
        observed_total = list_total(html)
        if observed_total == 0 and not batch and not result:
            if on_batch:
                on_batch([], True)
            return []
        if not batch:
            raise AuthFailure('empty_publication_list')
        current = {row['elibrary_item_id'] for row in batch}
        if not current - seen:
            raise AuthFailure('pagination_loop')
        seen.update(current)
        result = merge_records(result, batch, lambda row: row.get('elibrary_item_id'))
        total = observed_total if total is None else total
        if total is None:
            raise AuthFailure('publication_total_missing')
        if observed_total != total or len(result) > total:
            raise AuthFailure('publication_total_changed')
        stamp = now()
        for row in batch:
            row['observed_at'] = stamp
        result = merge_records(result, batch, lambda row: row.get('elibrary_item_id'))
        if on_batch:
            on_batch(batch, len(result) == total)
        if len(result) >= total:
            return result
        # The site's own paging form preserves the author, category and filters.
        available = page.evaluate("() => typeof goto_page === 'function' && !!document.querySelector('[name=pagenum]')")
        if not available:
            raise AuthFailure('pagination_control_changed')
        page.evaluate('(n) => goto_page(n)', number + 1)
        page.wait_for_load_state('domcontentloaded', timeout=90000)
        page.wait_for_timeout(1200)
    raise AuthFailure('pagination_limit')


def collect_details(page, records, previous, *, on_item=None):
    payload = copy.deepcopy(previous) if isinstance(previous, dict) and 'items' in previous else {'items': {}}
    cached = payload['items']
    # Retry missing fields; valid old entries are never removed when an item fails.
    todo = [row for row in records if needs_details(row, cached)]
    limit = int(os.environ.get('ELIBRARY_ITEM_DETAILS_LIMIT', '100'))
    failed = 0
    completed = 0
    stopped = {}
    for row in todo[:limit]:
        item_id = item_id_from_pub(row)
        try:
            response = page.goto(f'https://elibrary.ru/item.asp?id={item_id}', wait_until='domcontentloaded', timeout=90000)
            assert_no_challenge(page)
            if not response or response.status != 200 or not elibrary_authenticated(page):
                raise AuthFailure('item_unavailable')
            html = page.content()
            # Check bibliographic evidence rather than accept a 200 login page.
            soup = BeautifulSoup(html, 'html.parser')
            title = soup.select_one('#title, .bigtext, meta[name="citation_title"]')
            if not title or len(soup.get_text(' ', strip=True)) < 500:
                raise AuthFailure('item_structure_changed')
            parsed = parse_detail_html(html)
            # Authentication/session sidebars must never appear in published data.
            parsed.pop('raw_text_excerpt', None)
            old = cached.get(item_id, {}).get('parsed', {})
            optional = ('venue', 'publisher', 'volume', 'issue', 'pages', 'doi', 'isbn', 'issn')
            stamp = now()
            cached[item_id] = {'fetched_at': stamp, 'observed_at': stamp, 'status': 'success', 'observed_absent_fields': [key for key in optional if not parsed.get(key)], 'url': f'https://elibrary.ru/item.asp?id={item_id}', 'parsed': {**old, **{k: v for k, v in parsed.items() if v is not None and v != ''}}}
            completed += 1
            if on_item:
                on_item(payload, completed, max(0, len(todo) - completed))
        except AuthFailure as exc:
            failed += 1
            if exc.reason in {'session_expired', 'human_verification_required', 'mfa_required', 'ip_blocked'}:
                stopped['reason'] = exc.reason
                if getattr(exc, 'verification_evidence', None):
                    stopped['verification_evidence'] = exc.verification_evidence
                break
        except Exception:
            failed += 1
        page.wait_for_timeout(int(float(os.environ.get('ELIBRARY_ITEM_DETAILS_DELAY_SEC', '1.5')) * 1000))
    payload.update({'generated_at': now(), 'schema': 'elibrary_item_details/v1'})
    return payload, {'fetched': completed, 'failed': failed, 'pending': max(0, len(todo) - completed), **stopped}


def target_profile_html(page, target=AUTHOR_ID):
    address = urlparse(page.url)
    if not (address.path.endswith('/author_profile.asp') and parse_qs(address.query).get('id') == [str(target)]):
        page.goto(f'https://elibrary.ru/author_profile.asp?id={target}', wait_until='domcontentloaded', timeout=90000)
    deadline = time.monotonic() + WAIT_SEC
    while time.monotonic() < deadline:
        assert_no_challenge(page)
        html = page.content()
        if elibrary_authenticated(page) and 'ОБЩИЕ ПОКАЗАТЕЛИ' in html and 'Индекс Хирша' in html:
            address = urlparse(page.url)
            if address.hostname not in {'elibrary.ru', 'www.elibrary.ru'} or not address.path.endswith('/author_profile.asp') or parse_qs(address.query).get('id') != [str(target)]:
                raise AuthFailure('wrong_author_profile')
            return html
        if re.search(r'Незарегистрированный пользователь|Вы не авторизованы', page.locator('body').inner_text(), re.I):
            raise AuthFailure('session_expired')
        page.wait_for_timeout(1000)
    raise AuthFailure('profile_not_authenticated_or_changed')


def authenticated_page(context, session_info, target=AUTHOR_ID, *, fresh_context=None):
    """Restore first; only a positively expired session permits one fresh login."""
    if session_info.get('status') == 'invalid':
        raise AuthFailure('invalid_session_checkpoint')

    def verify(page):
        try:
            return target_profile_html(page, target)
        except Exception as exc:
            failure = exc if isinstance(exc, AuthFailure) else AuthFailure(type(exc).__name__)
            failure.diagnostics = safe_browser_diagnostics(context)
            raise failure from None

    if session_info.get('status') == 'restored':
        page = context.new_page()
        try:
            verify(page)
            return page, 'existing_session_verified'
        except AuthFailure as exc:
            if exc.reason != 'session_expired':
                raise
            page.close()
            if fresh_context is None:
                raise
            context = fresh_context()
    page = login_elibrary(context)
    verify(page)
    return page, 'fresh_password_login'


def collect_from_page(page, target=AUTHOR_ID, previous=None, previous_report=None, *, on_checkpoint=None, temp=None):
    """Extract from a borrowed, authenticated page; never own/close its browser."""
    payloads = copy.deepcopy(previous or {'metrics': {}, 'publications': [], 'details': {}})
    for key, default in [('metrics', {}), ('publications', []), ('details', {})]:
        payloads.setdefault(key, default)
    previous_report = previous_report or {}
    attempted = now()
    components = {key: source_result(component_state(previous_report, key), status='blocked', reason='not_attempted', attempted_at=attempted) for key in ('metrics', 'publications', 'details')}
    report = {}

    def emit():
        complete = all(state.get('complete') for state in components.values())
        successful = any(state.get('complete') or state.get('observed_count') for state in components.values())
        failed = next((state for state in components.values() if not state.get('complete')), {})
        report.clear()
        report.update(source_result(previous_report, status='success' if complete else ('partial' if successful else failed.get('status', 'error')), count=len(payloads['publications']), reason=None if complete else failed.get('reason'), attempted_at=attempted))
        report.update(components=components, target_author_id=str(target), generated_at=now())
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
        if getattr(exc, 'verification_evidence', None):
            state['verification_evidence'] = exc.verification_evidence
        components[key] = state
        emit()

    try:
        profile = parse_elibrary_author_profile_html(target_profile_html(page, target))
        required = ('publications_rinc', 'citations_rinc', 'h_index_rinc', 'publications_elibrary', 'citations_elibrary', 'h_index_elibrary')
        if any(profile.get('summary', {}).get(key) is None for key in required):
            raise AuthFailure('profile_metrics_missing')
        state = source_result(component_state(previous_report, 'metrics'), status='success', count=1, attempted_at=attempted)
        old = payloads['metrics']
        observed = dict(profile.get('summary', {}))
        profile = {**old, **profile, 'authorid': str(target)}
        profile['summary'] = {**old.get('summary', {}), **{key: value for key, value in observed.items() if value is not None}}
        profile['metric_observed_at'] = {key: state['last_success_at'] if observed.get(key) is not None else old.get('metric_observed_at', {}).get(key, component_state(previous_report, 'metrics').get('last_success_at')) for key in profile['summary']}
        profile['retained_metric_fields'] = [key for key in profile['summary'] if observed.get(key) is None]
        profile['last_success_at'] = state['last_success_at']
        payloads['metrics'] = profile
        components['metrics'] = state
        emit()
    except Exception as exc:
        if isinstance(exc, CheckpointWriteError):
            raise
        fail('metrics', exc)
        # An identity/session/challenge failure forbids further page interaction.
        if not isinstance(exc, AuthFailure) or exc.reason != 'profile_metrics_missing':
            return report, payloads

    observed_ids = set()

    def batch(rows, complete):
        stamp = now()
        fresh = [{**row, 'source': 'elibrary_authenticated_browser', 'observed_at': row.get('observed_at') or stamp} for row in rows]
        observed_ids.update(str(row['elibrary_item_id']) for row in fresh)
        payloads['publications'] = merge_records(payloads['publications'], fresh, lambda row: row.get('elibrary_item_id'))
        components['publications'] = source_result(component_state(previous_report, 'publications'), status='success' if complete else 'partial', count=len(payloads['publications']), reason=None if complete else 'pagination_in_progress', attempted_at=attempted)
        components['publications'].update(observed_count=len(observed_ids), last_observation_at=stamp)
        emit()

    try:
        if temp is None:
            with tempfile.TemporaryDirectory(prefix='elibrary-items-', dir=os.environ.get('RUNNER_TEMP')) as directory:
                collect_items(page, directory, target=target, on_batch=batch)
        else:
            collect_items(page, temp, target=target, on_batch=batch)
    except Exception as exc:
        if isinstance(exc, CheckpointWriteError):
            raise
        fail('publications', exc)
        return report, payloads

    def item_checkpoint(details, completed, pending):
        payloads['details'] = copy.deepcopy(details)
        state = source_result(component_state(previous_report, 'details'), status='partial', count=len(details.get('items', {})), reason='details_in_progress', attempted_at=attempted)
        state.update(observed_count=completed, last_observation_at=now(), pending=pending)
        components['details'] = state
        emit()

    try:
        details, outcome = collect_details(page, payloads['publications'], payloads['details'], on_item=item_checkpoint)
        payloads['details'] = details
        done = outcome['pending'] == 0 and outcome['failed'] == 0
        state = source_result(component_state(previous_report, 'details'), status='success' if done else 'partial', count=len(details.get('items', {})), reason=None if done else outcome.get('reason', 'details_pending'), attempted_at=attempted)
        state.update(outcome, observed_count=outcome['fetched'])
        if outcome['fetched']:
            state['last_observation_at'] = now()
        components['details'] = state
        emit()
    except Exception as exc:
        if isinstance(exc, CheckpointWriteError):
            raise
        fail('details', exc)
    return report, payloads


def main():
    from browser_sessions import restore_context, checkpoint_session
    maintenance = os.environ.get('BROWSER_SESSION_MAINTENANCE') == '1'
    checkpoint_path = REPORT.parent / 'collection_checkpoint.json'
    existing = load_checkpoint(checkpoint_path)
    previous_report = existing['report'] if existing else read_json(REPORT, {})
    payloads = existing['payloads'] if existing else {'metrics': read_json(PROFILE_OUT, {}), 'publications': read_json(ITEMS_OUT, []), 'details': read_json(DETAILS_OUT, {})}
    if not previous_report.get('last_success_at'):
        legacy = read_json('data/elibrary/profile_metrics_fetch_report.json', {})
        previous_report['last_success_at'] = payloads['metrics'].get('last_success_at') or snapshot_time(legacy.get('snapshot_path'))
    report, stage, session, restored = {}, 'initialization', {}, {}
    authentication = None
    persisted = False
    saved_components = set()

    def persist(state, data):
        nonlocal persisted, session
        successful = {(name, result.get('last_success_at')) for name, result in state.get('components', {}).items() if result.get('status') == 'success' and result.get('complete')}
        if successful - saved_components:
            # A successful component has just verified this page/context. Failed
            # or challenged components never trigger a session replacement.
            try:
                assert_no_challenge(page)
                authenticated = elibrary_authenticated(page)
            except Exception:
                authenticated = False
            session = checkpoint_session(context, 'elibrary', authenticated=authenticated, target_verified=True, target_id=AUTHOR_ID, verified_page=page)
            saved_components.update(successful)
        state.update(authentication=authentication, session_checkpoint=session, session_restore=restored)
        write_checkpoint(checkpoint_path, state, data)
        persisted = True
        materialize_checkpoint(checkpoint_path, 'elibrary')

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {'headless': os.environ.get('ELIBRARY_BROWSER_HEADLESS', 'true').lower() not in {'false', '0', 'no'}, 'args': ['--disable-dev-shm-usage', '--no-sandbox']}
            if os.environ.get('ELIBRARY_BROWSER_CHANNEL'):
                launch['channel'] = os.environ['ELIBRARY_BROWSER_CHANNEL']
            browser = playwright.chromium.launch(**launch)
            options = {'locale': 'ru-RU', 'timezone_id': 'Europe/Moscow', 'viewport': {'width': 1366, 'height': 900}}
            context, restored = restore_context(browser, 'elibrary', **options)

            def replace_expired_context():
                nonlocal context
                context.close()
                context = browser.new_context(**options)
                verify_browser_egress(context)
                return context

            stage = 'route_verification'
            verify_browser_egress(context)
            stage = 'login'
            page, authentication = authenticated_page(context, restored, fresh_context=replace_expired_context)
            session = checkpoint_session(context, 'elibrary', authenticated=True, target_verified=True, target_id=AUTHOR_ID, verified_page=page)
            if maintenance:
                report = {'provider': 'elibrary', 'status': 'success' if session.get('status') == 'checkpointed' else 'error', 'reason': session.get('reason'), 'authentication': authentication, 'target_verified': True, 'session_checkpoint': session, 'session_restore': restored, 'attempted_at': now()}
            else:
                stage = 'collection'
                report, payloads = collect_from_page(page, previous=payloads, previous_report=previous_report, on_checkpoint=persist)
                report.update(authentication=authentication, session_checkpoint=session)
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
            report = {'provider': 'elibrary', 'status': 'blocked' if isinstance(exc, AuthFailure) else 'error', 'reason': reason, 'attempted_at': now()}
        elif current:
            report, payloads = current['report'], current['payloads']
            report.update(status='partial', complete=False, reason=reason)
        else:
            report = source_result(previous_report, status='blocked' if isinstance(exc, AuthFailure) else 'error', count=len(payloads['publications']), reason=reason)
            report['components'] = {key: source_result(component_state(previous_report, key), status=report['status'], reason=reason) for key in ('metrics', 'publications', 'details')}
        report.update(stage=stage, session_checkpoint=session, session_restore=restored)
        for field in ('diagnostics', 'verification_evidence', 'authentication_evidence'):
            if getattr(exc, field, None):
                report[field] = getattr(exc, field)
        if init:
            report['initialization'] = init
    if maintenance:
        output = os.environ.get('BROWSER_SESSION_REPORT_DIR')
        if output:
            write_json(Path(output) / 'elibrary.json', report)
    else:
        persist(report, payloads)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report.get('status') == 'success' if maintenance else report.get('complete')) else 2


if __name__ == '__main__':
    raise SystemExit(main())
