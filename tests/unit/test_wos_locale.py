"""Localized profile parsing: only observed, semantically matching metrics."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import parse_wos_author_profile as wos


def values(**items):
    return {key: {'value': value} for key, value in items.items()}


class WosLocaleTests(unittest.TestCase):
    def test_current_russian_indexed_label_is_separate_from_core(self):
        summary = wos.normalized_summary(values(**{
            'Публикации, индексированные в Web of Science': 19,
            'Публикации Web of Science Core Collection': 13,
            'Всего документов': 28}), values(**{'Publications': 13}))
        self.assertEqual(summary['indexed_publications'], 19)
        self.assertEqual(summary['core_collection_publications'], 13)
        self.assertEqual(summary['publications'], 13)

    def test_committed_english_profile_metrics_still_parse(self):
        snapshot = Path(__file__).resolve().parents[1] / 'fixtures/wos_profile_shell.html'
        result = wos.parse_file(str(snapshot))
        self.assertEqual(result['summary'], {'publications': 10, 'citations': 7, 'h_index': 2,
                                            'total_documents': 25, 'indexed_publications': 17,
                                            'core_collection_publications': 10})

    def test_russian_metric_labels_and_nonsemantic_icons(self):
        # Minimal DOM using the existing snapshot classes and the provider's
        # Russian metric terminology, not a claim to be a captured RU profile.
        html = '''<p class="summary-item"><span>12</span><span>Всего документов</span></p>
        <p class="summary-item"><span>7</span><span>Публикации в Web of Science Core Collection</span></p>
        <div class="wat-author-metric-inline-block"><div>7</div><div>Публикации</div><mat-icon>help_outline</mat-icon></div>
        <div class="wat-author-metric-inline-block"><div>1&#8239;234</div><div>Суммарное количество цитирований</div><mat-icon>info_outline</mat-icon></div>
        <div class="wat-author-metric-inline-block"><div>0</div><div>H-Index</div></div>'''
        summary = wos.parse_wos_author_profile_html(html)['summary']
        self.assertEqual(summary['publications'], 7)
        self.assertEqual(summary['citations'], 1234)
        self.assertEqual(summary['h_index'], 0)
        self.assertEqual(summary['total_documents'], 12)
        self.assertEqual(summary['core_collection_publications'], 7)
        self.assertIsNone(summary['indexed_publications'])

    def test_true_zero_wins_over_other_collection_and_summary_values(self):
        result = wos.normalized_summary(values(**{'Web of Science Core Collection publications': 8,
                                                 'Publications indexed in Web of Science': 19}),
                                        values(Publications=0, **{'Sum of Times Cited': 0, 'H-Index': 0}))
        self.assertEqual([result[key] for key in ('publications', 'citations', 'h_index')], [0, 0, 0])
        self.assertEqual(wos.int_value(0), 0)
        self.assertEqual(wos.int_value('1,234'), 1234)

    def test_missing_total_never_uses_patents_or_self_excluded_counts(self):
        for labels in [values(**{'Sum of Times Cited without self-citations': 4,
                                'Sum of Times Cited by Patents': 10}),
                       values(**{'Суммарное количество цитирований без самоцитирований': 4})]:
            result = wos.normalized_summary(values(**{'Publications indexed in Web of Science': 17}), labels)
            self.assertIsNone(result['citations'])
            self.assertIsNone(result['publications'])
            self.assertIsNone(result['h_index'])
        self.assertIsNone(wos.int_value('Не проиндексировано'))
        self.assertIsNone(wos.int_value('Web of Science 2026'))

    def test_russian_card_uses_stable_selectors_and_page_prefix(self):
        html = '''<app-record><div class="doctype-container"><span class="data-label">Статья</span></div>
        <app-summary-title><a data-ta="summary-record-title-link" href="/wos/woscc/full-record/WOS:12345">Демографические изменения регионов</a></app-summary-title>
        <span data-ta="summary-record-pubdate">20 сентября 2026</span>
        <span class="summary-source-title">Демографический журнал</span>
        <span data-ta="Summary-page-no">с. 12–18</span><span data-ta="Summary-issue">(2)</span>
        <app-summary-authors>Байгутлин Д.Р.</app-summary-authors></app-record>'''
        record = wos.parse_wos_author_profile_html(html)['records'][0]
        self.assertEqual(record['title'], 'Демографические изменения регионов')
        self.assertEqual(record['document_type'], 'Статья')
        self.assertEqual(record['pages'], '12-18')
        self.assertEqual(record['year'], 2026)
        self.assertEqual(record['issue'], '2')
        self.assertEqual(record['wos_uid'], 'WOS:12345')
        self.assertEqual(record['title_en'], '')
        self.assertEqual(record['venue_en'], '')


if __name__ == '__main__':
    unittest.main()
