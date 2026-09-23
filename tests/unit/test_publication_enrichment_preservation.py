"""Derived publication formatting must preserve published editorial content."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import build_public_data as builder
import merge_wos_records_into_public_data as wos_merge
import translate_publication_titles as translations
import enrich_publication_metadata as enrichment
import sanitize_publication_references as sanitizer
import validate_retention as retention


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


@contextlib.contextmanager
def offline_workspace():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        os.chdir(root)
        try:
            with patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('Network disabled in derived-data regression')), \
                 patch.object(translations.ArgosTranslator, 'ensure', return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                yield root
        finally:
            os.chdir(previous)


class PublicationEnrichmentTests(unittest.TestCase):
    def test_unknown_provider_pagination_is_not_added_but_existing_values_survive(self):
        old = [{'id': 'old', 'elibrary_item_id': '1', 'title': 'Old'},
               {'id': 'manual', 'elibrary_item_id': '2', 'title': 'Manual', 'pages': 'без номера'}]
        incoming = [{'id': 'old', 'elibrary_item_id': '1', 'title': 'Old', 'pages': 'без номера'},
                    {'id': 'new', 'elibrary_item_id': '3', 'title': 'New', 'pages': 'без номера'}]
        result = {row['id']: row for row in builder.merge_publication_sets(old, incoming)}
        self.assertFalse(result['old'].get('pages'))
        self.assertFalse(result['new'].get('pages'))
        self.assertEqual(result['manual']['pages'], 'без номера')
        self.assertFalse(enrichment.set_if_missing(result['old'], 'pages', 'без номера'))
        self.assertTrue(enrichment.set_if_missing(result['old'], 'pages', 'S1–S9'))

    def test_translation_retains_manual_text_and_provenance_verbatim(self):
        original = {'id': 'manual', 'title': 'Ручной заголовок',
                    'title_en': 'ESG and R&D: a Manual TITLE', 'title_en_source': 'editorial',
                    'venue_en': 'Journal of GIS & ESG', 'venue_en_source': 'editorial',
                    'scopus': {'title': 'DIFFERENT PROVIDER TITLE', 'journal_or_source': 'Provider journal'},
                    'rinc_citations': 0}
        with offline_workspace():
            dump(Path('data/public/publications.json'), [original])
            self.assertEqual(translations.main(), 0)
            after = load(Path('data/public/publications.json'))[0]
            for key, value in original.items():
                self.assertEqual(after[key], value, key)

    def test_missing_translation_and_venue_are_filled_from_provider_or_cache(self):
        publications = [
            {'id': 'official', 'title': 'Статья', 'scopus': {'title': 'Official English title', 'journal_or_source': 'Official journal'}},
            {'id': 'cached', 'elibrary_item_id': '2', 'title': 'Другая статья'},
        ]
        with offline_workspace():
            dump(Path('data/public/publications.json'), publications)
            dump(Path('data/curation/publication_title_translations.json'), {'items': {
                'elibrary:2': {'title_en': 'Cached English title', 'title_en_source': 'reviewed_cache'}}})
            self.assertEqual(translations.main(), 0)
            official, cached = load(Path('data/public/publications.json'))
            self.assertEqual(official['title_en'], 'Official English title')
            self.assertEqual(official['venue_en'], 'Official journal')
            self.assertEqual(cached['title_en'], 'Cached English title')

    def test_metadata_fills_gaps_without_rewriting_manual_identifiers_or_references(self):
        original = {'id': 'manual', 'title': 'ESG: РОССИЯ — РАН', 'title_ru': 'Ручная правка: РАН и ESG',
                    'title_ru_display': 'Авторский вариант', 'title_en': 'Manual English title',
                    'doi': 'https://doi.org/10.1234/UPPER.Case', 'url': 'https://example.org/paper?title=a%20b',
                    'gost_ru': 'Авторская библиографическая запись.', 'apa_en': 'Editorial APA citation.',
                    'venue': 'Ручное название журнала', 'volume': 'XI', 'rinc_citations': 0, 'wos_citations': 0,
                    'metadata_raw': 'Т. 10. № 2. С. 11-19.'}
        with offline_workspace():
            dump(Path('data/public/publications.json'), [original])
            dump(Path('data/curation/crossref_metadata_cache.json'), {'items': {
                '10.1234/upper.case': {'status': 200, 'metadata': {'volume': '10', 'issue': '3', 'page': '1–9',
                                                               'container_title': 'New English journal', 'publisher': 'New publisher'}}}})
            self.assertEqual(enrichment.main(), 0)
            after = load(Path('data/public/publications.json'))[0]
            for key, value in original.items():
                self.assertEqual(after[key], value, key)
            self.assertEqual(after['issue'], '2')
            self.assertEqual(after['pages'], '11–19')
            self.assertEqual(after['publisher'], 'New publisher')
            self.assertEqual(after['venue_en'], 'New English journal')

    def test_sanitizer_preserves_manual_references_when_rejecting_new_invalid_metadata(self):
        original = {'id': 'manual', 'title': 'Manual title', 'year': 2026,
                    'gost_ru': 'Manually reviewed GOST citation.', 'apa_en': 'Manually reviewed APA citation.'}
        with offline_workspace():
            dump(Path('data/public/publications.json'), [{**original, 'pages': 'без номера'}])
            self.assertEqual(sanitizer.main(), 0)
            after = load(Path('data/public/publications.json'))[0]
            self.assertEqual(after['pages'], 'без номера')
            self.assertEqual(after['gost_ru'], original['gost_ru'])
            self.assertEqual(after['apa_en'], original['apa_en'])
            self.assertEqual(retention.compare_records([original], [after], 'publications'), [])

    def test_new_generated_references_are_cleaned_but_subsequent_manual_edit_wins(self):
        with offline_workspace():
            dump(Path('data/public/publications.json'), [{'id': 'new', 'title': 'New title', 'pages': 'без номера'}])
            enrichment.main()
            generated = load(Path('data/public/publications.json'))[0]
            self.assertTrue(enrichment.reference_is_generated(generated, 'gost_ru'))
            generated['apa_en'] = 'Subsequent manual edit with unchanged provenance label.'
            dump(Path('data/public/publications.json'), [generated])
            sanitizer.main()
            after = load(Path('data/public/publications.json'))[0]
            self.assertNotIn('без номера', after['gost_ru'])
            self.assertEqual(after['apa_en'], generated['apa_en'])
            self.assertFalse(enrichment.reference_is_generated(after, 'apa_en'))

    def test_invalid_publication_input_is_not_replaced_with_empty_array(self):
        with offline_workspace():
            path = Path('data/public/publications.json')
            path.parent.mkdir(parents=True)
            path.write_text('{broken json', encoding='utf-8')
            before = path.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(translations.main(), 1)
            for main in (enrichment.main, sanitizer.main):
                with self.assertRaises(SystemExit):
                    main()
                self.assertEqual(path.read_bytes(), before)

    def test_full_derived_pipeline_retains_real_published_baseline_offline(self):
        original = load(REPO / 'data/public/publications.json')
        with offline_workspace() as root:
            for name in ('public', 'processed', 'elibrary', 'scopus', 'open', 'wos', 'curation', 'admin_queue'):
                source = REPO / 'data' / name
                if source.exists():
                    shutil.copytree(source, root / 'data' / name)
            shutil.copytree(REPO / 'config', root / 'config')
            for module in (builder, wos_merge, translations, enrichment, sanitizer):
                with self.subTest(step=module.__name__):
                    self.assertIn(module.main(), (None, 0))
                    after = load(root / 'data/public/publications.json')
                    self.assertEqual(len(after), len(original))
                    self.assertEqual(retention.compare_records(original, after, 'publications'), [])


if __name__ == '__main__':
    unittest.main()
