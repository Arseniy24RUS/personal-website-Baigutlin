"""Offline append-only collection, partial progress and screenshot timing."""
import copy
import io
import json
import shutil
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_it_resources as h
import it_resources as data


def repo(number=1, **values):
    return {'id': number, 'name': 'Project' + str(number), 'full_name': 'Owner/Project' + str(number),
            'default_branch': 'main', 'description': 'A research application for exploring public information.',
            'homepage': None, 'has_pages': False, **values}


def png():
    result = io.BytesIO()
    Image.new('RGB', (1440, 810), '#ddd').save(result, format='PNG')
    return result.getvalue()


class Translate:
    def __init__(self, success=True):
        self.success = success
    def enrich(self, row):
        if self.success:
            for name in ('title_ru', 'title_en', 'description_ru', 'description_en'):
                row.setdefault(name, 'Текст' if name.endswith('ru') else 'Text')
        return self.success
    def save(self):
        pass


class Github:
    def __init__(self, rows, readme='', fail=None):
        self.rows, self.text, self.fail = rows, readme, fail
        self.reads = 0
    def repositories(self, owner):
        yield self.rows
        if self.fail:
            raise h.ITFailure(self.fail)
    def readme(self, row):
        self.reads += 1
        return self.text, 'sha'
    def pages(self, row):
        return None


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data.atomic_json(self.root / data.CONFIG, {'owner': 'Owner', 'excluded_repo_ids': [99, 100], 'repositories': {}, 'featured_ids': []})
        data.atomic_json(self.root / data.CATALOG, {'generated_at': 'old', 'items': []})

    def collect(self, github, **kwargs):
        return h.harvest(self.root, github=github, translator=kwargs.pop('translator', Translate()), **kwargs)

    def test_missing_owner_is_rejected_without_querying_another_person(self):
        data.atomic_json(self.root / data.CONFIG, {})
        github = Mock()
        with self.assertRaisesRegex(h.ITFailure, 'invalid_it_owner'):
            self.collect(github)
        github.repositories.assert_not_called()

    def test_migrated_editorial_catalog_is_preserved_on_discovery(self):
        project = Path(__file__).resolve().parents[2]
        legacy = data.read_json(project / 'data/it/repositories.json')
        catalog = data.read_json(project / data.CATALOG)
        config = data.read_json(project / data.CONFIG)
        self.assertEqual(config['owner'], 'Danil-phy-cmp-120')
        baseline = data.read_json(project / 'data/it/retention_baseline.json')
        self.assertGreaterEqual(len(catalog['items']), len(legacy))
        current_by_id = {row['id']: row for row in catalog['items']}
        for original, seeded in zip(legacy, baseline['items']):
            current = current_by_id[seeded['id']]
            for key, value in original.items():
                self.assertEqual(current[key], value)
            self.assertEqual((project / current['thumb']).read_bytes(), (project / original['image']).read_bytes())
        shutil.copytree(project / 'data/it', self.root / 'data/it', dirs_exist_ok=True)
        shutil.copytree(project / 'assets/it', self.root / 'assets/it', dirs_exist_ok=True)
        state = data.read_json(self.root / data.STATE)
        seed_ids = {row['id'] for row in baseline['items']}
        state['repositories'] = {number: entry for number, entry in state['repositories'].items()
                                 if entry.get('resource_id') in seed_ids}
        data.atomic_json(self.root / data.STATE, state)
        rows = [repo(int(number), full_name=entry['aliases'][0], owner={'login': config['owner']})
                for number, entry in state['repositories'].items()]
        github = Github(rows)
        before = (self.root / data.CATALOG).read_bytes()
        report = self.collect(github)
        self.assertEqual(report['status'], 'success')
        self.assertEqual(report['published_new'], 0)
        self.assertEqual(github.reads, 0)
        self.assertEqual((self.root / data.CATALOG).read_bytes(), before)

    def test_new_nonwebsite_neutral_and_repeat_is_exact_noop(self):
        github = Github([repo(), repo(99), repo(100)])
        result = self.collect(github)
        self.assertEqual(result['published_new'], 1)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '/audit/' not in p.as_posix()}
        result = self.collect(github)
        self.assertEqual(result['published_new'], 0)
        self.assertEqual(github.reads, 1)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '/audit/' not in p.as_posix()})

    def test_failed_translation_keeps_pending_then_resumes(self):
        gh = Github([repo()])
        self.assertEqual(self.collect(gh, translator=Translate(False))['pending'], 1)
        self.assertEqual(data.read_json(self.root / data.CATALOG)['items'], [])
        self.assertEqual(self.collect(gh)['published_new'], 1)

    def test_partial_pagination_keeps_observed_and_previous_pending(self):
        self.collect(Github([repo(2)]), translator=Translate(False))
        result = self.collect(Github([repo(1)], fail='github_unavailable'))
        state = data.read_json(self.root / data.STATE)['repositories']
        self.assertEqual(state['1']['status'], 'published')
        self.assertEqual(state['2']['status'], 'pending')
        self.assertEqual(state['2']['reason'], 'repository_not_public_current')
        self.assertFalse(result['discovery_complete'])

    def test_rate_limit_stops_readme_requests_preserves_queue(self):
        gh = Github([repo()], fail='github_rate_limited')
        result = self.collect(gh)
        self.assertEqual(gh.reads, 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['reason'], 'github_rate_limited')

    def test_temporary_image_failure_is_not_neutral(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = h.ITFailure('public_http_error', 503)
        report = self.collect(Github([repo()], '![Project overview](image.png)'), fetcher=fetcher)
        self.assertEqual(report['pending'], 1)
        self.assertEqual(report['published_new'], 0)
        self.assertIn('readme_image_temporarily_unavailable', report['pending_reasons'])

    def test_readme_verified_image_precedes_screenshot(self):
        fetcher = Mock()
        fetcher.fetch.return_value = (png(), 'image/png', 'https://example.org/image.png')
        shot = Mock(side_effect=AssertionError('must not screenshot'))
        report = self.collect(Github([repo()], '![Project overview](image.png)'), fetcher=fetcher, screenshot=shot)
        self.assertEqual(report['published_new'], 1)
        self.assertEqual(data.read_json(self.root / data.STATE)['repositories']['1']['image_origin'], 'readme')
        shot.assert_not_called()

    def test_screenshot_cap_persists_remaining_then_next_run_completes(self):
        fetcher = Mock()
        fetcher.fetch.side_effect = lambda url, **kw: (b'<html>Site</html>', 'text/html', url)
        def screenshot(url, path, **kw):
            self.assertEqual(kw['wait_seconds'], 60)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png())
            return {'wait_seconds': 60}
        rows = [repo(i, homepage='https://example.org/' + str(i)) for i in range(1, 12)]
        result = self.collect(Github(rows), fetcher=fetcher, screenshot=screenshot)
        self.assertEqual((result['screenshots_attempted'], result['pending']), (10, 1))
        self.assertEqual(self.collect(Github(rows), fetcher=fetcher, screenshot=screenshot)['published_new'], 1)

    def test_url_alias_does_not_add_second_card(self):
        self.collect(Github([repo()]))
        old = data.read_json(self.root / data.CATALOG)
        old['items'][0]['url'] = 'https://example.org/'
        data.atomic_json(self.root / data.CATALOG, old)
        fetcher = Mock()
        fetcher.fetch.return_value = (b'<html>App</html>', 'text/html', 'https://EXAMPLE.org')
        result = self.collect(Github([repo(), repo(2, homepage='https://example.org/')]), fetcher=fetcher)
        self.assertEqual(result['published_new'], 0)
        self.assertEqual(data.read_json(self.root / data.STATE)['repositories']['2']['resource_id'], 'github-1')


