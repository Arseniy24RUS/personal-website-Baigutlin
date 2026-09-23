"""Offline transport composition: API evidence survives browser failures."""
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import harvest_wos as dispatcher
import harvest_wos_api as api
import refresh_pipeline as pipeline
from source_health import load_checkpoint, materialize_checkpoint, source_result, write_checkpoint

OLD = '2026-09-19T10:00:00+00:00'
CURRENT = '2026-09-20T10:00:00+00:00'


def baseline():
    report = source_result(status='success', count=1, attempted_at=OLD)
    report['components'] = {name: source_result(status='success', count=1, attempted_at=OLD)
                            for name in dispatcher.COMPONENTS}
    payload = {'metrics': {'summary': {'publications': 1, 'citations': 7, 'h_index': 1}},
               'publications': [{'wos_uid': 'WOS:OLD', 'doi': '10.1234/old', 'title': 'Old title',
                                  'title_en': 'Reviewed English', 'wos_citations': 7, 'observed_at': OLD,
                                  'citation_observed_at': {'wos': OLD}, 'sources': ['wos']}], 'details': {}}
    return report, payload


def save(root, report, payload):
    path = root / 'data/wos/collection_checkpoint.json'
    write_checkpoint(path, report, payload)
    materialize_checkpoint(path, 'wos')


def observation(metrics='success', publications='success', *, rows=None, summary=None, reason=None, extra=None):
    old_report, old_payload = baseline()
    report = source_result(old_report, status='success' if metrics == publications == 'success' else 'partial',
                           count=1, attempted_at=CURRENT, reason=reason)
    report['components'] = {
        name: source_result(old_report['components'][name], status=status, count=1, attempted_at=CURRENT,
                            reason=reason if status != 'success' else None)
        for name, status in [('metrics', metrics), ('publications', publications)]
    }
    payload = copy.deepcopy(old_payload)
    if summary is not None:
        payload['metrics']['summary'] = summary
    if rows is not None:
        payload['publications'] = rows
    if extra:
        report.update(extra)
    return report, payload


