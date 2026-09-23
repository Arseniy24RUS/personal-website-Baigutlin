"""Failure boundaries for borrowed sessions and independently committed observations."""
import copy
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_elibrary_browser as elib
import harvest_wos_authenticated as wos
from provider_auth import AuthFailure
from source_health import write_checkpoint, load_checkpoint


def elib_metrics():
    return {'authorid': '1170779', 'summary': {key: 0 for key in ('publications_rinc', 'citations_rinc', 'h_index_rinc', 'publications_elibrary', 'citations_elibrary', 'h_index_elibrary')}}


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.scope = patch.object(wos, 'select_core_collection')
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_russian_publication_count_grammar(self):
        for html, expected in [('<p>Всего найдена 1 публикация</p>', 1), ('Найдено: 0 публикаций', 0), ('Всего найдены 2 публикации', 2), ('Всего найдено <b>1\xa0234</b> публикаций', 1234)]:
            with self.subTest(html=html):
                self.assertEqual(elib.list_total(html), expected)

    def test_elibrary_metrics_survive_list_challenge_and_borrowed_page_stays_open(self):
        page = MagicMock()
        snapshots = []
        old = {'metrics': {'summary': {'citations_rinc': 99}}, 'publications': [{'elibrary_item_id': 'old', 'rinc_citations': 7}], 'details': {}}
        with patch.object(elib, 'target_profile_html', return_value='valid'), patch.object(elib, 'parse_elibrary_author_profile_html', return_value=elib_metrics()), patch.object(elib, 'collect_items', side_effect=AuthFailure('human_verification_required')):
            report, payload = elib.collect_from_page(page, previous=old, on_checkpoint=lambda state, values: snapshots.append((state, values)))
        self.assertEqual(snapshots[0][0]['components']['metrics']['status'], 'success')
        self.assertEqual(payload['metrics']['summary']['citations_rinc'], 0)
        self.assertEqual(payload['publications'], old['publications'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['components']['publications']['reason'], 'human_verification_required')
        page.close.assert_not_called()
        self.assertEqual(old['metrics']['summary']['citations_rinc'], 99)

    def test_verified_partial_page_updates_zero_only_for_observed_rows(self):
        old = {'metrics': {}, 'publications': [{'elibrary_item_id': '1', 'rinc_citations': 8, 'observed_at': '2025-01-01'}, {'elibrary_item_id': '2', 'rinc_citations': 9, 'observed_at': '2025-01-01'}], 'details': {}}
        def pages(page, temp, *, target, on_batch):
            on_batch([{'elibrary_item_id': '1', 'rinc_citations': 0}], False)
            raise AuthFailure('human_verification_required')
        with patch.object(elib, 'target_profile_html', return_value='valid'), patch.object(elib, 'parse_elibrary_author_profile_html', return_value=elib_metrics()), patch.object(elib, 'collect_items', side_effect=pages):
            report, payload = elib.collect_from_page(MagicMock(), previous=old)
        self.assertEqual(payload['publications'][0]['rinc_citations'], 0)
        self.assertNotEqual(payload['publications'][0]['observed_at'], '2025-01-01')
        self.assertEqual(payload['publications'][1], old['publications'][1])
        self.assertFalse(report['components']['publications']['complete'])
        self.assertEqual(report['components']['publications']['observed_count'], 1)

    def test_detail_challenge_does_not_cancel_complete_core(self):
        def pages(page, temp, *, target, on_batch):
            on_batch([{'elibrary_item_id': '1'}], True)
        detail_result = ({'items': {}}, {'fetched': 0, 'failed': 1, 'pending': 1, 'reason': 'human_verification_required'})
        with patch.object(elib, 'target_profile_html', return_value='valid'), patch.object(elib, 'parse_elibrary_author_profile_html', return_value=elib_metrics()), patch.object(elib, 'collect_items', side_effect=pages), patch.object(elib, 'collect_details', return_value=detail_result):
            report, _ = elib.collect_from_page(MagicMock())
        self.assertTrue(report['components']['metrics']['complete'])
        self.assertTrue(report['components']['publications']['complete'])
        self.assertFalse(report['components']['details']['complete'])
        self.assertFalse(report['complete'])

    def test_wrong_profile_stops_all_later_collection(self):
        with patch.object(elib, 'target_profile_html', side_effect=AuthFailure('wrong_author_profile')), patch.object(elib, 'collect_items') as items:
            report, _ = elib.collect_from_page(MagicMock())
        items.assert_not_called()
        self.assertEqual(report['components']['metrics']['reason'], 'wrong_author_profile')

    def test_wos_metrics_do_not_require_publication_rendering(self):
        previous = {'metrics': {'summary': {'citations': 7}}, 'publications': [{'wos_uid': 'WOS:old'}], 'details': {}}
        with patch.object(wos, 'read_profile_metrics', return_value={'summary': {'publications': 13, 'citations': 0, 'h_index': 0}}), patch.object(wos, 'collect_publications', side_effect=AuthFailure('profile_records_not_ready')):
            report, payload = wos.collect_from_page(MagicMock(), previous=previous)
        self.assertEqual(payload['metrics']['summary']['citations'], 0)
        self.assertEqual(payload['publications'], previous['publications'])
        self.assertTrue(report['components']['metrics']['complete'])
        self.assertFalse(report['components']['publications']['complete'])

    def test_zero_publication_list_is_complete_when_explicit(self):
        payload = {'summary': {'publications': 0}, 'records': []}
        with patch.object(wos, 'assert_no_challenge'), patch.object(wos, 'parse_wos_author_profile_html', return_value=payload):
            self.assertEqual(wos.read_records(MagicMock())['records'], [])

    def test_duplicate_wos_cards_cannot_satisfy_expected_total(self):
        data = {'summary': {'publications': 2}, 'records': [{'wos_uid': 'WOS:1'}, {'wos_uid': 'WOS:1'}]}
        with patch.object(wos, 'read_records', return_value=data), patch.object(wos, 'visible', return_value=None), self.assertRaisesRegex(AuthFailure, 'incomplete_pagination'):
            wos.collect_publications(MagicMock())

    def test_abort_after_metric_commit_has_atomic_recoverable_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collection_checkpoint.json'
            def checkpoint(report, payload):
                write_checkpoint(path, report, payload)
                raise KeyboardInterrupt()
            with patch.object(wos, 'read_profile_metrics', return_value={'summary': {'publications': 13, 'citations': 0, 'h_index': 0}}), self.assertRaises(KeyboardInterrupt):
                wos.collect_from_page(MagicMock(), on_checkpoint=checkpoint)
            restored = load_checkpoint(path)
            self.assertEqual(restored['payloads']['metrics']['summary']['citations'], 0)
            self.assertTrue(restored['report']['components']['metrics']['complete'])
            self.assertFalse(restored['report']['components']['publications']['complete'])

    def test_restored_sessions_are_verified_without_login(self):
        for module, login in [(elib, 'login_elibrary'), (wos, 'login_wos')]:
            with self.subTest(provider=module.__name__), patch.object(module, 'target_profile_html', return_value='valid'), patch.object(module, login) as normal_login:
                context = MagicMock()
                page, method = module.authenticated_page(context, {'status': 'restored'})
                self.assertEqual(method, 'existing_session_verified')
                self.assertIs(page, context.new_page.return_value)
                normal_login.assert_not_called()

    def test_only_explicit_expiry_allows_exactly_one_login(self):
        for module, login in [(elib, 'login_elibrary'), (wos, 'login_wos')]:
            with self.subTest(provider=module.__name__), patch.object(module, 'target_profile_html', side_effect=[AuthFailure('session_expired'), 'valid']), patch.object(module, login) as normal_login:
                fresh = MagicMock()
                replace = MagicMock(return_value=fresh)
                module.authenticated_page(MagicMock(), {'status': 'restored'}, fresh_context=replace)
                normal_login.assert_called_once()
                self.assertIs(normal_login.call_args.args[0], fresh)
                replace.assert_called_once()
            for reason in ('human_verification_required', 'profile_not_authenticated_or_changed', 'wrong_author_profile'):
                with self.subTest(provider=module.__name__, reason=reason), patch.object(module, 'target_profile_html', side_effect=AuthFailure(reason)), patch.object(module, login) as normal_login, self.assertRaises(AuthFailure):
                    module.authenticated_page(MagicMock(), {'status': 'restored'})
                normal_login.assert_not_called()

    def test_maintenance_never_writes_public_data_or_observation_timestamps(self):
        import browser_sessions
        for module, provider in [(elib, 'elibrary'), (wos, 'wos')]:
            with tempfile.TemporaryDirectory() as directory, self.subTest(provider=provider), patch.dict(os.environ, {'BROWSER_SESSION_MAINTENANCE': '1', 'BROWSER_SESSION_REPORT_DIR': directory}), patch('playwright.sync_api.sync_playwright') as playwright, patch.object(browser_sessions, 'restore_context', return_value=(MagicMock(), {'status': 'restored'})), patch.object(browser_sessions, 'checkpoint_session', return_value={'status': 'checkpointed', 'validated_at': '2026-09-20T18:00:00Z'}), patch.object(module, 'verify_browser_egress'), patch.object(module, 'authenticated_page', return_value=(MagicMock(), 'existing_session_verified')), patch.object(module, 'collect_from_page') as collect, patch.object(module, 'write_checkpoint') as write, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)
                collect.assert_not_called()
                write.assert_not_called()
                saved = __import__('json').loads((Path(directory) / f'{provider}.json').read_text())
                self.assertNotIn('components', saved)
                self.assertNotIn('last_success_at', saved)

    def test_teardown_failure_does_not_revoke_committed_observations(self):
        import browser_sessions
        state = {'status': 'success', 'origin': 'live', 'complete': True, 'attempted_at': '2026-09-20T18:00:00Z', 'last_success_at': '2026-09-20T18:00:00Z'}
        report = {**state, 'components': {'metrics': dict(state), 'publications': dict(state)}}
        payload = {'metrics': {'summary': {'publications': 1, 'citations': 0, 'h_index': 0}}, 'publications': [{'wos_uid': 'WOS:1'}], 'details': {}}
        def collect(page, **kwargs):
            kwargs['on_checkpoint'](copy.deepcopy(report), copy.deepcopy(payload))
            return copy.deepcopy(report), copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wos/harvest_report.json'
            context = MagicMock()
            context.close.side_effect = RuntimeError('private teardown detail')
            with patch.dict(os.environ, {'BROWSER_SESSION_MAINTENANCE': '0'}), patch('playwright.sync_api.sync_playwright'), patch.object(browser_sessions, 'restore_context', return_value=(context, {'status': 'restored'})), patch.object(browser_sessions, 'checkpoint_session', return_value={'status': 'checkpointed'}), patch.object(wos, 'REPORT', path), patch.object(wos, 'OUT', path.parent / 'profile_metrics.json'), patch.object(wos, 'verify_browser_egress'), patch.object(wos, 'assert_no_challenge'), patch.object(wos, 'wos_authenticated', return_value=True), patch.object(wos, 'authenticated_page', return_value=(MagicMock(), 'existing_session_verified')), patch.object(wos, 'collect_from_page', side_effect=collect), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wos.main(), 0)
            result = load_checkpoint(path.parent / 'collection_checkpoint.json')
            self.assertTrue(result['report']['components']['metrics']['complete'])
            self.assertEqual(result['report']['cleanup_reason'], 'browser_close_failed')
            self.assertNotIn('private teardown detail', str(result))

    def test_projection_failure_does_not_downgrade_atomic_metric_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collection_checkpoint.json'
            def checkpoint(report, payload):
                write_checkpoint(path, report, payload)
                raise OSError('projection write failed')
            with patch.object(wos, 'read_profile_metrics', return_value={'summary': {'publications': 13, 'citations': 0, 'h_index': 0}}), self.assertRaises(wos.CheckpointWriteError):
                wos.collect_from_page(MagicMock(), on_checkpoint=checkpoint)
            self.assertTrue(load_checkpoint(path)['report']['components']['metrics']['complete'])


