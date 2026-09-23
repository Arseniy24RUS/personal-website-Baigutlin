"""Provider metadata changes must not grow duplicate nested observations."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import build_public_data as builder


class PublicationProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        replacement = patch.object(builder, 'DATA', Path(temporary.name))
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_repeated_new_raw_metadata_updates_last_observation_without_growth(self):
        old = {'source': 'orcid_public_api', 'put_code': 123, 'doi': '10/example',
               'title': 'Provider title', 'raw': {'modified': 'old'}, 'cited_by_count': 4}
        record = {'elibrary_item_id': '1', 'doi': '10/example', 'title': 'Manual title',
                  'title_en': 'Reviewed English', 'open_sources': [copy.deepcopy(old), copy.deepcopy(old)],
                  'sources': ['elibrary', 'orcid_public_api']}
        incoming = {**old, 'raw': {'modified': 'new'}, 'cited_by_count': 0,
                    'publisher': 'Publisher', 'sources': ['orcid_public_api', 'crossref_api'],
                    'provider_records': {'crossref_api': {'doi': '10/example', 'publisher': 'Publisher'}}}
        rows = [record]
        for _ in range(3):
            builder.merge_open(rows, [incoming])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(record['open_sources']), 2)  # Preserve historical entries.
        self.assertEqual(record['open_sources'][0], old)
        self.assertEqual(record['open_sources'][-1]['cited_by_count'], 0)
        self.assertEqual(record['open_sources'][-1]['raw'], {'modified': 'new'})
        self.assertIn('crossref_api', record['sources'])
        self.assertIn('crossref_api', record['open_sources'][-1]['provider_records'])
        self.assertEqual(record['title'], 'Manual title')
        self.assertEqual(record['title_en'], 'Reviewed English')

    def test_openalex_observation_matches_id_even_when_title_and_metrics_change(self):
        row = {'open_sources': [{'source': 'openalex_api', 'openalex_id': 'https://openalex.org/W1',
                                 'title': 'Old provider title', 'cited_by_count': 4}]}
        changed = {'source': 'openalex_api', 'openalex_id': 'https://openalex.org/W1',
                   'title': 'Corrected provider title', 'cited_by_count': 0}
        builder.merge_open_observation(row, changed)
        builder.merge_open_observation(row, changed)
        self.assertEqual(row['open_sources'], [changed])

    def test_concurrent_merge_retains_current_raw_and_adds_missing_provenance(self):
        old = {'source': 'orcid_public_api', 'put_code': 123, 'doi': '10/example',
               'title': 'Provider title', 'raw': {'modified': 'new'}, 'cited_by_count': 7}
        current = {'elibrary_item_id': '1', 'title': 'Manual title', 'open_sources': [old]}
        incoming = {'elibrary_item_id': '1', 'title': 'Provider title',
                    'open_sources': [{**old, 'raw': {'modified': 'old'}, 'cited_by_count': 3,
                                      'provider_records': {'crossref_api': {'doi': '10/example'}}}]}
        result = builder.merge_publication_sets([current], [incoming])
        repeated = builder.merge_publication_sets(result, [incoming])
        self.assertEqual(result, repeated)
        self.assertEqual(len(result[0]['open_sources']), 1)
        self.assertEqual(result[0]['open_sources'][0]['raw'], {'modified': 'new'})
        self.assertEqual(result[0]['open_sources'][0]['cited_by_count'], 7)
        self.assertIn('crossref_api', result[0]['open_sources'][0]['provider_records'])
        self.assertEqual(current['open_sources'], [old])

    def test_distinct_provider_work_ids_are_not_coalesced_away(self):
        row = {'open_sources': [{'source': 'orcid_public_api', 'put_code': 1, 'doi': '10/example'}]}
        for observation in ({'source': 'orcid_public_api', 'put_code': 2, 'doi': '10/example'},
                            {'source': 'crossref_api', 'doi': 'https://doi.org/10/EXAMPLE'}):
            builder.merge_open_observation(row, observation)
            builder.merge_open_observation(row, observation)
        self.assertEqual(len(row['open_sources']), 3)


if __name__ == '__main__':
    unittest.main()