def fresh_row(**values):
    return {'wos_uid': 'WOS:NEW', 'title': 'New verified title', 'observed_at': CURRENT, **values}


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        save(self.root, *baseline())

    def collect(self, observations):
        calls = []

        def run(transport, root):
            calls.append(transport)
            result = observations[transport]
            if callable(result):
                return result(root)
            report, payload, *exit_code = copy.deepcopy(result)
            save(root, report, payload)
            return (exit_code or [0])[0], 'completed'

        plan = list(observations)
        if 'browser' not in plan:
            plan.append('browser')
        with patch.object(dispatcher, 'run_transport', side_effect=run), patch.object(dispatcher, 'now', return_value=CURRENT), \
                patch.object(dispatcher, 'transport_plan', return_value=plan):
            report = dispatcher.dispatch(self.root)
        return report, load_checkpoint(self.root / 'data/wos/collection_checkpoint.json')['payloads'], calls

    def test_full_api_skips_browser_retains_manual_rows_and_accepts_true_zero(self):
        result = observation(rows=[fresh_row(wos_citations=0, retained_citation_fields=[],
                                            citation_observed_at={'wos': CURRENT})],
                             summary={'publications': 1, 'citations': 0, 'h_index': 0})
        report, payload, calls = self.collect({'api': result})
        self.assertEqual(calls, ['api'])
        self.assertTrue(report['complete'])
        self.assertNotIn('providers', report)
        self.assertEqual(payload['metrics']['summary']['citations'], 0)
        self.assertEqual(len(payload['publications']), 2)
        self.assertEqual(payload['publications'][0]['title_en'], 'Reviewed English')
        self.assertEqual(report['components']['publications']['observed_count'], 1)
        self.assertEqual(report['components']['publications']['record_count'], 2)
        legacy = json.loads((self.root / 'data/wos/profile_metrics.json').read_text())
        self.assertEqual(legacy['records'], payload['publications'])

    def test_api_publications_survive_browser_challenge_without_confirming_metrics(self):
        api_result = observation(metrics='blocked', rows=[fresh_row()], reason='api_citations_unavailable')
        browser = observation(metrics='blocked', publications='blocked', reason='human_verification_required')
        report, payload, calls = self.collect({'api': api_result, 'browser': browser})
        self.assertEqual(calls, ['api', 'browser'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['components']['publications']['transport'], 'api')
        self.assertEqual(report['components']['publications']['last_success_at'], CURRENT)
        self.assertEqual(report['components']['metrics']['last_success_at'], OLD)
        self.assertEqual(payload['metrics']['summary']['citations'], 7)
        self.assertIn('WOS:NEW', [row['wos_uid'] for row in payload['publications']])
        self.assertEqual(report['transport_attempts']['browser']['reason'], 'human_verification_required')

    def test_api_metrics_survive_browser_timeout(self):
        api_result = observation(publications='error', reason='api_timeout',
                                 summary={'publications': 13, 'citations': 0, 'h_index': 0})
        report, payload, _ = self.collect({'api': api_result, 'browser': lambda root: (124, 'transport_timeout')})
        self.assertEqual(report['components']['metrics']['status'], 'success')
        self.assertEqual(report['components']['metrics']['transport'], 'api')
        self.assertEqual(payload['metrics']['summary']['citations'], 0)
        self.assertEqual(payload['publications'], baseline()[1]['publications'])
        self.assertFalse(report['complete'])

    def test_partial_official_hindex_keeps_its_field_stamp_through_browser_failure(self):
        report, payload = observation(metrics='partial', publications='error', reason='api_timeout')
        payload['metrics'].update(metric_observed_at={'h_index': CURRENT, 'citations': OLD, 'publications': OLD},
                                  retained_metric_fields=['publications', 'citations'])
        payload['metrics']['summary']['h_index'] = 2
        result, data, _ = self.collect({'api': (report, payload), 'browser': lambda root: (124, 'transport_timeout')})
        self.assertEqual(data['metrics']['summary']['h_index'], 2)
        self.assertEqual(data['metrics']['metric_observed_at']['h_index'], CURRENT)
        self.assertEqual(data['metrics']['summary']['citations'], 7)
        self.assertEqual(result['components']['metrics']['status'], 'partial')
        self.assertEqual(result['components']['metrics']['transport'], 'api')
        self.assertEqual(result['components']['metrics']['last_success_at'], OLD)

    def test_complementary_api_list_and_browser_metrics_are_complete(self):
        api_result = observation(metrics='blocked', reason='api_citations_unavailable', rows=[fresh_row()])
        browser_result = observation(publications='blocked', reason='human_verification_required',
                                     summary={'publications': 13, 'citations': 8, 'h_index': 2})
        report, payload, _ = self.collect({'api': api_result, 'browser': browser_result})
        self.assertTrue(report['complete'])
        self.assertEqual(report['components']['metrics']['transport'], 'browser')
        self.assertEqual(report['components']['publications']['transport'], 'api')
        self.assertEqual(payload['metrics']['summary']['citations'], 8)

    def test_api_entitlement_failure_is_visible_when_browser_succeeds(self):
        failed = observation(metrics='blocked', publications='blocked', reason='api_http_401',
                             extra={'api_provider': 'researcher', 'api_attempts': [{'endpoint': 'profile', 'http_status': 401}]})
        report, _, _ = self.collect({'api': failed, 'browser': observation(rows=[fresh_row()])})
        self.assertTrue(report['complete'])
        self.assertEqual(report['transport_attempts']['api']['reason'], 'api_http_401')
        self.assertEqual(report['transport_attempts']['api']['attempts'], [{'endpoint': 'profile', 'http_status': 401}])

    def test_metadata_only_citation_retains_old_clock_then_known_zero_clears_marker(self):
        row = fresh_row(wos_uid='WOS:OLD', wos_citations=7, retained_citation_fields=['wos_citations'],
                        citation_observed_at={'wos': OLD})
        _, payload, _ = self.collect({'api': observation(rows=[row])})
        self.assertEqual(payload['publications'][0]['wos_citations'], 7)
        self.assertEqual(payload['publications'][0]['citation_observed_at']['wos'], OLD)
        save(self.root, *baseline())
        row.update(wos_citations=0, retained_citation_fields=[], citation_observed_at={'wos': CURRENT})
        _, payload, _ = self.collect({'api': observation(rows=[row])})
        self.assertEqual(payload['publications'][0]['wos_citations'], 0)
        self.assertEqual(payload['publications'][0]['retained_citation_fields'], [])
        self.assertEqual(payload['publications'][0]['citation_observed_at']['wos'], CURRENT)

    def test_same_uid_changed_title_and_doi_only_alias_do_not_duplicate(self):
        for row in (fresh_row(wos_uid='WOS:OLD'), fresh_row(wos_uid='WOS:NEW', doi='https://doi.org/10.1234/OLD')):
            with self.subTest(row=row):
                save(self.root, *baseline())
                _, payload, _ = self.collect({'api': observation(rows=[row])})
                self.assertEqual(len(payload['publications']), 1)
                self.assertEqual(payload['publications'][0]['wos_uid'], 'WOS:OLD')
                self.assertEqual(payload['publications'][0]['title_en'], 'Reviewed English')

    def test_success_without_current_atomic_checkpoint_cannot_skip_browser(self):
        report, _, calls = self.collect({'api': lambda root: (0, 'completed'), 'browser': observation()})
        self.assertEqual(calls, ['api', 'browser'])
        self.assertEqual(report['transport_attempts']['api']['reason'], 'missing_atomic_checkpoint')

    def test_incomplete_metrics_payload_cannot_claim_complete_or_replace_baseline(self):
        bad = observation(summary={'publications': 1, 'h_index': 1})
        report, payload, calls = self.collect({'api': bad, 'browser': lambda root: (124, 'transport_timeout')})
        self.assertEqual(calls, ['api', 'browser'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['transport_attempts']['api']['status'], 'partial')
        self.assertEqual(report['transport_attempts']['api']['components']['metrics']['reason'], 'invalid_metrics_payload')
        self.assertEqual(payload['metrics'], baseline()[1]['metrics'])

    def test_stale_component_cannot_smuggle_new_rows(self):
        report, payload = observation(rows=[fresh_row()])
        report['components']['publications'] = baseline()[0]['components']['publications']
        result, data, _ = self.collect({'api': (report, payload), 'browser': lambda root: (124, 'transport_timeout')})
        self.assertEqual(data['publications'], baseline()[1]['publications'])
        self.assertFalse(result['complete'])

    def test_checkpoint_written_before_browser_and_baseline_workspaces_are_independent(self):
        def interrupted(browser_root):
            baseline_copy = load_checkpoint(browser_root / 'data/wos/collection_checkpoint.json')
            self.assertEqual(baseline_copy['payloads'], baseline()[1])
            committed = load_checkpoint(self.root / 'data/wos/collection_checkpoint.json')
            self.assertEqual(committed['report']['components']['publications']['status'], 'success')
            self.assertEqual(len(committed['payloads']['publications']), 2)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.collect({'api': observation(metrics='blocked', reason='api_citations_unavailable', rows=[fresh_row()]),
                          'browser': interrupted})

    def test_real_api_collector_checkpoint_is_accepted_without_network_or_browser(self):
        class Response:
            status_code, headers = 200, {}
            def json(self):
                return {'metadata': {'page': 1, 'limit': 50, 'total': 1}, 'hits': [
                    {'uid': 'WOS:NEW', 'title': 'API verified article', 'source': {'publishYear': 2026},
                     'identifiers': {}, 'citations': [{'db': 'WOS', 'count': 0}]}]}
        class HTTP:
            def get(self, url, **kwargs):
                self.last = kwargs
                return Response()
        http = HTTP()
        def real_api(root):
            with patch.dict(os.environ, {'WOS_STARTER_API_KEY': 'synthetic-key'}, clear=True), patch.object(api, 'now', return_value=CURRENT):
                report = api.harvest(root, provider='starter', session=http)
            self.assertEqual(report['status'], 'success')
            return 0, 'completed'
        report, payload, calls = self.collect({'api': real_api})
        self.assertEqual(calls, ['api'])
        self.assertTrue(report['complete'])
        self.assertEqual(payload['metrics']['summary']['citations'], 0)
        self.assertEqual(report['transport_attempts']['api']['provider'], 'starter')
        self.assertEqual(report['transport_attempts']['api']['attempts'], [{'endpoint': 'documents', 'http_status': 200}])
        self.assertEqual(http.last['headers']['X-ApiKey'], 'synthetic-key')
        self.assertNotIn('synthetic-key', json.dumps(report))

    def test_both_keys_try_starter_after_researcher_denial_before_browser(self):
        report, _, calls = self.collect({
            'api_researcher': observation(metrics='blocked', publications='blocked', reason='api_http_403'),
            'api_starter': observation(rows=[fresh_row()]),
        })
        self.assertEqual(calls, ['api_researcher', 'api_starter'])
        self.assertTrue(report['complete'])
        self.assertEqual(report['transport_attempts']['api_researcher']['reason'], 'api_http_403')
        self.assertEqual(report['components']['metrics']['transport'], 'api_starter')

    def test_browser_metrics_reset_previous_api_methods_and_retained_flags(self):
        old_report, old_payload = baseline()
        old_payload['metrics'].update(
            metric_methods={key: 'starter_api_calculated_complete_core' for key in ('publications', 'citations', 'h_index')},
            api_metric_method={'citations': 'starter_api_calculated_complete_core'},
            retained_metric_fields=['citations'], metric_observed_at={'citations': OLD})
        save(self.root, old_report, old_payload)
        report, payload, _ = self.collect({
            'api': observation(metrics='blocked', publications='blocked', reason='api_http_401'),
            'browser': observation(),
        })
        self.assertTrue(report['complete'])
        self.assertEqual(payload['metrics']['metric_methods'], {key: 'provider_profile' for key in ('publications', 'citations', 'h_index')})
        self.assertNotIn('citations', payload['metrics']['api_metric_method'])
        self.assertEqual(payload['metrics']['retained_metric_fields'], [])
        self.assertEqual(payload['metrics']['metric_observed_at']['citations'], CURRENT)

    def test_fresh_browser_core_metrics_keep_retained_indexed_scope_marker(self):
        report, payload = baseline()
        payload['metrics']['summary']['indexed_publications'] = 19
        payload['metrics'].update(retained_metric_fields=['publications', 'citations', 'h_index', 'indexed_publications'],
                                  metric_observed_at={'indexed_publications': OLD})
        save(self.root, report, payload)
        fresh_browser = observation()
        fresh_browser[1]['metrics'].update(retained_metric_fields=['indexed_publications'],
                                           metric_observed_at={'indexed_publications': OLD})
        result, merged, _ = self.collect({
            'api': observation(metrics='blocked', publications='blocked', reason='api_http_401'),
            'browser': fresh_browser,
        })
        self.assertTrue(result['complete'])
        self.assertEqual(merged['metrics']['retained_metric_fields'], ['indexed_publications'])
        self.assertEqual(merged['metrics']['summary']['indexed_publications'], 19)
        self.assertEqual(merged['metrics']['metric_observed_at']['indexed_publications'], OLD)
        self.assertEqual(merged['metrics']['metric_observed_at']['citations'], CURRENT)


class EntryAndPrivacyTests(unittest.TestCase):
    def test_fixed_transport_order_and_only_configured_providers(self):
        for env, expected in [({}, ['browser']),
                              ({'WOS_STARTER_API_KEY': 'x'}, ['api_starter', 'browser']),
                              ({'WOS_RESEARCHER_API_KEY': 'x'}, ['api_researcher', 'browser']),
                              ({'WOS_STARTER_API_KEY': 'x', 'WOS_RESEARCHER_API_KEY': 'y'}, ['api_researcher', 'api_starter', 'browser'])]:
            with self.subTest(expected=expected), patch.dict(os.environ, env, clear=True):
                self.assertEqual(dispatcher.transport_plan(), expected)

    def test_no_key_and_maintenance_use_original_browser_path(self):
        for env in ({}, {'BROWSER_SESSION_MAINTENANCE': '1', 'WOS_STARTER_API_KEY': 'configured'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(dispatcher, 'browser_only', return_value=17) as original, \
                    patch.object(dispatcher, 'dispatch') as dispatched:
                self.assertEqual(dispatcher.main(), 17)
                original.assert_called_once_with()
                dispatched.assert_not_called()

    def test_transport_environment_and_errors_do_not_relay_credentials(self):
        secrets = {'WOS_STARTER_API_KEY': 'synthetic-api-key', 'WOS_RESEARCHER_API_KEY': 'synthetic-other-api',
                   'WOS_ORCID_PASSWORD': 'synthetic-password', 'BROWSER_SESSION_KEY': 'synthetic-session-key'}
        with patch.dict(os.environ, secrets, clear=True), patch.object(dispatcher.subprocess, 'run') as process:
            process.return_value = subprocess.CompletedProcess([], 0, b'synthetic-secret-output', b'synthetic-secret-error')
            self.assertEqual(dispatcher.run_transport('api', Path('.')), (0, 'completed'))
            api_env = process.call_args.kwargs['env']
            self.assertNotIn('WOS_ORCID_PASSWORD', api_env)
            self.assertNotIn('BROWSER_SESSION_KEY', api_env)
            self.assertIn('WOS_STARTER_API_KEY', api_env)
            self.assertEqual(dispatcher.run_transport('browser', Path('.')), (0, 'completed'))
            browser_env = process.call_args.kwargs['env']
            self.assertNotIn('WOS_STARTER_API_KEY', browser_env)
            self.assertNotIn('WOS_RESEARCHER_API_KEY', browser_env)
            self.assertIn('WOS_ORCID_PASSWORD', browser_env)
            dispatcher.run_transport('api_researcher', Path('.'))
            explicit = process.call_args.kwargs['env']
            self.assertEqual(explicit['WOS_API_PROVIDER'], 'researcher')
            self.assertNotIn('WOS_STARTER_API_KEY', explicit)
            process.side_effect = OSError('synthetic-private-value')
            self.assertEqual(dispatcher.run_transport('api', Path('.')), (1, 'transport_execution_failed'))
            process.side_effect = subprocess.TimeoutExpired('synthetic-private-value', 1)
            self.assertEqual(dispatcher.run_transport('api', Path('.')), (124, 'transport_timeout'))

    def test_explicit_login_trial_and_invalid_mode_cannot_use_api_instead(self):
        for mode in ('fresh_orcid', 'invalid'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'WOS_AUTH_MODE': mode, 'WOS_RESEARCHER_API_KEY': 'configured'}, clear=True), \
                    patch.object(dispatcher, 'browser_only', return_value=2) as browser, \
                    patch.object(dispatcher, 'dispatch') as api:
                self.assertEqual(dispatcher.main(), 2)
                browser.assert_called_once_with()
                api.assert_not_called()

    def test_metadata_drops_unrecognized_strings_urls_and_malformed_counts(self):
        report, payload = observation(extra={'api_provider': 'starter', 'api_attempts': [
            {'endpoint': 'documents', 'http_status': 401, 'url': 'https://private.invalid/key', 'token': 'secret'},
            {'endpoint': 'private_value', 'http_status': 403}, {'endpoint': 'profile', 'http_status': float('nan')},
            {'endpoint': 'profile', 'http_status': '200', 'reason': 'private_value'},
        ]}, reason='private_value')
        attempt = {'report': report, 'payloads': payload, 'started': CURRENT}
        metadata = dispatcher._metadata(attempt, 2, 'nonzero_exit')
        encoded = json.dumps(metadata, allow_nan=False)
        for value in ('private_value', 'private.invalid', 'secret', 'NaN'):
            self.assertNotIn(value, encoded)
        self.assertEqual(metadata['attempts'], [{'endpoint': 'documents', 'http_status': 401},
                                                {'endpoint': 'profile'}, {'endpoint': 'profile'}])

    def test_workflow_preserves_optional_keys_only_in_full_collection(self):
        import yaml
        workflow = yaml.safe_load((REPO / '.github/workflows/refresh-data.yml').read_text())
        steps = workflow['jobs']['refresh']['steps']
        collect = next(step for step in steps if step.get('name') == 'Collect and build candidate through home route')
        maintenance = next(step for step in steps if step.get('name') == 'Maintain browser sessions through home route')
        for name in dispatcher.API_KEYS:
            self.assertIn(name, collect['env'])
            self.assertIn(name, collect['run'])
            self.assertNotIn(name, maintenance.get('env', {}))
            self.assertNotIn(name, maintenance['run'])
        self.assertEqual(next(item[1] for item in pipeline.SOURCES if item[0] == 'wos'), 'harvest_wos.py')

    def test_workflow_carries_explicit_login_mode_across_vpn_user_boundary(self):
        import yaml
        workflow = yaml.safe_load((REPO / '.github/workflows/refresh-data.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        mode = triggers['workflow_dispatch']['inputs']['wos_auth_mode']
        self.assertEqual(mode['options'], ['restore', 'fresh_orcid'])
        self.assertEqual(mode['default'], 'restore')
        self.assertEqual(triggers['workflow_call']['inputs']['wos_auth_mode']['default'], 'restore')
        self.assertNotIn('WOS_AUTH_MODE', workflow['env'])
        for step in workflow['jobs']['refresh']['steps']:
            if step.get('name') in {'Collect and build candidate through home route', 'Maintain browser sessions through home route'}:
                self.assertEqual(step['env']['WOS_AUTH_MODE'], "${{ inputs.wos_auth_mode || 'restore' }}")
                preserve = re.search(r'sudo --preserve-env=([^\s]+)', step['run'])
                self.assertIsNotNone(preserve)
                self.assertIn('WOS_AUTH_MODE', preserve.group(1).split(','))
            else:
                self.assertNotIn('WOS_AUTH_MODE', step.get('env', {}))


if __name__ == '__main__':
    unittest.main()
