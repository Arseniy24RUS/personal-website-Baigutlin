"""CV and rendered-page observations must share the same preservation boundary."""
import copy
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_wos_authenticated as wos
from provider_auth import AuthFailure


def exported():
    return {
        'author': {'rid': 'AAN-4717-2020'},
        'date_generated': 'September 21st 2026',
        'period': {'start': 'January 1900', 'end': 'September 2026'},
        'records': {'publication': {
            'total_citations': 2, 'total_h_index': 1,
            'total_publications': 3, 'total_publications_cc': 2,
            'period_publications': 3, 'period_publications_cc': 2,
            'list': [
                {'ut': 'WOS:000000000000001', 'title': 'First paper', 'journal': 'Journal', 'publication_date': 'Sep 2026', 'citation_count': 2},
                {'ut': 'WOS:000000000000002', 'title': 'Second paper', 'journal': 'Journal', 'publication_date': '2025', 'citation_count': 0},
                {'ut': 'RC:OTHER', 'title': 'Other collection', 'citation_count': 900},
            ],
        }},
    }


class CvCollectionTests(unittest.TestCase):
    def run_collection(self, fetch, *, metrics=None, prior=None, dom_error=None, checkpoint=None):
        with ExitStack() as stack:
            read = stack.enter_context(patch.object(wos, 'read_profile_metrics'))
            if isinstance(metrics, Exception):
                read.side_effect = metrics
            else:
                read.return_value = metrics or {'summary': {'publications': 2, 'citations': 2, 'h_index': 1}}
            verify = stack.enter_context(patch.object(wos, 'target_profile_html', return_value='verified'))
            stack.enter_context(patch.object(wos, 'select_core_collection'))
            dom = stack.enter_context(patch.object(wos, 'collect_publications'))
            if dom_error:
                dom.side_effect = dom_error
            else:
                dom.side_effect = lambda page, on_batch: on_batch([
                    {'wos_uid': 'WOS:000000000000001', 'title': 'DOM paper', 'wos_citations': 2},
                    {'wos_uid': 'WOS:000000000000002', 'title': 'DOM second', 'wos_citations': 0},
                ], True)
            report, payload = wos.collect_from_page(MagicMock(), previous=prior, cv_export=fetch, on_checkpoint=checkpoint)
            return report, payload, dom, verify

    def test_complete_cv_avoids_dom_and_preserves_existing_records_and_fields(self):
        old = {'metrics': {}, 'publications': [
            {'wos_uid': 'WOS:000000000000002', 'manual_note': 'Keep this', 'wos_citations': 8},
            {'wos_uid': 'WOS:000000000000099', 'title': 'Archived paper'},
        ], 'details': {}}
        snapshots = []
        def fetch(page):
            self.assertTrue(snapshots[0]['components']['metrics']['complete'])
            return exported()
        report, payload, dom, verify = self.run_collection(fetch, prior=old, checkpoint=lambda state, data: snapshots.append(copy.deepcopy(state)))
        self.assertTrue(report['complete'])
        self.assertEqual(report['publication_transport'], 'cv_export')
        self.assertEqual(report['components']['publications']['observed_count'], 2)
        self.assertEqual(len(payload['publications']), 3)
        row = next(r for r in payload['publications'] if r['wos_uid'].endswith('002'))
        self.assertEqual(row['wos_citations'], 0)
        self.assertEqual(row['manual_note'], 'Keep this')
        dom.assert_not_called()
        verify.assert_not_called()

    def test_export_failure_falls_back_after_profile_verification(self):
        fetch = MagicMock(side_effect=ValueError('private signed URL must not escape'))
        report, _, dom, verify = self.run_collection(fetch)
        self.assertTrue(report['complete'])
        self.assertEqual(report['cv_export'], {'status': 'error', 'reason': 'cv_export_unavailable'})
        self.assertNotIn('private', str(report))
        verify.assert_called_once()
        dom.assert_called_once()

    def test_export_challenge_preserves_metrics_without_fallback(self):
        report, payload, dom, verify = self.run_collection(MagicMock(side_effect=AuthFailure('human_verification_required')))
        self.assertTrue(report['components']['metrics']['complete'])
        self.assertFalse(report['complete'])
        self.assertEqual(payload['metrics']['summary']['citations'], 2)
        dom.assert_not_called()
        verify.assert_not_called()

    def test_known_export_failure_keeps_only_its_fixed_diagnostic_reason(self):
        from wos_cv_export import CVExportError
        report, _, dom, _ = self.run_collection(MagicMock(side_effect=CVExportError('cv_export_controls_missing')))
        self.assertEqual(report['cv_export']['reason'], 'cv_export_controls_missing')
        self.assertTrue(report['complete'])
        dom.assert_called_once()

    def test_export_failure_stage_is_allowlisted_before_reporting(self):
        from wos_cv_export import CVExportError, CV_STAGES
        allowed = next(iter(CV_STAGES))
        for stage in (allowed, 'https://private.invalid/?SID=secret'):
            with self.subTest(allowed=stage == allowed):
                failure = CVExportError('cv_export_ui_failed')
                failure.stage = stage
                report, _, dom, _ = self.run_collection(MagicMock(side_effect=failure))
                self.assertEqual(report['cv_export'].get('stage'), allowed if stage == allowed else None)
                self.assertNotIn('secret', str(report))
                self.assertTrue(report['complete'])
                dom.assert_called_once()

    def test_wrong_export_author_is_not_treated_as_retriable_download_failure(self):
        document = exported()
        document['author']['rid'] = 'OTHER-1'
        report, payload, dom, _ = self.run_collection(lambda page: document)
        self.assertEqual(report['components']['publications']['reason'], 'wrong_author_profile')
        self.assertEqual(payload['publications'], [])
        dom.assert_not_called()

    def test_partial_cv_rows_survive_later_dom_challenge(self):
        document = exported()
        document['records']['publication']['list'] = document['records']['publication']['list'][:1]
        report, payload, _, _ = self.run_collection(lambda page: document, dom_error=AuthFailure('human_verification_required'))
        self.assertFalse(report['complete'])
        self.assertTrue(report['components']['metrics']['complete'])
        self.assertEqual(report['components']['publications']['observed_count'], 1)
        self.assertEqual(len(payload['publications']), 1)

    def test_cv_recovers_missing_dom_metrics_without_refreshing_retained_fields(self):
        previous = {'metrics': {'summary': {'indexed_publications': 19}, 'metric_observed_at': {'indexed_publications': '2026-06-03T00:00:00Z'}}, 'publications': [], 'details': {}}
        report, payload, dom, _ = self.run_collection(lambda page: exported(), metrics=AuthFailure('profile_metrics_missing'), prior=previous)
        self.assertTrue(report['complete'])
        self.assertEqual(payload['metrics']['summary']['citations'], 2)
        self.assertEqual(payload['metrics']['metric_observed_at']['indexed_publications'], '2026-06-03T00:00:00Z')
        self.assertIn('indexed_publications', payload['metrics']['retained_metric_fields'])
        dom.assert_not_called()

    def test_profile_and_cv_count_disagreement_uses_verified_dom(self):
        report, _, dom, verify = self.run_collection(lambda page: exported(), metrics={'summary': {'publications': 3, 'citations': 2, 'h_index': 1}})
        self.assertEqual(report['publication_transport'], 'rendered_profile')
        dom.assert_called_once()
        verify.assert_called_once()

    def test_cv_metric_success_uses_receipt_time_instead_of_attempt_start(self):
        clock = ['2026-09-21T12:00:00+00:00']
        def fetch(page):
            clock[0] = '2026-09-21T12:03:00+00:00'
            return exported()
        with patch.object(wos, 'now', side_effect=lambda: clock[0]):
            report, payload, _, _ = self.run_collection(fetch, metrics=AuthFailure('profile_metrics_missing'))
        state = report['components']['metrics']
        self.assertEqual(state['attempted_at'], '2026-09-21T12:00:00+00:00')
        self.assertEqual(state['last_success_at'], '2026-09-21T12:03:00+00:00')
        self.assertEqual(payload['metrics']['metric_observed_at']['citations'], '2026-09-21T12:03:00+00:00')

    def test_failed_cv_checkpoint_never_falls_back_over_committed_observation(self):
        def persist(state, data):
            if state['components']['publications']['complete']:
                raise OSError('disk failure')
        with self.assertRaises(wos.CheckpointWriteError):
            self.run_collection(lambda page: exported(), checkpoint=persist)


if __name__ == '__main__':
    unittest.main()
