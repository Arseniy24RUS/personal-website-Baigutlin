"""Metrics, partial publication pages and session-free public URLs are independent."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import build_public_data as builder
import publish_refresh as publisher
import refresh_pipeline as pipeline
import report_safety as safety
import source_health as health
import merge_wos_records_into_public_data as wos_merge

OLD, MIDDLE, NEW = ['2026-09-20T' + hour + ':00:00+00:00' for hour in ('10', '11', '12')]


def success(stamp=NEW, count=1):
    return health.source_result(status='success', count=count, attempted_at=stamp)


def failure(stamp=NEW, last=OLD):
    return health.source_result({'last_success_at': last}, status='error', count=1,
                                reason='pagination_timeout', attempted_at=stamp)


def report(metrics, publications):
    return {**failure(), 'status': 'partial', 'components': {'metrics': metrics, 'publications': publications}}


class ComponentConsumers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_partial_list_does_not_discard_verified_zero_metrics(self):
        state = builder.normalize_health(report(success(), failure()))
        self.assertEqual(state['components']['metrics']['last_success_at'], NEW)
        result = builder.build_scientometrics([], {}, {}, {'summary': {'publications': 13, 'citations': 0, 'h_index': None}},
            {'wos': state}, {'sources': {'wos': {'publications': 10, 'citations': 7, 'h_index': 2,
                                               'last_success_at': OLD}}})['sources']['wos']
        self.assertEqual((result['publications'], result['citations'], result['h_index']), (13, 0, 2))
        self.assertEqual(result['metric_observed_at']['citations'], NEW)
        self.assertEqual(result['metric_observed_at']['h_index'], OLD)
        self.assertEqual(result['retained_metrics'], ['h_index'])

    def test_partial_page_citations_update_only_observed_rows_in_both_passes(self):
        old = {'wos_uid': 'WOS:1', 'title': 'Manual title', 'wos_citations': 9,
               'citation_observed_at': {'wos': MIDDLE}}
        new = {'wos_uid': 'WOS:1', 'title': 'Source title', 'wos_citations': 0, 'observed_at': NEW}
        builder.enrich_from_wos(old, new, fresh=False)
        self.assertEqual(old['wos_citations'], 0)
        self.assertEqual(old['title'], 'Manual title')
        wos_merge.enrich_existing(old, {**new, 'wos_citations': 4, 'observed_at': OLD}, fresh=True)
        self.assertEqual(old['wos_citations'], 0)
        self.assertEqual(old['citation_observed_at']['wos'], NEW)

    def test_atomic_checkpoint_recovers_metrics_after_timeout_before_materialization(self):
        stage = self.root
        report_path = stage / 'data/wos/harvest_report.json'
        prior = report(success(OLD), success(OLD))
        health.write_json(report_path, prior)
        health.write_json(stage / 'data/audit/refresh_run.json', {'state': 'collecting'})
        current = report(success(), failure())

        def run(script, *args, **kwargs):
            if script == 'fixture_collector.py':
                health.write_checkpoint(stage / 'data/wos/collection_checkpoint.json', current,
                    {'metrics': {'summary': {'citations': 0}}, 'publications': [{'id': 'kept'}], 'details': {}})
                return 124, 'timeout'
            return 2, 'source_degraded'

        with patch.dict(os.environ, {'HOME_VPN_REQUIRED': '1'}), patch.object(pipeline, 'now', return_value=NEW), \
             patch.object(pipeline, 'SOURCES', [('wos', 'fixture_collector.py', 'data/wos/harvest_report.json', 1)]), \
             patch.object(pipeline, 'DERIVED', []), patch.object(pipeline, 'run', side_effect=run):
            pipeline.collect(stage, 'wos')
        output = health.read_json(report_path)
        self.assertEqual(output['components']['metrics']['status'], 'success')
        self.assertEqual(output['components']['publications']['status'], 'error')
        self.assertEqual(health.read_json(stage / 'data/wos/profile_metrics.json')['summary']['citations'], 0)
        self.assertEqual(health.read_json(stage / 'data/audit/refresh_run.json')['state'], 'ready')

    def test_current_top_report_cannot_reuse_prior_component_success(self):
        prior = report(success(OLD), success(OLD))
        current = report(success(), success(OLD))
        health.write_json(self.root / 'data/wos/harvest_report.json', prior)
        health.write_json(self.root / 'data/audit/refresh_run.json', {'state': 'collecting'})

        def run(script, *args, **kwargs):
            if script == 'fixture_collector.py':
                health.write_json(self.root / 'data/wos/harvest_report.json', current)
                return 0, 'completed'
            return 2, 'source_degraded'

        with patch.dict(os.environ, {'HOME_VPN_REQUIRED': '1'}), patch.object(pipeline, 'now', return_value=NEW), \
             patch.object(pipeline, 'SOURCES', [('wos', 'fixture_collector.py', 'data/wos/harvest_report.json', 1)]), \
             patch.object(pipeline, 'DERIVED', []), patch.object(pipeline, 'run', side_effect=run):
            pipeline.collect(self.root, 'wos')
        state = health.read_json(self.root / 'data/wos/harvest_report.json')['components']
        self.assertEqual(state['metrics']['status'], 'success')
        self.assertEqual(state['publications']['status'], 'error')
        self.assertEqual(state['publications']['last_success_at'], OLD)

    def test_crashed_writer_without_atomic_payload_cannot_claim_component_success(self):
        prior = report(success(OLD), success(OLD))
        health.write_json(self.root / 'data/wos/harvest_report.json', prior)
        health.write_json(self.root / 'data/audit/refresh_run.json', {'state': 'collecting'})

        def run(script, *args, **kwargs):
            if script == 'fixture_collector.py':
                health.write_json(self.root / 'data/wos/harvest_report.json', report(success(), failure()))
                return 124, 'timeout'
            return 2, 'source_degraded'

        with patch.dict(os.environ, {'HOME_VPN_REQUIRED': '1'}), patch.object(pipeline, 'now', return_value=NEW), \
             patch.object(pipeline, 'SOURCES', [('wos', 'fixture_collector.py', 'data/wos/harvest_report.json', 1)]), \
             patch.object(pipeline, 'DERIVED', []), patch.object(pipeline, 'run', side_effect=run):
            pipeline.collect(self.root, 'wos')
        state = health.read_json(self.root / 'data/wos/harvest_report.json')['components']['metrics']
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['reason'], 'missing_atomic_checkpoint')
        self.assertEqual(state['last_success_at'], OLD)

    def test_concurrent_components_merge_metrics_and_partial_records_independently(self):
        current, candidate = self.root / 'current', self.root / 'candidate'
        before = report(failure(last=OLD), success(MIDDLE, 2))
        incoming = report(success(), {**failure(last=OLD), 'status': 'partial', 'last_observation_at': NEW})
        old_rows = [{'wos_uid': 'WOS:1', 'title': 'First', 'wos_citations': 8, 'observed_at': MIDDLE},
                    {'wos_uid': 'WOS:2', 'title': 'Retained', 'observed_at': MIDDLE}]
        fresh_rows = [{'wos_uid': 'WOS:1', 'title': 'First', 'wos_citations': 0, 'observed_at': NEW},
                      {'wos_uid': 'WOS:3', 'title': 'New', 'observed_at': NEW}]
        for root, state, metrics, rows in [(current, before, {'summary': {'citations': 7}}, old_rows),
                                          (candidate, incoming, {'summary': {'citations': 0}}, fresh_rows)]:
            checkpoint = root / 'data/wos/collection_checkpoint.json'
            health.write_checkpoint(checkpoint, state, {'metrics': metrics, 'publications': rows, 'details': {}})
            health.materialize_checkpoint(checkpoint, 'wos')
        merged = publisher.merge_provider(candidate, current, 'wos', 'harvest_report.json')
        checkpoint = health.load_checkpoint(current / 'data/wos/collection_checkpoint.json')
        self.assertEqual(checkpoint['payloads']['metrics']['summary']['citations'], 0)
        rows = {row['wos_uid']: row for row in checkpoint['payloads']['publications']}
        self.assertEqual(set(rows), {'WOS:1', 'WOS:2', 'WOS:3'})
        self.assertEqual(rows['WOS:1']['wos_citations'], 0)
        self.assertEqual(merged['components']['publications']['status'], 'partial')
        self.assertEqual(merged['components']['publications']['last_success_at'], MIDDLE)

    def test_signed_record_urls_become_canonical_and_other_secrets_are_idempotently_redacted(self):
        url = 'https://www.webofscience.com/wos/alldb/full-record/WOS:12345?SrcAppSID=secret1&HMAC=secret2&SID=secret3'
        expected = 'https://www.webofscience.com/wos/woscc/full-record/WOS:12345'
        self.assertEqual(safety.clean_url(url), expected)
        value = {'url': url, 'nested': {'SrcAppSID': 'secret1', 'HMAC': 'secret2'},
                 'other': 'https://www.webofscience.com/author-search?HMAC=secret2#id_token=secret4'}
        cleaned = safety.sanitize(value)
        self.assertEqual(safety.sanitize(cleaned), cleaned)
        self.assertFalse(any(secret in json.dumps(cleaned) for secret in ('secret1', 'secret2', 'secret3', 'secret4')))
        self.assertEqual(safety.clean_url('https://elibrary.ru/author_profile.asp?id=1170779'),
                         'https://elibrary.ru/author_profile.asp?id=1170779')

    def test_new_wos_card_does_not_claim_an_unobserved_rinc_zero(self):
        rows = []
        builder.merge_wos(rows, [{'wos_uid': 'WOS:4', 'title': 'A new paper', 'wos_citations': 0}])
        self.assertEqual(rows[0]['wos_citations'], 0)
        self.assertIsNone(rows[0]['rinc_citations'])
        self.assertIsNone(builder.wos_metric({'core_collection_metrics': {
            'Sum of Times Cited by Patents': {'value': 99}}}, 'citations'))

    def test_partial_elibrary_rows_keep_manual_fields_and_apply_only_new_observations(self):
        health.write_json(self.root / 'data/public/publications.json', [
            {'elibrary_item_id': '1', 'title': 'Manual one', 'rinc_citations': 8},
            {'elibrary_item_id': '2', 'title': 'Manual two', 'rinc_citations': 6}])
        health.write_json(self.root / 'data/public/profile.json', {'identifiers': {}, 'source_health': {
            'elibrary': report(success(MIDDLE), success(MIDDLE))}})
        health.write_json(self.root / 'data/processed/elibrary_publications.json', [
            {'elibrary_item_id': '1', 'title': 'Source one', 'rinc_citations': 0, 'observed_at': NEW},
            {'elibrary_item_id': '2', 'title': 'Source two', 'rinc_citations': 0, 'observed_at': OLD}])
        health.write_json(self.root / 'data/elibrary/browser_fetch_report.json', report(success(), failure()))
        with patch.object(builder, 'DATA', self.root / 'data'):
            rows = {row['elibrary_item_id']: row for row in builder.load_elib({})}
        self.assertEqual((rows['1']['rinc_citations'], rows['2']['rinc_citations']), (0, 6))
        self.assertEqual((rows['1']['title'], rows['2']['title']), ('Manual one', 'Manual two'))

    def test_new_publication_component_cannot_replace_newer_metric_snapshot(self):
        current, candidate = self.root / 'current', self.root / 'candidate'
        before = report(success(), success(OLD))
        incoming = report({**failure(), 'last_success_at': MIDDLE}, success())
        for root, state, citations, rows in [(current, before, 20, [{'elibrary_item_id': '1', 'title': 'Kept'}]),
                                            (candidate, incoming, 11, [{'elibrary_item_id': '2', 'title': 'New'}])]:
            checkpoint = root / 'data/elibrary/collection_checkpoint.json'
            health.write_checkpoint(checkpoint, state, {'metrics': {'summary': {'citations_rinc': citations}},
                                                        'publications': rows, 'details': {}})
            health.materialize_checkpoint(checkpoint, 'elibrary')
        result = publisher.merge_provider(candidate, current, 'elibrary', 'browser_fetch_report.json')
        payload = health.load_checkpoint(current / 'data/elibrary/collection_checkpoint.json')['payloads']
        self.assertEqual(payload['metrics']['summary']['citations_rinc'], 20)
        self.assertEqual({row['elibrary_item_id'] for row in payload['publications']}, {'1', '2'})
        self.assertEqual(result['components']['metrics']['status'], 'error')
        self.assertEqual(result['components']['metrics']['last_success_at'], NEW)

    def test_partial_detail_observation_preserves_known_nested_metadata(self):
        previous = [{'id': '1', 'observed_at': OLD, 'authors': ['Known author'], 'parsed': {'doi': '10.example/kept', 'pages': '1-4'}}]
        incoming = [{'id': '1', 'observed_at': NEW, 'authors': [], 'parsed': {'pages': '1-5', 'doi': None}}]
        result = publisher.merge_observed_rows(previous, incoming, success(OLD), failure())
        self.assertEqual(result[0]['parsed'], {'doi': '10.example/kept', 'pages': '1-5'})
        self.assertEqual(previous[0]['parsed']['pages'], '1-4')
        self.assertEqual(result[0]['authors'], ['Known author'])

if __name__ == '__main__':
    unittest.main()
