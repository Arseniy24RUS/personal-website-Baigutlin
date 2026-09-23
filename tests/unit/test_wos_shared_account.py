"""A shared WoS login cannot change the author whose data is collected."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_wos_authenticated as collector
import provider_auth as auth
from parse_wos_cv import CvParseError, parse_wos_cv


class SharedAccountTests(unittest.TestCase):
    def test_configured_account_names_are_separate_from_target_author(self):
        self.assertIn('Arseniy Sitkovskiy', auth.wos_account_names())
        self.assertNotIn('Danil Baigutlin', auth.wos_account_names())
        self.assertFalse(auth.wos_cv_export_allowed('AAN-4717-2020'))
        self.assertTrue(auth.wos_cv_export_allowed('AAG-1530-2021'))

    def test_shared_account_reads_target_profile_without_exporting_owner_cv(self):
        export = MagicMock(side_effect=AssertionError('The account owner CV must not be requested'))
        target = 'AAN-4717-2020'
        expected = {'wos_uid': 'WOS:TARGET1', 'title': 'Target author paper', 'wos_citations': 3}
        def publications(page, on_batch):
            on_batch([expected], True)
        with patch.object(collector, 'read_profile_metrics', return_value={'summary': {'publications': 1, 'citations': 3, 'h_index': 1}}) as metrics, \
                patch.object(collector, 'select_core_collection'), \
                patch.object(collector, 'collect_publications', side_effect=publications):
            page = MagicMock()
            report, payloads = collector.collect_from_page(page, target=target,
                cv_export=export if auth.wos_cv_export_allowed(target) else None)
        export.assert_not_called()
        metrics.assert_called_once_with(page, target)
        self.assertTrue(report['complete'])
        self.assertEqual(report['publication_transport'], 'rendered_profile')
        self.assertEqual(payloads['publications'][0]['wos_uid'], expected['wos_uid'])

    def test_owner_cv_is_rejected_before_reading_records(self):
        with self.assertRaisesRegex(CvParseError, 'cv_identity_mismatch'):
            parse_wos_cv({'author': {'rid': 'AAG-1530-2021'}, 'records': {'publication': {'list': []}}},
                         observed_at='2026-09-23T00:00:00Z', researcher_id='AAN-4717-2020')


if __name__ == '__main__':
    unittest.main()