class ParsingTests(unittest.TestCase):
    def test_explicit_languages_purpose_not_caption_or_language_navigation(self):
        text = '''# Family Support Catalog

[English](#english) · [Русский](#русский) · [Open the platform](https://example.org/app/)

Live site: <https://example.org/>

## English

*The application UI is Russian-only; captions are supplied in both languages.*

### Purpose

Family Support Catalog helps families navigate support measures. It includes examples called «семья» in Russian.

## Русский

### Назначение

Каталог помогает семьям находить доступные меры социальной поддержки.
'''
        document = h.read_readme_document(text, repo())
        fields = document['fields']
        self.assertTrue(fields['description_en'].startswith('Family Support Catalog helps'))
        self.assertTrue(fields['description_ru'].startswith('Каталог помогает'))
        self.assertIn('https://example.org/', document['site_links'])
        self.assertIn('https://example.org/app/', document['site_links'])
        self.assertNotIn('UI', fields['description_en'])

    def test_markdown_reference_and_html_relative_images_skip_badges(self):
        text = '# OmniTwin\n\n![Status](https://img.shields.io/badge/a)\n![Preview][main]\n<img src="../hero.png" alt="Overview">\n\n[main]: visuals/preview.png\n'
        doc = h.read_readme_document(text, repo(_readme_path='docs/README.md'))
        self.assertEqual(doc['fields']['title_ru'], 'OmniTwin')
        self.assertEqual(doc['images'], ['https://raw.githubusercontent.com/Owner/Project1/main/docs/visuals/preview.png', 'https://raw.githubusercontent.com/Owner/Project1/main/hero.png'])

    def test_technical_second_sentence_and_long_description_bounded(self):
        value = 'Research browser compares international indicators. It is not the full backend workspace. Third sentence.'
        self.assertEqual(h.brief_description(value), 'Research browser compares international indicators.')
        long = 'A research application ' + 'public information ' * 30
        result = h.brief_description(long)
        self.assertLessEqual(len(result), 320)
        self.assertTrue(result.endswith('…'))

    def test_brand_titles_and_local_development_link_not_public_site(self):
        for heading in ('GIR · Global Index Research Platform', 'TechCoop.AI public static release', 'OmniTwin'):
            fields = h.read_readme_document('# ' + heading, repo())['fields']
            self.assertEqual(fields['title_ru'], fields['title_en'])
        self.assertEqual(h.website_candidates(repo(), {'site_links':['http://127.0.0.1:8000/app/', 'http://localhost/']}), [])


