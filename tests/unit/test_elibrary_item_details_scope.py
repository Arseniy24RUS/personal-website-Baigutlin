"""Article metadata must never come from eLibrary menus, abstracts or references."""
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from harvest_elibrary_item_details import parse_detail_html, sanitize_detail_fields
import harvest_elibrary_browser as browser


class ItemDetailScopeTests(unittest.TestCase):
    def test_conference_labels_ignore_plural_navigation_abstract_and_cited_doi(self):
        html = '''<nav>ЖУРНАЛЫ СБОРНИКИ ИЗДАТЕЛЬСТВА</nav>
          <div>ЖУРНАЛЫ</div><div>Издательств:</div><div>123</div>
          <div>СБОРНИК:</div><a>ДНИ КАЛОРИКИ В ДАГЕСТАНЕ: ФУНКЦИОНАЛЬНЫЕ МАТЕРИАЛЫ</a>
          <div>Издательство:</div><div>Челябинский государственный университет</div>
          <div>Год издания: 2023</div><div>Страницы:</div><div>15-17</div>
          <h3>АННОТАЦИЯ:</h3><p>Работа поддержана грантом № 075-01493-23-00.</p>
          <h3>СПИСОК ЛИТЕРАТУРЫ:</h3><p>Metals. 2023. Т. 13. № 4. DOI: 10.3390/met13040728</p>'''
        parsed = parse_detail_html(html)
        self.assertEqual(parsed['venue'], 'ДНИ КАЛОРИКИ В ДАГЕСТАНЕ: ФУНКЦИОНАЛЬНЫЕ МАТЕРИАЛЫ')
        self.assertEqual(parsed['publisher'], 'Челябинский государственный университет')
        self.assertEqual(parsed['year'], '2023')
        self.assertEqual(parsed['pages'], '15-17')
        for field in ('doi', 'volume', 'issue'):
            self.assertNotIn(field, parsed)

    def test_title_contains_tom_and_unlabeled_grants_are_not_metadata(self):
        html = '''<h1>СПЛАВЫ С ЭФФЕКТОМ ПАМЯТИ ФОРМЫ</h1>
          <div>Сборник:</div><div>СПЛАВЫ С ЭФФЕКТОМ ПАМЯТИ ФОРМЫ. ТРЕТЬЯ МЕЖДУНАРОДНАЯ НАУЧНАЯ КОНФЕРЕНЦИЯ</div>
          <div>Год: 2018</div><div>Страницы: 61</div>
          <p>Грант № 17-72-20022. См. DOI 10.1234/cited. Т. 13.</p>'''
        parsed = parse_detail_html(html)
        self.assertTrue(parsed['venue'].startswith('СПЛАВЫ С ЭФФЕКТОМ'))
        self.assertEqual(parsed['pages'], '61')
        self.assertFalse({'volume', 'issue', 'doi'} & parsed.keys())

    def test_explicit_journal_metadata_survives_inline_and_split_cells(self):
        html = '''<div>Название журнала:</div><a>Metals</a>
          <div>Том: 13 Номер: 4 Страницы: 728</div><div>Год: 2023</div>
          <div>DOI:</div><a href="https://doi.org/10.3390/met13040728">10.3390/met13040728</a>
          <div>ISSN: 2075-4701</div><h3>Аннотация</h3><p>Volume: 999</p>'''
        parsed = parse_detail_html(html)
        self.assertEqual({key: parsed[key] for key in ('venue', 'volume', 'issue', 'pages', 'year', 'doi', 'issn')},
                         {'venue': 'Metals', 'volume': '13', 'issue': '4', 'pages': '728',
                          'year': '2023', 'doi': '10.3390/met13040728', 'issn': '2075-4701'})

    def test_reference_labels_after_section_cannot_fill_missing_fields(self):
        html = '<div>Год: 2022</div><h2>СПИСОК ЛИТЕРАТУРЫ</h2><div>DOI: 10.1234/reference</div><div>Том: 55</div>'
        self.assertEqual(set(parse_detail_html(html)), {'year', 'raw_text_excerpt'})

    def test_cache_shape_guard_rejects_known_fragments_without_losing_valid_identifiers(self):
        bad = {'venue': 'Ы', 'publisher': 'ств:', 'volume': 'ПАМЯТИ ФОРМЫ. ТРЕТЬЯ КОНФЕРЕНЦИЯ',
               'issue': '22-12-20032', 'pages': '72-73', 'year': '2022', 'doi': '10.1234/own-work'}
        self.assertEqual(sanitize_detail_fields(bad), {'pages': '72-73', 'year': '2022', 'doi': '10.1234/own-work'})
        good = {'venue': 'JOM', 'place': 'Уфа', 'volume': '125', 'issue': '4-1', 'pages': '062401'}
        self.assertEqual(sanitize_detail_fields(good), good)
        for issue in ('а', '075-01493-23-00', '24-12-20016)', 'государственной регистрации:'):
            self.assertNotIn('issue', sanitize_detail_fields({'issue': issue}))

    def test_successful_fresh_observation_removes_previously_cited_metadata(self):
        page = Mock()
        page.goto.return_value = Mock(status=200)
        page.content.return_value = '<div class="bigtext">Conference abstract</div><div>Год: 2023</div><div>Страницы: 15-17</div><h3>АННОТАЦИЯ</h3><p>' + 'Scientific abstract. ' * 50 + '</p>'
        previous = {'items': {'54314104': {'status': 'success', 'parsed': {'doi': '10.3390/met13040728', 'volume': '13', 'issue': '075-01493-23-00'}}}}
        untouched = copy.deepcopy(previous)
        with patch.object(browser, 'needs_details', return_value=True), \
             patch.object(browser, 'assert_no_challenge'), \
             patch.object(browser, 'elibrary_authenticated', return_value=True), \
             patch.dict(os.environ, {'ELIBRARY_ITEM_DETAILS_DELAY_SEC': '0'}):
            result, report = browser.collect_details(page, [{'elibrary_item_id': '54314104'}], previous)
        self.assertEqual(report['fetched'], 1)
        self.assertEqual(previous, untouched)
        self.assertEqual(result['items']['54314104']['parsed'], {'year': '2023', 'pages': '15-17'})
        self.assertIn('doi', result['items']['54314104']['observed_absent_fields'])

    def test_template_without_bibliographic_labels_preserves_previous_cache(self):
        page = Mock()
        page.goto.return_value = Mock(status=200)
        page.content.return_value = '<div class="bigtext">Work title</div><p>' + 'Template changed. ' * 50 + '</p>'
        previous = {'items': {'1': {'status': 'success', 'parsed': {'year': '2023', 'pages': '1-3'}}}}
        with patch.object(browser, 'needs_details', return_value=True), \
             patch.object(browser, 'assert_no_challenge'), \
             patch.object(browser, 'elibrary_authenticated', return_value=True), \
             patch.dict(os.environ, {'ELIBRARY_ITEM_DETAILS_DELAY_SEC': '0'}):
            result, report = browser.collect_details(page, [{'elibrary_item_id': '1'}], previous)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(result['items'], previous['items'])


if __name__ == '__main__':
    unittest.main()
