"""Synthetic native CV export fixtures; no real CV or authenticated requests."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from parse_wos_cv import CvParseError, METHOD, parse_wos_cv
from publish_refresh import merge_observed_rows

RID = 'AAA-1234-2020'
STAMP = '2026-09-20T22:05:00+00:00'
OLD = '2026-08-01T00:00:00+00:00'


def row(uid='WOS:000000000000001', citations=2, **extra):
    return {
        'ut': uid, 'title': 'A synthetic publication', 'journal': 'Synthetic Journal',
        'doi': '10.1234/EXAMPLE', 'publication_date': 'Jul 2026',
        'publication_authors': {'count': 2, 'authors': 'Example, A.; Example, B.'},
        'citation_count': citations, **extra,
    }


def sample():
    return {
        'author': {'rid': RID, 'publishing_name': 'Synthetic Author'},
        'date_generated': 'September 20th 2026',
        'period': {'start': 'January 1900', 'end': 'September 2026'},
        'records': {'publication': {
            'list': [row(), row('WOS:000000000000002', 0, title='A second publication', doi=None),
                     row('RC:123_S24', 500), row('', None)],
            'total_publications': 5, 'period_publications': 4,
            'total_publications_cc': 2, 'period_publications_cc': 2,
            'total_citations': 2, 'period_citations': 2,
            'total_h_index': 1, 'period_h_index': 1,
        }},
    }


def parse(data=None, **kwargs):
    return parse_wos_cv(sample() if data is None else data, researcher_id=RID,
                        observed_at=STAMP, **kwargs)


class WoSCvTests(unittest.TestCase):
    def test_actual_shape_filters_core_and_keeps_official_total_metrics(self):
        value = sample()
        before = copy.deepcopy(value)
        result = parse(value)
        self.assertEqual(value, before)
        self.assertEqual(result['report']['status'], 'success')
        self.assertEqual(result['report']['components']['publications']['excluded_non_core_count'], 2)
        self.assertEqual(len(result['payloads']['publications']), 2)
        metrics = result['payloads']['metrics']
        self.assertEqual(metrics['summary'], {'publications': 2, 'core_collection_publications': 2,
                                             'citations': 2, 'h_index': 1})
        self.assertEqual(metrics['export_context']['total_publications'], 5)
        self.assertEqual(metrics['metric_methods']['citations'], METHOD)
        self.assertEqual(metrics['metric_observed_at']['citations'], STAMP)

    def test_period_values_never_replace_total_aggregates(self):
        value = sample()
        publication = value['records']['publication']
        publication.update(period_publications_cc=1, period_citations=0, period_h_index=0)
        publication['list'] = publication['list'][1:]
        result = parse(value)
        self.assertEqual(result['report']['status'], 'partial')
        self.assertTrue(result['report']['components']['metrics']['complete'])
        self.assertFalse(result['report']['components']['publications']['complete'])
        self.assertEqual(result['payloads']['metrics']['summary']['citations'], 2)
        self.assertEqual(result['payloads']['metrics']['summary']['h_index'], 1)

    def test_completeness_uses_counts_not_english_date_labels(self):
        value = sample()
        value['period'] = {'start': 'unrecognized locale', 'end': 'unrecognized locale'}
        self.assertTrue(parse(value)['report']['complete'])
        value['records']['publication']['period_publications_cc'] = 1
        result = parse(value)
        self.assertFalse(result['report']['components']['publications']['complete'])

    def test_zero_is_known_and_unknown_citation_has_no_fresh_field_stamp(self):
        for missing in (None, -1, True, '0', 0.0):
            with self.subTest(value=missing):
                value = sample()
                value['records']['publication']['list'][0]['citation_count'] = missing
                result = parse(value)
                unknown, zero = result['payloads']['publications']
                self.assertIsNone(unknown['wos_citations'])
                self.assertEqual(unknown['citation_observed_at'], {'wos': None})
                self.assertEqual(unknown['retained_citation_fields'], ['wos_citations'])
                self.assertEqual(zero['wos_citations'], 0)
                self.assertEqual(zero['citation_observed_at'], {'wos': STAMP})
                self.assertEqual(zero['retained_citation_fields'], [])
                self.assertTrue(result['report']['complete'])

    def test_missing_metrics_do_not_become_zero_or_use_period_values(self):
        for invalid in (None, -1, True, '2', 2.0):
            with self.subTest(value=invalid):
                value = sample()
                value['records']['publication']['total_citations'] = invalid
                result = parse(value)
                metrics = result['payloads']['metrics']
                self.assertIsNone(metrics['summary']['citations'])
                self.assertIsNone(metrics['metric_observed_at']['citations'])
                self.assertIn('citations', metrics['retained_metric_fields'])
                self.assertFalse(result['report']['components']['metrics']['complete'])
                self.assertTrue(result['report']['components']['publications']['complete'])

    def test_complete_zero_profile_is_valid(self):
        value = sample()
        value['records']['publication'] = {'list': [], 'total_publications_cc': 0,
            'period_publications_cc': 0, 'total_citations': 0, 'total_h_index': 0}
        result = parse(value)
        self.assertTrue(result['report']['complete'])
        self.assertEqual(result['payloads']['metrics']['summary']['citations'], 0)
        self.assertEqual(result['payloads']['publications'], [])

    def test_missing_or_wrong_author_is_rejected_without_echo(self):
        for author in (None, {}, {'rid': 'private-secret'}, {'rid': RID.lower()}, {'rid': [RID]}):
            value = sample()
            value['author'] = author
            with self.subTest(author=author), self.assertRaises(CvParseError) as raised:
                parse(value)
            self.assertEqual(str(raised.exception), 'cv_identity_mismatch')

    def test_malformed_top_level_and_list_are_rejected(self):
        for value in ([], {}, {'author': {'rid': RID}}, {'author': {'rid': RID}, 'records': {'publication': {'list': {}}}}):
            with self.subTest(value=value), self.assertRaises(CvParseError):
                parse(value)

    def test_observation_time_must_be_explicit_and_timezone_aware(self):
        for stamp in (None, '', '2026-09-20', '2026-09-20T22:05:00', 'secret-token'):
            with self.subTest(stamp=stamp), self.assertRaises(CvParseError) as raised:
                parse_wos_cv(sample(), researcher_id=RID, observed_at=stamp)
            self.assertEqual(raised.exception.reason, 'cv_observed_at_invalid')
        normalized = parse_wos_cv(sample(), researcher_id=RID, observed_at='2026-09-21T01:05:00+03:00')
        self.assertEqual(normalized['report']['attempted_at'], STAMP)

    def test_identical_duplicates_are_idempotent(self):
        value = sample()
        value['records']['publication']['list'].append(copy.deepcopy(value['records']['publication']['list'][0]))
        result = parse(value)
        self.assertTrue(result['report']['complete'])
        self.assertEqual(len(result['payloads']['publications']), 2)
        self.assertEqual(result['report']['components']['publications']['duplicate_uid_count'], 1)

    def test_conflicting_duplicates_remove_ambiguous_row_only(self):
        for change in ({'citation_count': 9}, {'title': 'Contradictory title'}, {'doi': '10.9999/another'}):
            value = sample()
            value['records']['publication']['list'].append(row(**change))
            with self.subTest(change=change):
                result = parse(value)
                state = result['report']['components']['publications']
                self.assertEqual(state['reason'], 'cv_conflicting_uid')
                self.assertEqual(state['conflicting_uid_count'], 1)
                self.assertFalse(state['complete'])
                self.assertEqual([r['wos_uid'] for r in result['payloads']['publications']], ['WOS:000000000000002'])
                self.assertTrue(result['report']['components']['metrics']['complete'])

    def test_malformed_core_row_cannot_make_complete_count(self):
        for change in ({'title': None}, {'ut': 'WOS:invalid?session=secret'}, {'ut': 123}):
            value = sample()
            value['records']['publication']['list'][0].update(change)
            with self.subTest(change=change):
                result = parse(value)
                self.assertFalse(result['report']['components']['publications']['complete'])
                self.assertEqual(result['report']['components']['publications']['reason'], 'cv_record_invalid')

    def test_full_list_cross_checks_sum_and_h_index(self):
        for field, value in (('total_citations', 3), ('total_h_index', 0)):
            data = sample()
            data['records']['publication'][field] = value
            with self.subTest(field=field):
                result = parse(data)
                self.assertEqual(result['report']['reason'], 'cv_metric_mismatch')
                self.assertFalse(result['report']['components']['metrics']['complete'])
                self.assertFalse(result['report']['components']['publications']['complete'])
                key = 'citations' if field == 'total_citations' else 'h_index'
                self.assertIsNone(result['payloads']['metrics']['summary'][key])

    def test_impossible_total_h_index_is_not_observed(self):
        data = sample()
        data['records']['publication']['list'][0]['citation_count'] = None
        data['records']['publication']['total_h_index'] = 3
        result = parse(data)
        self.assertIsNone(result['payloads']['metrics']['summary']['h_index'])
        self.assertEqual(result['report']['reason'], 'cv_metric_mismatch')

    def test_output_allowlist_drops_personal_sections_and_raw_signed_urls(self):
        data = sample()
        data['author'].update(email='PRIVATE_EMAIL', address='PRIVATE_ADDRESS')
        data['records']['peer_review'] = {'content': 'PRIVATE_REVIEW'}
        data['records']['publication']['list'][0].update(
            url='https://www.webofscience.com/?SrcAppSID=PRIVATE_SESSION', cookies='PRIVATE_COOKIE',
            doi='https://evil.example/path?token=PRIVATE_TOKEN')
        result = parse(data)
        dumped = json.dumps(result)
        self.assertNotIn('PRIVATE_', dumped)
        self.assertNotIn('SrcAppSID', dumped)
        record = result['payloads']['publications'][0]
        self.assertEqual(record['url'], 'https://www.webofscience.com/wos/woscc/full-record/WOS:000000000000001')
        self.assertIsNone(record['doi'])

    def test_unknown_citation_retains_old_value_and_clock_in_existing_merge(self):
        data = sample()
        data['records']['publication']['list'][0]['citation_count'] = None
        result = parse(data)
        old = {'id': 'published-id', 'wos_uid': 'WOS:000000000000001', 'title': 'Old metadata',
               'title_ru': 'Manual translation', 'wos_citations': 7,
               'citation_observed_at': {'wos': OLD}, 'observed_at': OLD}
        merged = merge_observed_rows([old], result['payloads']['publications'],
                                    {'last_success_at': OLD}, result['report']['components']['publications'])
        self.assertEqual(merged[0]['id'], 'published-id')
        self.assertEqual(merged[0]['title_ru'], 'Manual translation')
        self.assertEqual(merged[0]['wos_citations'], 7)
        self.assertEqual(merged[0]['citation_observed_at']['wos'], OLD)
        self.assertEqual(merged[0]['observed_at'], STAMP)


if __name__ == '__main__':
    unittest.main()
