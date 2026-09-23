"""IT scope isolation and immutable published cards."""
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
import it_resources
import publish_refresh as publisher
import refresh_pipeline as pipeline
import validate_retention as retention
import verify_published


def dump(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def card(identifier):
    return {'id': identifier, 'title_ru': 'Проект ' + identifier,
            'title_en': 'Project ' + identifier, 'description_ru': 'Описание проекта.',
            'description_en': 'Project description.', 'url': f'https://example.org/{identifier}/',
            'thumb': f'assets/it/thumbs/{identifier}.svg', 'tags': ['Данные']}


def seed(root, ids=('featured', 'old')):
    rows = [card(identifier) for identifier in ids]
    dump(root / 'data/it/resources.json', {'generated_at': '2026-09-21',
                                          'featured_ids': ['featured'], 'items': rows})
    dump(root / 'data/it/config.json', {'featured_ids': ['featured']})
    for row in rows:
        asset = root / row['thumb']
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="200" height="100"/></svg>', encoding='utf-8')
    return rows


def snapshot(root, prefix='data'):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / prefix).rglob('*') if p.is_file()}


class ItRetentionTests(unittest.TestCase):
    def test_whole_objects_including_empty_fields_and_tags_are_immutable(self):
        old = [dict(card('one'), optional='')]
        for key, value in [('tags', ['Changed']), ('optional', 'Filled'), ('new_field', 'Added')]:
            new = copy.deepcopy(old)
            new[0][key] = value
            self.assertEqual(retention.compare_records(old, new, 'it')[0]['code'], 'published_it_card_changed')
        self.assertEqual(retention.compare_records(old, [card('new')] + old, 'it'), [])

    def test_removal_duplicate_and_reordering_rejected(self):
        old = [card('one'), card('two')]
        for rows, code in [([old[0]], 'record_removed'),
                           ([old[0], old[0], old[1]], 'duplicate_identity'),
                           (old[::-1], 'published_it_order_changed')]:
            self.assertIn(code, {issue['code'] for issue in retention.compare_records(old, rows, 'it')})


class ItStageTests(unittest.TestCase):
    def test_preparation_preserves_science_audit_and_uses_separate_it_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, stage = Path(temporary) / 'root', Path(temporary) / 'stage'
            names = ['data/audit/refresh_run.json', 'data/audit/retention_report.json',
                     'data/it/resources.json', 'data/it/audit/harvest_report.json']
            for name in names:
                dump(root / name, {'previous': name})

            def output(arguments, **kwargs):
                return '\0'.join(names).encode() if arguments[1] == 'ls-files' else 'base\n'

            with patch.object(pipeline, 'ROOT', root), patch.object(pipeline.subprocess, 'check_output', side_effect=output):
                pipeline.prepare(stage, 'it')
            for name in names[:2]:
                self.assertEqual((root / name).read_bytes(), (stage / name).read_bytes())
            self.assertFalse((stage / names[-1]).exists())
            self.assertEqual(read(stage / 'data/it/audit/refresh_run.json')['scope'], 'it')

    def test_scoped_collection_never_calls_science_builders_and_retains_partial_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            dump(root / 'data/public/profile.json', {'scientometrics': {'last_success_at': 'unchanged'}})
            science = (root / 'data/public/profile.json').read_bytes()
            timestamp = '2026-09-21T10:00:00+00:00'
            calls = []

            def collect(script, cwd, timeout, args=()):
                calls.append(script)
                dump(root / 'data/it/audit/harvest_report.json', {
                    'status': 'partial', 'attempted_at': timestamp, 'complete': False,
                    'origin': 'live', 'pending': 1, 'reason': 'translation_unavailable'})
                dump(root / 'data/it/discovery_state.json', {'repositories': {
                    '123': {'status': 'pending', 'reason': 'translation_unavailable'}}})
                return 2, 'nonzero_exit'

            with patch.object(pipeline, 'run', side_effect=collect), patch.object(pipeline, 'now', return_value=timestamp), \
                 patch.object(pipeline, 'derive', side_effect=AssertionError('science builder invoked')), \
                 patch.object(pipeline, 'sanitize_public_tree', side_effect=AssertionError('science sanitization invoked')), \
                 patch.dict(os.environ, {}, clear=True):
                pipeline.collect(root, '', 'it')
            self.assertEqual(calls, ['harvest_it_resources.py'])
            self.assertEqual((root / 'data/public/profile.json').read_bytes(), science)
            self.assertEqual(read(root / 'data/it/audit/refresh_run.json')['state'], 'ready')
            self.assertEqual(pipeline.health(root, 'it'), 2)

    def test_missing_report_cannot_pass_as_fresh_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            dump(root / 'data/it/audit/harvest_report.json', {'status': 'success', 'complete': True,
                 'origin': 'live', 'attempted_at': '2026-09-20T10:00:00Z', 'last_success_at': '2026-09-20T10:00:00Z'})
            with patch.object(pipeline, 'run', return_value=(0, 'completed')):
                pipeline.collect(root, '', 'it')
            self.assertEqual(read(root / 'data/it/audit/harvest_report.json')['reason'], 'missing_current_source_report')
            self.assertEqual(pipeline.health(root, 'it'), 2)

    def test_promotion_preserves_current_cards_non_it_data_and_excludes_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            current, candidate = Path(temporary) / 'main', Path(temporary) / 'candidate'
            old = seed(current)
            seed(candidate, ('featured', 'old', 'new'))
            payload = read(candidate / 'data/it/resources.json')
            payload['items'][1]['title_ru'] = 'Unwanted automatic edit'
            dump(candidate / 'data/it/resources.json', payload)
            dump(current / 'data/public/profile.json', {'metric': 100})
            dump(candidate / 'data/public/profile.json', {'metric': 5})
            dump(candidate / 'data/it/audit/refresh_run.json', {'state': 'ready'})
            pipeline.promote(candidate, current, 'it')
            result = read(current / 'data/it/resources.json')['items']
            self.assertEqual(result, [old[0], card('new'), old[1]])
            self.assertEqual(read(current / 'data/public/profile.json'), {'metric': 100})
            self.assertFalse((current / 'data/it/audit').exists())
            first = snapshot(current)
            pipeline.promote(candidate, current, 'it')
            self.assertEqual(snapshot(current), first)

    def test_no_race_portfolio_copy_still_preserves_newly_published_it_cards(self):
        with tempfile.TemporaryDirectory() as temporary:
            current, candidate = Path(temporary) / 'main', Path(temporary) / 'candidate'
            seed(current, ('featured', 'old', 'concurrent'))
            seed(candidate, ('featured', 'old'))
            dump(candidate / 'data/public/profile.json', {'metric': 100})
            publisher.copy_candidate(candidate, current)
            self.assertEqual([x['id'] for x in read(current / 'data/it/resources.json')['items']], ['featured', 'old', 'concurrent'])
            self.assertEqual(read(current / 'data/public/profile.json'), {'metric': 100})


