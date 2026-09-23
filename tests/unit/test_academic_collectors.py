from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_open_sources as opening
import harvest_scopus as scopus
import harvest_elibrary_browser as elibrary
import harvest_wos_authenticated as wos
import provider_auth as auth
from harvest_elibrary_item_details import needs_details
from parse_elibrary_author_items import parse_elibrary_author_items
from source_health import merge_records, read_json, write_json, source_result


class CollectionTests(unittest.TestCase):
    def test_source_failure_retains_timestamp(self):
        state = source_result({'last_success_at': '2026-06-01'}, status='blocked', count=58, reason='session_expired')
        self.assertEqual(state['last_success_at'], '2026-06-01')
        self.assertEqual(state['origin'], 'snapshot')
        self.assertFalse(state['complete'])

    def test_safe_merge_preserves_records_and_real_zero(self):
        previous = [{'id': 'a', 'citations': 10, 'manual': 'keep'}, {'id': 'b', 'citations': 5}]
        result = merge_records(previous, [{'id': 'a', 'citations': 0, 'manual': None}], lambda row: row['id'])
        self.assertEqual(result[0], {'id': 'a', 'citations': 0, 'manual': 'keep'})
        self.assertEqual(result[1], previous[1])

    def test_openalex_multiple_cursor_pages(self):
        pages = [({'meta': {'count': 3, 'next_cursor': 'next'}, 'results': [{'id': 'a'}, {'id': 'b'}]}, {}),
                 ({'meta': {'count': 3, 'next_cursor': 'end'}, 'results': [{'id': 'c'}]}, {})]
        with patch.object(opening, 'get_json', side_effect=pages) as get:
            result, diagnostic = opening.fetch_cursor_pages('https://api.openalex.org/works', {}, 'openalex')
        self.assertEqual(len(result['results']), 3)
        self.assertEqual(diagnostic['pages'], 2)
        self.assertIn('cursor=next', get.call_args.args[0])

    def test_crossref_multiple_pages(self):
        pages = [({'message': {'total-results': 2, 'next-cursor': 'next', 'items': [{'DOI': 'one'}]}}, {}),
                 ({'message': {'total-results': 2, 'next-cursor': 'last', 'items': [{'DOI': 'two'}]}}, {})]
        with patch.object(opening, 'get_json', side_effect=pages):
            result, diagnostic = opening.fetch_cursor_pages('https://api.crossref.org/works', {}, 'crossref')
        self.assertEqual(len(result['message']['items']), 2)

    def test_crossref_cursor_does_not_send_forbidden_published_sort(self):
        payload = {'message': {'total-results': 2, 'items': [{'DOI': 'old', 'published': {'date-parts': [[2020]]}}, {'DOI': 'new', 'published': {'date-parts': [[2026]]}}]}}
        with patch.object(opening, 'get_json', return_value=(payload, {})) as request:
            result, _ = opening.fetch_cursor_pages('https://api.crossref.org/works', {'sort': 'published', 'order': 'desc', 'filter': 'orcid:test'}, 'crossref')
        self.assertNotIn('sort=', request.call_args.args[0])
        self.assertNotIn('order=', request.call_args.args[0])
        self.assertIn('cursor=', request.call_args.args[0])
        self.assertEqual(result['message']['items'][0]['DOI'], 'new')

    def test_partial_pages_never_form_a_successful_snapshot(self):
        for second in ((None, {'reason': 'http_429'}), ({'meta': {'count': 2}, 'results': []}, {})):
            with self.subTest(second=second), patch.object(opening, 'get_json', side_effect=[({'meta': {'count': 2, 'next_cursor': 'n'}, 'results': [{'id': 'a'}]}, {}), second]):
                result, report = opening.fetch_cursor_pages('https://api.openalex.org/works', {}, 'openalex')
                self.assertIsNone(result)
                self.assertTrue(report['reason'])

    def test_duplicate_cursor_page_cannot_claim_completeness(self):
        payload = {'meta': {'count': 2, 'next_cursor': 'next'}, 'results': [{'id': 'same'}]}
        with patch.object(opening, 'get_json', side_effect=[(payload, {}), (payload, {})]):
            result, report = opening.fetch_cursor_pages('https://api.openalex.org/works', {}, 'openalex')
        self.assertIsNone(result)
        self.assertEqual(report['reason'], 'duplicate_pagination')

    def test_doi_dedupe_unions_provider_provenance(self):
        records = [{'doi': '10.1/abc', 'source': 'orcid_public_api', 'title': 'Work'},
                   {'doi': '10.1/ABC', 'source': 'openalex_api', 'title': 'Work', 'cited_by_count': 7}]
        result = opening.dedupe_records(records)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['sources'], ['openalex_api', 'orcid_public_api'])
        self.assertEqual(result[0]['provider_records']['openalex_api']['cited_by_count'], 7)

    def test_open_total_failure_keeps_each_source_and_public_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {'records': [{'source': 'orcid_public_api', 'title': 'old', 'doi': '10.1/old'}]}
            write_json(root / 'open_publications.json', payload)
            cached = {'group': [{'work-summary': [{'title': {'title': {'value': 'cached'}}, 'external-ids': {'external-id': []}}]}]}
            write_json(root / 'orcid_works.json', cached)
            with patch.object(opening, 'OUT', root), patch.object(opening, 'read_profile', return_value={'profile': {'identifiers': {'orcid': 'id'}}}), patch.object(opening, 'get_json', return_value=(None, {'reason': 'http_503'})), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(opening.main(), 2)
            self.assertEqual(read_json(root / 'orcid_works.json'), cached)
            self.assertEqual(len(read_json(root / 'open_publications.json')['records']), 2)
            self.assertFalse(read_json(root / 'harvest_report.json')['complete'])

    def test_http_retries_429_and_never_retains_error_body(self):
        error = HTTPError('https://api.openalex.org', 429, 'quota', {}, io.BytesIO(b'private response'))
        with patch.object(opening.urllib.request, 'urlopen', side_effect=error) as request, patch.object(opening.time, 'sleep'):
            result, diagnostic = opening.get_json('https://api.openalex.org')
        self.assertIsNone(result)
        self.assertEqual(request.call_count, 3)
        self.assertNotIn('private', json.dumps(diagnostic))

    def test_scopus_official_metrics_are_nested_and_nullable(self):
        profile = scopus.flatten_author_profile({'author-retrieval-response': [{'coredata': {'document-count': '8'}, 'h-index': '2', 'citation-count': {'$': '0'}}]})
        self.assertEqual(profile['h_index'], 2)
        self.assertEqual(profile['citation_count'], 0)
        self.assertIsNone(profile['cited_by_count'])

    def test_scopus_missing_citations_are_not_zero(self):
        self.assertIsNone(scopus.normalize_work({'dc:title': 'work'})['cited_by_count'])
        self.assertEqual(scopus.normalize_work({'citedby-count': '0'})['cited_by_count'], 0)

    def test_scopus_late_failure_is_incomplete(self):
        pages = [({'search-results': {'opensearch:totalResults': '2', 'opensearch:startIndex': '0', 'opensearch:itemsPerPage': '1', 'entry': [{'dc:title': 'first', 'eid': 'one'}]}}, {}), ({'_http_status': 403, '_reason': 'http_403'}, {})]
        with patch.object(scopus, 'request_json', side_effect=pages), patch.object(scopus.time, 'sleep'):
            rows, payload, _ = scopus.fetch_works('not-a-real-key', '123')
        self.assertEqual(len(rows), 1)
        self.assertFalse(payload['_collection']['complete'])
        self.assertEqual(payload['_collection']['reason'], 'http_403')

    def test_scopus_failure_keeps_last_good_files_byte_for_byte(self):
        for failed_search in ({'_http_status': 401, '_reason': 'http_401'}, {'search-results': {}}):
            with self.subTest(failed_search=failed_search), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prefix = 'scopus_author_123_'
                write_json(root / (prefix + 'works.json'), [{'eid': 'old', 'title': 'retained'}])
                write_json(root / (prefix + 'metrics.json'), {'citation_sum_from_search': 7})
                before = {path.name: path.read_bytes() for path in root.glob('*.json')}
                with patch.dict(os.environ, {'SCOPUS_OUT_DIR': directory, 'SCOPUS_AUTHOR_ID': '123', 'SCOPUS_API_KEY': 'not-a-real-key'}), patch.object(scopus, 'request_json', return_value=(failed_search, {})), patch('sys.stdout', new_callable=io.StringIO):
                    self.assertEqual(scopus.main(), 2)
                for name, data in before.items():
                    self.assertEqual((root / name).read_bytes(), data)
                report = read_json(root / (prefix + 'access_report.json'))
                self.assertEqual(set(report['author_attempts']), {'STANDARD', 'ENHANCED'})
                self.assertFalse(report['complete'])

    def test_scopus_search_success_does_not_require_author_entitlement(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = [({'_http_status': 401, '_reason': 'http_401'}, {}), ({'_http_status': 403, '_reason': 'http_403'}, {}), ({'search-results': {'opensearch:totalResults': '1', 'entry': [{'dc:title': 'Work', 'eid': 'id', 'citedby-count': '0'}]}}, {})]
            with patch.dict(os.environ, {'SCOPUS_OUT_DIR': directory, 'SCOPUS_AUTHOR_ID': '123', 'SCOPUS_API_KEY': 'not-a-real-key'}), patch.object(scopus, 'request_json', side_effect=responses), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(scopus.main(), 0)
            metrics = read_json(Path(directory) / 'scopus_author_123_metrics.json')
            self.assertEqual(metrics['citation_sum_from_search'], 0)
            self.assertEqual(metrics['h_index_recomputed_from_retrieved_works'], 0)
            self.assertEqual(metrics['method'], 'calculated_from_complete_search')
            self.assertIsInstance(read_json(Path(directory) / 'scopus_author_123_works.json'), list)

    def test_eLibrary_unknown_citations_and_real_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'items.html'
            path.write_text('<tr id="arw1"><td>1</td><td><a href="item.asp?id=1">Work</a></td><td>—</td></tr><tr id="arw2"><td>2</td><td><a href="item.asp?id=2">Another</a></td><td>0</td></tr>', encoding='utf-8')
            rows = parse_elibrary_author_items(str(path))
            self.assertIsNone(rows[0]['rinc_citations'])
            self.assertEqual(rows[1]['rinc_citations'], 0)

    def test_elibrary_total_uses_site_counter(self):
        self.assertEqual(elibrary.list_total('<p>Всего найдено <b>1 234</b> публикаций</p>'), 1234)
        self.assertIsNone(elibrary.list_total('<p>Sign in</p>'))

    def test_successful_detail_cache_does_not_require_optional_isbn(self):
        at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        row = {'elibrary_item_id': '1', 'year': 2026, 'venue': 'Journal'}
        entry = {'fetched_at': (at - timedelta(days=7)).isoformat(), 'status': 'success', 'parsed': {'venue': 'Journal', 'doi': '10.1/test'}, 'observed_absent_fields': ['isbn', 'volume']}
        self.assertFalse(needs_details(row, {'1': entry}, at=at))
        self.assertTrue(needs_details(row, {'1': entry}, at=at + timedelta(days=23)))

    def test_older_details_refresh_after_90_days_and_failed_fetch_retries(self):
        at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        row = {'elibrary_item_id': '1', 'year': 2020}
        entry = {'fetched_at': (at - timedelta(days=60)).isoformat(), 'parsed': {'pages': '1-4'}}
        self.assertFalse(needs_details(row, {'1': entry}, at=at))
        self.assertTrue(needs_details(row, {'1': entry}, at=at + timedelta(days=30)))
        self.assertTrue(needs_details(row, {'1': {**entry, 'status': 'error'}}, at=at))
        self.assertTrue(needs_details(row, {'1': {**entry, 'fetched_at': (at + timedelta(days=1)).isoformat()}}, at=at))
        self.assertTrue(needs_details(row, {}, at=at))

    def test_elibrary_full_pagination(self):
        page = MagicMock()
        pages = ['<p>Всего найдено 2 публикаций</p>', '<p>Всего найдено 2 публикаций</p>']
        with patch.object(elibrary, 'wait_ready', side_effect=pages), patch.object(elibrary, 'parse_items', side_effect=[[{'elibrary_item_id': '1'}], [{'elibrary_item_id': '2'}]]):
            rows = elibrary.collect_items(page, 'unused')
        self.assertEqual(len(rows), 2)
        self.assertEqual(page.goto.call_count, 1)
        self.assertEqual(page.wait_for_load_state.call_count, 1)

    def test_login_challenges_are_classified(self):
        samples = [('Please verify you are human', 'human_verification_required'), ('Enter your two-factor authentication code', 'mfa_required'), ('Link your account', 'account_link_required'), ('Incorrect password', 'invalid_credentials')]
        for text, expected in samples:
            self.assertEqual(auth.challenge_reason(text), expected)
        self.assertIsNone(auth.challenge_reason('Scientific publication about Turing machines'))
        self.assertEqual(auth.challenge_reason('Invalid sign in details. Please check your ORCID sign in details and then try signing in again.'), 'invalid_credentials')

    def test_orcid_response_evidence_is_specific_and_private(self):
        payload = {'success': False, 'errors': ['private message'], 'email': 'private@example.test', 'url': 'https://orcid.org?token=private'}
        evidence = auth.orcid_auth_response_evidence(200, payload)
        self.assertEqual(evidence['reason'], 'orcid_signin_rejected')
        self.assertNotIn('private', json.dumps(evidence))
        self.assertEqual(auth.orcid_auth_response_evidence(200, {'verificationCodeRequired': True})['reason'], 'mfa_required')
        self.assertIsNone(auth.orcid_auth_response_evidence(200, {'success': True})['reason'])
        self.assertEqual(auth.orcid_auth_response_evidence(401, {})['reason'], 'orcid_auth_http_401')

    def test_orcid_username_markdown_normalization_is_narrow(self):
        self.assertEqual(auth.normalize_orcid_username('  fixture\\@example.test \n'), 'fixture@example.test')
        self.assertTrue(auth.valid_orcid_username('fixture@example.test'))
        self.assertTrue(auth.valid_orcid_username('0000-0002-4130-3812'))
        self.assertFalse(auth.valid_orcid_username('fixture\\@example.test'))
        self.assertEqual(auth.normalize_orcid_username('name\\part@example.test'), 'name\\part@example.test')

    def test_vpn_mismatch_stops_before_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'expected'
            path.write_text('192.0.2.1')
            context = MagicMock()
            context.new_page.return_value.goto.return_value.json.return_value = {'ip': '192.0.2.2'}
            with patch.dict(os.environ, {'HOME_VPN_REQUIRED': '1', 'HOME_VPN_EXPECTED_IP_FILE': str(path)}):
                with self.assertRaisesRegex(auth.AuthFailure, '^vpn_route_mismatch$'):
                    auth.verify_browser_egress(context)
            context.new_page.return_value.close.assert_called_once()

    def test_wos_public_orcid_link_is_never_used_as_login(self):
        page = MagicMock()
        with patch.object(auth, 'visible') as selector, patch.object(auth, 'click_named', return_value=False):
            self.assertFalse(auth.choose_orcid_signin(page, 'www.webofscience.com'))
            selector.assert_not_called()
        self.assertFalse(auth.provider_host('not-orcid.org', 'orcid.org'))
        self.assertTrue(auth.provider_host('orcid.org', 'orcid.org'))

    def test_oauth_popup_may_close_during_navigation_wait(self):
        page = MagicMock()
        page.wait_for_timeout.side_effect = RuntimeError('Target closed')
        page.is_closed.return_value = True
        auth.wait_navigation(page)
        page.is_closed.return_value = False
        with self.assertRaises(RuntimeError):
            auth.wait_navigation(page)

    def test_browser_initialization_diagnostics_are_allowlisted(self):
        error = RuntimeError('chrome_crashpad_handler: --database is required. Permission denied /home/private-owner/private-cookie https://private.test/?SID=private-session password=private-password')
        diagnostics = auth.browser_initialization_diagnostics(error)
        self.assertEqual(diagnostics['reason'], 'filesystem_permission_denied')
        self.assertIn('crashpad_initialization_failed', diagnostics['signals'])
        self.assertEqual(diagnostics['error_type'], 'OtherError')
        serialized = json.dumps(diagnostics)
        for private in ('private-owner', 'private-cookie', 'private-session', 'private-password', 'https://', '/home/'):
            self.assertNotIn(private, serialized)

    def test_browser_initialization_detects_display_or_missing_executable(self):
        self.assertEqual(auth.browser_initialization_diagnostics(RuntimeError('Missing X server or $DISPLAY'))['reason'], 'display_unavailable')
        self.assertEqual(auth.browser_initialization_diagnostics(RuntimeError("Executable doesn't exist at a private path"))['reason'], 'browser_executable_missing')

    def test_wos_partial_pagination_is_not_accepted(self):
        page = MagicMock()
        data = {'summary': {'publications': 2}, 'records': [{'wos_uid': 'WOS:1'}]}
        with patch.object(wos, 'read_records', return_value=data), patch.object(wos, 'visible', return_value=None):
            with self.assertRaisesRegex(auth.AuthFailure, 'incomplete_pagination'):
                wos.collect_profile(page, {})

    def test_wos_verified_zero_and_old_record_are_preserved(self):
        page = MagicMock()
        data = {'summary': {'publications': 1, 'citations': 0, 'h_index': 0}, 'records': [{'wos_uid': 'WOS:new', 'title': 'New'}]}
        old = {'summary': {'citations': 7, 'h_index': 2}, 'records': [{'wos_uid': 'WOS:old', 'title': 'Old'}]}
        with patch.object(wos, 'read_records', return_value=data), patch.object(wos, 'visible', return_value=None):
            result = wos.collect_profile(page, old)
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['summary']['citations'], 0)
        self.assertEqual(result['summary']['h_index'], 0)

    def test_wos_records_do_not_claim_freshness_for_missing_metrics(self):
        data = {'summary': {'publications': 1}, 'records': [{'wos_uid': 'WOS:1'}]}
        with patch.object(wos, 'read_records', return_value=data), patch.object(wos, 'visible', return_value=None):
            with self.assertRaisesRegex(auth.AuthFailure, 'profile_metrics_missing'):
                wos.collect_profile(MagicMock(), {'summary': {'citations': 7, 'h_index': 2}})

    def test_wos_profile_diagnostics_only_expose_counts_and_schema(self):
        data = {'summary': {'publications': 1, 'citations': 0, 'h_index': 'private'}, 'records': [{'title': 'private', 'url': 'https://private.test?SID=private'}], 'summary_metrics': {'private': {'value': 1}}}
        with patch.object(wos, 'parse_wos_author_profile_html', return_value=data):
            diagnostic = wos.safe_profile_diagnostics(MagicMock())
        self.assertEqual(diagnostic['parsed_record_count'], 1)
        self.assertEqual(diagnostic['summary']['citations'], 0)
        self.assertIsNone(diagnostic['summary']['h_index'])
        self.assertEqual(diagnostic['record_fields'], ['title', 'url'])
        self.assertNotIn('private', str(diagnostic))

    def test_verification_evidence_survives_diagnostic_export(self):
        import refresh_pipeline
        evidence = {'trigger': 'iframe_title', 'frame': {'provider_host': 'www.google.com', 'provider_path': '/recaptcha/api2/bframe', 'active_challenge_controls': True, 'checkbox_checked': None, 'rect': {'x': 0, 'y': 0, 'width': 300, 'height': 200}}}
        with tempfile.TemporaryDirectory() as directory:
            stage, output = Path(directory) / 'stage', Path(directory) / 'output'
            write_json(stage / 'data/audit/refresh_run.json', {'attempted_at': '2026-09-20T15:00:00Z', 'selected_sources': ['wos', 'elibrary']})
            for filename in ('wos/harvest_report.json', 'elibrary/browser_fetch_report.json'):
                write_json(stage / 'data' / filename, {'attempted_at': '2026-09-20T15:01:00Z', 'verification_evidence': evidence})
            refresh_pipeline.diagnostics(stage, output)
            for filename in ('wos/harvest_report.json', 'elibrary/browser_fetch_report.json'):
                self.assertEqual(read_json(output / 'data' / filename, {})['verification_evidence'], evidence)

    def test_elibrary_detail_challenge_evidence_is_preserved(self):
        evidence = {'trigger': 'page_marker', 'marker_ids': ['turing_test_ru']}
        with patch.object(elibrary, 'needs_details', return_value=True), patch.object(elibrary, 'assert_no_challenge', side_effect=auth.AuthFailure('human_verification_required', verification_evidence=evidence)):
            _, report = elibrary.collect_details(MagicMock(), [{'elibrary_item_id': '123'}], {})
        self.assertEqual(report['verification_evidence'], evidence)
        self.assertEqual(report['failed'], 1)


if __name__ == '__main__':
    unittest.main()