class MergeTests(unittest.TestCase):
    def test_current_cards_immutable_new_after_featured_previous_order(self):
        old = {'items': [{'id': 'f', 'url': 'https://f.test', 'title': 'manual'}, {'id': 'a', 'url': 'https://a.test'}, {'id': 'b', 'url': 'https://b.test'}], 'featured_ids': ['f']}
        new = {'items': [{'id': 'a', 'url': 'https://changed.test'}, {'id': 'c', 'url': 'https://c.test'}]}
        result = data.merge_catalog(old, new)
        self.assertEqual([r['id'] for r in result['items']], ['f', 'c', 'a', 'b'])
        self.assertEqual([r for r in result['items'] if r['id'] != 'c'], old['items'])

    def test_missing_catalog_merge_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(data.merge_it_resources(root/'a', root/'b')['added'], 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_concurrent_url_alias_reconciles_state_and_preserves_current_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            current, candidate=root/'current',root/'candidate'
            card={'id':'current','title_ru':'Название','title_en':'Title','description_ru':'Описание',
                  'description_en':'Description','url':'https://example.org/','thumb':data.ASSETS+'/old.svg','tags':['manual']}
            for folder in (current,candidate):
                (folder/data.ASSETS).mkdir(parents=True)
                (folder/card['thumb']).write_bytes(h.NEUTRAL)
            data.atomic_json(current/data.CATALOG,{'items':[card]})
            before=(current/data.CATALOG).read_bytes()
            other={**card,'id':'incoming','url':'https://EXAMPLE.org','title_en':'Should not overwrite'}
            data.atomic_json(candidate/data.CATALOG,{'items':[other]})
            data.atomic_json(candidate/data.STATE,{'repositories':{'2':{'resource_id':'incoming','status':'published'}}})
            result=data.merge_it_resources(candidate,current)
            self.assertEqual(result['added'],0)
            self.assertEqual((current/data.CATALOG).read_bytes(),before)
            self.assertEqual(data.read_json(current/data.STATE)['repositories']['2']['resource_id'],'current')
            (candidate/card['thumb']).write_bytes(h.NEUTRAL+b'changed')
            with self.assertRaisesRegex(ValueError,'it_published_asset_conflict'):
                data.merge_it_resources(candidate,current)
            self.assertEqual((current/card['thumb']).read_bytes(),h.NEUTRAL)


class ScreenshotTests(unittest.TestCase):
    def test_screenshot_waits_60_after_domcontentloaded_without_real_sleep(self):
        clock = [100.0]
        page, context, browser = Mock(), Mock(), Mock()
        page.goto.side_effect = lambda *a, **kw: (clock.__setitem__(0, 112.0) or Mock(status=200))
        page.url = 'https://example.org/'
        def screenshot(**kw):
            self.assertGreaterEqual(clock[0], 172)
            self.assertFalse(kw['full_page'])
            return png()
        page.screenshot.side_effect = screenshot
        context.new_page.return_value = page
        browser.new_context.return_value = context
        resolver = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with tempfile.TemporaryDirectory() as temporary, patch.object(h, 'page_readiness', return_value={'ready_state':'complete'}):
            result = h.capture_site_screenshot(page.url, Path(temporary)/'shot.png', browser=browser,
                clock=lambda:clock[0], sleep=lambda n:clock.__setitem__(0,clock[0]+n), resolver=resolver)
        self.assertEqual(result['wait_seconds'], 60)
        self.assertEqual(browser.new_context.call_args.kwargs['viewport'], {'width':1440,'height':810})
        context.close.assert_called_once()

    def test_login_error_challenge_and_loading_pages_are_not_screenshots(self):
        for text, password, loading, reason in [('Verify you are human',0,0,'website_human_verification'),
            ('A login page with a sufficiently long explanatory paragraph.',1,0,'website_login_required'),
            ('404 Not Found',0,0,'website_error_page'), ('Application is loading its dataset for display.',0,1,'website_not_ready')]:
            with self.subTest(reason=reason):
                page = Mock()
                def locator(selector):
                    loc = Mock()
                    loc.inner_text.return_value = text
                    loc.all.return_value = []
                    loc.count.return_value = password if selector.startswith('input') else loading
                    return loc
                page.locator.side_effect = locator
                with self.assertRaises(h.ITFailure) as error:
                    h.page_readiness(page)
                self.assertEqual(error.exception.reason, reason)


if __name__ == '__main__':
    unittest.main()
