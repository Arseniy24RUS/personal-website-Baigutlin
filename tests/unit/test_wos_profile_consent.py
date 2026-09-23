"""OneTrust modal fixtures; all requests are routed locally, no real sessions."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth


class ProfileConsentBrowserTests(unittest.TestCase):
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
                raise unittest.SkipTest('Install Playwright Chromium for profile consent fixtures')
            options['executable_path'] = str(edge)
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def fixture(self, *, reject=False, late=False, challenge=False, challenge_after=False, unknown=False, block_after=False, disabled=False):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        settings = json.dumps({'late': late, 'challengeAfter': challenge_after, 'blockAfter': block_after})
        controls = '<button onclick="counts.close++">Close</button>'
        if not unknown:
            controls += '<button id="onetrust-pc-btn-handler" onclick="counts.preferences++">Manage cookie preferences</button>'
            controls += f'<button id="onetrust-accept-btn-handler" {"disabled" if disabled else ""} onclick="consent(\'accept\')">Accept all</button>'
            if reject:
                controls += '<button id="onetrust-reject-all-handler" onclick="consent(\'reject\')">Reject all</button>'
        body = f'''<body>
          <button data-ta="wos-header-user_name" onmouseenter="lateBanner()"
            onclick="counts.account++;document.getElementById('menu').hidden=false">Fixture Researcher</button>
          <div id="menu" role="menu" hidden><button role="menuitem" onclick="counts.logout++">Sign out</button></div>
          <div id="banner" {"hidden" if late else ""} style="position:fixed;inset:0;z-index:1000;background:white">
            <section style="position:absolute;bottom:20px">{controls}</section>
          </div>
          <div id="other" hidden style="position:fixed;inset:0;z-index:2000;background:white">Unrelated overlay</div>
          <div id="challenge" {"" if challenge else "hidden"}>Please verify you are human</div>
          <script>
            const settings={settings};window.counts={{accept:0,reject:0,account:0,logout:0,close:0,preferences:0}};
            let shown=false;
            function lateBanner() {{if(settings.late&&!shown){{shown=true;document.getElementById('banner').hidden=false;}}}}
            function consent(kind) {{counts[kind]++;document.getElementById('banner').hidden=true;
              if(settings.challengeAfter)document.getElementById('challenge').hidden=false;
              if(settings.blockAfter)document.getElementById('other').hidden=false;
            }}
            document.addEventListener('keydown', event=>{{if(event.key==='Escape')document.getElementById('menu').hidden=true;}});
          </script></body>'''
        context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=body))
        page = context.new_page()
        page.goto('https://www.webofscience.com/wos/author/record/FIXTURE')
        return page

    def check(self, page):
        # Only test-side timing acceleration; production retains normal clicks.
        from playwright.sync_api import Locator
        original = Locator.click
        attempts = []
        def click(locator, *args, **kwargs):
            if kwargs.get('timeout') == 15000:
                attempts.append(True)
                kwargs['timeout'] = 750
            return original(locator, *args, **kwargs)
        with patch.object(Locator, 'click', click):
            try:
                result = auth.wos_authenticated(page)
            except auth.AuthFailure as failure:
                return failure, attempts
        return result, attempts

    def test_initial_banner_uses_requested_accept_and_preserves_logout_proof(self):
        page = self.fixture(reject=True)
        result, attempts = self.check(page)
        self.assertIs(result, True)
        counts = page.evaluate('counts')
        self.assertEqual((counts['reject'], counts['accept'], counts['account'], counts['logout']), (0, 1, 1, 0))
        self.assertEqual(len(attempts), 1)
        self.assertTrue(page.locator('#banner').is_hidden())

    def test_initial_observed_accept_control_is_used_when_reject_absent(self):
        page = self.fixture()
        result, _ = self.check(page)
        self.assertIs(result, True)
        counts = page.evaluate('counts')
        self.assertEqual((counts['accept'], counts['preferences'], counts['close'], counts['logout']), (1, 0, 0, 0))

    def test_banner_mounting_during_account_click_allows_one_explicit_recovery(self):
        page = self.fixture(late=True)
        result, attempts = self.check(page)
        self.assertIs(result, True)
        self.assertEqual(len(attempts), 2)
        counts = page.evaluate('counts')
        self.assertEqual((counts['accept'], counts['account'], counts['logout']), (1, 1, 0))

    def test_human_verification_before_or_after_cookie_action_stops_without_account_click(self):
        for after in (False, True):
            with self.subTest(after=after):
                page = self.fixture(challenge=not after, challenge_after=after)
                failure, attempts = self.check(page)
                self.assertIsInstance(failure, auth.AuthFailure)
                self.assertEqual(failure.reason, 'human_verification_required')
                self.assertEqual(attempts, [])
                counts = page.evaluate('counts')
                self.assertEqual(counts['accept'], int(after))
                self.assertEqual((counts['account'], counts['logout'], counts['close']), (0, 0, 0))
                self.assertTrue(page.locator('#challenge').is_visible())

    def test_unrelated_close_overlay_is_not_dismissed_or_retried(self):
        page = self.fixture(unknown=True)
        failure, attempts = self.check(page)
        self.assertIsInstance(failure, auth.AuthFailure)
        self.assertEqual(failure.reason, 'TimeoutError')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(page.evaluate('counts.close'), 0)
        self.assertEqual(failure.authentication_evidence,
            {'account_click_timed_out': True, 'cookie_banner_observed': False, 'cookie_banner_dismissed': False})

    def test_failed_second_click_is_not_retried_and_reports_only_fixed_evidence(self):
        page = self.fixture(late=True, block_after=True)
        failure, attempts = self.check(page)
        self.assertIsInstance(failure, auth.AuthFailure)
        self.assertEqual(failure.reason, 'TimeoutError')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(page.evaluate('counts.accept'), 1)
        expected = {'account_click_timed_out': True, 'cookie_banner_observed': True, 'cookie_banner_dismissed': True, 'consent_accepted': True}
        self.assertEqual(failure.authentication_evidence, expected)
        self.assertEqual(auth.safe_wos_login_evidence({**expected, 'body': 'private-token'}), expected)

    def test_disabled_cookie_control_is_never_clicked(self):
        page = self.fixture(disabled=True)
        failure, attempts = self.check(page)
        self.assertIsInstance(failure, auth.AuthFailure)
        self.assertEqual(failure.reason, 'TimeoutError')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(page.evaluate('counts.accept'), 0)
        self.assertTrue(failure.authentication_evidence['cookie_banner_observed'])
        self.assertFalse(failure.authentication_evidence['cookie_banner_dismissed'])


if __name__ == '__main__':
    unittest.main()
