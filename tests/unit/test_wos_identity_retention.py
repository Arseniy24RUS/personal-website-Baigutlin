"""Stable WoS identities survive API/browser retransmission and concurrent merges."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import build_public_data as builder
import publish_refresh as publisher
import validate_retention as retention

OLD = '2026-09-20T10:00:00+00:00'
NEW = '2026-09-20T12:00:00+00:00'
LATER = '2026-09-20T13:00:00+00:00'


class WosIdentityRetentionTests(unittest.TestCase):
    def card(self, **values):
        return {'id': 'manual-card', 'wos_uid': 'WOS:1', 'title': 'Manual title',
                'title_en': 'Curated translation', 'gost_ru': 'Curated reference',
                'wos_citations': 8, 'citation_observed_at': {'wos': OLD},
                'sources': ['wos', 'curated'], **values}

    def source(self, **values):
        return {'wos_uid': 'WOS:1', 'title': 'Renamed provider title',
                'wos_citations': 0, 'observed_at': NEW,
                'sources': ['wos_researcher_api'], **values}

    def merge(self, previous, incoming, *, complete=False):
        return publisher.merge_observed_rows(previous, incoming,
            {'last_success_at': OLD}, {'last_success_at': NEW if complete else OLD})

    def test_uid_rename_without_doi_preserves_card_and_updates_verified_zero(self):
        rows = [self.card()]
        before = copy.deepcopy(rows)
        self.assertEqual(builder.merge_wos(rows, [self.source(wos_uid=' wos:1 ')], fresh=False), (1, 0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(retention.compare_records(before, rows, 'publications'), [])
        self.assertEqual(rows[0]['wos_citations'], 0)
        self.assertEqual(rows[0]['citation_observed_at']['wos'], NEW)
        self.assertIn('wos_researcher_api', rows[0]['sources'])

    def test_uid_in_existing_nested_record_is_a_strong_alias(self):
        rows = [self.card(wos_uid=None, wos_records=[{'wos_uid': 'WOS:1'}])]
        self.assertEqual(builder.merge_wos(rows, [self.source()]), (1, 0))
        self.assertEqual(rows[0]['id'], 'manual-card')

    def test_doi_acquires_uid_without_duplicate_and_remembers_alias_for_retransmission(self):
        before = [{'id': 'retained', 'wos_uid': 'WOS:OLD', 'doi': 'https://doi.org/10.123/ABC',
                   'title': 'Old', 'wos_citations': 8, 'observed_at': OLD}]
        incoming = [self.source(wos_uid='WOS:NEW', doi='10.123/abc', id='new-source-id')]
        rows = self.merge(before, incoming)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'retained')
        self.assertEqual(rows[0]['wos_uid'], 'WOS:OLD')
        self.assertEqual(rows[0]['wos_citations'], 0)
        retransmit = self.source(wos_uid='WOS:NEW', observed_at=LATER, wos_citations=1)
        rows = self.merge(rows, [retransmit])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['wos_citations'], 1)
        self.assertEqual(before[0]['wos_citations'], 8)

    def test_builder_remembers_new_uid_after_doi_match(self):
        rows = [self.card(doi='10.123/ABC')]
        self.assertEqual(builder.merge_wos(rows, [self.source(wos_uid='WOS:2', doi='https://dx.doi.org/10.123/abc')]), (1, 0))
        self.assertEqual(builder.merge_wos(rows, [self.source(wos_uid='WOS:2', observed_at=LATER)]), (1, 0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['wos_uid'], 'WOS:1')

    def test_alias_added_earlier_in_same_batch_is_used(self):
        incoming = [self.source(doi='10.123/one'), self.source(wos_uid='WOS:2', doi='10.123/one'),
                    self.source(wos_uid='WOS:2', doi=None, observed_at=LATER)]
        self.assertEqual(len(self.merge([], incoming)), 1)
        rows = []
        self.assertEqual(builder.merge_wos(rows, incoming), (2, 1))
        self.assertEqual(len(rows), 1)

    def test_known_uid_selects_one_of_existing_same_doi_cards_without_collapsing(self):
        rows = [self.card(id='first', doi='10.123/shared'),
                self.card(id='second', wos_uid='WOS:2', doi='10.123/shared')]
        before = copy.deepcopy(rows)
        self.assertEqual(builder.merge_wos(rows, [self.source(wos_uid='WOS:2', doi='10.123/shared')]), (1, 0))
        self.assertEqual([row['wos_citations'] for row in rows], [8, 0])
        self.assertEqual(retention.compare_records(before, rows, 'publications'), [])
        merged = self.merge(before, [self.source(wos_uid='WOS:2', doi='10.123/shared')])
        self.assertEqual([row['id'] for row in merged], ['first', 'second'])
        self.assertEqual([row['wos_citations'] for row in merged], [8, 0])

    def test_conflicting_uid_doi_bridge_does_not_cross_contaminate_or_add(self):
        rows = [self.card(id='first', doi='10.123/one'),
                self.card(id='second', wos_uid='WOS:2', doi='10.123/two')]
        before = copy.deepcopy(rows)
        bridge = self.source(wos_uid='WOS:1', doi='10.123/two')
        self.assertEqual(builder.merge_wos(rows, [bridge]), (0, 0))
        self.assertEqual(rows, before)
        self.assertEqual(self.merge(before, [bridge]), before)

    def test_ambiguous_doi_only_row_preserves_separate_existing_records(self):
        rows = [self.card(id='first', doi='10.123/shared'),
                self.card(id='second', wos_uid='WOS:2', doi='10.123/shared')]
        before = copy.deepcopy(rows)
        incoming = self.source(wos_uid='WOS:NEW', doi='10.123/shared')
        self.assertEqual(builder.merge_wos(rows, [incoming]), (0, 0))
        self.assertEqual(rows, before)
        self.assertEqual(self.merge(before, [incoming]), before)

    def test_partial_old_retransmission_keeps_newer_citations_and_unions_provenance(self):
        before = [self.source(wos_citations=7, observed_at=LATER, sources=['browser'])]
        rows = self.merge(before, [self.source(observed_at=NEW, sources=['api'])])
        self.assertEqual(rows[0]['wos_citations'], 7)
        self.assertEqual(rows[0]['observed_at'], LATER)
        self.assertEqual(set(rows[0]['sources']), {'browser', 'api'})

    def test_retained_citation_is_not_a_fresh_observation(self):
        card = self.card()
        incoming = self.source(wos_citations=8, observed_at=NEW,
            citation_observed_at={'wos': OLD}, retained_citation_fields=['wos_citations'])
        builder.enrich_from_wos(card, incoming, fresh=True)
        self.assertEqual(card['citation_observed_at']['wos'], OLD)
        self.assertFalse(builder.citation_is_new(incoming, self.card(), 'wos', True))

    def test_explicit_citation_timestamp_precedes_new_metadata_timestamp(self):
        card = self.card(wos_citations=4, citation_observed_at={'wos': NEW})
        incoming = self.source(wos_citations=8, observed_at=LATER, citation_observed_at={'wos': OLD})
        builder.enrich_from_wos(card, incoming, fresh=True)
        self.assertEqual(card['wos_citations'], 4)
        self.assertEqual(card['citation_observed_at']['wos'], NEW)

    def test_metadata_only_merge_preserves_field_stamp_until_confirmed_zero_clears_marker(self):
        before = [self.source(wos_citations=8, observed_at=OLD, citation_observed_at={'wos': OLD})]
        retained = self.source(wos_citations=8, observed_at=NEW, citation_observed_at={'wos': OLD},
                               retained_citation_fields=['wos_citations'])
        rows = self.merge(before, [retained])
        self.assertEqual(rows[0]['observed_at'], NEW)
        self.assertEqual(rows[0]['citation_observed_at']['wos'], OLD)
        self.assertEqual(rows[0]['retained_citation_fields'], ['wos_citations'])
        verified = self.source(observed_at=LATER, citation_observed_at={'wos': LATER}, retained_citation_fields=[])
        rows = self.merge(rows, [verified])
        self.assertEqual(rows[0]['wos_citations'], 0)
        self.assertEqual(rows[0]['retained_citation_fields'], [])
        self.assertEqual(rows[0]['citation_observed_at']['wos'], LATER)

    def test_old_retained_marker_cannot_replace_newer_citation_observation(self):
        before = [self.source(wos_citations=4, observed_at=NEW, citation_observed_at={'wos': NEW}, retained_citation_fields=[])]
        retained = self.source(wos_citations=8, observed_at=LATER, citation_observed_at={'wos': OLD},
                               retained_citation_fields=['wos_citations'])
        rows = self.merge(before, [retained])
        self.assertEqual(rows[0]['wos_citations'], 4)
        self.assertEqual(rows[0]['citation_observed_at']['wos'], NEW)

    def test_retained_intermediate_observation_can_fill_older_public_baseline(self):
        card = self.card(citation_observed_at={'wos': OLD})
        incoming = self.source(observed_at=LATER, citation_observed_at={'wos': NEW},
                               retained_citation_fields=['wos_citations'])
        builder.enrich_from_wos(card, incoming, fresh=False)
        self.assertEqual(card['wos_citations'], 0)
        self.assertEqual(card['citation_observed_at']['wos'], NEW)

    def test_retained_unknown_field_timestamp_cannot_use_metadata_as_fallback(self):
        card = self.card()
        for stamps in ({}, {'wos': None}, {'wos': 'invalid'}):
            with self.subTest(stamps=stamps):
                incoming = self.source(observed_at=NEW, citation_observed_at=stamps,
                                       retained_citation_fields=['wos_citations'])
                self.assertFalse(builder.citation_is_new(incoming, card, 'wos', True))

    def test_fresh_api_metric_methods_are_visible_in_backend_only(self):
        method = {key: 'starter_api_calculated_complete_core' for key in ('publications', 'citations', 'h_index')}
        api = {'summary': {'publications': 13, 'citations': 8, 'h_index': 2}, 'metric_methods': method}
        state = {'status': 'success', 'complete': True, 'origin': 'live', 'last_success_at': NEW}
        result = builder.build_scientometrics([], {}, {}, api, {'wos': state})['sources']['wos']
        self.assertEqual(result['method'], method)

    def test_stale_api_does_not_replace_existing_metric_method(self):
        previous = {'sources': {'wos': {'publications': 13, 'citations': 8, 'h_index': 2,
                                       'method': 'provider_profile', 'last_success_at': OLD}}}
        api = {'summary': {'publications': 0, 'citations': 0, 'h_index': 0},
               'metric_methods': {'h_index': 'starter_api_calculated_complete_core'}}
        state = {'status': 'partial', 'complete': False, 'origin': 'live', 'last_success_at': OLD}
        result = builder.build_scientometrics([], {}, {}, api, {'wos': state}, previous)['sources']['wos']
        self.assertEqual(result['method'], 'provider_profile')
        self.assertEqual((result['publications'], result['citations'], result['h_index']), (13, 8, 2))

    def test_concurrent_public_title_edit_matches_uid_before_bibliography_key(self):
        current = [self.card(title='New manual title', title_en='New curated English')]
        candidate = [self.card(title='Old title', title_en='Old translation', sources=['wos', 'api'])]
        result = builder.merge_publication_sets(current, candidate)
        self.assertEqual(len(result), 1)
        self.assertEqual(retention.compare_records(current, result, 'publications'), [])
        self.assertIn('api', result[0]['sources'])

    def test_public_merge_keeps_baseline_duplicates_and_rejects_bridge(self):
        current = [self.card(id='first', doi='10.123/one'),
                   self.card(id='second', wos_uid='WOS:2', doi='10.123/two')]
        result = builder.merge_publication_sets(current, [self.source(doi='10.123/two')])
        self.assertEqual(len(result), 2)
        self.assertEqual(retention.compare_records(current, result, 'publications'), [])
        self.assertTrue(all(row['wos_citations'] == 8 for row in result))

    def test_real_published_baseline_retransmission_keeps_every_card(self):
        path = Path(__file__).resolve().parents[2] / 'data/public/publications.json'
        current = json.loads(path.read_text(encoding='utf-8'))
        result = builder.merge_publication_sets(current, copy.deepcopy(current))
        self.assertEqual(len(result), len(current))
        self.assertEqual(retention.compare_records(current, result, 'publications'), [])

    def test_same_title_with_distinct_uids_and_no_shared_doi_stays_distinct(self):
        current = [self.card()]
        incoming = self.source(wos_uid='WOS:2', title='Manual title', title_en='Manual title')
        self.assertEqual(builder.merge_wos(current, [incoming]), (0, 1))
        self.assertEqual(len(current), 2)
        current = [self.card()]
        result = builder.merge_publication_sets(current, [incoming])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['id'], 'manual-card')

    def test_fresh_browser_metrics_reset_previous_calculated_method(self):
        old = {'sources': {'wos': {'method': {'h_index': 'starter_api_calculated_complete_core'}}}}
        state = {'status': 'success', 'complete': True, 'origin': 'live', 'last_success_at': NEW}
        current = {'summary': {'publications': 13, 'citations': 8, 'h_index': 2}}
        result = builder.build_scientometrics([], {}, {}, current, {'wos': state}, old)['sources']['wos']
        self.assertEqual(result['method'], 'provider_profile')

    def test_retained_unknown_citation_never_inherits_metadata_or_component_clock(self):
        before = [self.source(wos_citations=8, observed_at=NEW, retained_citation_fields=['wos_citations'])]
        incoming = self.source(wos_citations=8, observed_at=LATER, retained_citation_fields=['wos_citations'])
        result = self.merge(before, [incoming], complete=True)
        self.assertNotIn('wos', result[0].get('citation_observed_at', {}))
        self.assertFalse(builder.citation_is_new(result[0], self.card(), 'wos', True))

    def test_explicit_null_old_field_stamp_never_uses_later_component_clock(self):
        previous = [self.source(wos_citations=8, observed_at=LATER,
                                citation_observed_at={'wos': None})]
        incoming = self.source(wos_citations=0, observed_at=NEW,
                               citation_observed_at={'wos': NEW})
        result = publisher.merge_observed_rows(previous, [incoming],
            {'last_success_at': LATER}, {'last_success_at': LATER})
        self.assertEqual(result[0]['wos_citations'], 0)
        self.assertEqual(result[0]['citation_observed_at']['wos'], NEW)
        self.assertEqual(result[0]['observed_at'], LATER)
        self.assertIsNone(previous[0]['citation_observed_at']['wos'])


if __name__ == '__main__':
    unittest.main()
