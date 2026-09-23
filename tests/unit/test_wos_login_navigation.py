"""Fixed navigation diagnostics; synthetic events and routed browser only."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth


class Context:
    def __init__(self):
        self.pages, self.listeners = [], {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in list(self.listeners.get(event, [])):
            callback(value)

    def page(self, url='about:blank'):
        page = MagicMock()
        page.url, page.context = url, self
        page.is_closed.return_value = False
        page.main_frame = SimpleNamespace(page=page, parent_frame=None)
        self.pages.append(page)
        return page


def request(page, url='https://access.clarivate.com/callback?SID=private-token', **changes):
    result = SimpleNamespace(frame=page.main_frame, url=url, resource_type='document',
        is_navigation_request=lambda: True, failure='net::ERR_CONNECTION_RESET private-token')
    result.__dict__.update(changes)
    return result


class LoginNavigationTests(unittest.TestCase):
    def test_only_attempt_main_documents_are_observed(self):
        context, evidence = Context(), {'stage': 'orcid_response'}
        old = context.page()
        observer = auth._WosLoginNavigationObserver(context, evidence)
        page = context.page()
        other = Context().page()
        iframe = SimpleNamespace(page=page, parent_frame=page.main_frame)
        observer.start()
        for excluded in (request(old), request(other), request(page, resource_type='script'),
                         request(page, resource_type='xhr'), request(page, is_navigation_request=lambda: False),
                         request(page, frame=iframe)):
            context.emit('requestfailed', excluded)
            context.emit('response', SimpleNamespace(request=excluded, status=503))
        self.assertNotIn('navigation_failures', evidence)
        for url, expected in (('https://www.webofscience.com/private?SID=private-token', 'wos'),
                              ('https://signin.clarivate.com/private', 'clarivate'),
                              ('https://orcid.org/private', 'orcid'),
                              ('https://unexpected.invalid/private', 'other')):
            context.emit('requestfailed', request(page, url))
            self.assertEqual(evidence['navigation_failures'][-1]['provider'], expected)
        context.emit('response', SimpleNamespace(request=request(page), status=200))
        context.emit('response', SimpleNamespace(request=request(page), status=429))
        self.assertEqual(evidence['navigation_failure_count'], 5)
        self.assertEqual(evidence['navigation_failures'][-1],
            {'kind': 'http_error', 'provider': 'clarivate', 'stage': 'orcid_response', 'http_status': 429})
        self.assertNotIn('private', json.dumps(auth.safe_wos_login_evidence(evidence)))
        observer.stop()
        context.emit('requestfailed', request(page))
        self.assertEqual(evidence['navigation_failure_count'], 5)

    def test_event_list_is_capped_and_malformed_schema_never_leaks_or_raises(self):
        evidence = {'stage': 'orcid_response'}
        for _ in range(12):
            auth._record_navigation_failure(evidence, 'request_failed', 'wos', code='ERR_ABORTED')
        safe = auth.safe_wos_login_evidence(evidence)
        self.assertEqual(len(safe['navigation_failures']), 8)
        self.assertEqual(safe['navigation_failure_count'], 12)
        for field in ('kind', 'provider', 'stage', 'network_error_code', 'http_status'):
            for malformed in ([], {}, None, True, 'private-value'):
                item = {'kind': 'request_failed', 'provider': 'other', 'stage': 'homepage',
                        'network_error_code': 'ERR_FAILED', 'http_status': 503, field: malformed,
                        'url': 'https://private.invalid/?SID=private-token', 'body': 'private'}
                with self.subTest(field=field, malformed=malformed):
                    value = auth.safe_wos_login_evidence({'navigation_failures': [item]})
                    self.assertNotIn('private', json.dumps(value))
        self.assertEqual(auth._network_error_code('ERR_UNRECOGNIZED_PRIVATE'), 'unknown')

    def test_internal_error_page_is_failure_without_allowing_origin_or_ui_actions(self):
        context, evidence = Context(), {'stage': 'orcid_response', 'submit_clicked': True}
        page = context.page('chrome-error://chromewebdata/')
        page.locator.return_value.inner_text.return_value = 'private SID=private-token ERR_TOO_MANY_REDIRECTS'
        with self.assertRaisesRegex(auth.AuthFailure, '^login_navigation_failed$') as caught:
            auth._wos_login_page(context, (), evidence)
        safe = caught.exception.authentication_evidence
        self.assertTrue(safe['browser_error_page_observed'])
        self.assertEqual(safe['navigation_failures'][-1]['network_error_code'], 'ERR_TOO_MANY_REDIRECTS')
        self.assertEqual(safe['navigation_failures'][-1]['provider'], 'browser')
        self.assertNotIn('private', json.dumps(safe))
        page.goto.assert_not_called()
        page.keyboard.press.assert_not_called()
        page.locator.return_value.click.assert_not_called()
        self.assertNotIn('chromewebdata', auth.WOS_LOGIN_HOSTS)

    def test_unreadable_error_page_records_unknown_but_unknown_web_origin_stays_blocked(self):
        context = Context()
        page = context.page('chrome-error://chromewebdata/')
        page.locator.return_value.inner_text.side_effect = RuntimeError('private-body-error')
        with self.assertRaises(auth.AuthFailure) as caught:
            auth._wos_login_page(context, (), {'stage': 'orcid_response'})
        self.assertEqual(caught.exception.authentication_evidence['navigation_failures'][-1]['network_error_code'], 'unknown')
        page.url = 'https://unexpected.invalid/?SID=private-token'
        page.locator.reset_mock()
        with self.assertRaisesRegex(auth.AuthFailure, '^unexpected_login_origin$'):
            auth._wos_login_page(context, ())
        page.locator.assert_not_called()

    def test_direct_goto_fatal_error_is_classified_but_abort_is_not_promoted(self):
        from playwright.sync_api import Error as PlaywrightError
        page = MagicMock()
        page.url = 'about:blank'
        evidence = {'stage': 'homepage'}
        page.goto.side_effect = PlaywrightError('Page.goto: net::ERR_NAME_NOT_RESOLVED at https://private.invalid/?SID=private-token')
        with self.assertRaisesRegex(auth.AuthFailure, '^login_navigation_failed$'):
            auth._goto_wos_login(page, 'https://www.webofscience.com/', evidence, timeout=90000)
        self.assertEqual(evidence['navigation_failures'][-1]['network_error_code'], 'ERR_NAME_NOT_RESOLVED')
        self.assertNotIn('private', json.dumps(evidence))
        abort = PlaywrightError('Page.goto: net::ERR_ABORTED private-token')
        page.goto.side_effect = abort
        with self.assertRaises(PlaywrightError) as caught:
            auth._goto_wos_login(page, 'https://www.webofscience.com/', evidence, timeout=90000)
        self.assertIs(caught.exception, abort)

    def test_observers_start_before_login_and_stop_after_success_or_unrelated_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                context = Context()
                def login(ctx, profile, timeout, evidence, *, navigation=None):
                    self.assertTrue(ctx.listeners['requestfailed'])
                    page = ctx.page()
                    evidence['stage'] = 'orcid_response'
                    ctx.emit('requestfailed', request(page, failure='net::ERR_ABORTED private-token'))
                    if fail:
                        raise RuntimeError('private later unrelated error')
                    return page
                with patch.object(auth, '_login_wos', side_effect=login), patch.object(auth, 'safe_browser_diagnostics', return_value=[]):
                    if fail:
                        with self.assertRaisesRegex(auth.AuthFailure, '^RuntimeError$') as caught:
                            auth.login_wos(context, 'https://www.webofscience.com/profile')
                        evidence = caught.exception.authentication_evidence
                    else:
                        evidence = auth.login_wos(context, 'https://www.webofscience.com/profile')._wos_login_evidence
                self.assertEqual(evidence['navigation_failures'][0]['network_error_code'], 'ERR_ABORTED')
                self.assertTrue(all(not callbacks for callbacks in context.listeners.values()))
                self.assertNotIn('private', json.dumps(evidence))


class LoginNavigationBrowserTests(unittest.TestCase):
    def test_routed_main_document_failure_has_safe_transport_evidence(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is not installed')
        with sync_playwright() as playwright:
            options = {'headless': True}
            if not Path(playwright.chromium.executable_path).exists():
                edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
                if not edge.exists():
                    self.skipTest('Install Playwright Chromium for local navigation fixtures')
                options['executable_path'] = str(edge)
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context()
                context.route('**/*', lambda route: route.abort('connectionrefused'))
                with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.invalid',
                                            'WOS_ORCID_PASSWORD': 'private-synthetic-password'}):
                    with self.assertRaisesRegex(auth.AuthFailure, '^login_navigation_failed$') as caught:
                        auth.login_wos(context, 'https://www.webofscience.com/wos/author/record/FIXTURE', timeout=3)
                evidence = caught.exception.authentication_evidence
                self.assertTrue(evidence['initial_profile_requested'])
                self.assertFalse(evidence['homepage_requested'])
                self.assertFalse(evidence['submit_clicked'])
                self.assertTrue(any(event.get('network_error_code') == 'ERR_CONNECTION_REFUSED'
                                    and event['provider'] == 'wos' for event in evidence['navigation_failures']))
                self.assertNotIn('private', json.dumps(evidence))
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
