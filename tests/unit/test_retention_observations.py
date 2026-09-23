import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from validate_retention import compare_records


class ObservationRetentionTests(unittest.TestCase):
    def test_citation_time_can_advance_but_cannot_disappear_or_regress(self):
        old = {'id': 'work', 'citation_observed_at': {'wos': '2026-09-20T10:00:00Z'}}
        fresh = {'id': 'work', 'citation_observed_at': {'wos': '2026-09-20T11:00:00+00:00'}}
        self.assertEqual(compare_records([old], [fresh], 'publications'), [])
        for updated in [{'id': 'work'}, {'id': 'work', 'citation_observed_at': {'wos': '2026-09-19T10:00:00Z'}}]:
            self.assertTrue(any(i['code'] == 'citation_observation_regressed' for i in compare_records([old], [updated], 'publications')))

    def test_signed_wos_link_can_be_replaced_with_same_canonical_record(self):
        base = 'https://www.webofscience.com/wos/woscc/full-record/WOS:12345'
        self.assertEqual(compare_records([{'id': 'work', 'url': base+'?SID=old&HMAC=old'}], [{'id': 'work', 'url': base}], 'publications'), [])
        self.assertTrue(compare_records([{'id': 'work', 'url': base}], [{'id': 'work', 'url': base+'6'}], 'publications'))

    def test_other_urls_remain_protected(self):
        self.assertTrue(compare_records([{'id': 'work', 'url': 'https://example.org/article?id=1'}], [{'id': 'work', 'url': 'https://example.org/article?id=2'}], 'publications'))


if __name__ == '__main__':
    unittest.main()