class CoreScopeBrowserTests(unittest.TestCase):
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
                raise unittest.SkipTest('Install Playwright Chromium for scope tests')
            options['executable_path'] = str(edge)
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_actual_selected_round_chip_controls_scope(self):
        page = self.browser.new_page()
        try:
            page.set_content('''<button id="indexed" class="selected-round-chip">Все индексированные документы (19)</button>
                <button id="core" aria-label="Web of Science Core Collection (13)" onclick="this.classList.add('selected-round-chip');document.getElementById('indexed').classList.remove('selected-round-chip');window.clicks=(window.clicks||0)+1">Web of Science Core Collection (13)</button>''')
            wos.select_core_collection(page)
            self.assertIn('selected-round-chip', page.locator('#core').get_attribute('class'))
            self.assertEqual(page.evaluate('window.clicks'), 1)
            wos.select_core_collection(page)
            self.assertEqual(page.evaluate('window.clicks'), 1)
        finally:
            page.close()

    def test_challenge_blocks_scope_click(self):
        page = self.browser.new_page()
        try:
            page.set_content('<body>Please verify you are human<button onclick="window.clicked=true">Web of Science Core Collection (13)</button></body>')
            with self.assertRaisesRegex(AuthFailure, 'human_verification_required'):
                wos.select_core_collection(page)
            self.assertIsNone(page.evaluate('window.clicked'))
        finally:
            page.close()


if __name__ == '__main__':
    unittest.main()
