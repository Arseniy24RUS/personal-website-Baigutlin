"""Authentication failures retain parser evidence without publishing page values."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_wos_authenticated as wos
from provider_auth import AuthFailure


class WosProfileDiagnosticsTests(unittest.TestCase):
    def test_evidence_contains_only_finite_metrics_counts_and_known_field_names(self):
        parsed = {
            'page_title': 'private title', 'source_url': 'https://example.test/?token=private',
            'unexpected_private_field': 'private value',
            'summary': {'publications': 13, 'citations': 0, 'h_index': 2,
                        'total_documents': float('nan'), 'indexed_publications': True,
                        'core_collection_publications': 'private text'},
            'records': [{'title': 'private work', 'url': 'private signed URL', 'secret_key': 'private'}],
            'summary_metrics': {'private label': 13}, 'core_collection_metrics': {'private label': 2},
        }
        with patch.object(wos, 'parse_wos_author_profile_html', return_value=parsed):
            result = wos.safe_profile_diagnostics(MagicMock())
        self.assertEqual(result['parsed_record_count'], 1)
        self.assertEqual(result['summary']['citations'], 0)
        self.assertEqual(result['summary_metric_count'], 1)
        self.assertEqual(result['record_fields'], ['title', 'url'])
        for key in ('total_documents', 'indexed_publications', 'core_collection_publications'):
            self.assertIsNone(result['summary'][key])
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn('private', encoded)
        self.assertNotIn('secret_key', encoded)
        self.assertNotIn('https://', encoded)

    def test_timeout_observes_page_before_context_closes_without_login_fallback(self):
        context = MagicMock()
        page = context.new_page.return_value
        page.url = wos.PROFILE_URL
        with patch.object(wos, 'WAIT_SEC', 0), patch.object(wos, 'safe_browser_diagnostics', return_value=[]), patch.object(wos, 'safe_profile_diagnostics', return_value={'parsed_record_count': 13}) as inspect, patch.object(wos, 'login_wos') as login:
            with self.assertRaises(AuthFailure) as caught:
                wos.authenticated_page(context, {'status': 'restored'})
        self.assertEqual(caught.exception.reason, 'profile_not_authenticated_or_changed')
        self.assertEqual(caught.exception.profile_diagnostics, {'parsed_record_count': 13})
        inspect.assert_called_once_with(page)
        login.assert_not_called()
        page.close.assert_not_called()
        context.close.assert_not_called()

    def test_parser_failure_reports_only_fixed_boolean(self):
        with patch.object(wos, 'parse_wos_author_profile_html', side_effect=ValueError('private parser detail')):
            self.assertEqual(wos.safe_profile_diagnostics(MagicMock()), {'parser_failed': True})

    def test_failure_evidence_reaches_maintenance_and_collection_reports(self):
        import browser_sessions
        for maintenance in ('0', '1'):
            failure = AuthFailure('profile_not_authenticated_or_changed')
            failure.profile_diagnostics = {'parsed_record_count': 13, 'summary': {'citations': 0}}
            with self.subTest(maintenance=maintenance), tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / 'data' / 'wos' / 'harvest_report.json'
                with patch.dict(os.environ, {'BROWSER_SESSION_MAINTENANCE': maintenance, 'BROWSER_SESSION_REPORT_DIR': directory}), patch('playwright.sync_api.sync_playwright'), patch.object(browser_sessions, 'restore_context', return_value=(MagicMock(), {'status': 'restored'})), patch.object(browser_sessions, 'checkpoint_session') as checkpoint, patch.object(wos, 'REPORT', report_path), patch.object(wos, 'OUT', report_path.parent / 'profile_metrics.json'), patch.object(wos, 'load_checkpoint', return_value=None), patch.object(wos, 'read_json', return_value={}), patch.object(wos, 'verify_browser_egress'), patch.object(wos, 'authenticated_page', side_effect=failure), patch.object(wos, 'write_checkpoint') as write, patch.object(wos, 'materialize_checkpoint'), contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(wos.main(), 2)
                saved = json.loads(stdout.getvalue())
                self.assertEqual(saved['profile_diagnostics'], failure.profile_diagnostics)
                self.assertEqual(saved['status'], 'blocked')
                checkpoint.assert_not_called()
                if maintenance == '1':
                    write.assert_not_called()
                    disk = json.loads((Path(directory) / 'wos.json').read_text())
                    self.assertEqual(disk['profile_diagnostics'], failure.profile_diagnostics)
                    self.assertNotIn('last_success_at', disk)
                else:
                    self.assertEqual(write.call_args.args[1]['profile_diagnostics'], failure.profile_diagnostics)
                    self.assertFalse(saved['complete'])


if __name__ == '__main__':
    unittest.main()
