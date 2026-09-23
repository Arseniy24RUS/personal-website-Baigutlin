"""One passive profile-entry window; no real source or challenge interaction."""
import json
import contextlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import provider_auth as auth
import harvest_wos_authenticated as wos
from test_challenge_settling import ObservationPage, loading_frame


class TimedPage(ObservationPage):
    url = 'https://www.webofscience.com/wos/author/record/FIXTURE-1'

    def __init__(self, *, clear_at=None, interactive_at=None, changing=False, active=True):
        super().__init__([{}])
        self.clear_at, self.interactive_at = clear_at, interactive_at
        self.changing, self.active = changing, active

    @property
    def state(self):
        frame = loading_frame(ready_state='loading', frame_text_length=8, frame_tag_count=2,
                              frame_button_count=0, frame_role_button_count=0,
                              frame_input_count=0, frame_canvas_count=0)
        if self.changing:
            frame['ready_state'] = 'complete' if self.step % 2 else 'loading'
            frame['provider_host'] = 'newassets.hcaptcha.com' if self.step % 2 else 'assets.hcaptcha.com'
        if self.interactive_at is not None and self.clock >= self.interactive_at:
            frame['active_challenge_controls'] = True
        frames = [frame] if self.active and (self.clear_at is None or self.clock < self.clear_at) else []
        return {'text': 'FIXTURE-1', 'frames': frames}

    def inner_text(self, **kwargs):
        if 'timeout' in kwargs:
            self.read_timeouts.append(kwargs['timeout'])
        return self.state['text']

    def content(self):
        return '<body>FIXTURE-1</body>'


