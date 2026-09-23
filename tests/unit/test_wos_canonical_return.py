"""Strict probe eligibility and routed UI after a synthetic transport boundary.

Route.fulfill deliberately does not reproduce Chromium's malformed redirect
network error. The gate itself is tested without weakening production checks;
the browser fixtures inject that already-tested boundary, then exercise the
real verification-only flow, challenge guards and target authorization.
"""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth
import harvest_wos_authenticated as wos


class CanonicalReturnTests(unittest.TestCase):
    def fixture(self):
        page = MagicMock()
        page.url = 'chrome-error://chromewebdata/'
        evidence = {'stage': 'orcid_response', 'submit_clicked': True}
        navigation = SimpleNamespace(last_document_failure={
            'page': page, 'provider': 'clarivate', 'code': 'ERR_INVALID_REDIRECT'}, authorization_denied=False)
        failure = auth.AuthFailure('login_navigation_failed', authentication_evidence={
            'navigation_failures': [{'kind': 'browser_error', 'provider': 'browser', 'network_error_code': 'ERR_INVALID_REDIRECT'}]})
        responses = [{'response_observed': True, 'success': True, 'http_status': 200, 'reason': None}]
        return page, failure, evidence, navigation, responses

    def test_fixed_same_page_probe_once_uses_only_remaining_original_budget(self):
        args = self.fixture()
        with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
            self.assertTrue(auth._probe_canonical_wos_home(*args, deadline=12.5))
            goto.assert_called_once_with(args[0], 'https://www.webofscience.com/', args[2],
                                        wait_until='domcontentloaded', timeout=2500)
            self.assertTrue(args[2]['submit_clicked'])
            self.assertTrue(args[2]['canonical_home_probe_attempted'])
            self.assertTrue(args[2]['canonical_home_probe_loaded'])
            self.assertFalse(auth._probe_canonical_wos_home(*args, deadline=12.5))
            self.assertEqual(goto.call_count, 1)
        args[0].context.new_page.assert_not_called()
        args[0].context.add_cookies.assert_not_called()

    def test_every_required_proof_and_remaining_deadline_is_mandatory(self):
        for changed in ('wrong_page', 'not_error_page', 'other_failure', 'wrong_provider', 'old_error',
                        'missing_success', 'false_success', 'missing_response', 'non200', 'mfa',
                        'reactivation', 'http_denied', 'captcha', 'expired'):
            with self.subTest(changed=changed):
                page, failure, evidence, navigation, responses = self.fixture()
                if changed == 'wrong_page': navigation.last_document_failure['page'] = MagicMock()
                if changed == 'not_error_page': page.url = 'https://unexpected.invalid/'
                if changed == 'other_failure': navigation.last_document_failure['code'] = 'ERR_CONNECTION_RESET'
                if changed == 'wrong_provider': navigation.last_document_failure['provider'] = 'other'
                if changed == 'old_error': failure.authentication_evidence['navigation_failures'][-1]['network_error_code'] = 'ERR_CERT_INVALID'
                if changed == 'missing_success': responses.clear()
                if changed == 'false_success': responses[-1]['success'] = False
                if changed == 'missing_response': responses[-1]['response_observed'] = False
                if changed == 'non200': responses[-1]['http_status'] = 403
                if changed == 'mfa': responses[-1]['reason'] = 'mfa_required'
                if changed == 'reactivation': responses[-1]['reason'] = 'account_reactivation_required'
                if changed == 'http_denied': navigation.authorization_denied = True
                if changed == 'captcha': failure.reason = 'human_verification_required'
                with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
                    self.assertFalse(auth._probe_canonical_wos_home(page, failure, evidence, navigation, responses,
                                                                   deadline=10 if changed == 'expired' else 20))
                    goto.assert_not_called()
                self.assertNotIn('canonical_home_probe_attempted', evidence)

    def test_only_owned_main_document_denials_disable_the_probe(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                old = MagicMock()
                context = SimpleNamespace(pages=[old])
                evidence = {'stage': 'orcid_response'}
                observer = auth._WosLoginNavigationObserver(context, evidence)
                page = MagicMock()
                page.context = context
                page.main_frame = SimpleNamespace(page=page, parent_frame=None)
                old.context = context
                old.main_frame = SimpleNamespace(page=old, parent_frame=None)
                def request(frame, resource='document'):
                    return SimpleNamespace(frame=frame, resource_type=resource,
                        is_navigation_request=lambda: resource == 'document',
                        url='https://access.clarivate.com/callback', failure='net::ERR_INVALID_REDIRECT')
                for excluded in (request(old.main_frame), request(page.main_frame, 'xhr'),
                                 request(SimpleNamespace(page=page, parent_frame=page.main_frame))):
                    observer.response(SimpleNamespace(request=excluded, status=status))
                self.assertFalse(observer.authorization_denied)
                observer.failed(request(page.main_frame))
                self.assertIs(observer.last_document_failure['page'], page)
                observer.response(SimpleNamespace(request=request(page.main_frame), status=status))
                self.assertTrue(observer.authorization_denied)


class CanonicalReturnBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')
        cls.playwright = sync_playwright().start()
        options = {'headless': True}
        if not Path(cls.playwright.chromium.executable_path).exists():
            edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
            if not edge.exists():
                cls.playwright.stop()
                raise unittest.SkipTest('Install Playwright Chromium for canonical return fixtures')
            options['executable_path'] = str(edge)
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def flow(self, outcome):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        state = {'posts': 0, 'homepage': 0, 'initial_profile': 0, 'signin': 0, 'authorize': 0, 'profile': 0, 'requests': []}
        account = '''<button data-ta="wos-header-user_name" onclick="document.getElementById('menu').hidden=false">Fixture Researcher</button>
            <div id="menu" hidden><button role="menuitem">Sign out</button></div><p>FIXTURE-1</p>
            <script>document.addEventListener('keydown',event=>{if(event.key==='Escape')document.getElementById('menu').hidden=true;});</script>'''
        def route(item):
            request, url = item.request, item.request.url
            state['requests'].append(url)
            if url == 'https://www.webofscience.com/':
                state['homepage'] += 1
                if state['posts'] and outcome in {'authorized', 'delayed_account', 'wrong_rid'}:
                    body = account if outcome != 'delayed_account' else ('<div id="late" hidden>' + account
                        + '</div><script>setTimeout(()=>document.getElementById("late").hidden=false,1500)</script>')
                elif state['posts'] and outcome == 'challenge':
                    body = '<p>Please verify you are human</p><button onclick="location.href=\'https://fixture.invalid/forbidden\'">Continue</button>'
                elif state['posts'] and outcome == 'idp':
                    body = '<script>location.href="https://orcid.org/consent"</script>'
                else:
                    body = '<button onclick="location.href=\'https://access.clarivate.com/login\'">Sign in</button>'
            elif url == 'https://access.clarivate.com/login':
                state['signin'] += 1
                body = '<a href="https://orcid.org/signin">ORCID</a>'
            elif url == 'https://orcid.org/signin':
                body = '''<form onsubmit="event.preventDefault();fetch('/signin/auth.json',{method:'POST'}).then(response=>response.json()).then(()=>setTimeout(()=>location.href='https://access.clarivate.com/callback',500))">
                    <input id="username-input"><input type="password"><button id="signin-button" type="submit">Sign in to ORCID</button></form>'''
            elif url == 'https://orcid.org/signin/auth.json':
                state['posts'] += 1
                item.fulfill(status=200, content_type='application/json', body='{"success":true}')
                return
            elif url == 'https://access.clarivate.com/callback':
                body = '<p>Synthetic failed callback boundary</p>'
            elif url == 'https://orcid.org/consent':
                body = '<button onclick="location.href=\'https://fixture.invalid/authorize\'">Authorize access</button>'
            elif url == 'https://fixture.invalid/authorize':
                state['authorize'] += 1
                body = '<p>Unexpected consent submission</p>'
            elif '/wos/author/record/FIXTURE-1' in url:
                if state['posts']:
                    state['profile'] += 1
                    body = account.replace('FIXTURE-1', 'OTHER-2') if outcome == 'wrong_rid' else account
                else:
                    state['initial_profile'] += 1
                    body = '<button onclick="location.href=\'https://access.clarivate.com/login\'">Sign in</button>'
            else:
                body = '<p>Unexpected request</p>'
            item.fulfill(status=200, content_type='text/html', body='<body>' + body + '</body>')
        context.route('**/*', route)
        return context, state

    def attempt(self, outcome):
        context, state = self.flow(outcome)
        select = auth._wos_login_page
        real_time, authenticated = auth.time, auth.wos_authenticated
        clock = {'offset': 0.0, 'deadline': None, 'negative_proofs': 0, 'counts_at_expiry': None}
        # The unauthorized case tests verification-only behavior, not which DOM
        # read wins a race against the last millisecond of a real-time deadline.
        # Keep Playwright/stdlib clocks untouched. Expire this module's clock
        # only after its real challenge and negative account checks have run.
        test_time = (SimpleNamespace(monotonic=lambda: real_time.monotonic() + clock['offset'])
                     if outcome == 'unauthorized' else real_time)
        def account_proof(page):
            result = authenticated(page)
            if outcome == 'unauthorized' and clock['deadline'] is not None:
                self.assertFalse(result)
                clock['negative_proofs'] += 1
                clock['counts_at_expiry'] = tuple(state[key] for key in ('posts', 'signin', 'authorize', 'profile', 'homepage'))
                clock['offset'] = clock['deadline'] + 1.0 - real_time.monotonic()
            return result
        def failed_callback(ctx, existing, evidence):
            page = select(ctx, existing, evidence)
            if page is not None and page.url == 'https://access.clarivate.com/callback':
                auth._record_navigation_failure(evidence, 'request_failed', 'clarivate', code='ERR_INVALID_REDIRECT')
                auth._record_navigation_failure(evidence, 'browser_error', 'browser', code='ERR_INVALID_REDIRECT')
                raise auth.AuthFailure('login_navigation_failed', authentication_evidence=auth.safe_wos_login_evidence(evidence))
            return page
        def synthetic_transport_boundary(page, failure, evidence, navigation, responses, deadline):
            self.assertEqual(page.url, 'https://access.clarivate.com/callback')
            self.assertEqual(failure.reason, 'login_navigation_failed')
            self.assertTrue(responses[-1]['success'])
            self.assertEqual(responses[-1]['http_status'], 200)
            self.assertIsNone(responses[-1].get('reason'))
            self.assertFalse(evidence.get('canonical_home_probe_attempted'))
            remaining = deadline - auth.time.monotonic()
            self.assertGreater(remaining, 0)
            evidence['canonical_home_probe_attempted'] = True
            auth._goto_wos_login(page, 'https://www.webofscience.com/', evidence,
                                wait_until='domcontentloaded', timeout=min(90000, remaining * 1000))
            evidence['canonical_home_probe_loaded'] = True
            clock['deadline'] = deadline
            return True
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.invalid', 'WOS_ORCID_PASSWORD': 'synthetic-password'}), \
                patch.object(wos, 'WAIT_SEC', 15), patch.object(auth, '_wos_login_page', side_effect=failed_callback), \
                patch.object(auth, '_probe_canonical_wos_home', side_effect=synthetic_transport_boundary), \
                patch.object(auth, 'time', test_time), patch.object(auth, 'wos_authenticated', side_effect=account_proof):
            try:
                page, authentication = wos.authenticated_page(context, {'status': 'skipped'}, target='FIXTURE-1')
                result = page._wos_login_evidence
                self.assertEqual(authentication, 'fresh_orcid_login')
            except auth.AuthFailure as failure:
                result = failure
        self.assertEqual(state['posts'], 1)
        self.assertEqual(state['initial_profile'], 1)
        self.assertEqual(state['signin'], 1)
        self.assertEqual(state['authorize'], 0)
        if outcome == 'unauthorized':
            self.assertEqual(clock['negative_proofs'], 1)
            self.assertEqual(tuple(state[key] for key in ('posts', 'signin', 'authorize', 'profile', 'homepage')),
                             clock['counts_at_expiry'])
        self.assertTrue(all(urlparse(url).hostname in {'www.webofscience.com', 'access.clarivate.com', 'orcid.org'}
                            for url in state['requests']))
        return result, state

    def test_after_simulated_transport_boundary_home_requires_real_account_and_target(self):
        evidence, state = self.attempt('authorized')
        self.assertIsInstance(evidence, dict, getattr(evidence, 'reason', 'missing evidence'))
        self.assertEqual(state['homepage'], 1)
        self.assertEqual(state['profile'], 1)
        self.assertTrue(evidence['canonical_home_probe_attempted'])
        self.assertTrue(evidence['canonical_home_probe_loaded'])
        self.assertTrue(evidence['wos_session_confirmed'])
        self.assertTrue(evidence['submit_clicked'])
        self.assertTrue(any(row.get('network_error_code') == 'ERR_INVALID_REDIRECT' for row in evidence['navigation_failures']))
        self.assertNotIn('private', json.dumps(evidence))

    def test_after_simulated_boundary_late_account_menu_is_passively_awaited(self):
        evidence, state = self.attempt('delayed_account')
        self.assertIsInstance(evidence, dict, getattr(evidence, 'reason', 'missing evidence'))
        self.assertEqual(state['homepage'], 1)
        self.assertEqual(state['profile'], 1)
        self.assertTrue(evidence['wos_session_confirmed'])

    def test_after_simulated_boundary_authorized_wrong_rid_is_not_success(self):
        failure, state = self.attempt('wrong_rid')
        self.assertIsInstance(failure, auth.AuthFailure)
        self.assertEqual(failure.reason, 'profile_not_authenticated_or_changed')
        self.assertEqual(state['homepage'], 1)
        self.assertEqual(state['profile'], 1)
        self.assertTrue(failure.authentication_evidence['wos_session_confirmed'])

    def test_probe_never_restarts_signin_submits_consent_or_continues_past_challenge(self):
        for outcome, reason in (('unauthorized', 'wos_login_not_confirmed'),
                                ('idp', 'wos_login_not_confirmed'), ('challenge', 'human_verification_required')):
            with self.subTest(outcome=outcome):
                failure, state = self.attempt(outcome)
                self.assertIsInstance(failure, auth.AuthFailure)
                self.assertEqual(failure.reason, reason)
                self.assertEqual(state['homepage'], 1)
                self.assertEqual(state['profile'], 0)
                self.assertTrue(failure.authentication_evidence['canonical_home_probe_attempted'])
                self.assertFalse(failure.authentication_evidence['wos_session_confirmed'])
                self.assertFalse(failure.authentication_evidence['profile_requested'])


if __name__ == '__main__':
    unittest.main()
