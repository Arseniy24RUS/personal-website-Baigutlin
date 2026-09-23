"""Preserve the original Baigutlin profile and bibliography during first refresh."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import build_public_data as builder
from export_profile_env import public_profile_identifiers
from parse_elibrary_author_profile import extract_affiliations

ROOT = Path(__file__).resolve().parents[2]


class ProfileMigrationTests(unittest.TestCase):
    def test_affiliation_parser_accepts_chelyabinsk(self):
        result = extract_affiliations('Челябинский государственный университет (Челябинск) 2017-2026 59')
        self.assertEqual(result, [{'organization': 'Челябинский государственный университет (Челябинск)',
                                  'period': '2017-2026', 'publications': 59}])

    def test_first_build_preserves_entire_published_bibliography_rich_profile_and_metrics(self):
        profile = json.loads((ROOT / 'data/public/profile.json').read_text(encoding='utf-8'))
        publications = json.loads((ROOT / 'data/public/publications.json').read_text(encoding='utf-8'))
        metrics = json.loads((ROOT / 'data/public/metrics.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            (stage / 'config').mkdir()
            (stage / 'config/profile.yml').write_bytes((ROOT / 'config/profile.yml').read_bytes())
            public = stage / 'data/public'
            public.mkdir(parents=True)
            for name, payload in [('profile', profile), ('publications', publications), ('metrics', metrics)]:
                (public / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            previous_cwd = Path.cwd()
            try:
                os.chdir(stage)
                builder.main()
            finally:
                os.chdir(previous_cwd)
            after_profile = json.loads((public / 'profile.json').read_text(encoding='utf-8'))
            after_publications = json.loads((public / 'publications.json').read_text(encoding='utf-8'))
            self.assertEqual(json.loads((public / 'metrics.json').read_text()), metrics)
            generated = {'generated_at', 'source_health', 'scientometrics', 'identifiers', 'elibrary_metrics',
                         'elibrary_profile_metrics', 'wos_profile_metrics', 'scopus_metrics', 'open_sources_report'}
            for field, value in profile.items():
                if field not in generated and not field.endswith(('_count', '_size')):
                    self.assertEqual(after_profile[field], value, field)
            self.assertEqual(len(after_publications), len(publications))
            by_id = {row['id']: row for row in after_publications}
            for publication in publications:
                for field, value in publication.items():
                    self.assertEqual(by_id[publication['id']][field], value, (publication['id'], field))
            for source, legacy in [('rinc', 'risc'), ('scopus', 'scopus'), ('wos', 'wos')]:
                for field in ('publications', 'citations', 'h_index'):
                    self.assertEqual(after_profile['scientometrics']['sources'][source][field], metrics[legacy][field])

    def test_legacy_profile_link_mismatch_is_rejected(self):
        profile = {'links': {'elibrary': 'https://www.elibrary.ru/author_profile.asp?id=999'}}
        with patch.object(builder, 'read_json', return_value=profile):
            self.assertFalse(builder.profile_matches_existing({'elibrary_authorid': '1170779'}))
        with self.assertRaisesRegex(ValueError, 'conflict'):
            public_profile_identifiers({**profile, 'identifiers': {'elibrary_authorid': '1170779'}})

    def test_metrics_refresh_retains_history_and_unavailable_sources(self):
        prior = {'risc': {'publications': 59, 'citations': 202, 'h_index': 7, 'core_publications': 33},
                 'scopus': {'publications': 25, 'citations': 176, 'h_index': 7},
                 'annual': {'years': [2024], 'risc_publications': [9]}}
        fresh = {'status': 'success', 'origin': 'live', 'complete': True,
                 'last_success_at': '2026-09-23T00:00:00+00:00'}
        science = {'sources': {'rinc': {'publications': 60, 'citations': 205, 'h_index': 8},
                               'scopus': {'publications': 0, 'citations': 0, 'h_index': 0}}}
        result = builder.update_legacy_metrics(prior, science, {'summary': {'publications_core_rinc': 34}},
                                              {'elibrary': fresh, 'scopus': {'status': 'blocked'}})
        self.assertEqual(result['risc'], {'publications': 60, 'citations': 205, 'h_index': 8, 'core_publications': 34})
        self.assertEqual(result['scopus'], prior['scopus'])
        self.assertEqual(result['annual'], prior['annual'])


if __name__ == '__main__':
    unittest.main()
