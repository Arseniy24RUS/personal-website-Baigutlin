import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch
import zipfile

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('browser_sessions', SCRIPTS / 'browser_sessions.py')
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)
KEY = base64.b64encode(b'a' * 32).decode()
OTHER_KEY = base64.b64encode(b'b' * 32).decode()


def payload(provider='wos', age=0, kind='confirmed'):
    stamp = (datetime.now(timezone.utc) - timedelta(days=age)).replace(microsecond=0).isoformat()
    return {'schema': sessions.SCHEMA, 'repository': sessions.REPOSITORY, 'provider': provider, 'target_id': sessions.DEFAULT_TARGET[provider],
            'kind': kind, 'created_at': stamp, 'validated_at': stamp if kind == 'confirmed' else None,
            'storage_state': {'cookies': [
                {'domain': 'www.webofscience.com' if provider == 'wos' else '.elibrary.ru',
                 'name': 'WOSSID' if provider == 'wos' else 'SCookieGUID', 'value': 'do-not-print-session',
                 'path': '/', 'httpOnly': True, 'secure': True, 'sameSite': 'Lax', 'expires': 2208988800.0}],
                'origins': []}, 'session_storage': {}}


class BrowserSessionsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'BROWSER_SESSION_KEY': KEY, 'GITHUB_REPOSITORY': sessions.REPOSITORY,
                                                   'ELIBRARY_AUTHOR_ID': sessions.DEFAULT_TARGET['elibrary'],
                                                   'WOS_RESEARCHER_ID': sessions.DEFAULT_TARGET['wos']})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_authenticated_encryption_rejects_tamper_key_provider_author(self):
        sealed = sessions.encrypt_payload(payload())
        self.assertNotIn('do-not-print-session', json.dumps(sealed))
        self.assertEqual(sessions.decrypt_payload(sealed, 'wos')['storage_state'], payload()['storage_state'])
        for wrong in (OTHER_KEY, base64.b64encode(b'x').decode()):
            with self.assertRaises(sessions.SessionError):
                sessions.decrypt_payload(sealed, 'wos', key=wrong)
        with self.assertRaises(sessions.SessionError):
            sessions.decrypt_payload(sealed, 'elibrary')
        damaged = deepcopy(sealed)
        damaged['target_id'] = 'another-person'
        with self.assertRaises(sessions.SessionError):
            sessions.decrypt_payload(damaged, 'wos')
        damaged = {**sealed, 'repository': 'another-owner/personal-website'}
        with self.assertRaises(sessions.SessionError):
            sessions.decrypt_payload(damaged, 'wos')
        damaged = deepcopy(sealed)
        data = bytearray(base64.b64decode(damaged['ciphertext']))
        data[-1] ^= 1
        damaged['ciphertext'] = base64.b64encode(data).decode()
        with self.assertRaises(sessions.SessionError):
            sessions.decrypt_payload(damaged, 'wos')

    def test_allowlist_excludes_analytics_other_accounts_and_retains_64bit_expiry(self):
        data = payload()
        data['storage_state']['cookies'] += [
            {'domain': '.webofscience.com', 'name': '_sp_id', 'value': 'analytics'},
            {'domain': '.unrelated.example', 'name': 'WOSSID', 'value': 'another-account'}]
        data['storage_state']['origins'] = [
            {'origin': 'https://www.webofscience.com', 'localStorage': [{'name': 'token', 'value': 'own-token'}]},
            {'origin': 'https://unrelated.example', 'localStorage': [{'name': 'token', 'value': 'private'}]}]
        clean = sessions.validate_payload(data, 'wos')
        self.assertEqual(len(clean['storage_state']['cookies']), 1)
        self.assertEqual(clean['storage_state']['cookies'][0]['expires'], 2208988800.0)
        self.assertEqual(len(clean['storage_state']['origins']), 1)

    def test_wos_first_party_cf_bm_roundtrip_preserves_attributes_and_expiry(self):
        current = datetime.now(timezone.utc).timestamp()
        for domain in ('.webofscience.com', 'www.webofscience.com'):
            for expires in (current + 1800, current - 3600):
                with self.subTest(domain=domain, expired=expires < current), tempfile.TemporaryDirectory() as directory:
                    data = payload()
                    cookie = {'domain': domain, 'path': '/', 'name': '__cf_bm',
                              'value': 'synthetic-cf-cookie-do-not-print', 'expires': expires,
                              'httpOnly': True, 'secure': True, 'sameSite': 'None'}
                    data['storage_state']['cookies'].append(cookie)
                    with patch('sys.stdout', new_callable=io.StringIO) as output:
                        envelope = sessions.encrypt_payload(data)
                        restored = sessions.decrypt_payload(envelope, 'wos')
                        path = Path(directory) / 'wos.json'
                        sessions.private_write(path, restored)
                        browser = Mock()
                        with patch.dict(os.environ, {'BROWSER_SESSION_INPUT_WOS': str(path)}):
                            _, info = sessions.restore_context(browser, 'wos')
                    self.assertEqual(info['status'], 'restored')
                    supplied = browser.new_context.call_args.kwargs['storage_state']['cookies']
                    self.assertEqual(next(item for item in supplied if item['name'] == '__cf_bm'), cookie)
                    # A short-lived continuity cookie is not WoS account expiry.
                    self.assertEqual(info['cookie_expires_at'], sessions.cookie_expiry(payload()))
                    self.assertEqual(output.getvalue(), '')
                    self.assertNotIn(cookie['value'], json.dumps(envelope))

    def test_cf_bm_allowlist_does_not_include_analytics_or_unrelated_domains(self):
        data = payload()
        cookies = [
            {'domain': '.webofscience.com', 'name': '__cf_bm', 'value': 'synthetic-first-party'},
            {'domain': 'www.webofscience.com', 'name': 'analytics_sp_fixture', 'value': 'analytics'},
            {'domain': '.webofscience.com', 'name': 'cf_clearance', 'value': 'not-observed'},
            {'domain': '.unrelated.example', 'name': '__cf_bm', 'value': 'unrelated'},
            {'domain': 'other.webofscience.com', 'name': '__cf_bm', 'value': 'unobserved-origin'},
        ]
        data['storage_state']['cookies'].extend(cookies)
        restored = sessions.decrypt_payload(sessions.encrypt_payload(data), 'wos')
        kept = restored['storage_state']['cookies']
        self.assertEqual([(item['domain'], item['name']) for item in kept],
                         [('www.webofscience.com', 'WOSSID'), ('.webofscience.com', '__cf_bm')])
        self.assertEqual(data['storage_state']['cookies'][1:], cookies)

    def test_bootstrap_is_not_accepted_as_confirmed_artifact(self):
        sealed = sessions.encrypt_payload(payload(kind='bootstrap'))
        with self.assertRaisesRegex(sessions.SessionError, 'unconfirmed'):
            sessions.decrypt_payload(sealed, 'wos')
        self.assertEqual(sessions.decrypt_payload(sealed, 'wos', allow_bootstrap=True)['kind'], 'bootstrap')
        with self.assertRaisesRegex(sessions.SessionError, 'stale_bootstrap'):
            sessions.encrypt_payload(payload(age=2, kind='bootstrap'))

    def test_30_day_gap_restores_but_91_day_checkpoint_rejected(self):
        self.assertEqual(sessions.decrypt_payload(sessions.encrypt_payload(payload(age=30)), 'wos')['kind'], 'confirmed')
        with self.assertRaisesRegex(sessions.SessionError, 'stale_session'):
            sessions.encrypt_payload(payload(age=91))

    def test_trusted_failed_main_run_accepted_fork_branch_and_other_workflow_rejected(self):
        run = {'repository': {'id': 123}, 'head_repository': {'id': 123}, 'head_branch': 'main',
               'path': sessions.WORKFLOW_PATH, 'event': 'schedule', 'conclusion': 'failure'}
        self.assertTrue(sessions.trusted_run(run, 123))
        for field, value in [('head_repository', {'id': 456}), ('head_branch', 'feature'),
                             ('path', '.github/workflows/evil.yml'), ('event', 'pull_request_target')]:
            self.assertFalse(sessions.trusted_run({**run, field: value}, 123))

    def test_latest_validated_wins_not_artifact_packaging_date_and_old_key_rotates(self):
        store = Mock()
        store.candidates.return_value = [{'id': 1}, {'id': 2}, {'id': 3}]
        old = sessions.encrypt_payload(payload(age=3), key=OTHER_KEY)
        newest = sessions.encrypt_payload(payload(age=1), key=OTHER_KEY)
        store.download.side_effect = [sessions.SessionError('invalid_session_artifact'), old, newest] * 2
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_PREVIOUS_KEY': OTHER_KEY}):
            # Only WoS candidates here; eLibrary skips provider-mismatched packets.
            result = sessions.restore_files(directory, store)
            saved = json.loads((Path(directory) / 'wos.json').read_text())
            self.assertEqual(saved['validated_at'], sessions.decrypt_payload(newest, 'wos', key=OTHER_KEY)['validated_at'])
            self.assertEqual(result['wos']['status'], 'restored')

    def test_checkpoint_requires_current_auth_and_target_and_preserves_previous_on_error(self):
        context = Mock()
        context.storage_state.return_value = payload()['storage_state']
        context.pages = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_OUTBOX': directory}):
            rejected = sessions.checkpoint_session(context, 'wos', authenticated=False, target_verified=True,
                                                   target_id=sessions.DEFAULT_TARGET['wos'])
            self.assertEqual(rejected['status'], 'rejected')
            self.assertEqual(list(Path(directory).iterdir()), [])
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'])
            path = Path(directory) / 'wos.enc.json'
            before = path.read_bytes()
            self.assertEqual(result['status'], 'checkpointed')
            context.storage_state.side_effect = RuntimeError('secret-cookie')
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'])
            self.assertEqual(result, {'status': 'error', 'reason': 'checkpoint_write_failed'})
            self.assertEqual(path.read_bytes(), before)

    def test_restore_context_missing_corrupt_then_scoped_state(self):
        browser = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wos.json'
            with patch.dict(os.environ, {'BROWSER_SESSION_INPUT_WOS': str(path)}):
                _, info = sessions.restore_context(browser, 'wos', locale='en-US')
                self.assertEqual(info['status'], 'missing')
                path.write_text('broken-json')
                _, info = sessions.restore_context(browser, 'wos', locale='en-US')
                self.assertEqual(info['status'], 'invalid')
                data = payload()
                data['session_storage'] = {'https://www.webofscience.com': {'foo': 'secret-session-value'},
                                           'https://unrelated.example': {'evil': 'other-account'}}
                sessions.private_write(path, data)
                _, info = sessions.restore_context(browser, 'wos', locale='en-US')
                self.assertEqual(info['status'], 'restored')
                self.assertEqual(browser.new_context.call_args.kwargs['locale'], 'en-US')
                script = browser.new_context.return_value.add_init_script.call_args.kwargs['script']
                self.assertNotIn('other-account', script)

    def test_verified_page_wins_over_empty_or_different_same_origin_tab(self):
        context = Mock()
        context.storage_state.return_value = payload()['storage_state']
        target = Mock(url='https://www.webofscience.com/wos/author/record/AAN-4717-2020')
        target.evaluate.return_value = {'token': 'verified-target-token', sessions.HYDRATION_MARKER: '1'}
        other = Mock(url='https://www.webofscience.com/wos/')
        for alternative in ({}, {'token': 'unrelated-tab-token'}):
            for order in ([target, other], [other, target]):
                with self.subTest(alternative=bool(alternative), target_first=order[0] is target), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_OUTBOX': directory}):
                    other.evaluate.reset_mock()
                    other.evaluate.return_value = alternative
                    context.pages = order
                    result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                         target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
                    self.assertEqual(result['status'], 'checkpointed')
                    envelope = json.loads((Path(directory) / 'wos.enc.json').read_text())
                    restored = sessions.decrypt_payload(envelope, 'wos')
                    self.assertEqual(restored['session_storage'], {'https://www.webofscience.com': {'token': 'verified-target-token'}})
                    other.evaluate.assert_not_called()

    def test_cross_tab_conflict_does_not_replace_confirmed_checkpoint(self):
        context = Mock()
        context.storage_state.return_value = payload()['storage_state']
        target = Mock(url='https://www.webofscience.com/wos/author/record/AAN-4717-2020')
        target.evaluate.return_value = {'token': 'verified-token'}
        first = Mock(url='https://orcid.org/signin')
        first.evaluate.return_value = {'token': 'first-tab-token'}
        second = Mock(url='https://orcid.org/my-orcid')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_OUTBOX': directory}):
            context.pages = [target]
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
            self.assertEqual(result['status'], 'checkpointed')
            path = Path(directory) / 'wos.enc.json'
            before = path.read_bytes()
            for alternative in ({}, {'token': 'different-tab-token'}):
                second.evaluate.return_value = alternative
                for order in ([target, first, second], [second, first, target]):
                    context.pages = order
                    result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                         target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
                    self.assertEqual(result, {'status': 'error', 'reason': 'session_storage_conflict'})
                    self.assertEqual(path.read_bytes(), before)

    def test_unselected_conflicting_tabs_never_create_confirmed_checkpoint(self):
        context = Mock()
        context.storage_state.return_value = payload()['storage_state']
        first = Mock(url='https://www.webofscience.com/wos/')
        second = Mock(url='https://www.webofscience.com/wos/author/')
        first.evaluate.return_value = {'token': 'one'}
        second.evaluate.return_value = {'token': 'two'}
        context.pages = [first, second]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_OUTBOX': directory}):
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'])
            self.assertEqual(result, {'status': 'error', 'reason': 'session_storage_conflict'})
            self.assertFalse((Path(directory) / 'wos.enc.json').exists())
            second.evaluate.return_value = {'token': 'one'}
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'])
            self.assertEqual(result['status'], 'checkpointed')

    def test_verified_page_must_belong_to_context_and_keep_its_origin(self):
        context = Mock()
        context.storage_state.return_value = payload()['storage_state']
        target = Mock(url='https://www.webofscience.com/wos/')
        target.evaluate.return_value = {'token': 'verified-token'}
        context.pages = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BROWSER_SESSION_OUTBOX': directory}):
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
            self.assertEqual(result['reason'], 'verified_page_unavailable')
            context.pages = [target]
            def changed_origin(_):
                target.url = 'https://orcid.org/signin'
                return {'token': 'another-origin-token'}
            target.evaluate.side_effect = changed_origin
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
            self.assertEqual(result, {'status': 'error', 'reason': 'session_storage_origin_changed'})
            self.assertFalse((Path(directory) / 'wos.enc.json').exists())
            target.url = 'https://www.webofscience.com/wos/'
            target.evaluate.side_effect = None
            other = Mock(url='https://orcid.org/signin')
            other.evaluate.side_effect = changed_origin
            context.pages = [target, other]
            result = sessions.checkpoint_session(context, 'wos', authenticated=True, target_verified=True,
                                                 target_id=sessions.DEFAULT_TARGET['wos'], verified_page=target)
            self.assertEqual(result, {'status': 'error', 'reason': 'session_storage_origin_changed'})
            self.assertFalse((Path(directory) / 'wos.enc.json').exists())

    def test_artifact_zip_does_not_extract_paths(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('../wos.enc.json', '{}')
        store = sessions.ArtifactStore('owner/repo', 'token')
        store._get = Mock(return_value=Mock(content=content.getvalue()))
        with self.assertRaisesRegex(sessions.SessionError, 'invalid_session_artifact'):
            store.download({'id': 1}, 'wos')

    def test_browser_rejecting_saved_state_falls_back_to_clean_context(self):
        browser = Mock()
        clean = Mock()
        browser.new_context.side_effect = [RuntimeError('cookie-value-must-not-escape'), clean]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wos.json'
            sessions.private_write(path, payload())
            with patch.dict(os.environ, {'BROWSER_SESSION_INPUT_WOS': str(path)}):
                context, info = sessions.restore_context(browser, 'wos', locale='en-US')
        self.assertIs(context, clean)
        self.assertEqual(info['reason'], 'browser_rejected_session_checkpoint')
        self.assertNotIn('storage_state', browser.new_context.call_args.kwargs)

    @unittest.skipUnless(shutil.which('node'), 'Node is required to execute the browser hydration script')
    def test_session_storage_hydrates_once_and_does_not_overwrite_renewed_token(self):
        browser = Mock()
        with tempfile.TemporaryDirectory() as directory:
            data = payload()
            data['session_storage'] = {'https://www.webofscience.com': {'token': 'old-token'}}
            path = Path(directory) / 'wos.json'
            sessions.private_write(path, data)
            with patch.dict(os.environ, {'BROWSER_SESSION_INPUT_WOS': str(path)}):
                sessions.restore_context(browser, 'wos')
        script = browser.new_context.return_value.add_init_script.call_args.kwargs['script']
        runner = r'''const vm = require('node:vm'); const values = new Map();
const context = vm.createContext({location: {origin: 'https://www.webofscience.com'}, sessionStorage: {
getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)}});
const script = JSON.parse(process.argv[1]); vm.runInContext(script, context);
if (values.get('token') !== 'old-token') process.exit(1);
values.set('token','server-renewed-token'); vm.runInContext(script, context);
if (values.get('token') !== 'server-renewed-token') process.exit(2);'''
        result = sessions.subprocess.run(['node', '-e', runner, json.dumps(script)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)

    def test_maintenance_does_not_invoke_pipeline_or_change_published_data(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / 'stage'
            (stage / 'data/public').mkdir(parents=True)
            published = stage / 'data/public/profile.json'
            published.write_text('{"last_success_at":"keep"}')
            before = published.read_bytes()
            reports = Path(directory) / 'reports'
            def produce_report(command, **kwargs):
                provider = 'elibrary' if 'elibrary' in command[1] else 'wos'
                sessions.private_write(reports / f'{provider}.json', {
                    'status': 'success', 'target_verified': True, 'attempted_at': sessions.now(),
                    'session_checkpoint': {'status': 'checkpointed', 'validated_at': sessions.now()}})
                return Mock(returncode=0)
            with patch.object(sessions.subprocess, 'run', side_effect=produce_report) as run:
                self.assertEqual(sessions.maintain(stage, reports), 0)
            self.assertEqual(published.read_bytes(), before)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all('harvest_' in call.args[0][1] for call in run.call_args_list))
            self.assertTrue(all(call.kwargs['env']['BROWSER_SESSION_MAINTENANCE'] == '1' for call in run.call_args_list))

    def test_maintenance_zero_exit_without_fresh_checkpoint_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory) / 'reports'
            with patch.object(sessions.subprocess, 'run', return_value=Mock(returncode=0)):
                self.assertEqual(sessions.maintain(Path(directory), reports), 2)
            data = json.loads((reports / 'maintenance.json').read_text())
            self.assertEqual(data['providers']['wos']['reason'], 'maintenance_not_confirmed')

    def test_session_diagnostics_do_not_copy_plaintext_or_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / 'source', Path(directory) / 'destination'
            source.mkdir()
            sessions.private_write(source / 'wos.json', {'status': 'success', 'session_checkpoint': {
                'status': 'checkpointed', 'validated_at': sessions.now(),
                'storage_state': payload()['storage_state'], 'cookies': 'must-not-export'},
                'raw_html': '<html>private</html>', 'password': 'must-not-export'})
            sessions.private_write(source / 'private.json', payload())
            sessions.export_diagnostics(source, destination)
            saved = json.loads((destination / 'wos.json').read_text())
            self.assertEqual(saved['status'], 'success')
            self.assertNotIn('must-not-export', json.dumps(saved))
            self.assertNotIn('do-not-print-session', json.dumps(saved))
            self.assertEqual([path.name for path in destination.iterdir()], ['wos.json'])

    def test_daily_maintenance_workflow_never_promotes_or_publishes(self):
        import yaml
        workflow = yaml.safe_load((SCRIPTS.parent / '.github/workflows/refresh-data.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual({row['cron'] for row in triggers['schedule']}, {'47 5 * * 1', '47 5 * * 0,2-6'})
        self.assertEqual(workflow['concurrency']['group'], 'portfolio-data-writer')
        steps = workflow['jobs']['refresh']['steps']
        by_name = {row.get('name'): row for row in steps}
        for name in ('Collect and build candidate through home route', 'Promote and validate safe candidate',
                     'Browser checks on refreshed data', 'Commit validated data'):
            self.assertIn("env.BROWSER_SESSION_MAINTENANCE != '1'", by_name[name]['if'])
        for name in ('Save confirmed eLibrary session', 'Save confirmed WoS session'):
            self.assertEqual(by_name[name]['with']['retention-days'], 90)
            self.assertLess(steps.index(by_name[name]), steps.index(by_name['Stop tunnel and remove private state']))

    def test_session_diagnostics_keep_only_safe_login_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / 'source', Path(directory) / 'destination'
            sessions.private_write(source / 'wos.json', {'status': 'blocked', 'authentication_mode': 'fresh_orcid',
                'authentication_evidence': {'stage': 'orcid_submit', 'orcid_selected': True,
                    'submit_clicked': True, 'http_status': 403, 'password': 'private-secret',
                    'url': 'https://private.invalid/?SID=secret', 'profile_loaded': 'private-secret'}})
            sessions.export_diagnostics(source, destination)
            saved = json.loads((destination / 'wos.json').read_text())
            self.assertEqual(saved['authentication_mode'], 'fresh_orcid')
            self.assertEqual(saved['authentication_evidence'], {'stage': 'orcid_submit',
                'orcid_selected': True, 'submit_clicked': True, 'http_status': 403})
            self.assertNotIn('private', json.dumps(saved))

    def test_profile_diagnostics_export_only_numeric_summary_and_field_names(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / 'source', Path(directory) / 'destination'
            sessions.private_write(source / 'wos.json', {'status': 'blocked', 'profile_diagnostics': {
                'parsed_record_count': 0, 'summary_metric_count': 3, 'core_metric_count': 2,
                'summary': {'publications': 13, 'citations': 0, 'h_index': None,
                            'total_documents': 28, 'indexed_publications': 19,
                            'core_collection_publications': 'private-not-numeric', 'cookie': 'private-cookie'},
                'schema_fields': ['summary', 'records', 'https://private.invalid/?SID=secret'],
                'record_fields': ['title', 'year', 'private-token-123'],
                'raw_html': '<html>private</html>', 'session': 'private-session'}})
            sessions.export_diagnostics(source, destination)
            result = json.loads((destination / 'wos.json').read_text())['profile_diagnostics']
            self.assertEqual(result['parsed_record_count'], 0)
            self.assertEqual(result['summary'], {'publications': 13, 'citations': 0, 'h_index': None,
                                                'total_documents': 28, 'indexed_publications': 19})
            self.assertEqual(result['schema_fields'], ['summary', 'records'])
            self.assertEqual(result['record_fields'], ['title', 'year'])
            self.assertNotIn('private', json.dumps(result))

    def test_session_diagnostics_keep_navigation_codes_without_callback_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / 'source', Path(directory) / 'destination'
            safe = {'kind': 'request_failed', 'stage': 'orcid_response',
                    'provider': 'clarivate', 'network_error_code': 'ERR_CONNECTION_RESET'}
            sessions.private_write(source / 'wos.json', {
                'status': 'blocked', 'reason': 'login_navigation_failed',
                'authentication_evidence': {
                    'stage': 'orcid_response', 'success': True,
                    'browser_error_page_observed': True, 'navigation_failure_count': 1,
                    'navigation_failures': [{**safe, 'url': 'https://private.invalid/?SID=secret',
                        'headers': {'Authorization': 'private-secret'}, 'body': 'private-body'}],
                }})
            sessions.export_diagnostics(source, destination)
            saved = json.loads((destination / 'wos.json').read_text())
            self.assertEqual(saved['authentication_evidence'], {
                'stage': 'orcid_response', 'success': True, 'browser_error_page_observed': True,
                'navigation_failure_count': 1, 'navigation_failures': [safe]})
            self.assertNotIn('private', json.dumps(saved))


if __name__ == '__main__':
    unittest.main()
