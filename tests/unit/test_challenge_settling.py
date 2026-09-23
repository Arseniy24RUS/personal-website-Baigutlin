"""Bounded observation of automatic hCaptcha loading; no external requests."""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth


def loading_frame(**changes):
    return {
        'provider_host': 'newassets.hcaptcha.com',
        'title_challenge': True,
        'frame_dom_observed': True,
        'frame_host_matches_provider': True,
        'checkbox_present': False,
        'checkbox_visible': False,
        'active_challenge_controls': False,
        'marker_ids': [],
        **changes,
    }


class LocatorList:
    def __init__(self, rows):
        self.rows = rows

    def count(self):
        return len(self.rows)

    def nth(self, index):
        return self.rows[index]


class ObservationPage:
    """Only body/frame reads and waiting are allowed by this fixture."""
    url = 'https://www.webofscience.com/'

    def __init__(self, states):
        self.states = states
        self.step = 0
        self.clock = 0.0
        self.waits = []
        self.mutations = []
        self.read_timeouts = []

    @property
    def state(self):
        return self.states[min(self.step, len(self.states) - 1)]

    def locator(self, selector):
        if selector == 'body':
            return self
        if selector.startswith('iframe['):
            return LocatorList(self.state.get('frames', []))
        raise AssertionError('Unexpected selector: ' + selector)

    def inner_text(self, **kwargs):
        self.read_timeouts.append(kwargs['timeout'])
        self.clock += self.state.get('read_seconds', 0)
        if self.state.get('read_error'):
            raise self.state['read_error']
        return self.state.get('text', '')

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock += milliseconds / 1000
        self.step += 1

    def forbidden_mutation(self, *args, **kwargs):
        self.mutations.append(True)
        raise AssertionError('Challenge observation must not mutate the browser')

    click = fill = press = goto = evaluate = eval_on_selector_all = forbidden_mutation


