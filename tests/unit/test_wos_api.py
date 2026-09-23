"""Official API schema fixtures; no keys, provider requests or browser sessions."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_wos_api as api
from source_health import load_checkpoint, write_checkpoint

RID = 'AAA-1234-2020'
OLD = '2025-01-01T00:00:00+00:00'
FRESH = '2026-09-20T00:00:00+00:00'
MISSING = object()


def document(uid='WOS:000000000000001', citations=0, **extra):
    value = {'uid': uid, 'title': 'A verified document', 'types': ['Article'],
             'source': {'sourceTitle': 'Journal', 'publishYear': 2025, 'volume': '2',
                        'pages': {'range': '11-22'}}, 'identifiers': {'doi': '10.1234/test'},
             'links': {'record': 'https://api.clarivate.com/private?token=should-not-be-saved'}}
    if citations is not MISSING:
        value['citations'] = [{'db': 'WOS', 'count': citations}, {'db': 'WOK', 'count': 999}]
    return {**value, **extra}


def page(rows, *, total=None, page=1, limit=50):
    return {'metadata': {'total': len(rows) if total is None else total, 'page': page, 'limit': limit}, 'hits': rows}


def profile(*, rid=RID, count=2, citations=2, h=1):
    return {'ids': {'rids': [rid]}, 'metricsAllTime': {
        'hIndex': h, 'documents': {'count': count, 'self': '/private?token=do-not-save'},
        'totalTimesCited': citations, 'totalCitingPublications': citations}}


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return copy.deepcopy(self.body)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response(item)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'data/wos/collection_checkpoint.json'
        self.environ = patch.dict(os.environ, {'WOS_STARTER_API_KEY': 'fixture-key-private',
            'WOS_RESEARCHER_API_KEY': '', 'WOS_RESEARCHER_ID': RID})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        clock = patch.object(api, 'now', return_value=FRESH)
        clock.start()
        self.addCleanup(clock.stop)
        self.clock = 0.0
        monotonic = patch.object(api.time, 'monotonic', side_effect=lambda: self.clock)
        monotonic.start()
        self.addCleanup(monotonic.stop)
        def advance(seconds):
            self.clock += seconds
        sleeper = patch.object(api.time, 'sleep', side_effect=advance)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def seed(self):
        report = {'status': 'success', 'complete': True, 'origin': 'live', 'last_success_at': OLD,
                  'attempted_at': OLD, 'record_count': 1, 'reason': None}
        old_row = {'wos_uid': 'WOS:000000000000001', 'title': 'Old title', 'title_ru': 'Ручной перевод',
                   'url': 'https://www.webofscience.com/wos/woscc/full-record/WOS:000000000000001',
                   'wos_citations': 7, 'observed_at': OLD, 'citation_observed_at': {'wos': OLD}}
        write_checkpoint(self.path, {**report, 'components': {'metrics': dict(report), 'publications': dict(report)}}, {
            'metrics': {'summary': {'publications': 1, 'citations': 7, 'h_index': 1},
                        'last_success_at': OLD, 'metric_observed_at': {key: OLD for key in api.CORE_FIELDS}},
            'publications': [old_row], 'details': {'manual': 'retained'}})
        return load_checkpoint(self.path)

    def run_api(self, responses, provider='starter'):
        if provider == 'researcher':
            os.environ['WOS_RESEARCHER_API_KEY'] = 'fixture-key-private'
        session = Session(responses)
        report = api.harvest(self.root, provider=provider, session=session)
        checkpoint = load_checkpoint(self.path)
        self.assertEqual(checkpoint['report'], report)
        self.assertNotIn('fixture-key-private', json.dumps(checkpoint))
        self.assertNotIn('should-not-be-saved', json.dumps(checkpoint))
        self.assertNotIn('do-not-save', json.dumps(checkpoint))
        return report, checkpoint['payloads'], session

    def test_missing_key_makes_no_request_and_preserves_payloads(self):
        previous = self.seed()
        os.environ['WOS_STARTER_API_KEY'] = ''
        report, payloads, session = self.run_api([])
        self.assertEqual(session.calls, [])
        self.assertEqual(payloads, previous['payloads'])
        self.assertEqual(report['reason'], 'api_key_missing')
        self.assertEqual(report['last_success_at'], OLD)

    def test_starter_paginates_and_calculates_core_metrics_including_zero(self):
        rows = [document(citations=3), document('WOS:000000000000002', 1), document('WOS:000000000000003', 0)]
        report, payloads, session = self.run_api([page(rows[:2], total=3, limit=2), page(rows[2:], total=3, limit=2, page=2)])
        self.assertEqual(report['status'], 'success')
        self.assertEqual({key: payloads['metrics']['summary'][key] for key in api.CORE_FIELDS}, {'publications': 3, 'citations': 4, 'h_index': 1})
        self.assertEqual(payloads['metrics']['metric_methods']['citations'], 'starter_api_calculated_complete_core')
        self.assertEqual(session.calls[1][1]['params'], {'db': 'WOS', 'q': 'AI=' + RID, 'page': 2, 'limit': 50})
        self.assertTrue(all(url == api.BASES['starter'] + '/documents' and not opts['allow_redirects'] for url, opts in session.calls))
        self.assertTrue(all(opts['headers']['X-ApiKey'] == 'fixture-key-private' for _, opts in session.calls))
        self.assertEqual(payloads['publications'][-1]['wos_citations'], 0)
        self.assertEqual(self.sleep.call_args.args[0], 1.0)

    def test_free_starter_metadata_does_not_refresh_old_citation_or_metrics(self):
        self.seed()
        report, payloads, _ = self.run_api([page([document(citations=MISSING)])])
        row = payloads['publications'][0]
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['components']['publications']['complete'])
        self.assertEqual(report['components']['metrics']['reason'], 'api_citations_unavailable')
        self.assertEqual(row['wos_citations'], 7)
        self.assertEqual(row['observed_at'], FRESH)
        self.assertEqual(row['citation_observed_at']['wos'], OLD)
        self.assertEqual(row['retained_citation_fields'], ['wos_citations'])
        self.assertEqual(row['title_ru'], 'Ручной перевод')
        self.assertEqual(payloads['metrics']['summary']['citations'], 7)
        self.assertEqual(payloads['metrics']['metric_observed_at']['citations'], OLD)
        self.assertIn('citations', payloads['metrics']['retained_metric_fields'])

    def test_known_zero_clears_retained_citation_marker(self):
        self.seed()
        self.run_api([page([document(citations=MISSING)])])
        report, payloads, _ = self.run_api([page([document(citations=0)])])
        self.assertEqual(report['status'], 'success')
        row = payloads['publications'][0]
        self.assertEqual(row['wos_citations'], 0)
        self.assertEqual(row['retained_citation_fields'], [])
        self.assertEqual(row['citation_observed_at']['wos'], FRESH)
        self.assertEqual(payloads['metrics']['summary']['h_index'], 0)

    def test_unknown_citation_clock_never_falls_back_to_metadata_clock(self):
        for explicit_null in (False, True):
            with self.subTest(explicit_null=explicit_null):
                old = self.seed()
                row = old['payloads']['publications'][0]
                if explicit_null:
                    row['citation_observed_at'] = {'wos': None}
                else:
                    row.pop('citation_observed_at')
                    row['retained_citation_fields'] = ['wos_citations']
                write_checkpoint(self.path, old['report'], old['payloads'])
                _, payloads, _ = self.run_api([page([document(citations=MISSING)])])
                current = payloads['publications'][0]
                self.assertEqual(current['wos_citations'], 7)
                self.assertIsNone((current.get('citation_observed_at') or {}).get('wos'))
                self.assertEqual(current['observed_at'], FRESH)

    def test_researcher_filters_other_collections_and_preserves_indexed_total(self):
        rows = [document(citations=2), document('WOS:000000000000002', 0), document('MEDLINE:12345', 100)]
        report, payloads, session = self.run_api([profile(count=3, citations=102), page(rows)], 'researcher')
        self.assertEqual(report['status'], 'success')
        self.assertEqual(len(payloads['publications']), 2)
        self.assertEqual(payloads['metrics']['summary']['publications'], 2)
        self.assertEqual(payloads['metrics']['summary']['indexed_publications'], 3)
        self.assertEqual(payloads['metrics']['summary']['citations'], 2)
        self.assertEqual(payloads['metrics']['api_metrics']['documents_count'], 3)
        self.assertEqual(payloads['metrics']['metric_methods']['h_index'], 'researcher_api_official_core_hindex')
        self.assertEqual(session.calls[1][1]['params']['nonIndexed'], 'false')

    def test_researcher_matching_raw_totals_do_not_prove_official_core_scope(self):
        report, payloads, _ = self.run_api([profile(count=2, citations=2), page([document(citations=2), document('WOS:000000000000002', 0)])], 'researcher')
        self.assertEqual(report['status'], 'success')
        self.assertEqual(payloads['metrics']['metric_methods']['publications'], 'researcher_api_calculated_complete_core')
        self.assertEqual(payloads['metrics']['metric_methods']['citations'], 'researcher_api_calculated_complete_core')

    def test_profile_hindex_checkpoint_survives_document_endpoint_failure(self):
        self.seed()
        report, payloads, _ = self.run_api([profile(h=2), Response({}, 403)], 'researcher')
        self.assertEqual(report['components']['metrics']['status'], 'partial')
        self.assertEqual(payloads['metrics']['summary'], {'publications': 1, 'citations': 7, 'h_index': 2})
        self.assertEqual(payloads['metrics']['metric_observed_at']['h_index'], FRESH)
        self.assertEqual(payloads['metrics']['metric_observed_at']['publications'], OLD)
        self.assertEqual(payloads['publications'][0]['observed_at'], OLD)

    def test_researcher_identity_mismatch_never_requests_documents(self):
        previous = self.seed()
        report, payloads, session = self.run_api([profile(rid='BBB-9999-2000')], 'researcher')
        self.assertEqual(report['reason'], 'api_identity_mismatch')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(payloads, previous['payloads'])

    def test_partial_page_union_keeps_old_and_fresh_rows_after_http_failure(self):
        self.seed()
        report, payloads, session = self.run_api([page([document('WOS:000000000000002', 0)], total=2, limit=1), Response({}, 401)])
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['components']['publications']['complete'])
        self.assertEqual(len(payloads['publications']), 2)
        self.assertEqual(payloads['publications'][0]['observed_at'], OLD)
        self.assertEqual(payloads['publications'][1]['observed_at'], FRESH)
        self.assertEqual(payloads['metrics']['summary']['citations'], 7)
        self.assertEqual(len(session.calls), 2)

    def test_401_403_do_not_retry_or_emit_error_response_details(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.seed()
                report, _, session = self.run_api([Response({'error': 'fixture-key-private'}, status)])
                self.assertEqual(report['reason'], f'api_http_{status}')
                self.assertEqual(len(session.calls), 1)
        self.sleep.assert_not_called()

    def test_429_and_server_errors_retry_with_bounded_backoff(self):
        report, _, session = self.run_api([Response({}, 429, {'Retry-After': '3'}), Response({}, 503), page([document()])])
        self.assertEqual(report['status'], 'success')
        self.assertEqual(len(session.calls), 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [3, 2])

    def test_long_retry_after_aborts_instead_of_retrying_early(self):
        report, _, session = self.run_api([Response({}, 429, {'Retry-After': '60'})])
        self.assertEqual(report['reason'], 'api_http_429')
        self.assertEqual(len(session.calls), 1)
        self.sleep.assert_not_called()

    def test_exhausted_429_preserves_snapshot(self):
        previous = self.seed()
        report, payloads, session = self.run_api([Response({}, 429)] * 3)
        self.assertEqual(report['reason'], 'api_http_429')
        self.assertEqual(payloads, previous['payloads'])
        self.assertEqual(len(session.calls), 3)

    def test_timeout_messages_are_not_serialized_and_retries_are_bounded(self):
        report, _, session = self.run_api([requests.Timeout('fixture-key-private')] * 3)
        self.assertEqual(report['reason'], 'api_timeout')
        self.assertEqual(len(session.calls), 3)

    def test_redirect_is_never_followed_with_api_header(self):
        report, _, session = self.run_api([Response({}, 302, {'Location': 'https://other.invalid'})])
        self.assertEqual(report['reason'], 'api_redirect_refused')
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]['allow_redirects'])

    def test_changed_or_malformed_pagination_never_marks_list_complete(self):
        fixtures = [page([], total=1), page([document()], total=1, page=2),
                    {'metadata': {'total': '1', 'page': 1, 'limit': 50}, 'hits': [document()]}]
        for value in fixtures:
            with self.subTest(value=value):
                self.seed()
                report, payloads, _ = self.run_api([value])
                self.assertFalse(report['components']['publications']['complete'])
                self.assertEqual(payloads['metrics']['summary']['citations'], 7)

    def test_total_change_and_duplicate_pages_preserve_first_page(self):
        for second, expected in ((page([document('WOS:000000000000002')], total=3, limit=1, page=2), 'api_total_changed'),
                                 (page([document()], total=2, limit=1, page=2), 'api_duplicate_uid')):
            with self.subTest(expected=expected):
                report, payloads, _ = self.run_api([page([document()], total=2, limit=1), second])
                self.assertEqual(report['components']['publications']['reason'], expected)
                self.assertEqual(len(payloads['publications']), 1)

    def test_malformed_document_retains_valid_rows_without_completeness(self):
        report, payloads, _ = self.run_api([page([document(), {'uid': 'WOS:2'}])])
        self.assertEqual(report['components']['publications']['reason'], 'api_schema_invalid')
        self.assertEqual(len(payloads['publications']), 1)

    def test_invalid_or_absent_counts_never_become_zero(self):
        for value in (MISSING, None, -1, '0', True):
            with self.subTest(value=value):
                report, payloads, _ = self.run_api([page([document(citations=value)])])
                self.assertFalse(report['components']['metrics']['complete'])
                self.assertNotIn('citations', payloads['metrics']['summary'])

    def test_invalid_json_and_schema_preserve_previous_payloads(self):
        for value in (Response(ValueError('fixture-key-private')), [], {'hits': []}):
            with self.subTest(value=value):
                previous = self.seed()
                report, payloads, _ = self.run_api([value])
                self.assertFalse(report['complete'])
                self.assertEqual(payloads, previous['payloads'])

    def test_core_scope_violation_in_starter_is_not_published(self):
        report, payloads, _ = self.run_api([page([document('MEDLINE:12345')])])
        self.assertEqual(report['components']['publications']['reason'], 'api_scope_mismatch')
        self.assertEqual(payloads['publications'], [])

    def test_explicit_collection_cannot_contradict_core_uid(self):
        for collection in ('MEDLINE', 'unrecognized-collection', ['WOS']):
            with self.subTest(collection=collection):
                previous = self.seed()
                report, payloads, _ = self.run_api([
                    profile(count=1, citations=7, h=1),
                    page([document(collection=collection)])], 'researcher')
                self.assertEqual(report['components']['publications']['reason'], 'api_scope_mismatch')
                self.assertEqual(payloads['publications'], previous['payloads']['publications'])
                self.assertFalse(report['complete'])
        self.assertIsNotNone(api.parse_document(document(collection='WOS'), 'researcher', FRESH)[1])

    def test_researcher_impossible_core_count_cannot_mark_components_complete(self):
        report, payloads, _ = self.run_api([profile(count=1, citations=0, h=5), page([document()])], 'researcher')
        self.assertEqual(report['components']['metrics']['reason'], 'api_metric_mismatch')
        self.assertFalse(report['components']['publications']['complete'])
        self.assertFalse(report['complete'])

    def test_researcher_missing_core_rows_does_not_replace_old_core_count_with_zero(self):
        self.seed()
        report, payloads, _ = self.run_api([profile(count=1, citations=0, h=0), page([document('MEDLINE:12345')])], 'researcher')
        self.assertEqual(report['components']['publications']['reason'], 'api_empty_response')
        self.assertEqual(payloads['metrics']['summary']['publications'], 1)
        self.assertEqual(payloads['metrics']['summary']['citations'], 7)

    def test_complete_search_preserves_previously_published_documents(self):
        self.seed()
        report, payloads, _ = self.run_api([page([document('WOS:000000000000002', 0)])])
        self.assertEqual(report['status'], 'success')
        self.assertEqual(len(payloads['publications']), 2)
        self.assertEqual(payloads['metrics']['summary']['publications'], 1)
        self.assertEqual(payloads['details'], {'manual': 'retained'})

    def test_suspicious_empty_starter_search_retains_old_metrics(self):
        self.seed()
        report, payloads, _ = self.run_api([page([])])
        self.assertEqual(report['reason'], 'api_empty_response')
        self.assertEqual(payloads['metrics']['summary']['citations'], 7)
        self.assertFalse(report['complete'])

    def test_explicit_zero_researcher_profile_and_list_are_valid(self):
        report, payloads, _ = self.run_api([profile(count=0, citations=0, h=0), page([])], 'researcher')
        self.assertEqual(report['status'], 'success')
        self.assertEqual({key: payloads['metrics']['summary'][key] for key in api.CORE_FIELDS}, {key: 0 for key in api.CORE_FIELDS})

    def test_invalid_target_is_rejected_before_request(self):
        os.environ['WOS_RESEARCHER_ID'] = '../other?private=1'
        report, _, session = self.run_api([])
        self.assertEqual(report['reason'], 'api_researcher_id_invalid')
        self.assertEqual(session.calls, [])
        self.assertNotIn('private=1', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
