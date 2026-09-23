"""Explicit fresh ORCID starts before networking; no real provider requests."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import browser_sessions as sessions
import harvest_wos_authenticated as wos
from provider_auth import AuthFailure


class FreshAuthModeTests(unittest.TestCase):
    def test_fresh_context_never_reads_or_hydrates_saved_input(self):
        browser = MagicMock()
        options = {'locale': 'en-US', 'timezone_id': 'Europe/Moscow', 'viewport': {'width': 1440, 'height': 1100}}
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / 'wos.json'
            saved.write_text('unchanged private fixture', encoding='utf-8')
            with patch.dict(os.environ, {'BROWSER_SESSION_INPUT_WOS': str(saved)}), \
                    patch.object(sessions, 'restore_context') as restore, \
                    patch.object(sessions, 'hydrate_session_storage') as hydrate, \
                    patch.object(Path, 'read_text', side_effect=AssertionError('fresh login read saved state')):
                context, info = wos.initial_browser_context(browser, options, 'fresh_orcid')
            self.assertEqual(saved.read_text(encoding='utf-8'), 'unchanged private fixture')
        self.assertIs(context, browser.new_context.return_value)
        self.assertEqual(info, {'status': 'skipped', 'reason': 'fresh_orcid_login_requested'})
        browser.new_context.assert_called_once_with(**options)
        restore.assert_not_called()
        hydrate.assert_not_called()
        context.add_init_script.assert_not_called()

    def test_default_restore_preserves_existing_invalid_and_missing_states(self):
        browser = MagicMock()
        for status in ('restored', 'missing', 'invalid'):
            with self.subTest(status=status):
                expected = (MagicMock(), {'status': status})
                with patch.object(sessions, 'restore_context', return_value=expected) as restore:
                    self.assertIs(wos.initial_browser_context(browser, {'locale': 'en-US'}), expected)
                restore.assert_called_once_with(browser, 'wos', locale='en-US')
        browser.new_context.assert_not_called()

    def test_unknown_mode_does_not_create_or_restore_context(self):
        browser = MagicMock()
        with patch.object(sessions, 'restore_context') as restore:
            with self.assertRaisesRegex(AuthFailure, '^wos_auth_mode_invalid$'):
                wos.initial_browser_context(browser, {}, 'private-invalid-value')
        browser.new_context.assert_not_called()
        restore.assert_not_called()

    def test_fresh_challenge_attempts_ordinary_login_once_without_reset(self):
        context = MagicMock()
        reset = MagicMock()
        failure = AuthFailure('human_verification_required')
        with patch.object(wos, 'login_wos', side_effect=failure) as login, \
                patch.object(wos, 'target_profile_html') as verify:
            with self.assertRaisesRegex(AuthFailure, '^human_verification_required$'):
                wos.authenticated_page(context, {'status': 'skipped', 'reason': 'fresh_orcid_login_requested'},
                                       target='FIXTURE-1', fresh_context=reset)
        login.assert_called_once_with(context, 'https://www.webofscience.com/wos/author/record/FIXTURE-1', wos.WAIT_SEC)
        verify.assert_not_called()
        reset.assert_not_called()

    def test_fresh_success_still_requires_target_proof_and_invalid_restore_still_fails(self):
        context, page = MagicMock(), MagicMock()
        with patch.object(wos, 'login_wos', return_value=page) as login, \
                patch.object(wos, 'target_profile_html', return_value='verified') as verify:
            self.assertEqual(wos.authenticated_page(context, {'status': 'skipped'}, target='FIXTURE-1'),
                             (page, 'fresh_orcid_login'))
            verify.assert_called_once_with(page, 'FIXTURE-1', profile_entry_wait_seconds=60.0)
            with self.assertRaisesRegex(AuthFailure, '^invalid_session_checkpoint$'):
                wos.authenticated_page(context, {'status': 'invalid'})
            self.assertEqual(login.call_count, 1)

    def test_post_login_profile_challenge_retains_safe_login_progress_without_retry(self):
        context, page, reset = MagicMock(), MagicMock(), MagicMock()
        page._wos_login_evidence = {'stage': 'complete', 'submit_clicked': True,
                                    'wos_return_observed': True, 'password': 'private-fixture'}
        with patch.object(wos, 'login_wos', return_value=page) as login, \
                patch.object(wos, 'target_profile_html', side_effect=AuthFailure('human_verification_required')), \
                patch.object(wos, 'safe_browser_diagnostics', return_value=[]):
            with self.assertRaisesRegex(AuthFailure, '^human_verification_required$') as caught:
                wos.authenticated_page(context, {'status': 'skipped'}, fresh_context=reset)
        self.assertEqual(caught.exception.authentication_evidence,
                         {'stage': 'complete', 'submit_clicked': True, 'wos_return_observed': True})
        login.assert_called_once()
        reset.assert_not_called()

    def run_main(self, *, mode, failure=None, maintenance='1', evidence=None):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            env = {'BROWSER_SESSION_MAINTENANCE': maintenance, 'BROWSER_SESSION_REPORT_DIR': directory}
            if mode is not None:
                env['WOS_AUTH_MODE'] = mode
            stack.enter_context(patch.dict(os.environ, env))
            if mode is None:
                os.environ.pop('WOS_AUTH_MODE', None)
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            playwright = stack.enter_context(patch('playwright.sync_api.sync_playwright'))
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            initial = stack.enter_context(patch.object(wos, 'initial_browser_context', return_value=(MagicMock(), {'status': 'skipped'})))
            page = MagicMock()
            page._wos_login_evidence = evidence
            authenticated = stack.enter_context(patch.object(wos, 'authenticated_page',
                side_effect=failure, return_value=(page, 'fresh_orcid_login')))
            checkpoint = stack.enter_context(patch.object(sessions, 'checkpoint_session',
                return_value={'status': 'checkpointed', 'validated_at': sessions.now()}))
            for name, value in (('REPORT', Path(directory) / 'data/wos/harvest_report.json'),
                                ('OUT', Path(directory) / 'data/wos/profile_metrics.json')):
                stack.enter_context(patch.object(wos, name, value))
            stack.enter_context(patch.object(wos, 'load_checkpoint', return_value=None))
            stack.enter_context(patch.object(wos, 'read_json', return_value={}))
            stack.enter_context(patch.object(wos, 'verify_browser_egress'))
            stack.enter_context(patch.object(wos, 'materialize_checkpoint'))
            writes = stack.enter_context(patch.object(wos, 'write_checkpoint'))
            code = wos.main()
            report = json.loads(output.getvalue())
            if maintenance == '1':
                self.assertEqual(json.loads((Path(directory) / 'wos.json').read_text()), report)
            else:
                self.assertEqual(writes.call_args.args[1], report)
            return code, report, playwright, initial, authenticated, checkpoint, browser

    def test_main_reports_fresh_mode_on_success_and_failure_without_checkpointing_failure(self):
        code, report, _, initial, _, checkpoint, _ = self.run_main(mode='fresh_orcid', evidence={
            'stage': 'complete', 'submit_clicked': True, 'wos_return_observed': True, 'password': 'private-fixture'})
        self.assertEqual(code, 0)
        self.assertEqual(report['authentication_mode'], 'fresh_orcid')
        self.assertEqual(report['authentication'], 'fresh_orcid_login')
        self.assertEqual(report['authentication_evidence'],
                         {'stage': 'complete', 'submit_clicked': True, 'wos_return_observed': True})
        self.assertEqual(initial.call_args.args[2], 'fresh_orcid')
        checkpoint.assert_called_once()
        for maintenance in ('0', '1'):
            with self.subTest(maintenance=maintenance):
                code, report, _, _, login, checkpoint, _ = self.run_main(
                    mode='fresh_orcid', failure=AuthFailure('human_verification_required'), maintenance=maintenance)
                self.assertEqual(code, 2)
                self.assertEqual(report['authentication_mode'], 'fresh_orcid')
                self.assertEqual(report['reason'], 'human_verification_required')
                login.assert_called_once()
                checkpoint.assert_not_called()

    def test_main_defaults_to_restore_and_unknown_mode_is_safely_reported_before_browser(self):
        _, report, _, initial, _, _, _ = self.run_main(mode=None)
        self.assertEqual(report['authentication_mode'], 'restore')
        self.assertEqual(initial.call_args.args[2], 'restore')
        code, report, playwright, initial, login, checkpoint, _ = self.run_main(mode='private-invalid-value')
        self.assertEqual(code, 2)
        self.assertEqual(report['authentication_mode'], 'invalid')
        self.assertEqual(report['reason'], 'wos_auth_mode_invalid')
        self.assertEqual(report['stage'], 'configuration')
        self.assertNotIn('private-invalid-value', json.dumps(report))
        playwright.assert_not_called()
        initial.assert_not_called()
        login.assert_not_called()
        checkpoint.assert_not_called()


if __name__ == '__main__':
    unittest.main()
