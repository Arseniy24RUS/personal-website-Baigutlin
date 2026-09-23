"""The user's profile-first login order, using only locally routed browser pages."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth
import harvest_wos_authenticated as wos

TARGET = 'FIXTURE-1'
PROFILE = f'https://www.webofscience.com/wos/author/record/{TARGET}'
METRICS = '''<div class="wat-author-metric-inline-block"><span>13</span><span>Publications</span></div>
<div class="wat-author-metric-inline-block"><span>8</span><span>Sum of Times Cited</span></div>
<div class="wat-author-metric-inline-block"><span>2</span><span>H-Index</span></div>'''


class ProfileFirstBrowserTests(unittest.TestCase):
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
                raise unittest.SkipTest('Install Chromium for profile-first fixtures')
            options['executable_path'] = str(edge)
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def flow(self, *, challenge_at=None, intro=True, already_authenticated=False, wrong_rid=False,
             inline_signin=False, cookie_dialog=False, guest_session_menu=False, intro_label='Got it!'):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        state = {'events': [], 'requests': [], 'profile_gets': 0, 'posts': 0,
                 'authorized': already_authenticated}
        account = '''<button data-ta="wos-header-user_name" onclick="document.getElementById('menu').hidden=false">Fixture Researcher</button>
            <div id="menu" hidden><button role="menuitem">Sign out</button></div>
            <script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('menu').hidden=true;});</script>'''
        settings = json.dumps({'challengeAt': challenge_at, 'intro': intro, 'target': TARGET,
                               'inlineSignin': inline_signin, 'guestSessionMenu': guest_session_menu})
        initial = '''<body>
          <header><button onclick="action('signin')">Sign in</button></header>
          <div role="progressbar" id="loading">Loading profile</div><main id="profile" aria-busy="true"></main>
          <div role="dialog" aria-modal="true" id="intro" hidden style="position:fixed;inset:0;z-index:20;background:white">
            <p>Welcome to the researcher profile</p><button onclick="action('got_it')">Got it!</button>
          </div>
          <div id="onetrust-banner-sdk" COOKIE_ROLE hidden style="position:fixed;bottom:0;left:0;right:0;z-index:10;background:white">
            <button id="onetrust-accept-btn-handler" onclick="action('accept')">Accept all cookies</button>
            <button id="onetrust-reject-all-handler" onclick="action('reject')">Reject all</button>
          </div><div id="challenge" hidden>Please verify you are human</div>
          <div role="dialog" id="loginDialog" hidden><a href="https://orcid.org/signin">Sign in with ORCID</a>
            <button>Sign in with Clarivate</button></div>
          <div role="menu" id="guestMenu" hidden><a role="menuitem" href="https://access.clarivate.com/login">Sign In</a>
            <button role="menuitem" onclick="action('end_session')">End session</button></div>
          <script>
            const settings=SETTINGS;
            function challenge(phase){if(settings.challengeAt===phase)document.getElementById('challenge').hidden=false;}
            async function action(name){
              await fetch('/fixture-event/'+name);
              if(name==='got_it'){document.getElementById('intro').hidden=true;challenge('after_intro');}
              if(name==='accept'||name==='reject'){document.getElementById('onetrust-banner-sdk').hidden=true;challenge('after_cookie');}
              if(name==='signin'){
                if(settings.guestSessionMenu)document.getElementById('guestMenu').hidden=false;
                else if(settings.inlineSignin)document.getElementById('loginDialog').hidden=false;
                else location.href='https://access.clarivate.com/login';
              }
            }
            setTimeout(()=>{
              document.getElementById('profile').textContent='ResearcherID: '+settings.target;
              document.getElementById('profile').setAttribute('aria-busy','false');
              document.getElementById('loading').hidden=true;
              document.getElementById('intro').hidden=!settings.intro;
              document.getElementById('onetrust-banner-sdk').hidden=false;challenge('before_intro');
            },1500);
          </script></body>'''.replace('SETTINGS', settings).replace('COOKIE_ROLE', 'role="dialog"' if cookie_dialog else '').replace('>Got it!</button>', '>' + intro_label + '</button>')

        def route(item):
            req = item.request
            state['requests'].append((req.url, req.method))
            if req.url == PROFILE:
                state['profile_gets'] += 1
                rid = 'OTHER-2' if wrong_rid else TARGET
                body = f'<body>{account}<p>ResearcherID: {rid}</p>{METRICS}</body>' if state['authorized'] else initial
            elif req.url.startswith('https://www.webofscience.com/fixture-event/'):
                state['events'].append(req.url.rsplit('/', 1)[-1])
                item.fulfill(status=200, body='ok')
                return
            elif req.url == 'https://access.clarivate.com/login':
                body = '<body><a href="https://orcid.org/signin">Sign in with ORCID</a></body>'
            elif req.url == 'https://orcid.org/signin':
                body = f'''<body><form onsubmit="event.preventDefault();fetch('/signin/auth.json',{{method:'POST'}})
                    .then(r=>r.json()).then(()=>setTimeout(()=>location.href='{PROFILE}',500))">
                    <input id="username-input"><input type="password">
                    <button id="signin-button" type="submit">Sign in to ORCID</button></form></body>'''
            elif req.url == 'https://orcid.org/signin/auth.json':
                state['posts'] += 1
                state['authorized'] = True
                state['events'].append('orcid_submit')
                item.fulfill(status=200, content_type='application/json', body='{"success":true}')
                return
            else:
                self.fail('Unexpected external request in a fully routed login fixture')
            item.fulfill(status=200, content_type='text/html', body=body)
        context.route('**/*', route)
        return context, state

    def login(self, context):
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.invalid',
                                    'WOS_ORCID_PASSWORD': 'synthetic-password'}), patch.object(wos, 'WAIT_SEC', 20):
            return wos.authenticated_page(context, {'status': 'skipped'}, target=TARGET)[0]

    def test_delayed_intro_then_accept_then_orcid_returns_to_profile_without_third_navigation(self):
        context, state = self.flow()
        page = self.login(context)
        self.assertEqual(state['requests'][0], (PROFILE, 'GET'))
        self.assertEqual(state['events'], ['got_it', 'accept', 'signin', 'orcid_submit'])
        self.assertEqual((state['posts'], state['profile_gets']), (1, 2))
        evidence = page._wos_login_evidence
        for key in ('initial_profile_requested', 'initial_profile_loaded', 'onboarding_acknowledged',
                    'consent_accepted', 'returned_profile_reused', 'wos_session_confirmed'):
            self.assertTrue(evidence[key], key)
        self.assertFalse(evidence['homepage_requested'])
        wos.target_profile_html(page, TARGET)
        metrics = wos.read_profile_metrics(page, TARGET)['summary']
        self.assertEqual((metrics['publications'], metrics['citations'], metrics['h_index']), (13, 8, 2))
        self.assertEqual(state['profile_gets'], 2)
        self.assertNotIn('synthetic-password', json.dumps(evidence))

    def test_interactive_challenge_stops_before_next_onboarding_cookie_or_signin_action(self):
        for phase, expected in (('before_intro', []), ('after_intro', ['got_it']),
                                ('after_cookie', ['got_it', 'accept'])):
            with self.subTest(phase=phase):
                context, state = self.flow(challenge_at=phase)
                with self.assertRaises(auth.AuthFailure) as caught:
                    self.login(context)
                self.assertEqual(caught.exception.reason, 'human_verification_required')
                self.assertEqual(state['events'], expected)
                self.assertEqual((state['posts'], state['profile_gets']), (0, 1))

    def test_absent_optional_intro_still_accepts_cookies_and_completes_login(self):
        context, state = self.flow(intro=False)
        page = self.login(context)
        self.assertEqual(state['events'], ['accept', 'signin', 'orcid_submit'])
        self.assertEqual((state['posts'], state['profile_gets']), (1, 2))
        self.assertFalse(page._wos_login_evidence.get('onboarding_acknowledged', False))

    def test_inline_orcid_login_dialog_and_onetrust_dialog_keep_the_same_user_sequence(self):
        context, state = self.flow(inline_signin=True, cookie_dialog=True)
        page = self.login(context)
        self.assertEqual(state['events'], ['got_it', 'accept', 'signin', 'orcid_submit'])
        self.assertEqual((state['posts'], state['profile_gets']), (1, 2))
        self.assertTrue(page._wos_login_evidence['returned_profile_reused'])

    def test_already_authenticated_profile_does_not_reload_or_enter_credentials(self):
        context, state = self.flow(already_authenticated=True)
        page = self.login(context)
        self.assertEqual(state['events'], [])
        self.assertEqual((state['posts'], state['profile_gets']), (0, 1))
        self.assertFalse(page._wos_login_evidence['submit_clicked'])
        self.assertTrue(page._wos_login_evidence['wos_session_confirmed'])

    def test_authorized_return_with_wrong_rid_is_not_accepted(self):
        context, state = self.flow(wrong_rid=True)
        with self.assertRaises(auth.AuthFailure) as caught:
            self.login(context)
        self.assertEqual(caught.exception.reason, 'profile_not_authenticated_or_changed')
        self.assertEqual((state['posts'], state['profile_gets']), (1, 2))

    def test_unrelated_got_it_control_is_not_used_as_generic_dialog_dismissal(self):
        for container in ('<div>{buttons}</div>', '<div role="dialog">{buttons}<button>Cancel</button></div>'):
            with self.subTest(container=container):
                context = self.browser.new_context()
                self.addCleanup(context.close)
                body = '<body><button>Sign in</button>' + container.format(
                    buttons='<button onclick="window.acknowledged=true">Got it!</button>') + '</body>'
                context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=body))
                page = context.new_page()
                page.goto(PROFILE)
                try:
                    auth.prepare_wos_profile_login(page, {}, time.monotonic() + 2)
                except auth.AuthFailure:
                    pass
                self.assertIsNone(page.evaluate('window.acknowledged'))


if __name__ == '__main__':
    unittest.main()