class VerificationReadinessTests(unittest.TestCase):
    def observe(self, **options):
        page = TimedPage(**options)
        scope = ExitStack()
        self.addCleanup(scope.close)
        scope.enter_context(patch.object(auth.time, 'monotonic', side_effect=lambda: page.clock))
        scope.enter_context(patch.object(auth, 'in_visible_viewport', return_value=True))
        scope.enter_context(patch.object(auth, 'challenge_frame_evidence', side_effect=lambda frame, **kwargs: dict(frame)))
        self.addCleanup(lambda: self.assertEqual(page.mutations, []))
        return page

    def test_default_window_remains_ten_seconds(self):
        page = self.observe()
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page)
        self.assertEqual(page.clock, 10)
        self.assertEqual(caught.exception.verification_evidence['observation_budget_seconds'], 10)

    def test_late_natural_disappearance_returns_safe_success_trace(self):
        page = self.observe(clear_at=11)
        result = auth.assert_no_challenge(page, passive_wait_seconds=60)
        self.assertEqual(page.clock, 11)
        self.assertEqual(result['settling'], 'cleared')
        self.assertEqual(result['observation_timeline'][-1]['category'], 'clear')
        self.assertEqual(result['observation_timeline'][-1]['hcaptcha_frame_count'], 0)
        self.assertEqual([row['elapsed_seconds'] for row in result['observation_timeline']], [0, 2, 5, 10, 11])
        self.assertNotIn('hcaptcha.com', json.dumps(result))
        self.assertNotIn('FIXTURE-1', json.dumps(result))

    def test_frame_replacement_never_restarts_deadline_and_timeline_is_capped(self):
        page = self.observe(changing=True)
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=60)
        self.assertEqual(page.clock, 60)
        timeline = caught.exception.verification_evidence['observation_timeline']
        self.assertLessEqual(len(timeline), 16)
        self.assertEqual(timeline[-1]['category'], 'timed_out')
        self.assertEqual(timeline[-1]['elapsed_seconds'], 60)

    def test_new_recognized_interaction_stops_without_further_wait(self):
        page = self.observe(interactive_at=2)
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=60)
        self.assertEqual(page.clock, 2)
        evidence = caught.exception.verification_evidence
        self.assertEqual(evidence['settling'], 'stopped_by_guard')
        self.assertEqual(evidence['observation_timeline'][-1]['category'], 'interactive')

    def test_external_deadline_is_shared_with_delayed_start(self):
        page = self.observe()
        page.clock = 7
        with self.assertRaises(auth.AuthFailure) as caught:
            auth.assert_no_challenge(page, passive_wait_seconds=60, passive_deadline=60)
        self.assertEqual(page.clock, 60)
        self.assertEqual(caught.exception.verification_evidence['observation_budget_seconds'], 53)

    def test_menu_revealed_frame_blocks_profile_return_until_it_clears(self):
        page = self.observe(active=False, clear_at=11)
        def open_menu(_):
            page.active = True
            return True
        with patch.object(wos, 'wos_authenticated', side_effect=open_menu):
            self.assertEqual(wos.target_profile_html(page, 'FIXTURE-1', profile_entry_wait_seconds=60), page.content())
        self.assertEqual(page.clock, 11)
        self.assertEqual(page._profile_entry_observation['settling'], 'cleared')

    def test_ready_metrics_and_target_cannot_override_persistent_frame(self):
        page = self.observe(active=False)
        def open_menu(_):
            page.active = True
            return True
        with patch.object(wos, 'wos_authenticated', side_effect=open_menu), self.assertRaises(auth.AuthFailure):
            wos.target_profile_html(page, 'FIXTURE-1', profile_entry_wait_seconds=60)
        self.assertEqual(page.clock, 60)

    def test_disappearance_after_redirect_requires_new_authorization_proof(self):
        page = self.observe(active=False, clear_at=11)
        def open_menu(_):
            page.active = True
            if page.clock >= 11:
                page.url = 'https://www.webofscience.com/wos/author/record/OTHER'
            return True
        with patch.object(wos, 'wos_authenticated', side_effect=open_menu), self.assertRaisesRegex(auth.AuthFailure, '^wrong_author_profile$'):
            wos.target_profile_html(page, 'FIXTURE-1', profile_entry_wait_seconds=60)
        self.assertEqual(page.clock, 11)

    def test_normal_collection_guard_does_not_extend_or_replace_entry_trace(self):
        page = self.observe(active=False)
        trace = {'settling': 'cleared', 'elapsed_seconds': 11}
        page._profile_entry_observation = trace
        with patch.object(wos, 'wos_authenticated', return_value=True), patch.object(wos, 'assert_no_challenge', wraps=auth.assert_no_challenge) as guard:
            wos.target_profile_html(page, 'FIXTURE-1')
        self.assertTrue(all(not call.kwargs for call in guard.call_args_list))
        self.assertIs(page._profile_entry_observation, trace)

    def test_invalid_or_unbounded_budgets_are_rejected(self):
        page = self.observe()
        for budget in (0, -1, 61, float('inf'), float('nan'), True):
            with self.subTest(budget=budget), self.assertRaisesRegex(auth.AuthFailure, '^challenge_observation_budget_invalid$'):
                auth.assert_no_challenge(page, passive_wait_seconds=budget)
        self.assertEqual(page.clock, 0)

    def test_success_trace_reaches_maintenance_and_collection_reports(self):
        import browser_sessions
        trace = {'settling': 'cleared', 'elapsed_seconds': 11,
                 'observation_budget_seconds': 60,
                 'observation_timeline': [{'elapsed_seconds': 11, 'category': 'clear', 'ready_state': 'unavailable'}]}
        for maintenance in ('0', '1'):
            with self.subTest(maintenance=maintenance), tempfile.TemporaryDirectory() as directory:
                page = MagicMock()
                page._profile_entry_observation = trace
                report_path = Path(directory) / 'data/wos/harvest_report.json'
                with patch.dict(os.environ, {'BROWSER_SESSION_MAINTENANCE': maintenance, 'BROWSER_SESSION_REPORT_DIR': directory}), patch('playwright.sync_api.sync_playwright'), patch.object(browser_sessions, 'restore_context', return_value=(MagicMock(), {'status': 'restored'})), patch.object(browser_sessions, 'checkpoint_session', return_value={'status': 'checkpointed'}), patch.object(wos, 'OUT', report_path.parent / 'profile_metrics.json'), patch.object(wos, 'REPORT', report_path), patch.object(wos, 'load_checkpoint', return_value=None), patch.object(wos, 'read_json', return_value={}), patch.object(wos, 'verify_browser_egress'), patch.object(wos, 'authenticated_page', return_value=(page, 'existing_session_verified')), patch.object(wos, 'collect_from_page', return_value=({'status': 'success', 'complete': True}, {'metrics': {}, 'publications': [], 'details': {}})), patch.object(wos, 'write_checkpoint') as write, patch.object(wos, 'materialize_checkpoint'), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(wos.main(), 0)
                saved = json.loads(output.getvalue())
                self.assertEqual(saved['profile_entry_observation'], trace)
                if maintenance == '1':
                    saved_file = json.loads((Path(directory) / 'wos.json').read_text())
                    self.assertEqual(saved_file['profile_entry_observation'], trace)
                else:
                    self.assertEqual(write.call_args.args[1]['profile_entry_observation'], trace)


if __name__ == '__main__':
    unittest.main()
