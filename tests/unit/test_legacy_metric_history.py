import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from legacy_metric_history import update_annual_history


class LegacyMetricHistoryTests(unittest.TestCase):
    def test_new_year_and_verified_revisions_keep_older_years_aligned(self):
        previous = {'years': [2024, 2023], 'risc_publications': [9, 14],
                    'risc_citations': [32, 49], 'h_index_risc': [7, 7]}
        original = copy.deepcopy(previous)
        result = update_annual_history(previous, {
            'Число публикаций в РИНЦ': {'2025': 3, '2024': 9, '2023': 12},
            'Число цитирований в РИНЦ': {'2025': 43, '2024': 37},
            'Индекс Хирша в РИНЦ': {'2025': 7, '2024': 7, '2023': 6},
        })
        self.assertEqual(result['years'], [2025, 2024, 2023])
        self.assertEqual(result['risc_publications'], [3, 9, 12])
        self.assertEqual(result['risc_citations'], [43, 37, 49])
        self.assertEqual(result['h_index_risc'], [7, 7, 6])
        self.assertEqual(previous, original)

    def test_missing_series_does_not_invent_zero_for_a_new_year(self):
        previous = {'years': [2024], 'core_citations': [29], 'label': 'kept'}
        result = update_annual_history(previous, {'Число публикаций в РИНЦ': {'2025': 0}})
        self.assertEqual(result['risc_publications'], [0, None])
        self.assertEqual(result['core_citations'], [None, 29])
        self.assertEqual(result['label'], 'kept')

    def test_missing_or_invalid_history_preserves_the_previous_snapshot(self):
        previous = {'years': [2024], 'risc_publications': [9]}
        for incoming in ({}, {'Число публикаций в РИНЦ': {'2025': None, 'bad': 5}}):
            self.assertEqual(update_annual_history(previous, incoming), previous)


if __name__ == '__main__':
    unittest.main()
