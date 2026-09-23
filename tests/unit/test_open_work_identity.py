"""Research identities survive chemical markup, provider overlap and versions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_open_sources as harvest
import build_public_data as build


class OpenWorkIdentityTests(unittest.TestCase):
    def test_equivalent_formula_titles_are_readable_and_have_one_identity(self):
        versions = ['A study of Ni 2 MnGa', 'A study of Ni<sub>2</sub>MnGa',
                    r'A study of ${\\mathrm{Ni}}_{2}\\mathrm{MnGa}$', 'A study of Ni-=SUB=-2-=/SUB=-MnGa']
        self.assertEqual(len({harvest.work_title_key(title) for title in versions}), 1)
        self.assertEqual(harvest.normalize_title(versions[2]), 'A study of Ni2MnGa')
        self.assertEqual(harvest.normalize_title(versions[3]), 'A study of Ni2MnGa')

    def test_different_dois_and_preprints_are_not_title_merged(self):
        journal = {'title': 'A study of Ni2MnGa', 'year': 2020, 'doi': '10.1000/journal', 'source': 'crossref_api', 'type': 'journal-article'}
        preprint = {**journal, 'doi': '10.48550/arxiv.1234.56789', 'type': 'other'}
        self.assertFalse(harvest.same_open_work(journal, preprint))
        self.assertFalse(harvest.same_open_work(journal, {**journal, 'doi': '10.1000/translation'}))
        self.assertEqual(len(harvest.dedupe_records([journal, preprint])), 2)
        self.assertEqual(harvest.publication_type({'venue': 'ArXiv', 'type': 'journal-article'}), 'preprint')

    def test_provider_doi_overlap_fills_missing_authors_and_preserves_both_observations(self):
        records = [
            {'source': 'orcid_public_api', 'doi': '10.1000/paper', 'title': 'A title', 'year': 2025, 'authors_raw': ''},
            {'source': 'openalex_api', 'doi': 'https://doi.org/10.1000/PAPER', 'title': 'A title', 'year': 2025, 'authors_raw': 'D. Baigutlin, A. Example'},
        ]
        result = harvest.dedupe_records(records)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['authors_raw'], 'D. Baigutlin, A. Example')
        self.assertEqual(set(result[0]['provider_records']), {'orcid_public_api', 'openalex_api'})

    def test_url_identity_does_not_require_identical_title(self):
        self.assertTrue(harvest.same_open_work({'title': 'Old title', 'url': 'https://example.org/paper/'},
                                              {'title': 'Corrected title', 'url': 'https://example.org/paper'}))

    def test_published_editorial_record_is_enriched_once_while_preprint_remains_distinct(self):
        old = {'id': '1', 'elibrary_item_id': '1', 'title': 'A study of Ni 2 MnGa', 'year': 2020,
               'authors_raw': 'Reviewed authors', 'gost_ru': 'Reviewed reference', 'sources': ['elibrary']}
        canon = [copy.deepcopy(old)]
        journal = {'source': 'crossref_api', 'title': 'A study of Ni<sub>2</sub>MnGa', 'year': 2020,
                   'doi': '10.1000/journal', 'authors_raw': 'Automatic authors', 'type': 'journal-article'}
        preprint = {**journal, 'doi': '10.48550/arxiv.1234.56789', 'type': 'preprint'}
        with tempfile.TemporaryDirectory() as directory, patch.object(build, 'DATA', Path(directory)):
            enriched, added = build.merge_open(canon, [preprint, journal])
            self.assertEqual((enriched, added), (1, 1))
            self.assertEqual(canon[0]['doi'], '10.1000/journal')
            for field in ('id', 'title', 'authors_raw', 'gost_ru'):
                self.assertEqual(canon[0][field], old[field])
            self.assertEqual(canon[1]['publication_type'], 'preprint')
            self.assertEqual(build.merge_open(canon, [journal, preprint])[1], 0)
        self.assertEqual(len(canon), 2)

    def test_openalex_metadata_extraction_requires_requested_author(self):
        work = {'title': 'A paper', 'display_name': 'A paper', 'type': 'article',
                'authorships': [{'raw_author_name': 'D. Baigutlin', 'author': {'id': 'A1'}}],
                'biblio': {'volume': '5', 'issue': '2', 'first_page': '10', 'last_page': '15'}}
        self.assertEqual(harvest.normalize_openalex_works({'results': [work]}, author_id='OTHER'), [])
        row = harvest.normalize_openalex_works({'results': [work]}, author_id='A1')[0]
        self.assertEqual((row['authors_raw'], row['publication_type'], row['pages']), ('D. Baigutlin', 'journal-article', '10-15'))

    def test_orcid_detail_requests_are_bounded_and_successes_are_reused(self):
        records = [{'source': 'orcid_public_api', 'orcid': '0000-0002-4130-3812', 'put_code': n} for n in (1, 2)]
        payload = {'contributors': {'contributor': [{'credit-name': {'value': 'Danil Baigutlin'}}]}}
        with tempfile.TemporaryDirectory() as directory, patch.object(harvest, 'OUT', Path(directory)), \
                patch.object(harvest, 'get_json', return_value=(payload, {})) as fetch:
            harvest.enrich_orcid_contributors(records, max_requests=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(records[0]['authors_raw'], 'Danil Baigutlin')
            self.assertNotIn('authors_raw', records[1])
            harvest.enrich_orcid_contributors([{'orcid': '0000-0002-4130-3812', 'put_code': 1}], max_requests=1)
            self.assertEqual(fetch.call_count, 1)

    def test_ambiguous_unidentified_preprint_stays_pending(self):
        canon = [{'title': 'Coulomb correlation in noncollinear antiferromagnetic α-Mn', 'year': 2019,
                  'doi': '10.48550/arxiv.1904.10291', 'publication_type': 'preprint'}]
        candidate = {'title': 'Correlation in noncollinear antiferromagnetic α-Mn', 'year': 2020,
                     'source': 'orcid_public_api', 'publication_type': 'preprint'}
        with tempfile.TemporaryDirectory() as directory, patch.object(build, 'DATA', Path(directory)):
            self.assertEqual(build.merge_open(canon, [candidate])[1], 0)
            pending = json.loads((Path(directory) / 'open/pending_publications.json').read_text())
            self.assertEqual(pending['records'][0]['reason'], 'possible_preprint_title_variant')
        self.assertEqual(len(canon), 1)

    def test_new_work_without_verified_authors_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, 'DATA', Path(directory)):
            canon = []
            self.assertEqual(build.merge_open(canon, [{'title': 'Unresolved contributors', 'doi': '10.1000/unresolved', 'source': 'orcid_public_api'}])[1], 0)
            pending = json.loads((Path(directory) / 'open/pending_publications.json').read_text())
            self.assertEqual(pending['records'][0]['reason'], 'authors_not_provided_by_sources')
            self.assertEqual(canon, [])

    def test_repeated_cache_dedupe_keeps_flat_provider_observations(self):
        records = [{'source': 'orcid_public_api', 'doi': '10.1000/paper', 'title': 'A title'},
                   {'source': 'crossref_api', 'doi': '10.1000/paper', 'title': 'A title', 'authors_raw': 'D. Baigutlin'}]
        first = harvest.dedupe_records(records)
        second = harvest.dedupe_records([*records, *first])
        self.assertEqual(first, second)
        self.assertTrue(all('provider_records' not in row for row in second[0]['provider_records'].values()))


if __name__ == '__main__':
    unittest.main()