class ChallengeSettlingTests(unittest.TestCase):
    def observe(self, states):
        page = ObservationPage(states)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(auth.time, 'monotonic', side_effect=lambda: page.clock))
        stack.enter_context(patch.object(auth, 'CHALLENGE_SETTLE_SECONDS', 1.0))
        stack.enter_context(patch.object(auth, 'CHALLENGE_POLL_SECONDS', 0.25))
        stack.enter_context(patch.object(auth, 'in_visible_viewport', return_value=True))
        stack.enter_context(patch.object(auth, 'challenge_frame_evidence', side_effect=lambda frame, **kwargs: dict(frame)))
        self.addCleanup(lambda: self.assertEqual(page.mutations, []))
        return page

    def test_transient_frame_disappears_naturally(self):
        page = self.observe([{'frames': [loading_frame()]}, {}])
        self.assertEqual(auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)['settling'], 'cleared')
        self.assertEqual(page.waits, [250.0])

    def test_persistent_frame_fails_at_one_deadline(self):
        page = self.observe([{'frames': [loading_frame()]}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(sum(page.waits), 1000.0)

    def test_becomes_interactive_before_deadline(self):
        page = self.observe([
            {'frames': [loading_frame()]},
            {'frames': [loading_frame(active_challenge_controls=True)]},
        ])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.verification_evidence['settling'], 'stopped_by_guard')
        self.assertEqual(page.waits, [250.0])

    def test_page_human_marker_fails_without_wait(self):
        page = self.observe([{'text': 'Please verify you are human', 'frames': [loading_frame()]}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual({key: caught.exception.verification_evidence[key] for key in ('trigger', 'marker_ids')}, {
            'trigger': 'page_marker', 'marker_ids': ['verify_you_are_human'],
        })
        self.assertEqual(page.waits, [])

    def test_page_marker_appearing_during_wait_is_rechecked(self):
        page = self.observe([{'frames': [loading_frame()]}, {'text': 'Authentication code required'}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.reason, 'mfa_required')
        self.assertEqual(page.waits, [250.0])

    def test_other_interactive_frame_is_not_hidden_by_loader(self):
        page = self.observe([{'frames': [loading_frame(), loading_frame(checkbox_present=True, checkbox_visible=True)]}])
        with self.assertRaises(auth.AuthFailure):
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(page.waits, [])

    def test_replaced_loader_does_not_restart_deadline(self):
        page = self.observe([
            {'frames': [loading_frame()]},
            {'frames': [loading_frame(provider_host='otherassets.hcaptcha.com')]},
            {'frames': [loading_frame(), loading_frame()]},
        ])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(sum(page.waits), 1000.0)

    def test_first_observation_is_part_of_the_total_budget(self):
        page = self.observe([
            {'frames': [loading_frame()], 'read_seconds': 0.6},
            {'frames': [loading_frame()]},
        ])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertAlmostEqual(sum(page.waits), 400.0)
        self.assertLessEqual(max(page.read_timeouts), 1000.0)

    def test_slow_initial_observation_cannot_succeed_after_deadline(self):
        page = self.observe([{'read_seconds': 1.2}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.reason, 'challenge_observation_incomplete')
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(page.waits, [])

    def test_late_disappearance_cannot_succeed_after_deadline(self):
        page = self.observe([{'frames': [loading_frame()]}, {'read_seconds': 1.0}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(page.waits, [250.0])

    def test_read_failure_is_closed_and_has_fixed_evidence(self):
        page = self.observe([{}])
        with patch.object(page, 'inner_text', side_effect=RuntimeError('private observation detail')):
            with self.assertRaises(auth.AuthFailure) as caught:
                auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertEqual(caught.exception.reason, 'challenge_observation_incomplete')
        self.assertEqual(caught.exception.verification_evidence['trigger'], 'observation_incomplete')
        self.assertNotIn('private', str(caught.exception))

    def test_locator_timeout_at_pending_deadline_preserves_known_challenge(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        for error_type in (TimeoutError, PlaywrightTimeoutError):
            with self.subTest(error_type=error_type.__module__):
                page = self.observe([{'frames': [loading_frame()]}])
                read = page.inner_text
                def timeout_after_first_read(**kwargs):
                    if page.step:
                        page.clock = 1.0
                        raise error_type('private timeout detail')
                    return read(**kwargs)
                with patch.object(page, 'inner_text', side_effect=timeout_after_first_read), self.assertRaises(auth.AuthFailure) as caught:
                    auth.assert_no_challenge(page, passive_wait_seconds=1.0)
                evidence = caught.exception.verification_evidence
                self.assertEqual(caught.exception.reason, 'human_verification_required')
                self.assertEqual(evidence['settling'], 'timed_out')
                self.assertEqual(evidence['trigger'], 'iframe_title')
                self.assertEqual(evidence['observation_timeline'][-1]['category'], 'timed_out')
                self.assertNotIn('private', str(evidence))
                self.assertEqual(page.waits, [250.0])

    def test_early_locator_timeout_preserves_pending_within_original_deadline(self):
        page = self.observe([{'frames': [loading_frame()]}])
        read = page.inner_text
        def timeout_after_first_read(**kwargs):
            if page.step:
                raise TimeoutError('private timeout detail')
            return read(**kwargs)
        with patch.object(page, 'inner_text', side_effect=timeout_after_first_read), self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=1.0)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(page.clock, 1.0)
        self.assertEqual(page.waits, [250.0] * 4)

    def test_one_second_dom_timeout_is_retried_then_requires_a_complete_clear_scan(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        page = self.observe([{'read_seconds': 1.0, 'read_error': PlaywrightTimeoutError('private URL/token')}, {}])
        trace = auth.assert_no_challenge(page)
        self.assertEqual(page.clock, 1.25)
        self.assertEqual(page.waits, [250.0])
        self.assertEqual(len(page.read_timeouts), 2)
        self.assertEqual(trace['settling'], 'cleared')
        first, final = trace['observation_timeline'][0], trace['observation_timeline'][-1]
        self.assertEqual((first['category'], first['error_type'], first['read_phase']),
                         ('incomplete', 'timeout', 'page_text'))
        self.assertEqual(final['category'], 'clear')
        self.assertNotIn('private', str(trace))

    def test_known_playwright_navigation_errors_require_full_rescan(self):
        from playwright.sync_api import Error as PlaywrightError
        for message in ('Execution context was destroyed, most likely because of a navigation',
                        'Cannot find context with specified id', 'Frame was detached',
                        'Element is not attached to the DOM'):
            with self.subTest(message=message):
                page = self.observe([{'read_error': PlaywrightError(message + ': private URL/token')}, {}])
                trace = auth.assert_no_challenge(page)
                self.assertEqual(trace['settling'], 'cleared')
                self.assertEqual(trace['observation_timeline'][0]['error_type'], 'navigation')
                self.assertEqual(len(page.read_timeouts), 2)
                self.assertEqual(page.waits, [250.0])
                self.assertNotIn('private', str(trace))

    def test_persistent_read_timeout_never_becomes_a_challenge_or_resets_deadline(self):
        page = self.observe([{'read_error': TimeoutError('private detail')}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=1.0)
        evidence = caught.exception.verification_evidence
        self.assertEqual(caught.exception.reason, 'challenge_observation_incomplete')
        self.assertEqual(evidence['settling'], 'timed_out')
        self.assertEqual(evidence['error_type'], 'timeout')
        self.assertEqual(page.clock, 1.0)
        self.assertEqual(page.waits, [250.0] * 4)
        self.assertEqual(evidence['observation_timeline'][-1]['category'], 'timed_out')
        self.assertNotIn('private', str(evidence))

    def test_recovering_read_cannot_return_clear_after_shared_deadline(self):
        page = self.observe([{'read_error': TimeoutError('private detail')}, {'read_seconds': 1.0}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=1.0)
        self.assertEqual(caught.exception.reason, 'challenge_observation_incomplete')
        self.assertEqual(caught.exception.verification_evidence['settling'], 'timed_out')
        self.assertEqual(page.waits, [250.0])

    def test_real_challenge_or_mfa_after_transient_read_stops_immediately(self):
        for state, expected in (({'frames': [loading_frame(active_challenge_controls=True)]}, 'human_verification_required'),
                                ({'text': 'Authentication code required'}, 'mfa_required'),
                                ({'text': 'Verify you are human'}, 'human_verification_required')):
            with self.subTest(expected=expected, state=state):
                page = self.observe([{'read_error': TimeoutError('private detail')}, state])
                with self.assertRaises(auth.AuthFailure) as caught:
                    auth.assert_no_challenge(page, passive_wait_seconds=1.0)
                self.assertEqual(caught.exception.reason, expected)
                self.assertEqual(page.waits, [250.0])
                self.assertNotIn('private', str(caught.exception.verification_evidence))

    def test_unknown_or_closed_errors_fail_without_retry_or_raw_diagnostics(self):
        from playwright.sync_api import Error as PlaywrightError
        for error, category in ((RuntimeError('Execution context was destroyed, most likely because of a navigation: private'), 'unexpected'),
                                (PlaywrightError('private unknown protocol failure'), 'unexpected'),
                                (PlaywrightError('Target page, context or browser has been closed: private'), 'closed')):
            with self.subTest(category=category):
                page = self.observe([{'read_error': error}])
                with self.assertRaises(auth.AuthFailure) as caught:
                    auth.assert_no_challenge(page)
                evidence = caught.exception.verification_evidence
                self.assertEqual(caught.exception.reason, 'challenge_observation_incomplete')
                self.assertEqual((evidence['error_type'], evidence['read_phase']), (category, 'page_text'))
                self.assertEqual(page.waits, [])
                self.assertNotIn('private', str(evidence))

    def test_detached_pending_frame_is_not_clear_until_full_later_scan(self):
        from playwright.sync_api import Error as PlaywrightError
        page = self.observe([{'frames': [loading_frame()]},
                             {'frames': [loading_frame()]}, {}])
        def visibility(frame, **kwargs):
            if page.step == 1:
                raise PlaywrightError('Frame was detached: private')
            return True
        with patch.object(auth, 'in_visible_viewport', side_effect=visibility):
            trace = auth.assert_no_challenge(page, passive_wait_seconds=1.0)
        self.assertEqual(trace['settling'], 'cleared')
        self.assertEqual(page.waits, [250.0, 250.0])
        self.assertEqual(len(page.read_timeouts), 3)
        failure_sample = trace['observation_timeline'][1]
        self.assertEqual((failure_sample['error_type'], failure_sample['read_phase']), ('navigation', 'frame_visibility'))

    def test_explicit_auth_failure_is_never_wrapped_or_retried(self):
        failure = auth.AuthFailure('mfa_required')
        page = self.observe([{'read_error': failure}])
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page)
        self.assertIs(caught.exception, failure)
        self.assertEqual(page.waits, [])

    def test_unrecognized_or_incompletely_observed_frame_never_gets_grace(self):
        cases = [
            {'provider_host': 'hcaptcha.com.example.test'},
            {'provider_host': 'www.google.com'},
            {'frame_dom_observed': False},
            {'frame_host_matches_provider': False},
            {'observation_incomplete': True},
            {'marker_ids': ['verify_you_are_human']},
            {'marker_ids': None},
            {'checkbox_present': True},
            {'active_challenge_controls': None},
            {'title_challenge': False},
        ]
        for change in cases:
            with self.subTest(change=change):
                page = self.observe([{'frames': [loading_frame(**change)]}])
                with self.assertRaises(auth.AuthFailure):
                    auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
                self.assertEqual(page.waits, [])


class ChallengeFrameBrowserTests(unittest.TestCase):
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
                raise unittest.SkipTest('Install Playwright Chromium for browser authentication tests')
            options['executable_path'] = str(edge)
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def inspect_fixture(self, body):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=body))
        page = context.new_page()
        page.set_content('<iframe title="hCaptcha challenge" src="https://newassets.hcaptcha.com/captcha/private?token=private"></iframe>')
        # Real browser protocol round trips must fit inside the fixture budget.
        # Exact deadline behavior is covered by the deterministic clock tests.
        with patch.object(auth, 'CHALLENGE_SETTLE_SECONDS', 2.0):
            with self.assertRaises(auth.AuthFailure) as caught:
                auth.assert_no_challenge(page, passive_wait_seconds=auth.CHALLENGE_SETTLE_SECONDS)
        self.assertNotIn('private', str(caught.exception.verification_evidence))
        self.assertIsNone(page.evaluate('window.clicked'))
        return caught.exception.verification_evidence

    def navigation_with_delayed_body(self, body_text):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        def route(request):
            body = ('<body><script>location.replace("/profile")</script></body>'
                    if request.request.url.endswith('/callback') else
                    '<body><script>window.clicked=false;document.body.remove()</script></body>')
            request.fulfill(status=200, content_type='text/html', body=body)
        context.route('**/*', route)
        page = context.new_page()
        page.goto('https://local-fixture.invalid/callback')
        page.wait_for_url('https://local-fixture.invalid/profile')
        page.wait_for_function('document.body === null')
        # The app renders after the first real 1s body locator timeout. Only the
        # fixture mutates DOM; the guard may only read and wait through navigation.
        page.evaluate('''text => setTimeout(() => {
            const body=document.createElement('body');body.textContent=text;
            const button=document.createElement('button');button.textContent='Continue';
            button.onclick=()=>window.clicked=true;body.append(button);
            document.documentElement.append(body);
        }, 1500)''', body_text)
        return page

    def test_navigation_body_read_timeout_recovers_only_after_actual_dom_render(self):
        page = self.navigation_with_delayed_body('Ordinary profile content')
        trace = auth.assert_no_challenge(page, passive_wait_seconds=6)
        self.assertEqual(trace['settling'], 'cleared')
        self.assertTrue(any(sample.get('error_type') == 'timeout' and sample.get('read_phase') == 'page_text'
                            for sample in trace['observation_timeline']))
        self.assertEqual(trace['observation_timeline'][-1]['category'], 'clear')
        self.assertFalse(page.evaluate('window.clicked'))

    def test_navigation_body_read_timeout_does_not_skip_later_real_human_marker(self):
        page = self.navigation_with_delayed_body('Verify you are human')
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=6)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        evidence = caught.exception.verification_evidence
        self.assertTrue(any(sample.get('error_type') == 'timeout' for sample in evidence['observation_timeline']))
        self.assertEqual(evidence['observation_timeline'][-1]['category'], 'marker')
        self.assertFalse(page.evaluate('window.clicked'))

    def test_observed_empty_hcaptcha_dom_only_allows_bounded_wait(self):
        evidence = self.inspect_fixture('<body>Loading...</body>')
        self.assertEqual(evidence['settling'], 'timed_out')
        self.assertTrue(evidence['frame']['frame_dom_observed'])
        self.assertTrue(evidence['frame']['frame_host_matches_provider'])
        self.assertEqual(evidence['frame']['marker_ids'], [])

    def test_human_marker_inside_frame_is_immediate_failure(self):
        evidence = self.inspect_fixture('<body>Verify you are human<button onclick="window.clicked=true">Continue</button></body>')
        self.assertNotIn('settling', evidence)
        self.assertEqual(evidence['frame']['marker_ids'], ['verify_you_are_human'])

    def test_dom_counts_are_safe_observations_and_static_canvas_is_not_interaction(self):
        evidence = self.inspect_fixture('<body>private<canvas></canvas><svg><path></path></svg></body>')
        frame = evidence['frame']
        self.assertEqual(evidence['settling'], 'timed_out')
        self.assertFalse(frame['active_challenge_controls'])
        self.assertEqual(frame['frame_canvas_count'], 1)
        self.assertEqual(frame['frame_button_count'], 0)
        self.assertEqual(frame['frame_input_count'], 0)
        self.assertGreaterEqual(frame['frame_tag_count'], 3)
        self.assertGreater(frame['frame_text_length'], 0)
        self.assertIn(frame['ready_state'], ('loading', 'interactive', 'complete'))
        self.assertEqual(evidence['observation_timeline'][-1]['frame_canvas_count'], 1)

    def test_visible_native_or_role_buttons_inside_known_frame_stop_immediately(self):
        for markup in ('<button>Continue</button>', '<div role="button" tabindex="0">Continue</div>',
                       '<button>Continue</button>' + '<div role="button">Tile</div>' * 15):
            with self.subTest(markup=markup):
                evidence = self.inspect_fixture('<body>' + markup + '</body>')
                self.assertTrue(evidence['frame']['active_challenge_controls'])
                self.assertNotIn('settling', evidence)
                self.assertEqual(evidence['observation_timeline'][-1]['category'], 'interactive')

    def test_hidden_native_and_role_buttons_do_not_count_as_active_controls(self):
        evidence = self.inspect_fixture('<body><button hidden>Continue</button><div role="button" style="display:none">Tile</div></body>')
        self.assertFalse(evidence['frame']['active_challenge_controls'])
        self.assertEqual(evidence['settling'], 'timed_out')


if __name__ == '__main__':
    unittest.main()