class ItPublicationRaceTests(unittest.TestCase):
    def test_real_git_race_preserves_fresh_science_and_second_run_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            remote, checkout, competing = base / 'remote.git', base / 'checkout', base / 'competing'

            def git(*args, cwd=None):
                return subprocess.check_output(['git', *args], cwd=cwd or checkout, text=True, encoding='utf-8', stderr=subprocess.PIPE).strip()

            subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
            subprocess.run(['git', 'clone', str(remote), str(checkout)], check=True, capture_output=True)
            git('config', 'core.autocrlf', 'false')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.org')
            git('checkout', '-b', 'main')
            seed(checkout)
            dump(checkout / 'data/public/profile.json', {'metric': 10, 'last_success_at': 'before'})
            git('add', '.')
            git('commit', '-m', 'baseline')
            git('push', '-u', 'origin', 'main')
            baseline = git('rev-parse', 'HEAD')
            subprocess.run(['git', 'clone', '-b', 'main', str(remote), str(competing)], check=True, capture_output=True)
            git('config', 'user.name', 'Other writer', cwd=competing)
            git('config', 'user.email', 'other@example.org', cwd=competing)
            dump(competing / 'data/public/profile.json', {'metric': 101, 'last_success_at': 'fresh'})
            git('add', '.', cwd=competing)
            git('commit', '-m', 'fresh science', cwd=competing)
            git('push', 'origin', 'main', cwd=competing)
            seed(checkout, ('featured', 'old', 'new'))

            def validate(path, baseline_ref, scope='portfolio'):
                self.assertEqual(scope, 'it')
                self.assertEqual(retention.validate(path, baseline_ref, scope)['issues'], [])
                self.assertEqual(it_resources.validate_it_resources(path), [])
                publisher.assert_it_scope(path, baseline_ref)

            with patch.object(publisher, 'ROOT', checkout), patch.object(publisher, 'git', side_effect=git), \
                 patch.object(publisher, 'validate', side_effect=validate), \
                 patch.object(sys, 'argv', ['publish_refresh.py', '--scope', 'it', '--baseline-ref', baseline]):
                publisher.main()
            git('fetch', 'origin', 'main')
            published = git('rev-parse', 'origin/main')
            self.assertEqual(json.loads(git('show', f'{published}:data/public/profile.json')), {'metric': 101, 'last_success_at': 'fresh'})
            rows = json.loads(git('show', f'{published}:data/it/resources.json'))['items']
            self.assertEqual([row['id'] for row in rows], ['featured', 'new', 'old'])
            changed = git('diff-tree', '--no-commit-id', '--name-only', '-r', published).splitlines()
            self.assertTrue(all(publisher.allowed_it_path(name) for name in changed))
            git('reset', '--hard', published)
            with patch.object(publisher, 'ROOT', checkout), patch.object(publisher, 'git', side_effect=git), \
                 patch.object(publisher, 'validate', side_effect=validate), \
                 patch.object(sys, 'argv', ['publish_refresh.py', '--scope', 'it', '--baseline-ref', published]):
                publisher.main()
            git('fetch', 'origin', 'main')
            self.assertEqual(git('rev-parse', 'origin/main'), published)


class ItVerificationTests(unittest.TestCase):
    def test_it_deployment_verifies_both_pages_catalog_and_every_thumbnail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed(root)
            files = verify_published.verification_files('it', root)
            self.assertIn('en/it.html', files)
            self.assertIn('assets/it/thumbs/old.svg', files)
            self.assertNotIn('data/public/profile.json', files)

    def test_workflow_is_separate_public_github_scope(self):
        import yaml
        workflow = yaml.safe_load((REPO / '.github/workflows/refresh-it-resources.yml').read_text(encoding='utf-8'))
        triggers = workflow.get('on', workflow.get(True))
        self.assertEqual(triggers['schedule'], [{'cron': '47 5 * * 1'}])
        self.assertEqual(workflow['concurrency'], {'group': 'portfolio-data-writer', 'cancel-in-progress': False})
        text = json.dumps(workflow)
        self.assertNotIn('secrets.', text)
        self.assertNotIn('home_vpn', text)
        self.assertNotIn('build_public_data', text)
        self.assertIn('collect --scope it', text)


if __name__ == '__main__':
    unittest.main()
