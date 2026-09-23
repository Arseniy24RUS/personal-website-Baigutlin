"""Initial navigation recovery with an explicit synthetic native-error boundary.

Routed Playwright responses do not reproduce native malformed-redirect errors.
Only that boundary (native error URL/document plus the failed document event)
is injected; the production retry predicate, subsequent normal ORCID flow,
challenge guards and target proof all run unchanged.
"""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth
import test_wos_profile_first_login as fixture

PROFILE = fixture.PROFILE


class InitialProfileRetryTests(unittest.TestCase):
    def fixture(self):
        page = MagicMock()
        page.url = 'chrome-error://chromewebdata/'
        evidence = {'stage': 'initial_profile', 'initial_profile_requested': True}
        navigation = SimpleNamespace(authorization_denied=False, last_document_failure={
            'page': page, 'provider': 'clarivate', 'code': 'ERR_INVALID_REDIRECT', 'stage': 'initial_profile'})
        failure = auth.AuthFailure('login_navigation_failed', authentication_evidence={
            'navigation_failures': [{'kind': 'browser_error', 'provider': 'browser', 'network_error_code': 'ERR_INVALID_REDIRECT'}]})
        return page, failure, evidence, navigation

    def retry(self, page, failure, evidence, navigation, *, profile=PROFILE, initial_page=None, deadline=12.5):
        return auth._retry_initial_wos_profile(page, profile, failure, evidence, navigation, deadline,
                                              initial_page=page if initial_page is None else initial_page)

    def test_only_same_initial_page_fixed_get_uses_remaining_budget_once(self):
        args = self.fixture()
        with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
            self.assertTrue(self.retry(*args))
            goto.assert_called_once_with(args[0], PROFILE, args[2], wait_until='domcontentloaded', timeout=2500)
            self.assertTrue(args[2]['initial_profile_retry_attempted'])
            self.assertTrue(args[2]['initial_profile_retry_loaded'])
            self.assertFalse(self.retry(*args))
            self.assertEqual(goto.call_count, 1)
        args[0].context.new_page.assert_not_called()
        args[0].context.add_cookies.assert_not_called()

    def test_every_auth_ui_and_consent_action_disables_initial_retry(self):
        fields = ('signin_clicked', 'orcid_selected', 'orcid_page_observed', 'orcid_form_observed',
                  'submit_clicked', 'orcid_consent_clicked', 'response_observed', 'onboarding_acknowledged',
                  'consent_accepted', 'cookie_banner_dismissed', 'wos_return_observed',
                  'wos_session_confirmed', 'profile_requested', 'canonical_home_probe_attempted')
        for field in fields:
            with self.subTest(field=field):
                page, failure, evidence, navigation = self.fixture()
                evidence[field] = True
                with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
                    self.assertFalse(self.retry(page, failure, evidence, navigation))
                    goto.assert_not_called()

    def test_native_error_current_initial_page_event_stage_and_deadline_are_required(self):
        for case in ('popup', 'wrong_event_page', 'wrong_event_stage', 'wrong_stage', 'not_requested',
                     'not_native', 'unknown_native_code', 'different_event_code', 'different_provider',
                     'http_denied', 'captcha', 'mfa', 'timeout', 'expired'):
            with self.subTest(case=case):
                page, failure, evidence, navigation = self.fixture()
                options = {}
                if case == 'popup': options['initial_page'] = MagicMock()
                if case == 'wrong_event_page': navigation.last_document_failure['page'] = MagicMock()
                if case == 'wrong_event_stage': navigation.last_document_failure['stage'] = 'signin'
                if case == 'wrong_stage': evidence['stage'] = 'signin'
                if case == 'not_requested': evidence['initial_profile_requested'] = False
                if case == 'not_native': page.url = PROFILE
                if case == 'unknown_native_code': failure.authentication_evidence['navigation_failures'][-1]['network_error_code'] = 'unknown'
                if case == 'different_event_code': navigation.last_document_failure['code'] = 'ERR_CONNECTION_RESET'
                if case == 'different_provider': navigation.last_document_failure['provider'] = 'other'
                if case == 'http_denied': navigation.authorization_denied = True
                if case == 'captcha': failure.reason = 'human_verification_required'
                if case == 'mfa': failure.reason = 'mfa_required'
                if case == 'timeout': failure.reason = 'TimeoutError'
                if case == 'expired': options['deadline'] = 10
                with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
                    self.assertFalse(self.retry(page, failure, evidence, navigation, **options))
                    goto.assert_not_called()

    def test_noncanonical_target_and_failed_retry_never_create_another_request(self):
        for profile in (PROFILE + '?SID=private', PROFILE + '#private', PROFILE.replace('https:', 'http:'),
                        PROFILE.replace('www.webofscience.com', 'unrelated.invalid'), 'https://www.webofscience.com/'):
            with self.subTest(profile=profile):
                with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login') as goto:
                    self.assertFalse(self.retry(*self.fixture(), profile=profile))
                    goto.assert_not_called()
        args = self.fixture()
        with patch.object(auth.time, 'monotonic', return_value=10), patch.object(auth, '_goto_wos_login', side_effect=auth.AuthFailure('login_navigation_failed')) as goto:
            with self.assertRaises(auth.AuthFailure):
                self.retry(*args)
            self.assertTrue(args[2]['initial_profile_retry_attempted'])
            self.assertNotIn('initial_profile_retry_loaded', args[2])
            self.assertFalse(self.retry(*args))
            self.assertEqual(goto.call_count, 1)

    def test_retry_diagnostics_keep_only_boolean_progress(self):
        value = auth.safe_wos_login_evidence({'initial_profile_retry_attempted': True,
            'initial_profile_retry_loaded': False, 'page': 'private', 'url': PROFILE + '?SID=private'})
        self.assertEqual(value, {'initial_profile_retry_attempted': True, 'initial_profile_retry_loaded': False})
        self.assertNotIn('private', json.dumps(value))


class InitialProfileRetryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.ProfileFirstBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.ProfileFirstBrowserTests.tearDownClass.__func__(cls)

    flow = fixture.ProfileFirstBrowserTests.flow
    login = fixture.ProfileFirstBrowserTests.login

    def attempt(self, *, raised, challenge=False, repeated_failure=False):
        from playwright.sync_api import Page
        context, state = self.flow(challenge_at='before_intro' if challenge else None, intro_label='Got it')
        boundary = {'page': None, 'error': False, 'requests': 0}
        observers = []
        real_start = auth._WosLoginNavigationObserver.start
        real_goto, real_url = auth._goto_wos_login, Page.url.fget
        def start(observer):
            observers.append(observer)
            real_start(observer)
        def url(page):
            return 'chrome-error://chromewebdata/' if boundary['error'] and page is boundary['page'] else real_url(page)
        def goto(page, target, evidence, **options):
            if target == PROFILE and not evidence.get('submit_clicked'):
                boundary['requests'] += 1
                boundary['page'], boundary['error'] = page, False
                result = real_goto(page, target, evidence, **options)
                if boundary['requests'] == 1 or repeated_failure:
                    page.set_content('<body>ERR_INVALID_REDIRECT</body>')
                    boundary['error'] = True
                    observers[-1].failed(SimpleNamespace(frame=page.main_frame, resource_type='document',
                        is_navigation_request=lambda: True, url='https://access.clarivate.com/fixture',
                        failure='net::ERR_INVALID_REDIRECT'))
                    if raised or repeated_failure:
                        auth._check_browser_error_page(page, evidence)
                return result
            return real_goto(page, target, evidence, **options)
        with patch.object(auth._WosLoginNavigationObserver, 'start', start), \
                patch.object(Page, 'url', property(url)), patch.object(auth, '_goto_wos_login', side_effect=goto):
            try:
                result = self.login(context)
            except auth.AuthFailure as failure:
                result = failure
        self.assertEqual(boundary['requests'], 2)
        self.assertEqual(len(context.pages), 1)
        self.assertFalse(any(address == 'https://access.clarivate.com/fixture' for address, _ in state['requests']))
        return result, state

    def test_raised_initial_goto_retries_then_requires_full_orcid_and_target_proof(self):
        page, state = self.attempt(raised=True)
        self.assertFalse(isinstance(page, auth.AuthFailure), getattr(page, 'reason', 'failed'))
        self.assertEqual(state['events'], ['got_it', 'accept', 'signin', 'orcid_submit'])
        self.assertEqual((state['posts'], state['profile_gets']), (1, 3))
        self.assertTrue(page._wos_login_evidence['initial_profile_retry_loaded'])
        self.assertTrue(page._wos_login_evidence['returned_profile_reused'])

    def test_error_discovered_after_goto_return_uses_same_one_shot_retry(self):
        page, state = self.attempt(raised=False)
        self.assertFalse(isinstance(page, auth.AuthFailure), getattr(page, 'reason', 'failed'))
        self.assertEqual((state['posts'], state['profile_gets']), (1, 3))
        self.assertTrue(page._wos_login_evidence['initial_profile_retry_attempted'])

    def test_challenge_after_retry_and_second_invalid_redirect_stop_without_credentials(self):
        for options, reason in (({'challenge': True}, 'human_verification_required'),
                                ({'repeated_failure': True}, 'login_navigation_failed')):
            with self.subTest(options=options):
                failure, state = self.attempt(raised=True, **options)
                self.assertIsInstance(failure, auth.AuthFailure)
                self.assertEqual(failure.reason, reason)
                self.assertEqual((state['posts'], state['profile_gets']), (0, 2))
                self.assertEqual(state['events'], [])

    def test_guest_end_session_menu_cannot_skip_the_real_orcid_post(self):
        context, state = self.flow(guest_session_menu=True, intro_label='Got it')
        page = self.login(context)
        self.assertEqual(state['events'], ['got_it', 'accept', 'signin', 'orcid_submit'])
        self.assertEqual((state['posts'], state['profile_gets']), (1, 2))
        self.assertTrue(page._wos_login_evidence['submit_clicked'])

    def test_both_observed_intro_labels_work_but_unrelated_phrase_is_not_clicked(self):
        for label in ('Got it', 'Got it!', 'Got it and continue'):
            with self.subTest(label=label):
                context = self.browser.new_context()
                self.addCleanup(context.close)
                body = '<body><button>Sign in</button><div role="dialog" id="intro"><button onclick="window.ack=true;document.getElementById(\'intro\').hidden=true">' + label + '</button></div></body>'
                context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=body))
                page = context.new_page()
                page.goto(PROFILE)
                if label == 'Got it and continue':
                    with self.assertRaises(auth.AuthFailure):
                        auth.prepare_wos_profile_login(page, {}, time.monotonic() + 0.5)
                    self.assertIsNone(page.evaluate('window.ack'))
                else:
                    evidence = {}
                    auth.prepare_wos_profile_login(page, evidence, time.monotonic() + 3)
                    self.assertTrue(evidence['onboarding_acknowledged'])

    def test_bare_session_end_is_not_proof_even_with_public_author_name(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        for label in ('End session', 'Завершить сеанс'):
            for element in ('<button role="menuitem">{}</button>', '<a role="menuitem" href="/logout">{}</a>',
                            '<a role="menuitem" href="/logout" aria-label="{}"><span aria-hidden="true">logout</span></a>',
                            '<a role="menuitem" href="/logout" aria-label="{}">  </a>'):
                with self.subTest(label=label, element=element):
                    page.set_content('<body><p>Arseniy Sitkovskiy</p><button data-ta="user-menu">Account</button>'
                        '<div role="menu"><button role="menuitem">Sign In</button>' + element.format(label) + '</div></body>')
                    self.assertFalse(auth.wos_logout_visible(page))
                    self.assertFalse(auth.wos_authenticated(page))


if __name__ == '__main__':
    unittest.main()
