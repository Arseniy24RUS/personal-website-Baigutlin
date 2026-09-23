"""Offline regressions for safe refresh, diagnostics, and concurrent publication."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import build_public_data as builder
import publish_refresh as publisher
import refresh_pipeline as pipeline
import report_safety as safety

ATTEMPT = '2026-09-20T10:00:00+00:00'
LAST_SUCCESS = '2026-09-19T10:00:00+00:00'
FRESH = {'status': 'success', 'origin': 'live', 'complete': True,
         'attempted_at': ATTEMPT, 'last_success_at': LAST_SUCCESS,
         'record_count': 4, 'reason': None}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


class ReportSafetyTests(unittest.TestCase):
    def test_ordinary_author_urls_keep_identifier_and_exact_encoding(self):
        urls = [
            'https://www.elibrary.ru/author_items.asp?authorid=1012909',
            'https://example.org/paper?author=Sitkovskiy&title=a%20b&code=42#page-3',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(safety.clean_url(url), url)
                self.assertEqual(safety.sanitize({'url': url}), {'url': url})

    def test_nested_secrets_are_scrubbed_and_scrubbing_is_idempotent(self):
        original = {
            'url': 'https://demo-user:demo-pass@example.org/session?SID=private-session&authorid=1012909',
            'redirect': 'https://orcid.org/oauth/callback?code=private-code',
            'headers': {'Authorization': 'Bearer private-header', 'Set-Cookie': 'private-cookie'},
            'providers': [{'access_token': 'private-token', 'message': 'password=private-password'}],
            'storage_state': {'cookies': [{'value': 'private-storage'}]},
            'message': 'Configured token private-env-secret',
        }
        untouched = copy.deepcopy(original)
        with patch.dict(os.environ, {'TEST_API_KEY': 'private-env-secret'}, clear=True):
            cleaned = safety.sanitize(original)
            self.assertEqual(safety.sanitize(cleaned), cleaned)
        serialized = json.dumps(cleaned)
        for secret in ('demo-user', 'demo-pass', 'private-session', 'private-code',
                       'private-header', 'private-cookie', 'private-token',
                       'private-password', 'private-storage', 'private-env-secret'):
            self.assertNotIn(secret, serialized)
        self.assertIn('authorid=1012909', cleaned['url'])
        self.assertEqual(original, untouched)

    def test_public_tree_second_scrub_does_not_rewrite_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'data/audit/report.json'
            dump(path, {'headers': {'cookie': 'private-cookie'}, 'url': 'https://example.org/?sid=secret-session'})
            self.assertEqual(safety.sanitize_public_tree(root), 1)
            first = path.read_bytes()
            self.assertEqual(safety.sanitize_public_tree(root), 0)
            self.assertEqual(path.read_bytes(), first)


class RefreshStageTests(unittest.TestCase):
    def test_diagnostics_excludes_unattempted_sources_and_old_enrichment(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage, destination = Path(temporary) / 'stage', Path(temporary) / 'diagnostics'
            dump(stage / 'data/audit/refresh_run.json', {
                'attempted_at': ATTEMPT, 'state': 'collecting', 'selected_sources': ['wos']})
            dump(stage / 'data/wos/harvest_report.json', {**FRESH, 'attempted_at': ATTEMPT})
            dump(stage / 'data/elibrary/browser_fetch_report.json', {**FRESH, 'attempted_at': LAST_SUCCESS})
            dump(stage / 'data/audit/publication_title_translation_report.json', {'generated_at': LAST_SUCCESS})
            pipeline.diagnostics(stage, destination)
            self.assertTrue((destination / 'data/wos/harvest_report.json').exists())
            self.assertFalse((destination / 'data/elibrary/browser_fetch_report.json').exists())
            self.assertFalse((destination / 'data/audit/publication_title_translation_report.json').exists())
            self.assertIn('data/audit/publication_title_translation_report.json',
                          load(destination / 'data/audit/diagnostic_manifest.json')['omitted_previous_run_reports'])

    def test_prepare_does_not_reuse_prior_run_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, stage = Path(temporary) / 'checkout', Path(temporary) / 'stage'
            paths = ['data/public/publications.json', 'data/audit/retention_report.json',
                     'data/audit/refresh_pipeline_audit.json', 'data/audit/derived_steps.json']
            for name in paths:
                dump(root / name, {'prior': True})

            def git_output(argv, **kwargs):
                return '\0'.join(paths).encode() if argv[1] == 'ls-files' else 'base-sha\n'

            with patch.object(pipeline, 'ROOT', root), \
                 patch.object(pipeline.subprocess, 'check_output', side_effect=git_output):
                pipeline.prepare(stage)
            self.assertTrue((stage / paths[0]).exists())
            for name in paths[1:]:
                self.assertFalse((stage / name).exists())
            self.assertEqual(load(stage / 'data/audit/refresh_run.json')['state'], 'collecting')

    def test_optional_timeout_restores_collected_publications_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            publications = stage / 'data/public/publications.json'
            cache = stage / 'data/curation/publication_title_translations.json'
            dump(publications, [{'id': 'fresh', 'title': 'Свежая работа', 'title_en': 'Manual title'}])
            dump(cache, {'manual': 'preserved'})
            original = publications.read_bytes(), cache.read_bytes()
            executed = []

            def timeout_after_partial_write(script, cwd, timeout, args=()):
                executed.append(script)
                if script == 'translate_publication_titles.py':
                    publications.write_text('{broken', encoding='utf-8')
                    cache.write_text('{}', encoding='utf-8')
                    dump(stage / 'data/audit/publication_title_translation_report.json', {'partial': True})
                    return 124, 'timeout'
                return 0, 'completed'

            with patch.object(pipeline, 'DERIVED', [('translate_publication_titles.py', 1), ('sanitize_publication_references.py', 1)]), \
                 patch.object(pipeline, 'run', side_effect=timeout_after_partial_write):
                pipeline.derive(stage)
            self.assertEqual((publications.read_bytes(), cache.read_bytes()), original)
            self.assertFalse((stage / 'data/audit/publication_title_translation_report.json').exists())
            self.assertEqual(executed[-1], 'sanitize_publication_references.py')
            self.assertEqual(load(stage / 'data/audit/derived_steps.json')['steps'][0]['reason'], 'timeout')

    def test_successful_exit_with_invalid_optional_json_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            publications = stage / 'data/public/publications.json'
            dump(publications, [{'id': 'published'}])

            def malformed_output(*args):
                publications.write_text('null', encoding='utf-8')
                return 0, 'completed'

            with patch.object(pipeline, 'DERIVED', [('translate_publication_titles.py', 1)]), \
                 patch.object(pipeline, 'run', side_effect=malformed_output):
                pipeline.derive(stage)
            self.assertEqual(load(publications), [{'id': 'published'}])
            self.assertEqual(load(stage / 'data/audit/derived_steps.json')['steps'][0]['reason'], 'invalid_enrichment_output')

    def test_core_derived_failure_still_blocks_candidate(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(pipeline, 'DERIVED', [('build_public_data.py', 1)]), \
             patch.object(pipeline, 'run', return_value=(1, 'nonzero_exit')):
            stage = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, 'Mandatory derived-data'):
                pipeline.derive(stage)
            self.assertEqual(load(stage / 'data/audit/derived_steps.json')['steps'][0]['status'], 'error')

    def test_promotion_rejects_unready_candidate_without_changing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage, destination = Path(temporary) / 'stage', Path(temporary) / 'published'
            dump(stage / 'data/audit/refresh_run.json', {'state': 'collecting'})
            dump(stage / 'data/public/publications.json', [{'id': 'unverified'}])
            dump(destination / 'data/public/publications.json', [{'id': 'published'}])
            old = (destination / 'data/public/publications.json').read_bytes()
            with self.assertRaises(RuntimeError):
                pipeline.promote(stage, destination)
            self.assertEqual((destination / 'data/public/publications.json').read_bytes(), old)
            self.assertFalse((destination / 'data/audit/refresh_run.json').exists())

    def test_ready_promotion_retains_unmentioned_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage, destination = Path(temporary) / 'stage', Path(temporary) / 'published'
            dump(stage / 'data/audit/refresh_run.json', {'state': 'ready'})
            dump(stage / 'data/public/publications.json', [{'id': 'verified'}])
            dump(destination / 'data/legacy/manual.json', {'manual': 'retained'})
            asset = destination / 'assets/media/mentions/legacy.jpg'
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b'legacy published image')
            pipeline.promote(stage, destination)
            self.assertEqual(load(destination / 'data/public/publications.json'), [{'id': 'verified'}])
            self.assertEqual(load(destination / 'data/legacy/manual.json'), {'manual': 'retained'})
            self.assertEqual(asset.read_bytes(), b'legacy published image')

    def collect_with_report(self, old_report, *, source='scopus', exit_code=1, updated_report=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        stage = Path(temporary.name)
        report_path = f'data/{source}/harvest_report.json'
        dump(stage / report_path, old_report)
        dump(stage / 'data/audit/refresh_run.json', {'state': 'collecting'})

        def fake_run(script, cwd, timeout, args=()):
            if script == 'fixture_collector.py':
                if updated_report is not None:
                    dump(stage / report_path, updated_report)
                return exit_code, 'nonzero_exit' if exit_code else 'completed'
            return 2, 'nonzero_exit'  # structurally safe but degraded source audit

        with patch.dict(os.environ, {'HOME_VPN_REQUIRED': '1'}), \
             patch.object(pipeline, 'SOURCES', [(source, 'fixture_collector.py', report_path, 1)]), \
             patch.object(pipeline, 'DERIVED', []), \
             patch.object(pipeline, 'run', side_effect=fake_run), \
             patch.object(pipeline, 'now', return_value=ATTEMPT):
            pipeline.collect(stage, source)
        return load(stage / report_path)

    def assert_failed_report(self, result):
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['origin'], 'snapshot')
        self.assertFalse(result['complete'])
        self.assertEqual(result['attempted_at'], ATTEMPT)
        self.assertEqual(result['last_success_at'], LAST_SUCCESS)

    def test_failed_collector_cannot_reuse_same_second_prior_success(self):
        self.assert_failed_report(self.collect_with_report(FRESH))

    def test_failed_collector_cannot_claim_new_success_before_crashing(self):
        prior = {**FRESH, 'attempted_at': LAST_SUCCESS}
        claimed = {**FRESH, 'last_success_at': ATTEMPT}
        self.assert_failed_report(self.collect_with_report(prior, updated_report=claimed))

    def test_open_failure_invalidates_stale_nested_provider_success(self):
        prior = {**FRESH, 'attempted_at': LAST_SUCCESS,
                 'providers': {provider: {**FRESH, 'attempted_at': LAST_SUCCESS}
                               for provider in ('orcid', 'openalex_author', 'openalex_works', 'crossref')}}
        result = self.collect_with_report(prior, source='open')
        self.assert_failed_report(result)
        for provider in prior['providers']:
            with self.subTest(provider=provider):
                self.assert_failed_report(result['providers'][provider])

    def test_zero_exit_without_new_source_report_is_not_a_fresh_observation(self):
        prior = {**FRESH, 'attempted_at': LAST_SUCCESS}
        self.assert_failed_report(self.collect_with_report(prior, exit_code=0))

    def test_zero_exit_with_new_wrapper_cannot_reuse_old_provider_success(self):
        prior = {**FRESH, 'attempted_at': LAST_SUCCESS,
                 'providers': {'orcid': {**FRESH, 'attempted_at': LAST_SUCCESS}}}
        claimed = {**prior, 'attempted_at': ATTEMPT, 'last_success_at': ATTEMPT}
        result = self.collect_with_report(prior, source='open', exit_code=0, updated_report=claimed)
        self.assert_failed_report(result)
        self.assert_failed_report(result['providers']['orcid'])

    def test_current_complete_observation_is_accepted(self):
        prior = {**FRESH, 'attempted_at': LAST_SUCCESS}
        observed = {**FRESH, 'last_success_at': ATTEMPT}
        self.assertEqual(self.collect_with_report(prior, exit_code=0, updated_report=observed), observed)

    def test_health_checks_selected_sources_and_full_run_checks_all(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            dump(root / 'data/public/profile.json', {'source_health': {'wos': FRESH, 'elibrary': FRESH}})
            dump(root / 'data/audit/refresh_run.json', {'state': 'ready', 'selected_sources': ['wos', 'elibrary']})
            self.assertEqual(pipeline.health(root), 0)
            report = load(root / 'data/audit/source_health_check.json')
            self.assertEqual(set(report['checks']), {'wos', 'elibrary'})
            dump(root / 'data/audit/refresh_run.json', {'state': 'ready'})
            self.assertEqual(pipeline.health(root), 2)
            report = load(root / 'data/audit/source_health_check.json')
            self.assertEqual(set(report['checks']), {'wos', 'elibrary', 'scopus', 'open', 'media'})

    def test_failed_media_report_cannot_reuse_prior_required_sources_flag(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            dump(root / 'data/audit/refresh_run.json', {'state': 'ready', 'selected_sources': ['media']})
            dump(root / 'data/media/harvest_report.json', {**FRESH, 'status': 'error', 'origin': 'snapshot',
                                                          'complete': False, 'required_sources_ok': True})
            self.assertEqual(pipeline.health(root), 2)


class ConcurrentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.candidate = Path(self.temporary.name) / 'candidate'
        self.current = Path(self.temporary.name) / 'current-main'
        dump(self.current / 'data/public/publications.json', [])
        dump(self.current / 'data/public/profile.json', {'identifiers': {}})

    def run_derived_offline(self, argv, cwd, **kwargs):
        """Use the real public builder; no translation, browser, or network calls."""
        script = Path(argv[1]).name
        if script == 'build_public_data.py':
            with patch.object(builder, 'DATA', Path(cwd) / 'data'), \
                 patch.object(builder, 'PUBLIC', Path(cwd) / 'data/public'), \
                 patch.object(builder, 'profile', return_value={'identifiers': {}}):
                builder.main()
        elif script == 'report_safety.py':
            safety.sanitize_public_tree(Path(cwd))
        return subprocess.CompletedProcess(argv, 0)

    def recombine(self):
        with patch.object(publisher.subprocess, 'run', side_effect=self.run_derived_offline):
            publisher.recombine(self.candidate, self.current)

    def test_recombine_preserves_concurrent_manual_data_and_incoming_additions(self):
        current_publications = [
            {'id': 'old-pub', 'elibrary_item_id': '1', 'title': 'Manually corrected title', 'title_en': 'Manual translation'},
            {'id': 'manual-pub', 'elibrary_item_id': '2', 'title': 'Concurrently added publication'},
        ]
        incoming_publications = [
            {'id': 'old-pub', 'elibrary_item_id': '1', 'title': 'Stale title', 'title_en': 'Stale translation'},
            {'id': 'fresh-pub', 'elibrary_item_id': '3', 'title': 'New source publication', 'title_en': 'Reviewed new English title'},
        ]
        dump(self.current / 'data/public/publications.json', current_publications)
        dump(self.current / 'data/public/profile.json', {'identifiers': {}})
        dump(self.candidate / 'data/public/publications.json', incoming_publications)
        dump(self.candidate / 'data/processed/elibrary_publications.json', [
            {'id': 'fresh-pub', 'elibrary_item_id': '3', 'title': 'New source publication'}])

        old_media = {'id': 'old-media', 'url': 'https://news.example/old', 'title_ru': 'Правка редактора',
                     'title_en': 'Edited English', 'image': 'assets/media/mentions/old.jpg'}
        manual_media = {'id': 'manual-media', 'url': 'https://news.example/manual', 'title': 'Manual addition'}
        new_media = {'id': 'new-media', 'url': 'https://news.example/new', 'title': 'Incoming news'}
        for filename in ('published.json', 'news_mentions.json', 'published-fallback.json'):
            dump(self.current / 'data/media' / filename, {'records': [old_media, manual_media]})
            dump(self.candidate / 'data/media' / filename,
                 {'records': [{**old_media, 'title_ru': 'Устаревший текст', 'title_en': 'Stale English'}, new_media]})
        dump(self.current / 'data/admin_queue/publications.json', [{'id': 'review', 'note': 'Manual decision'}])
        dump(self.candidate / 'data/admin_queue/publications.json', [{'id': 'review', 'note': 'Stale decision'}, {'id': 'new-review'}])

        self.recombine()

        publications = {row['id']: row for row in load(self.current / 'data/public/publications.json')}
        self.assertEqual(set(publications), {'old-pub', 'manual-pub', 'fresh-pub'})
        self.assertEqual(publications['old-pub']['title'], current_publications[0]['title'])
        self.assertEqual(publications['old-pub']['title_en'], 'Manual translation')
        self.assertEqual(publications['fresh-pub']['title_en'], 'Reviewed new English title')
        for filename in ('published.json', 'news_mentions.json', 'published-fallback.json'):
            media = {row['id']: row for row in load(self.current / 'data/media' / filename)['records']}
            self.assertEqual(set(media), {'old-media', 'manual-media', 'new-media'})
            self.assertEqual(media['old-media'], old_media)
        queue = {row['id']: row for row in load(self.current / 'data/admin_queue/publications.json')}
        self.assertEqual(set(queue), {'review', 'new-review'})
        self.assertEqual(queue['review']['note'], 'Manual decision')

    def test_recombine_retains_current_asset_bytes_and_adds_new_assets(self):
        old = self.current / 'assets/media/mentions/existing.jpg'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'current manually replaced image')
        stale = self.candidate / 'assets/media/mentions/existing.jpg'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'older image from refresh start')
        (stale.parent / 'new.jpg').write_bytes(b'newly discovered image')
        self.recombine()
        self.assertEqual(old.read_bytes(), b'current manually replaced image')
        self.assertEqual((old.parent / 'new.jpg').read_bytes(), b'newly discovered image')

    def test_recombine_keeps_latest_media_attempt_and_discovery_history(self):
        dump(self.current / 'data/media/harvest_report.json', {**FRESH, 'attempted_at': LAST_SUCCESS})
        incoming_report = {**FRESH, 'last_success_at': ATTEMPT}
        dump(self.candidate / 'data/media/harvest_report.json', incoming_report)
        dump(self.current / 'data/media/discovery_state.json', {'processed': {'https://news.example/manual': {'status': 'published'}}, 'pending': {}})
        dump(self.candidate / 'data/media/discovery_state.json', {'processed': {'https://news.example/new': {'status': 'published'}}, 'pending': {}})
        self.recombine()
        report = load(self.current / 'data/media/harvest_report.json')
        for field in ('attempted_at', 'last_success_at', 'status', 'origin', 'complete', 'reason'):
            self.assertEqual(report[field], incoming_report[field])
        self.assertEqual(report['record_count'], 0)
        self.assertEqual(report['published'], 0)
        self.assertEqual(report['pending'], 0)
        state = load(self.current / 'data/media/discovery_state.json')
        self.assertEqual(set(state['processed']), {'https://news.example/manual', 'https://news.example/new'})

    def test_recombined_pending_work_prevents_false_complete_media_report(self):
        dump(self.candidate / 'data/media/harvest_report.json', {**FRESH, 'last_success_at': ATTEMPT})
        dump(self.current / 'data/media/discovery_state.json', {'processed': {},
             'pending': {'https://news.example/backlog': {'url': 'https://news.example/backlog'}}})
        self.recombine()
        report = load(self.current / 'data/media/harvest_report.json')
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['complete'])
        self.assertEqual(report['pending'], 1)
        self.assertEqual(report['last_success_at'], ATTEMPT)

    def test_recombine_cannot_regress_newer_source_snapshot(self):
        latest = {**FRESH, 'attempted_at': ATTEMPT, 'last_success_at': ATTEMPT, 'record_count': 1}
        older = {**FRESH, 'attempted_at': LAST_SUCCESS, 'last_success_at': LAST_SUCCESS}
        current_records = [{'id': 'current', 'elibrary_item_id': '4', 'title': 'More recent source publication'}]
        dump(self.current / 'data/elibrary/browser_fetch_report.json', latest)
        dump(self.current / 'data/elibrary/profile_metrics.json', {'summary': {'citations_rinc': 20}})
        dump(self.current / 'data/processed/elibrary_publications.json', current_records)
        dump(self.candidate / 'data/elibrary/browser_fetch_report.json', older)
        dump(self.candidate / 'data/elibrary/profile_metrics.json', {'summary': {'citations_rinc': 10}})
        dump(self.candidate / 'data/processed/elibrary_publications.json', [])
        self.recombine()
        self.assertEqual(load(self.current / 'data/elibrary/browser_fetch_report.json'), latest)
        self.assertEqual(load(self.current / 'data/processed/elibrary_publications.json'), current_records)
        profile = load(self.current / 'data/public/profile.json')
        self.assertEqual(profile['scientometrics']['sources']['rinc']['citations'], 20)

    def test_intermediate_success_updates_data_without_hiding_latest_failure(self):
        noon, one, two = ['2026-09-20T' + hour + ':00:00+00:00' for hour in ('12', '13', '14')]
        failure = {**FRESH, 'status': 'blocked', 'origin': 'snapshot', 'complete': False,
                   'attempted_at': two, 'last_success_at': noon, 'reason': 'captcha', 'record_count': 1}
        success = {**FRESH, 'attempted_at': one, 'last_success_at': one, 'record_count': 2}
        dump(self.current / 'data/elibrary/browser_fetch_report.json', failure)
        dump(self.current / 'data/elibrary/profile_metrics.json', {'summary': {'citations_rinc': 12}})
        old_paper = {'id': 'a', 'elibrary_item_id': '1', 'title': 'Reviewed title', 'rinc_citations': 12}
        dump(self.current / 'data/processed/elibrary_publications.json', [old_paper])
        dump(self.current / 'data/public/publications.json', [old_paper])
        dump(self.current / 'data/public/profile.json', {'identifiers': {}, 'scientometrics': {'sources': {
            'rinc': {'citations': 12, 'last_success_at': noon, 'metric_observed_at': {'citations': noon}}
        }}})
        dump(self.candidate / 'data/elibrary/browser_fetch_report.json', success)
        dump(self.candidate / 'data/elibrary/profile_metrics.json', {'summary': {'citations_rinc': 0}})
        chosen = [{**old_paper, 'rinc_citations': 0}, {'id': 'b', 'title': 'New paper'}]
        dump(self.candidate / 'data/processed/elibrary_publications.json', chosen)
        self.recombine()
        report = load(self.current / 'data/elibrary/browser_fetch_report.json')
        self.assertEqual(report, {**failure, 'last_success_at': one, 'record_count': 2})
        self.assertEqual(load(self.current / 'data/processed/elibrary_publications.json'), chosen)
        public = load(self.current / 'data/public/profile.json')
        self.assertEqual(public['source_health']['elibrary']['reason'], 'captcha')
        self.assertEqual(public['scientometrics']['sources']['rinc']['citations'], 0)
        self.assertEqual(public['scientometrics']['sources']['rinc']['metric_observed_at']['citations'], one)
        self.assertEqual(public['scientometrics']['sources']['rinc']['status'], 'blocked')
        paper = next(row for row in load(self.current / 'data/public/publications.json') if row['id'] == 'a')
        self.assertEqual(paper['rinc_citations'], 0)
        self.assertEqual(paper['title'], 'Reviewed title')

    def test_candidate_newer_failure_preserves_current_good_scopus_and_wos(self):
        noon, one, two = ['2026-09-20T' + hour + ':00:00+00:00' for hour in ('12', '13', '14')]
        success = {**FRESH, 'attempted_at': one, 'last_success_at': one, 'record_count': 2}
        failed = {**FRESH, 'attempted_at': two, 'last_success_at': noon, 'record_count': 1,
                  'status': 'error', 'origin': 'snapshot', 'complete': False, 'reason': 'network_error'}
        for provider, filename, datafile in [
            ('scopus', 'scopus_author_57211062810_access_report.json', 'scopus_author_57211062810_works.json'),
            ('wos', 'harvest_report.json', 'profile_metrics.json'),
        ]:
            rows = [{'id': 'current-one'}, {'id': 'current-two'}]
            payload = {'records': rows} if provider == 'wos' else rows
            stale = {'records': [{'id': 'stale'}]} if provider == 'wos' else [{'id': 'stale'}]
            dump(self.current / 'data' / provider / filename, success)
            dump(self.current / 'data' / provider / datafile, payload)
            dump(self.candidate / 'data' / provider / filename, failed)
            dump(self.candidate / 'data' / provider / datafile, stale)
        self.recombine()
        for provider, filename, datafile in [
            ('scopus', 'scopus_author_57211062810_access_report.json', 'scopus_author_57211062810_works.json'),
            ('wos', 'harvest_report.json', 'profile_metrics.json'),
        ]:
            with self.subTest(provider=provider):
                report = load(self.current / 'data' / provider / filename)
                self.assertEqual(report, {**failed, 'last_success_at': one, 'record_count': 2})
                payload = load(self.current / 'data' / provider / datafile)
                rows = payload['records'] if provider == 'wos' else payload
                self.assertEqual([row['id'] for row in rows], ['current-one', 'current-two'])

    def test_nested_open_sources_choose_independent_snapshots_and_latest_failures(self):
        noon, one, two = ['2026-09-20T' + hour + ':00:00+00:00' for hour in ('12', '13', '14')]
        success = {**FRESH, 'attempted_at': one, 'last_success_at': one, 'record_count': 1}
        failure = {**FRESH, 'attempted_at': two, 'last_success_at': noon, 'record_count': 1,
                   'status': 'error', 'origin': 'snapshot', 'complete': False, 'reason': 'http_429'}
        dump(self.current / 'data/open/harvest_report.json', {**failure, 'providers': {
            'orcid': failure, 'openalex_works': success,
        }})
        dump(self.candidate / 'data/open/harvest_report.json', {**success, 'providers': {
            'orcid': success, 'openalex_works': failure,
        }})
        for root, title in ((self.current, 'Older ORCID title'), (self.candidate, 'Newer ORCID title')):
            dump(root / 'data/open/orcid_works.json', {'path': '/0000-0002-8725-6580/works', 'group': [
                {'work-summary': [{'title': {'title': {'value': title}}, 'put-code': 42,
                                   'external-ids': {'external-id': [{'external-id-type': 'doi', 'external-id-value': '10.example/orcid'}]}}]}
            ]})
        dump(self.current / 'data/open/openalex_works.json', {'results': [{'id': 'W1', 'display_name': 'Newer OpenAlex title'}]})
        dump(self.candidate / 'data/open/openalex_works.json', {'results': [{'id': 'W2', 'display_name': 'Stale OpenAlex title'}]})
        self.recombine()
        report = load(self.current / 'data/open/harvest_report.json')
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['attempted_at'], two)
        self.assertEqual(report['last_success_at'], one)
        self.assertEqual(report['record_count'], 2)
        for provider in ('orcid', 'openalex_works'):
            self.assertEqual(report['providers'][provider], {**failure, 'last_success_at': one})
        titles = {row['title'] for row in load(self.current / 'data/open/open_publications.json')['records']}
        self.assertEqual(titles, {'Newer ORCID title', 'Newer OpenAlex title'})

    def test_same_second_reports_keep_failure_with_newest_success_epoch(self):
        failed = {**FRESH, 'status': 'error', 'origin': 'snapshot', 'complete': False, 'reason': 'timeout'}
        succeeded = {**FRESH, 'last_success_at': ATTEMPT}
        for previous, incoming in ((failed, succeeded), (succeeded, failed)):
            merged = publisher.merge_source_report(previous, incoming, record_count=3)
            self.assertEqual(merged, {**failed, 'last_success_at': ATTEMPT, 'record_count': 3})


if __name__ == '__main__':
    unittest.main()
