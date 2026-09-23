"""Switching from API metadata to a browser must preserve citation clocks."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import build_public_data as builder
import harvest_wos_authenticated as browser
from provider_auth import AuthFailure

OLD = '2026-09-19T10:00:00+00:00'
NEW = '2026-09-20T10:00:00+00:00'


class BrowserCitationTests(unittest.TestCase):
    def collect(self, previous_row, citation, interrupted=False):
        def pages(page, on_batch):
            on_batch([{'wos_uid': 'WOS:123', 'title': 'Publication', 'wos_citations': citation}], not interrupted)
            if interrupted:
                raise AuthFailure('human_verification_required')
        prior = {'metrics': {}, 'publications': [copy.deepcopy(previous_row)], 'details': {}}
        with patch.object(browser, 'now', return_value=NEW), \
             patch.object(browser, 'read_profile_metrics', return_value={'summary': {'publications': 1, 'citations': 0, 'h_index': 0}}), \
             patch.object(browser, 'select_core_collection'), \
             patch.object(browser, 'collect_publications', side_effect=pages):
            return browser.collect_from_page(MagicMock(), previous=prior)

    def test_confirmed_browser_zero_replaces_retained_api_count(self):
        old = {'wos_uid': 'WOS:123', 'title': 'Publication', 'wos_citations': 8,
               'observed_at': OLD, 'citation_observed_at': {'wos': OLD},
               'retained_citation_fields': ['wos_citations', 'rinc_citations']}
        report, payload = self.collect(old, 0)
        fresh = payload['publications'][0]
        self.assertEqual(fresh['wos_citations'], 0)
        self.assertEqual(fresh['citation_observed_at']['wos'], NEW)
        self.assertEqual(fresh['retained_citation_fields'], ['rinc_citations'])
        target = copy.deepcopy(old)
        builder.enrich_from_wos(target, fresh, fresh=True)
        self.assertEqual(target['wos_citations'], 0)
        self.assertEqual(target['citation_observed_at']['wos'], NEW)
        self.assertTrue(report['components']['publications']['complete'])

    def test_unknown_browser_count_does_not_renew_old_citation_date(self):
        old = {'wos_uid': 'WOS:123', 'title': 'Publication', 'wos_citations': 8, 'observed_at': OLD}
        _, payload = self.collect(old, None)
        fresh = payload['publications'][0]
        self.assertEqual(fresh['wos_citations'], 8)
        self.assertEqual(fresh['citation_observed_at']['wos'], OLD)
        self.assertEqual(fresh['observed_at'], NEW)
        self.assertIn('wos_citations', fresh['retained_citation_fields'])
        self.assertFalse(builder.citation_is_new(fresh, {'citation_observed_at': {'wos': OLD}}, 'wos', fresh=True))

    def test_partial_browser_page_keeps_its_confirmed_citation_after_challenge(self):
        old = {'wos_uid': 'WOS:123', 'wos_citations': 8, 'observed_at': OLD,
               'citation_observed_at': {'wos': OLD}, 'retained_citation_fields': ['wos_citations']}
        report, payload = self.collect(old, 0, interrupted=True)
        self.assertFalse(report['components']['publications']['complete'])
        self.assertEqual(report['components']['publications']['reason'], 'human_verification_required')
        self.assertEqual(payload['publications'][0]['citation_observed_at']['wos'], NEW)
        self.assertEqual(payload['publications'][0]['wos_citations'], 0)

    def test_retained_unknown_citation_date_cannot_use_metadata_date(self):
        for stamps in ({}, {'wos': None}):
            with self.subTest(stamps=stamps):
                old = {'wos_uid': 'WOS:123', 'wos_citations': 8, 'observed_at': OLD,
                       'citation_observed_at': stamps, 'retained_citation_fields': ['wos_citations']}
                _, payload = self.collect(old, None)
                fresh = payload['publications'][0]
                self.assertIsNone((fresh.get('citation_observed_at') or {}).get('wos'))
                self.assertEqual(fresh['wos_citations'], 8)
                self.assertFalse(builder.citation_is_new(fresh, {}, 'wos', fresh=True))


if __name__ == '__main__':
    unittest.main()
