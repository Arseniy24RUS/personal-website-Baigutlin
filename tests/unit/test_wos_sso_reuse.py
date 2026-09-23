"""Ordinary SSO restoration with local fixtures only; no live account requests."""
import copy
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import browser_sessions as sessions
import harvest_wos_authenticated as wos
import provider_auth as auth


def cookie(domain, name, value):
    return {'domain': domain, 'name': name, 'value': value, 'path': '/',
            'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'None'}


class SsoStateTests(unittest.TestCase):
    def test_popup_selection_ignores_existing_tabs_and_rejects_unknown_origins(self):
        context = MagicMock()
        existing, login, popup = MagicMock(), MagicMock(), MagicMock()
        for page in (existing, login, popup):
            page.is_closed.return_value = False
        existing.url, login.url = 'https://unrelated.test/', 'https://www.webofscience.com/'
        context.pages = [existing, login]
        self.assertIs(auth._wos_login_page(context, (existing,)), login)
        context.pages.append(popup)
        for url in ('http://orcid.org/signin', 'https://orcid.org.attacker.test/signin',
                    'https://unobserved.orcid.org/signin', 'https://user@orcid.org/signin'):
            popup.url = url
            with self.subTest(url=url), self.assertRaisesRegex(auth.AuthFailure, '^unexpected_login_origin$'):
                auth._wos_login_page(context, (existing,))
        popup.is_closed.return_value = True
        self.assertIs(auth._wos_login_page(context, (existing,)), login)
        login.is_closed.return_value = True
        self.assertIsNone(auth._wos_login_page(context, (existing,)))

    def test_popup_wait_uses_remaining_login_deadline_without_resetting_it(self):
        page = MagicMock()
        page.is_closed.return_value = False
        with patch.object(auth.time, 'monotonic', side_effect=[5.0, 5.2]):
            auth._wait_wos_login_navigation(page, 5.5)
        page.wait_for_load_state.assert_called_once_with('domcontentloaded', timeout=500)
        self.assertAlmostEqual(page.wait_for_timeout.call_args.args[0], 300)

    def test_only_expired_wos_state_is_discarded_before_normal_login(self):
        state = {'cookies': [cookie('.webofscience.com', 'WOSSID', 'old-wos'),
                             cookie('orcid.org', 'identity-session', 'kept-orcid'),
                             cookie('access.clarivate.com', 'identity-session', 'kept-clarivate'),
                             cookie('unrelated.test', 'identity-session', 'excluded')],
                 'origins': [
                     {'origin': 'https://www.webofscience.com', 'localStorage': [{'name': 'wos_sid', 'value': 'old-wos'}]},
                     {'origin': 'https://orcid.org', 'localStorage': [{'name': 'fixture-token', 'value': 'kept-orcid'}], 'indexedDB': [{'name': 'fixture-db', 'version': 1, 'stores': []}]},
                     {'origin': 'https://signin.clarivate.com', 'localStorage': [{'name': 'fixture-token', 'value': 'kept-clarivate'}]},
                     {'origin': 'https://unrelated.test', 'localStorage': []}]}
        original = copy.deepcopy(state)
        old, browser = MagicMock(), MagicMock()
        old.storage_state.return_value = state
        wos_page, idp_page = MagicMock(), MagicMock()
        wos_page.url, idp_page.url = wos.PROFILE_URL, 'https://orcid.org/oauth/authorize'
        wos_page.evaluate.side_effect = AssertionError('Discarded WoS storage must not be read')
        idp_page.evaluate.return_value = {'fixture-session': 'kept-orcid', sessions.HYDRATION_MARKER: '1'}
        old.pages = [wos_page, idp_page]
        events = []
        old.storage_state.side_effect = lambda **kwargs: events.append('snapshot') or state
        old.close.side_effect = lambda: events.append('close')
        browser.new_context.side_effect = lambda **kwargs: events.append('restore') or MagicMock()
        with patch.object(sessions, 'hydrate_session_storage') as hydrate:
            result = sessions.create_wos_reauthentication_context(browser, old, locale='en-US')
        self.assertEqual(events, ['snapshot', 'close', 'restore'])
        saved = browser.new_context.call_args.kwargs['storage_state']
        self.assertEqual(saved['cookies'], original['cookies'][1:3])
        self.assertEqual(saved['origins'], original['origins'][1:3])
        self.assertEqual(browser.new_context.call_args.kwargs['locale'], 'en-US')
        hydrate.assert_called_once_with(result, {'https://orcid.org': {'fixture-session': 'kept-orcid'}})
        wos_page.evaluate.assert_not_called()
        self.assertEqual(state, original)

    def test_capture_error_does_not_close_or_silently_replace_existing_context(self):
        old, browser = MagicMock(), MagicMock()
        old.storage_state.side_effect = RuntimeError('private source detail')
        with self.assertRaisesRegex(sessions.SessionError, '^wos_sso_state_capture_failed$'):
            sessions.create_wos_reauthentication_context(browser, old)
        old.close.assert_not_called()
        browser.new_context.assert_not_called()

    def test_conflicting_idp_tabs_do_not_silently_choose_a_session(self):
        old, browser = MagicMock(), MagicMock()
        old.storage_state.return_value = {'cookies': [], 'origins': []}
        pages = [MagicMock(), MagicMock()]
        for index, page in enumerate(pages):
            page.url = 'https://orcid.org/signin'
            page.evaluate.return_value = {'fixture-session': str(index)}
        old.pages = pages
        with self.assertRaisesRegex(sessions.SessionError, '^wos_sso_state_capture_failed$'):
            sessions.create_wos_reauthentication_context(browser, old)
        old.close.assert_not_called()
        browser.new_context.assert_not_called()

    def test_restore_error_never_retries_with_empty_context(self):
        old, browser = MagicMock(), MagicMock()
        old.storage_state.return_value = {'cookies': [cookie('orcid.org', 'identity-session', 'fixture')], 'origins': []}
        old.pages = []
        browser.new_context.side_effect = RuntimeError('private cookie value')
        with self.assertRaisesRegex(sessions.SessionError, '^wos_sso_state_restore_failed$'):
            sessions.create_wos_reauthentication_context(browser, old)
        browser.new_context.assert_called_once()

    def test_expiry_does_not_close_identity_provider_page_before_capture(self):
        context = MagicMock()
        page = context.new_page.return_value
        replacement = MagicMock()
        def recreate():
            page.close.assert_not_called()
            return replacement
        with patch.object(wos, 'target_profile_html', side_effect=[auth.AuthFailure('session_expired'), 'verified']), patch.object(wos, 'safe_browser_diagnostics', return_value=[]), patch.object(wos, 'login_wos') as login:
            wos.authenticated_page(context, {'status': 'restored'}, fresh_context=recreate)
        self.assertIs(login.call_args.args[0], replacement)


class SsoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.playwright = sync_playwright().start()
            options = {'headless': True}
            if not Path(cls.playwright.chromium.executable_path).exists():
                edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
                if not edge.exists():
                    cls.playwright.stop()
                    raise unittest.SkipTest('Install Playwright Chromium for SSO fixture tests')
                options['executable_path'] = str(edge)
            cls.browser = cls.playwright.chromium.launch(**options)
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(lambda: self.context.close())

    def flow(self, *, consent=False, challenge=False, direct=False, logout=True,
             popup=False, popup_delay=1500, close_popup=False, password_form=False,
             popup_url='https://orcid.org/oauth/authorize'):
        target = 'FIXTURE-1'
        profile = f'https://www.webofscience.com/wos/author/record/{target}'
        callback = 'https://www.webofscience.com/wos/author/author-search'
        seen = []
        session = {'authenticated': direct}
        account = '''<button data-ta="wos-header-user_name" aria-label="User account menu"
            onclick="document.getElementById('account').hidden=false;window.accountOpened=true">Fixture Researcher</button>
            <div id="account" role="menu" hidden><button role="menuitem">My Profile</button>'''
        if logout:
            account += '<button role="menuitem" onclick="window.loggedOut=true">Sign out</button>'
        account += '</div>'
        def route(request):
            address = request.request.url
            seen.append((address, request.request.method))
            if address == profile and session['authenticated']:
                html = f'<body>{account}<p>Web of Science ResearcherID: {target}</p></body>'
            elif address == callback or (direct and address == 'https://www.webofscience.com/'):
                session['authenticated'] = True
                marker = 'Please verify you are human' if challenge else ''
                html = f'<body>{account}<p>{marker}</p></body>'
            elif address in {profile, 'https://www.webofscience.com/'}:
                html = '<body><a href="https://access.clarivate.com/login">Sign in</a></body>'
            elif address.startswith('https://access.clarivate.com/'):
                if popup:
                    html = f'''<body><button title="Sign in with ORCID" onclick="setTimeout(() => window.open('{popup_url}', '_blank'), {popup_delay})">Sign in with ORCID</button></body>'''
                else:
                    html = '<body><a href="https://orcid.org/oauth/authorize">Sign in with ORCID</a></body>'
            elif address == 'https://orcid.org/oauth/authorize' and consent:
                html = f'<body><button onclick="location.href=\'{callback}\'">Authorize access</button></body>'
            elif address == 'https://orcid.org/oauth/authorize' and password_form:
                html = '''<body><form action="/fixture-submit" method="post"><input id="username-input" name="username"><input type="password" name="password"><button id="signin-button" type="submit">Sign in to ORCID</button></form></body>'''
            elif address in {'https://orcid.org/oauth/authorize', 'https://orcid.org/fixture-submit'}:
                html = (f'<body><script>window.opener.location.href="{callback}";window.close()</script></body>' if close_popup
                        else f'<body><script>location.href="{callback}"</script></body>')
            else:
                html = '<body>Unexpected fixture route</body>'
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
        self.context.route('**/*', route)
        return target, profile, seen

    def verify_flow(self, **options):
        target, profile, seen = self.flow(**options)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'unused-fixture-password'}), patch.object(wos, 'WAIT_SEC', 20):
            page, _ = wos.authenticated_page(self.context, {'status': 'missing'}, target)
        self.assertEqual(page.url, profile)
        self.assertTrue(auth.wos_authenticated(page))
        self.assertTrue(all(method == 'GET' for _, method in seen))
        self.assertIsNone(page.evaluate('window.loggedOut'))
        return seen

    def test_orcid_automatic_callback_succeeds_without_password_submission(self):
        seen = self.verify_flow()
        self.assertTrue(any(url.startswith('https://orcid.org/') for url, _ in seen))

    def test_orcid_consent_without_password_succeeds(self):
        self.verify_flow(consent=True)

    def test_delayed_orcid_popup_after_one_second_is_followed_without_new_login(self):
        seen = self.verify_flow(popup=True, popup_delay=1500)
        self.assertEqual(sum(url == 'https://orcid.org/oauth/authorize' for url, _ in seen), 1)
        self.assertEqual(sum(url.startswith('https://access.clarivate.com/') for url, _ in seen), 1)

    def test_closed_orcid_popup_returns_to_authenticated_opener(self):
        seen = self.verify_flow(popup=True, popup_delay=1500, close_popup=True)
        self.assertTrue(any('/wos/author/author-search' in url for url, _ in seen))
        self.assertEqual(len(self.context.pages), 1)

    def test_delayed_popup_submits_orcid_credentials_once_and_returns_to_opener(self):
        _, profile, seen = self.flow(popup=True, popup_delay=1500, password_form=True, close_popup=True)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'fixture-password'}):
            page = auth.login_wos(self.context, profile, timeout=20)
        self.assertEqual(page.url, profile)
        self.assertTrue(auth.wos_authenticated(page))
        self.assertEqual([(url, method) for url, method in seen if method == 'POST'],
                         [('https://orcid.org/fixture-submit', 'POST')])
        self.assertEqual(len(self.context.pages), 1)

    def test_wrong_origin_popup_stops_before_profile_or_form_submission(self):
        _, profile, seen = self.flow(popup=True, popup_url='https://unrelated.test/signin')
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'unused-fixture-password'}):
            with self.assertRaisesRegex(auth.AuthFailure, '^unexpected_login_origin$'):
                auth.login_wos(self.context, profile, timeout=15)
        self.assertTrue(all(method == 'GET' for _, method in seen))
        self.assertEqual(sum(url == profile for url, _ in seen), 1)

    def test_blank_popup_expires_under_original_deadline_without_retry(self):
        _, profile, seen = self.flow(popup=True, popup_delay=0, popup_url='about:blank')
        started = time.monotonic()
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'unused-fixture-password'}):
            with self.assertRaisesRegex(auth.AuthFailure, '^wos_login_form_changed$'):
                auth.login_wos(self.context, profile, timeout=5)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(sum(url.startswith('https://access.clarivate.com/') for url, _ in seen), 1)
        self.assertEqual(sum(url == profile for url, _ in seen), 1)

    def test_direct_authenticated_wos_profile_succeeds_without_orcid_navigation(self):
        seen = self.verify_flow(direct=True)
        self.assertFalse(any(url.startswith('https://orcid.org/') for url, _ in seen))

    def test_explicit_challenge_blocks_callback_before_account_menu_or_profile(self):
        _, profile, seen = self.flow(challenge=True)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'unused-fixture-password'}):
            with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$'):
                auth.login_wos(self.context, profile, timeout=20)
        self.assertEqual(sum(url == profile for url, _ in seen), 1)
        self.assertIsNone(self.context.pages[-1].evaluate('window.accountOpened'))

    def test_account_button_without_logout_cannot_confirm_direct_sso(self):
        _, profile, seen = self.flow(direct=True, logout=False)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'unused-fixture-password'}):
            with self.assertRaises(auth.AuthFailure):
                auth.login_wos(self.context, profile, timeout=4)
        self.assertEqual(sum(url == profile for url, _ in seen), 1)


if __name__ == '__main__':
    unittest.main()
