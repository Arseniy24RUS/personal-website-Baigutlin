"""Exercise ordinary login with a real browser and local, mocked provider pages.

No external requests or real credentials. These tests validate locator and
redirect behavior; the actual providers still require the Actions live test.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth


class AuthBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.playwright = sync_playwright().start()
            args = {'headless': True}
            if not Path(cls.playwright.chromium.executable_path).exists():
                edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
                if not edge.exists():
                    cls.playwright.stop()
                    raise unittest.SkipTest('Install Playwright Chromium for browser authentication tests')
                args['executable_path'] = str(edge)
            cls.browser = cls.playwright.chromium.launch(**args)
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()

    def tearDown(self):
        self.context.close()

    def test_elibrary_homepage_form_then_named_session(self):
        def route(request):
            if request.request.url.endswith('start_session.asp'):
                html = '<body><div id="win_session">Имя пользователя: Test Researcher</div>Личный кабинет</body>'
            else:
                html = '''<body><form method="post" action="/start_session.asp">
                    <input name="login"><input name="password" type="password">
                    <div onclick="check_all()">Вход</div></form>
                    <script>function check_all(){document.querySelector('form').submit()}</script></body>'''
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
        self.context.route('**/*', route)
        with patch.dict(os.environ, {'ELIBRARY_USERNAME': 'fixture-user', 'ELIBRARY_PASSWORD': 'fixture-password'}):
            page = auth.login_elibrary(self.context)
        self.assertTrue(auth.elibrary_authenticated(page))
        self.assertTrue(page.url.endswith('start_session.asp'))

    def test_elibrary_anonymous_session_is_not_authentication(self):
        page = self.context.new_page()
        page.set_content('<body><div id="win_session">Имя пользователя: Незарегистрированный пользователь</div>Личный кабинет Закрыть сессию</body>')
        self.assertFalse(auth.elibrary_authenticated(page))

    def test_wos_signin_menu_orcid_callback(self):
        visited = []
        form_data = []
        authenticated = False
        profile_url = 'https://www.webofscience.com/wos/author/record/TEST'

        def route(request):
            nonlocal authenticated
            url = request.request.url
            visited.append(url)
            if url.startswith('https://access.clarivate.com/'):
                html = '<body><a href="https://orcid.org/oauth/authorize">Sign in with ORCID</a></body>'
            elif url == 'https://orcid.org/finish':
                form_data.append(parse_qs(request.request.post_data or ''))
                authenticated = True
                html = f'<body><script>location.href="{profile_url}"</script></body>'
            elif url.startswith('https://orcid.org/oauth/'):
                html = '''<body><p>Please enter a valid email address or ORCID iD</p><form method="post" action="/finish"><input id="username-input" name="username"><input type="password" name="password"><button id="signin-button" type="submit">Sign in to ORCID</button></form></body>'''
            elif authenticated:
                html = '''<body><button onclick="document.getElementById('account').hidden=false">Arseniy Sitkovskiy</button>
                    <div id="account" hidden><button><span>logout</span> Выйти</button></div><p>Author works</p>
                    <iframe title="recaptcha challenge" style="position:absolute;left:-10000px"></iframe></body>'''
            else:
                html = '''<body><button onclick="document.querySelector('#submenu').hidden=false">Sign In</button>
                    <a id="submenu" hidden onclick="location.href='https://access.clarivate.com/login'">Sign In</a>
                    <a href="https://orcid.org/0000-public-record">ORCID</a></body>'''
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)

        self.context.route('**/*', route)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': ' fixture\\@example.test ', 'WOS_ORCID_PASSWORD': ' fixture\\@password '}):
            page = auth.login_wos(self.context, profile_url, timeout=30)
        self.assertTrue(auth.wos_authenticated(page))
        self.assertFalse(any('0000-public-record' in url for url in visited))
        self.assertTrue(any('access.clarivate.com/login' in url for url in visited))
        self.assertTrue(any('orcid.org/oauth' in url for url in visited))
        self.assertEqual(visited[0], profile_url)
        self.assertEqual(len(form_data), 1)
        self.assertEqual(form_data[0]['username'], ['fixture@example.test'])
        self.assertEqual(form_data[0]['password'], [' fixture\\@password '])
        self.assertEqual(page.url, profile_url)

    def test_captcha_frame_requires_visible_viewport_and_opacity(self):
        page = self.context.new_page()
        for style in ('position:absolute;left:-10000px', 'opacity:0', 'visibility:hidden', 'display:none'):
            with self.subTest(style=style):
                page.set_content(f'<body><iframe title="recaptcha challenge" style="{style}"></iframe></body>')
                auth.assert_no_challenge(page)
        page.set_content('<body><div style="opacity:0"><iframe title="recaptcha challenge"></iframe></div></body>')
        auth.assert_no_challenge(page)
        page.set_content('<body><div style="width:0;height:0;overflow:hidden"><iframe title="recaptcha challenge"></iframe></div></body>')
        auth.assert_no_challenge(page)
        page.set_content('<body><iframe title="recaptcha challenge"></iframe></body>')
        with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$'):
            auth.assert_no_challenge(page)

    def test_wos_account_name_alone_is_not_session_proof(self):
        page = self.context.new_page()
        page.set_content('<body><button>Arseniy Sitkovskiy</button><p>Public author record</p></body>')
        self.assertFalse(auth.wos_authenticated(page))

    def test_wos_anonymous_account_menu_is_not_session_proof(self):
        page = self.context.new_page()
        page.set_content('<body><button data-ta="user-menu" onclick="document.getElementById(\'menu\').hidden=false">Account</button><div id="menu" hidden><button>Sign in</button></div></body>')
        self.assertFalse(auth.wos_authenticated(page))

    def test_wos_current_russian_account_menu_confirms_without_ending_session(self):
        page = self.context.new_page()
        for label in ('Sign out', 'Завершить сеанс и выйти'):
            with self.subTest(label=label):
                page.set_content(f'''<body><button data-ta="wos-header-user_name"
                    aria-label="Раскрывающееся меню параметров учетной записи для пользователя Arseniy Sitkovskiy"
                    onclick="document.getElementById('menu').hidden=false">Arseniy Sitkovskiy</button>
                    <div id="menu" role="menu" hidden><button role="menuitem">Мой профиль</button>
                    <button role="menuitem">Настройки</button>
                    <button role="menuitem" onclick="window.sessionEnded=true">Завершить сеанс</button>
                    <button role="menuitem" onclick="window.sessionEnded=true">{label}</button></div>
                    <script>document.addEventListener('keydown', event => {{
                        if (event.key === 'Escape') document.getElementById('menu').hidden = true;
                    }});</script></body>''')
                self.assertFalse(auth.wos_logout_visible(page))
                self.assertTrue(auth.wos_authenticated(page))
                self.assertFalse(auth.wos_logout_visible(page))
                self.assertIsNone(page.evaluate('window.sessionEnded'))

    def test_wos_owned_modal_account_menu_is_closed_before_export_click(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        page = self.context.new_page()
        # Cover both stable account selectors and the configured-name fallback.
        for attribute in ('data-ta="wos-header-user_name"', ''):
            with self.subTest(attribute=attribute):
                page.set_content(f'''<body>
                    <button {attribute} onclick="document.getElementById('overlay').hidden=false">Arseniy Sitkovskiy</button>
                    <button style="position:absolute;top:100px;left:20px" onclick="window.exportClicked=true">Export CV</button>
                    <div id="overlay" hidden style="position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.1)">
                      <div role="menu" style="position:absolute;right:0;top:0;background:white">
                        <button role="menuitem" onclick="window.sessionEnded=true">Завершить сеанс и выйти</button>
                      </div>
                    </div>
                    <script>window.escapeCount=0;window.exportClicked=false;window.sessionEnded=false;
                      document.addEventListener('keydown', event => {{
                        if (event.key === 'Escape') {{
                          window.escapeCount++;document.getElementById('overlay').hidden=true;
                        }}
                      }});
                    </script></body>''')
                account = page.get_by_role('button', name='Arseniy Sitkovskiy', exact=True)
                export = page.get_by_role('button', name='Export CV', exact=True)
                account.click()
                with self.assertRaises(PlaywrightTimeoutError):
                    export.click(timeout=300)
                page.keyboard.press('Escape')
                before = page.evaluate('window.escapeCount')
                self.assertTrue(auth.wos_authenticated(page))
                self.assertEqual(page.evaluate('window.escapeCount'), before + 1)
                self.assertTrue(page.locator('#overlay').is_hidden())
                export.click(timeout=1000)
                self.assertTrue(page.evaluate('window.exportClicked'))
                self.assertFalse(page.evaluate('window.sessionEnded'))

    def test_wos_negative_account_check_closes_only_the_menu_it_opened(self):
        page = self.context.new_page()
        page.set_content('''<body><button data-ta="user-menu"
            onclick="document.getElementById('menu').hidden=false">Account</button>
            <div id="menu" role="menu" hidden><button>Sign in</button></div>
            <script>window.escapeCount=0;document.addEventListener('keydown', event => {
              if(event.key==='Escape'){window.escapeCount++;document.getElementById('menu').hidden=true;}
            });</script></body>''')
        self.assertFalse(auth.wos_authenticated(page))
        self.assertTrue(page.locator('#menu').is_hidden())
        self.assertEqual(page.evaluate('window.escapeCount'), 1)
        # A caller-opened menu already providing logout proof is left untouched.
        page.set_content('''<body><div role="menu"><button role="menuitem"
            onclick="window.sessionEnded=true">Sign out</button></div>
            <script>window.escapeCount=0;document.addEventListener('keydown', event => {
              if(event.key==='Escape')window.escapeCount++;
            });</script></body>''')
        self.assertTrue(auth.wos_authenticated(page))
        self.assertTrue(auth.wos_logout_visible(page))
        self.assertEqual(page.evaluate('window.escapeCount'), 0)
        self.assertIsNone(page.evaluate('window.sessionEnded'))

    def test_wos_account_cleanup_does_not_dismiss_human_verification(self):
        page = self.context.new_page()
        for initially_shown in (True, False):
            with self.subTest(initially_shown=initially_shown):
                page.set_content(f'''<body><button data-ta="user-menu"
                  onclick="window.accountClicked=true;document.getElementById('menu').hidden=false;document.getElementById('challenge').hidden=false">Account</button>
                  <div id="menu" role="menu" hidden><button role="menuitem">Sign out</button></div>
                  <div id="challenge" {'hidden' if not initially_shown else ''}>Please verify you are human</div>
                  <script>window.accountClicked=false;window.escapeCount=0;
                    document.addEventListener('keydown', event => {{
                      if(event.key==='Escape'){{window.escapeCount++;document.getElementById('challenge').hidden=true;}}
                    }});
                  </script></body>''')
                with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$'):
                    auth.wos_authenticated(page)
                self.assertEqual(page.evaluate('window.accountClicked'), not initially_shown)
                self.assertEqual(page.evaluate('window.escapeCount'), 0)
                self.assertTrue(page.locator('#challenge').is_visible())

    def test_wos_current_russian_account_button_without_visible_logout_is_not_proof(self):
        page = self.context.new_page()
        page.set_content('''<body><button data-ta="wos-header-user_name"
            aria-label="Раскрывающееся меню параметров учетной записи для пользователя Arseniy Sitkovskiy"
            onclick="document.getElementById('menu').hidden=false">Arseniy Sitkovskiy</button>
            <div id="menu" role="menu" hidden><button role="menuitem">Мой профиль</button>
            <button role="menuitem">Настройки</button>
            <button role="menuitem" hidden>Завершить сеанс и выйти</button></div></body>''')
        self.assertFalse(auth.wos_authenticated(page))
        self.assertFalse(auth.wos_logout_visible(page))

    def test_captcha_is_explicit_and_not_interacted_with(self):
        page = self.context.new_page()
        page.set_content('<body>private page text Please verify you are human<button onclick="window.clicked=true">Continue</button></body>')
        with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$') as caught:
            auth.assert_no_challenge(page)
        self.assertEqual({key: caught.exception.verification_evidence[key] for key in ('trigger', 'marker_ids')}, {'trigger': 'page_marker', 'marker_ids': ['verify_you_are_human']})
        self.assertIsNone(page.evaluate('window.clicked'))

    def test_normal_recaptcha_widget_evidence_does_not_weaken_guard(self):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body>private frame text<div id="recaptcha-anchor" role="checkbox" aria-checked="true" onclick="window.clicked=true">private checkbox</div><input value="private value"></body>'))
        page = self.context.new_page()
        page.set_content('<body><iframe src="https://www.google.com/recaptcha/api2/anchor?size=normal&amp;token=private#private" title="private title"></iframe></body>')
        with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$') as caught:
            auth.assert_no_challenge(page)
        evidence = caught.exception.verification_evidence
        self.assertEqual(evidence['trigger'], 'recaptcha_normal_widget')
        frame = evidence['frame']
        self.assertEqual(frame['provider_path'], '/recaptcha/api2/anchor')
        self.assertTrue(frame['checkbox_present'])
        self.assertTrue(frame['checkbox_visible'])
        self.assertTrue(frame['checkbox_checked'])
        self.assertFalse(frame['active_challenge_controls'])
        self.assertTrue(frame['center_hit_iframe'])
        self.assertNotIn('private', str(evidence))
        self.assertIsNone(page.frames[1].evaluate('window.clicked'))

    def test_active_frame_and_occlusion_are_diagnostic_only(self):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body><div class="rc-imageselect">private challenge</div><button id="recaptcha-verify-button" onclick="window.clicked=true">Verify</button></body>'))
        page = self.context.new_page()
        page.set_content('<body><div role="dialog" aria-modal="true"><iframe title="private challenge title" src="https://www.google.com/recaptcha/api2/bframe?token=private"></iframe></div><div style="position:fixed;inset:0;z-index:999">Overlay</div></body>')
        with self.assertRaisesRegex(auth.AuthFailure, '^human_verification_required$') as caught:
            auth.assert_no_challenge(page)
        evidence = caught.exception.verification_evidence
        self.assertEqual(evidence['trigger'], 'iframe_title')
        self.assertTrue(evidence['frame']['active_challenge_controls'])
        self.assertTrue(evidence['frame']['ancestor_modal'])
        self.assertFalse(evidence['frame']['center_hit_iframe'])
        self.assertFalse(evidence['frame']['checkbox_present'])
        self.assertNotIn('private', str(evidence))
        self.assertIsNone(page.frames[1].evaluate('window.clicked'))

    def test_wos_direct_clarivate_redirect_chooses_orcid_first(self):
        visited = []
        authenticated = False
        profile_url = 'https://www.webofscience.com/wos/author/record/TEST'

        def route(request):
            nonlocal authenticated
            url = request.request.url
            visited.append(url)
            if url == 'https://www.webofscience.com/' or (url == profile_url and not authenticated):
                html = '<body><script>location.href="https://access.clarivate.com/login"</script></body>'
            elif url.startswith('https://access.clarivate.com/'):
                html = '<body><form action="https://invalid.test/empty-password"><button>Sign In</button></form><a href="https://orcid.org/oauth/authorize">ORCID</a></body>'
            elif url == 'https://orcid.org/finish':
                authenticated = True
                html = f'<body><script>location.href="{profile_url}"</script></body>'
            elif url.startswith('https://orcid.org/oauth/'):
                html = '<body><form method="post" action="/finish"><input id="username-input"><input type="password"><button>Sign in</button></form></body>'
            elif authenticated:
                html = '<body><button data-ta="user-menu" onclick="document.getElementById(\'menu\').hidden=false">Account</button><div id="menu" hidden><button><span>exit_to_app</span> Sign out</button></div></body>'
            else:
                html = '<body>Unexpected route</body>'
            request.fulfill(status=200, content_type='text/html', body=html)

        self.context.route('**/*', route)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'fixture-password'}):
            page = auth.login_wos(self.context, profile_url, timeout=30)
        self.assertTrue(auth.wos_authenticated(page))
        self.assertFalse(any('invalid.test' in url for url in visited))

    def test_diagnostics_exclude_form_values_and_url_query(self):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body>Private body token<input name="username" value="private-login"><input type="password" value="private-password"><button>Sign In</button></body>'))
        page = self.context.new_page()
        page.goto('https://orcid.org/oauth/authorize?token=private-query#private-fragment')
        diagnostics = str(auth.safe_browser_diagnostics(self.context))
        for secret in ('private-login', 'private-password', 'Private body token', 'private-query', 'private-fragment'):
            self.assertNotIn(secret, diagnostics)
        self.assertIn('username', diagnostics)

    def test_orcid_rejected_credentials_are_evidence_not_callback_timeout(self):
        def route(request):
            url = request.request.url
            if url == 'https://www.webofscience.com/' or '/wos/author/record/TEST' in url:
                html = '<body><script>location.href="https://access.clarivate.com/login"</script></body>'
            elif url.startswith('https://access.clarivate.com/'):
                html = '<body><a href="https://orcid.org/signin">ORCID</a></body>'
            elif url.endswith('/signin/auth.json'):
                request.fulfill(status=200, content_type='application/json', body='{"success":false,"email":"private@example.test","errors":["private server detail"]}')
                return
            else:
                html = '''<body><form onsubmit="event.preventDefault();fetch('/signin/auth.json',{method:'POST',body:'fixture'});"><input id="username-input"><input type="password"><button id="signin-button" type="submit">Sign in to ORCID</button></form></body>'''
            request.fulfill(status=200, content_type='text/html', body=html)
        self.context.route('**/*', route)
        with patch.dict(os.environ, {'WOS_ORCID_USERNAME': 'fixture@example.test', 'WOS_ORCID_PASSWORD': 'fixture-password'}):
            with self.assertRaises(auth.AuthFailure) as caught:
                auth.login_wos(self.context, 'https://www.webofscience.com/wos/author/record/TEST', timeout=20)
        self.assertEqual(caught.exception.reason, 'orcid_signin_rejected')
        self.assertFalse(caught.exception.authentication_evidence['success'])
        self.assertNotIn('private', str(caught.exception.authentication_evidence))


if __name__ == '__main__':
    unittest.main()
