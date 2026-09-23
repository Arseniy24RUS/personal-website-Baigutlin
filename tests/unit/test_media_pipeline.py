"""Regression cases for independent discovery, retention and transient failures."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_media_mentions as media
import media_postprocess as images
import check_seo as seo
from media_translation import MediaTranslator, pending_translation_fields

CSU = {'name': 'CSU', 'url': 'https://www.csu.ru/news/',
         'link_selector': '.news .title a', 'item_class': 'news', 'date_selector': '.date'}
PARTNER = {'name': 'PARTNER', 'url': 'https://physics.example.org/news/',
       'link_selector': '.news-list-view .post-teaser-text .post-title h3 a',
       'item_class': 'post-teaser-text', 'date_selector': '.post-head .date'}
CFG = {'listing_sources': [CSU, PARTNER], 'seed_urls': [], 'auto_publish_threshold': .75,
       'context_terms': ['физик', 'челгу', 'материаловед'], 'initial_lookback_days': 90}
# Synthetic article/listing fixtures exercise discovery independently of the
# reviewed seeds; they do not claim to reproduce a live CSU response.
CSU_LIST = """<div class="news"><p class="date">17 Сентября 2026</p><p class="title">
<a href="/news/physics-research-2026">Защита диссертации Байгутлина Данила Расуловича</a></p></div>
<div class="news"><p class="date">01 Мая 2026</p><p class="title"><a href="/news/old">Old</a></p></div>"""
PARTNER_LIST = """<div class="news-list-view"><div class="post-teaser-text"><div class="post-head"><span class="date">15 сентября 2026 года</span></div>
<div class="post-title"><h3><a href="https://physics.example.org/news/2026/9/baigutlin-research">Д.Р. Байгутлин успешно защитил диссертацию</a></h3></div></div></div>"""
CSU_ARTICLE = """<nav>Байгутлин Данил</nav><div class="post">
<div class="post-head"><div class="post-head__date">17 сентября 2026 года</div><h1 class="post-head__title">Первая защита в диссертационном совете ЧелГУ</h1></div>
<div class="text-content">
<p>15 сентября состоялась защита диссертации Байгутлина Данила Расуловича на тему физических свойств сплавов Гейслера.</p>
<img src="/images/defence.jpg"></div></div>"""
PARTNER_ARTICLE = """<div class="news-single"><time datetime="2026-09-15">15 сентября 2026 года</time>
<header class="main-headline"><h1>Д.Р. Байгутлин успешно защитил диссертацию</h1></header><div class="body-text">
<p>На заседании ЧелГУ состоялась успешная защита диссертации научного сотрудника Данила Расуловича Байгутлина о физических свойствах материалов.</p></div></div>"""


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in [('FETCH_DEADLINE', None), ('HOST_FAILURES', {})]:
            p = patch.object(media, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in [('OUT', self.root / 'media'), ('QUEUE', self.root / 'queue')]:
            p = patch.object(media, target, value)
            p.start()
            self.addCleanup(p.stop)

    def fake_fetch(self, url, *args, **kwargs):
        pages = {CSU['url']: CSU_LIST, PARTNER['url']: PARTNER_LIST}
        raw = pages.get(url, CSU_ARTICLE if 'csu.ru' in url else PARTNER_ARTICLE)
        return raw, {'url': url, 'status': 'ok', 'final_url': url}

    def read(self, name):
        return json.loads((media.OUT / name).read_text(encoding='utf-8'))

    def test_discovery_of_both_articles_without_seeds(self):
        with patch.object(media, 'fetch_text', side_effect=self.fake_fetch):
            report = media.run(CFG, ['listings'], mirror=False, translate=False)
        records = self.read('published.json')['records']
        self.assertEqual(len(records), 2)
        self.assertEqual({r['published_at'] for r in records}, {'2026-09-17', '2026-09-15'})
        self.assertTrue(all(r['confidence'] >= .75 and not r.get('force_publish') for r in records))
        self.assertTrue(report['required_sources_ok'])
        self.assertEqual(self.read('published.json'), self.read('published-fallback.json'))

    def test_cutoff_and_changed_template(self):
        self.assertEqual(len(media.parse_listing(CSU_LIST, CSU, '2026-06-01')), 1)
        with self.assertRaises(ValueError):
            media.parse_listing('<html>No articles</html>', CSU, '2026-06-01')

    def test_declensions_initials_english_and_context(self):
        names = ['Байгутлину Данилу', 'Байгутлина Данила Расуловича',
                 'Данила Расуловича Байгутлина', 'Д. Р. Байгутлин',
                 'Байгутлин Д.Р.', 'Danil R. Baigutlin', 'Danil Baygutlin']
        for name in names:
            self.assertGreaterEqual(media.score_record(name + ' физика', '', 'https://site.org', CFG), .75, name)
        self.assertLess(media.score_record('Байгутлин Сергей физик', '', '', CFG), .75)
        self.assertLess(media.score_record('Байгутлин Данил ранее прибыл', '', '', CFG), .75)
        meta = media.article_meta('<nav>Данил Байгутлин физик</nav><article><h1>Другая новость</h1><p>' + 'Чужой текст. ' * 20 + '</p></article>', 'https://site.org')
        self.assertEqual(media.score_record(meta['text'], meta['title'], '', CFG), 0)

    def test_target_configuration_matches_baigutlin_and_rejects_namesakes(self):
        config_path = Path(__file__).resolve().parents[2] / 'config/media_sources.yml'
        cfg = yaml.safe_load(config_path.read_text(encoding='utf-8'))['media_monitoring']
        names = ['Данил Расулович Байгутлин', 'Байгутлиным Данилом Расуловичем',
                 'Байгутлин Д. Р.', 'Д.Р. Байгутлин', 'Danil Rasulovich Baigutlin',
                 'D. R. Baigutlin', 'Baigutlin D.R.', 'Daniil Baygutlin']
        for name in names:
            with self.subTest(name=name):
                self.assertGreaterEqual(media.score_record(name + ' physics ЧелГУ', '', '', cfg), .75)
        for unrelated in ['Сергей Байгутлин, физик ЧелГУ', 'Арсений Ситковский, физик ЧелГУ',
                          'Daria Baigutlin, materials science', 'Danila Baigutlin, physics',
                          'Данил Байгутлинский, физик ЧелГУ', 'Baigutlinov Danil, physics']:
            self.assertLess(media.score_record(unrelated, '', '', cfg), .75, unrelated)
        self.assertFalse(cfg['seed_urls'], 'Reviewed historical records belong to the target seed file.')

    def test_target_listing_paths_filter_navigation_and_deduplicate(self):
        config_path = Path(__file__).resolve().parents[2] / 'config/media_sources.yml'
        source = yaml.safe_load(config_path.read_text(encoding='utf-8'))['media_monitoring']['listing_sources'][0]
        raw = '''<nav><a class="post-card__link" href="/press/news/">News</a><a class="post-card__link" href="/press/news/?PAGEN_1=2">More</a></nav>
          <article class="post-card"><div class="post-card-period"><div>23</div><div>сентября</div><div>2026</div></div>
          <a class="post-card__link" href="/press/news/materials-research/">Materials research</a>
          <a class="post-card__link" href="/press/news/materials-research/">Read more</a></article>
          <a class="post-card__link" href="https://other.org/press/news/another/">External</a>'''
        records = media.parse_listing(raw, source, '2026-06-01')
        self.assertEqual([r['url'] for r in records], ['https://www.csu.ru/press/news/materials-research/'])
        self.assertEqual(records[0]['published_at'], '2026-09-23')

    def test_original_target_array_and_seeds_survive_source_failure(self):
        # seed.json is the original target corpus, unchanged by this migration.
        seed_path = Path(__file__).resolve().parents[2] / 'data/media/seed.json'
        original = json.loads(seed_path.read_text(encoding='utf-8'))
        media.write_json(media.OUT / 'published.json', original)
        media.write_json(media.OUT / 'seed.json', original)
        with patch.object(media, 'fetch_text', return_value=(None, {'status': 'http_error', 'http_status': 403})):
            report = media.run(CFG, ['listings'], mirror=False, translate=False)
        records = self.read('published.json')['records']
        self.assertEqual(len(records), len(original))
        self.assertFalse(report['required_sources_ok'])
        self.assertEqual(report['new_records'], 0)
        for old in original:
            new = next(record for record in records if record['id'] == old['id'])
            self.assertEqual({key: new[key] for key in old}, old)
            self.assertEqual(new['published_at'], old['date'])
            self.assertEqual(new['title'], old['title_ru'])
            self.assertEqual(new['source_name'], old['source_name_ru'])
        self.assertEqual(self.read('published.json'), self.read('published-fallback.json'))
        self.assertEqual(self.read('published.json'), self.read('news_mentions.json'))
        with patch.object(media, 'fetch_text', side_effect=AssertionError('Seeds-only migration stays offline')):
            media.run(CFG, seeds_only=True, mirror=False, translate=False)
        self.assertEqual(self.read('published.json')['records'], records)

    def test_seed_bootstrap_keeps_stable_target_ids_and_bilingual_text(self):
        seed_path = Path(__file__).resolve().parents[2] / 'data/media/seed.json'
        original = json.loads(seed_path.read_text(encoding='utf-8'))
        media.write_json(media.OUT / 'seed.json', original)
        records = media.seed_records(CFG)
        self.assertEqual({r['id'] for r in records}, {r['id'] for r in original})
        for old in original:
            new = next(record for record in records if record['id'] == old['id'])
            for key in ('url', 'title_ru', 'title_en', 'description_ru', 'description_en', 'image', 'date'):
                self.assertEqual(new[key], old[key])

    def test_candidate_profile_is_not_media_and_corrupt_snapshot_fails_closed(self):
        self.assertTrue(images.is_blocked_record({'url': 'https://www.csu.ru/science/diss-sovet/dis-sovet-24243101/phd-candidates/baygutlin-d-r/index.php/'}))
        path = self.root / 'broken.json'
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            images.read_records(path)

    def test_legacy_postprocess_preserves_editorial_copy_and_context_illustrations(self):
        seed_path = Path(__file__).resolve().parents[2] / 'data/media/seed.json'
        original = json.loads(seed_path.read_text(encoding='utf-8'))
        with patch.object(images, 'record_meta', return_value={'title': 'Плохая замена',
                'description': 'Плохая замена', 'published_at': '2026-09-23',
                'image': 'https://example.org/replacement.jpg'}), \
                patch.object(images, 'fetch_bytes', side_effect=AssertionError('Keep existing local assets')):
            records = images.postprocess_records(original)
        for old in original:
            new = next(record for record in records if record['id'] == old['id'])
            self.assertEqual({key: new[key] for key in old}, old)
            self.assertEqual(new['published_at'], old['date'])
            self.assertEqual(new['title'], old['title_ru'])

    def test_article_body_does_not_include_sidebar_namesakes(self):
        raw = '''<main><aside>Данил Байгутлин, ЧелГУ</aside>
          <article><h1>Другая новость</h1><p>''' + 'Новости другого исследователя. ' * 10 + '''</p></article></main>'''
        metadata = media.article_meta(raw, 'https://other.org/news/story/')
        self.assertEqual(media.score_record(metadata['text'], metadata['title'], '', CFG), 0)

    def test_csu_post_body_date_and_description_exclude_recommendation_cards(self):
        # Minimized structure verified on the CSU dissertation story in seed.json.
        related = '<article class="post-card"><h3>Unrelated current story</h3><p>' + 'Unrelated news. ' * 10 + '</p></article>'
        raw = related + CSU_ARTICLE
        metadata = media.article_meta(raw, 'https://www.csu.ru/press/news/story/')
        self.assertEqual(metadata['published_at'], '2026-09-17')
        self.assertIn('Байгутлина Данила Расуловича', metadata['description'])
        self.assertNotIn('Unrelated', metadata['text'])
        self.assertGreaterEqual(media.score_record(metadata['text'], metadata['title'], '', CFG), .75)
        self.assertIsNone(media.article_meta(related, 'https://www.csu.ru/press/news/story/'))

    def test_url_identity_and_preserved_manual_fields(self):
        old = {'id': 'stable', 'url': 'http://www.example.org/news/1/', 'title_ru': 'Ручной заголовок',
               'title_en': 'Reviewed title', 'image': 'assets/good.jpg', 'manual': {'x': 1}}
        new = {'id': 'different', 'url': 'https://example.org/news/1?utm_source=x#fragment',
               'title_en': 'Bad replacement', 'image': None, 'description_en': 'New metadata'}
        result = media.merge_records([old], [new])
        self.assertEqual(len(result), 1)
        for field in old:
            self.assertEqual(result[0][field], old[field])
        self.assertEqual(result[0]['description_en'], 'New metadata')

    def test_failure_retains_published_and_uncertain_queue(self):
        old = {'id': 'old', 'url': 'https://site.org/old', 'title_en': 'Manual title', 'image': 'assets/saved.jpg'}
        uncertain = {'id': 'review', 'url': 'https://site.org/maybe', 'title': 'Maybe'}
        media.write_json(media.OUT / 'published.json', {'records': [old]})
        media.write_json(media.QUEUE / 'media_mentions.json', [uncertain])
        with patch.object(media, 'fetch_text', return_value=(None, {'status': 'http_error', 'http_status': 403})):
            report = media.run(CFG, ['listings'], mirror=False, translate=False)
        self.assertEqual(self.read('published.json')['records'], [old])
        self.assertFalse(report['required_sources_ok'])
        self.assertFalse(report['complete'])
        self.assertEqual(json.loads((media.QUEUE / 'media_mentions.json').read_text()), [uncertain])

    def test_failed_article_is_pending_and_retry_is_idempotent(self):
        def failure(url, *args, **kwargs):
            if url in (CSU['url'], PARTNER['url']):
                return self.fake_fetch(url)
            return None, {'status': 'http_error', 'http_status': 429}
        with patch.object(media, 'fetch_text', side_effect=failure):
            report = media.run(CFG, ['listings'], max_articles=1, mirror=False, translate=False)
        self.assertEqual(report['pending'], 2)
        self.assertFalse(self.read('discovery_state.json')['processed'])
        with patch.object(media, 'fetch_text', side_effect=self.fake_fetch):
            media.run(CFG, ['listings'], mirror=False, translate=False)
            records = self.read('published.json')['records']
            media.run(CFG, ['listings'], mirror=False, translate=False)
        self.assertEqual(self.read('published.json')['records'], records)
        self.assertFalse(self.read('discovery_state.json')['pending'])

    def test_nested_sitemap_and_persistent_remainder(self):
        pages = {'https://site.org/map.xml': '<sitemapindex><sitemap><loc>https://site.org/child.xml</loc></sitemap></sitemapindex>',
                 'https://site.org/child.xml': '<urlset><url><loc>https://site.org/news/one</loc></url><url><loc>https://site.org/news/two</loc></url></urlset>'}
        state = {}
        with patch.object(media, 'fetch_text', side_effect=lambda u: (pages[u], {'status': 'ok'})):
            urls, _ = media.fetch_sitemap_urls('https://site.org/map.xml', 1, '/news/', max_sitemaps=1, state=state)
            self.assertEqual(urls, [])
            self.assertIn('https://site.org/child.xml', state['sitemap_pending']['https://site.org/map.xml'])
            urls, _ = media.fetch_sitemap_urls('https://site.org/map.xml', 1, '/news/', state=state)
        self.assertEqual(len(urls), 2)  # All discovered items reach durable backlog.

    def test_required_new_and_retry_news_precede_9500_generic_pending_urls(self):
        generic = {f'https://archive.org/news/{i}': {'url': f'https://archive.org/news/{i}',
                    'source': 'sitemap_scan', 'title': 'Archive entry'} for i in range(9500)}
        retry_url = 'https://www.csu.ru/news/physics-research-2026'
        pending = {**generic, media.canonical(retry_url): {'url': retry_url,
                    'source': 'institutional_news_listing', 'last_attempt_at': '2026-09-19'}}
        media.write_json(media.OUT / 'discovery_state.json', {'processed': {}, 'pending': pending,
                         'initial_cutoff': '2026-06-01'})
        with patch.object(media, 'fetch_text', side_effect=self.fake_fetch) as fetch:
            report = media.run(CFG, ['listings'], max_articles=2, mirror=False, translate=False)
        self.assertEqual(report['new_records'], 2)
        self.assertEqual(report['pending'], 9500)
        self.assertEqual(self.read('discovery_state.json')['pending'], generic)
        self.assertFalse(any('archive.org' in call.args[0] for call in fetch.call_args_list))
        self.assertEqual(len(self.read('discovery_state.json')['processed']), 2)

    def test_sitemap_cutoff_keeps_current_unknown_and_old_index_children(self):
        pages = {
            'https://site.org/map.xml': '<sitemapindex><sitemap><loc>https://site.org/child.xml</loc><lastmod>2020-01-01</lastmod></sitemap></sitemapindex>',
            'https://site.org/child.xml': '''<urlset>
              <url><loc>https://site.org/old</loc><lastmod>2020-01-01</lastmod></url>
              <url><loc>https://site.org/current</loc><lastmod>2026-09-17T12:00:00Z</lastmod></url>
              <url><loc>https://site.org/unknown</loc></url>
              <url><loc>https://site.org/invalid-date</loc><lastmod>2020-99-99</lastmod></url>
            </urlset>''',
        }
        with patch.object(media, 'fetch_text', side_effect=lambda url: (pages[url], {'url': url, 'status': 'ok'})):
            records, reports = media.fetch_sitemap_urls('https://site.org/map.xml', 60,
                state={'initial_cutoff': '2026-06-01'}, include_metadata=True)
        self.assertEqual({r['url'] for r in records}, {'https://site.org/current', 'https://site.org/unknown', 'https://site.org/invalid-date'})
        self.assertEqual(records[0]['sitemap_lastmod'], '2026-09-17')
        self.assertEqual(sum(r.get('excluded_before_cutoff', 0) for r in reports), 1)

    def test_old_article_is_checkpointed_without_deleting_existing_publication(self):
        old = {'id': 'manual-old', 'url': 'https://old.org/news', 'title': 'Existing reviewed archive',
               'published_at': '2000-01-01', 'title_en': 'Existing reviewed archive'}
        media.write_json(media.OUT / 'published.json', {'records': [old]})
        def fetch(url, *args, **kwargs):
            raw, report = self.fake_fetch(url)
            if 'csu.ru/news/' in url and url != CSU['url']:
                raw = raw.replace('2026-09-17', '2020-01-17').replace('17 сентября 2026 года', '17 января 2020 года')
            return raw, report
        with patch.object(media, 'fetch_text', side_effect=fetch):
            media.run(CFG, ['listings'], mirror=False, translate=False)
        records = self.read('published.json')['records']
        self.assertIn(old, records)
        self.assertEqual(len(records), 2)  # Saved archive and current PARTNER news.
        self.assertIn('outside_lookback', {r['status'] for r in self.read('discovery_state.json')['processed'].values()})
        self.assertFalse(self.read('discovery_state.json')['pending'])

    def test_identity_and_date_hints_precede_unknown_generic_backlog(self):
        anonymous = {'url': 'https://site.org/news/123', 'source': 'sitemap_scan'}
        recent = {**anonymous, 'sitemap_lastmod': '2026-09-19'}
        identity = {**anonymous, 'title': 'Данил Байгутлин: физическое исследование', 'last_attempt_at': '2026-09-19'}
        self.assertGreater(media.candidate_priority(identity, CFG), media.candidate_priority(recent, CFG))
        self.assertGreater(media.candidate_priority(recent, CFG), media.candidate_priority(anonymous, CFG))

    def test_five_rss_timeouts_cannot_starve_other_discovery_channels(self):
        clock = [100.0]
        rss_calls, calls = [], []
        rss_duration = [None]
        cfg = {**CFG, 'queries': ['one', 'two', 'three'],
               'sitemap_sources': [{'name': 'Map', 'sitemap_url': 'https://maps.org/map.xml'}],
               'site_scan_sources': [{'name': 'Site', 'start_urls': ['https://site.org']}],
               'telegram_channels': [{'channel': 'example'}]}

        def fetch(url, *args, **kwargs):
            if media.FETCH_DEADLINE - clock[0] <= 2:
                return None, {'url': url, 'status': 'budget_exhausted'}
            calls.append(url)
            if 'news.google.com' in url:
                if rss_duration[0] is None:
                    rss_duration[0] = (media.FETCH_DEADLINE - clock[0]) / 5
                clock[0] += rss_duration[0]
                rss_calls.append(url)
                return None, {'url': url, 'status': 'error', 'reason': 'ReadTimeout', 'attempts': 1}
            if url in (CSU['url'], PARTNER['url']):
                return self.fake_fetch(url)
            return ('<urlset/>' if 'maps.org' in url else '<html/>'), {'url': url, 'status': 'ok'}

        with patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(media, 'fetch_text', side_effect=fetch):
            # Required listings must run first even with a different CLI order.
            report = media.run(cfg, ['rss', 'listings', 'sitemaps', 'sites', 'telegram'], max_articles=0, mirror=False, translate=False)
        self.assertEqual(calls[:2], [CSU['url'], PARTNER['url']])
        self.assertEqual(len(rss_calls), 5)
        for url in ('https://maps.org/map.xml', 'https://site.org', 'https://t.me/s/example'):
            self.assertIn(url, calls)
        self.assertTrue(report['required_sources_ok'])
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['complete'])
        self.assertEqual(report['pending'], 2)  # Deferred article processing remains durable.
        self.assertEqual(self.read('discovery_state.json')['rss_cursor']['next_index'], 5)
        self.assertEqual({r['collector'] for r in report['discovery_budgets']}, {'rss', 'listings', 'sitemaps', 'sites', 'telegram'})

    def test_slow_sitemap_cannot_starve_other_roots_and_stays_pending(self):
        clock = [100.0]
        calls = []
        state, reports = {}, []
        sources = [{'name': host, 'sitemap_url': 'https://' + host + '/map.xml'} for host in ('slow.org', 'reachable.org')]

        def fetch(url):
            calls.append(url)
            if 'slow.org' in url:
                clock[0] = media.FETCH_DEADLINE
                return None, {'url': url, 'status': 'budget_exhausted'}
            self.assertGreater(media.FETCH_DEADLINE, clock[0])
            return '<urlset/>', {'url': url, 'status': 'ok'}

        with patch.object(media, 'FETCH_DEADLINE', 130), patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(media, 'fetch_text', side_effect=fetch):
            media.discover_sitemaps({'sitemap_sources': sources}, reports, state)
        self.assertEqual(calls, [s['sitemap_url'] for s in sources])
        self.assertEqual(state['sitemap_pending']['https://slow.org/map.xml'], ['https://slow.org/map.xml'])

    def test_host_circuit_breaker_is_local_and_retries_next_run(self):
        ok = Mock(status_code=200, url='https://reachable.org', encoding='utf-8')
        ok.iter_content.return_value = [b'<html>ok</html>']
        with patch.object(media.requests, 'get', side_effect=[media.requests.ReadTimeout()] * 3 + [ok]) as get, patch.object(media.time, 'sleep'):
            media.fetch_text('https://news.google.com/rss/one')
            raw, report = media.fetch_text('https://news.google.com/rss/two')
            self.assertIsNone(raw)
            self.assertEqual(report['status'], 'circuit_open')
            self.assertEqual(get.call_count, 3)
            self.assertEqual(media.fetch_text('https://reachable.org')[1]['status'], 'ok')
        ok.iter_content.return_value = [b'<rss><channel/></rss>']
        with patch.object(media.requests, 'get', return_value=ok) as get:
            media.run({'queries': ['test']}, ['rss'], mirror=False, translate=False)
        self.assertEqual(get.call_count, 2)
        self.assertFalse(media.HOST_FAILURES)

    def test_rss_rotation_resumes_queries_skipped_by_budget(self):
        state, reports = {}, []
        cfg = {'queries': ['first', 'second']}
        with patch.object(media, 'fetch_text', side_effect=[
                ('<rss/>', {'status': 'ok'}), (None, {'status': 'error', 'reason': 'ReadTimeout'}),
                (None, {'status': 'budget_exhausted'}), (None, {'status': 'budget_exhausted'})]):
            media.discover_rss(cfg, reports, state)
        self.assertEqual(state['rss_cursor']['next_index'], 2)
        with patch.object(media, 'fetch_text', return_value=('<rss/>', {'status': 'ok'})) as fetch:
            media.discover_rss(cfg, [], state)
        self.assertIn('q=second&hl=ru', fetch.call_args_list[0].args[0])
        merged = media.merge_discovery_state({'rss_cursor': {'next_index': 1, 'attempted_at': '2026-01-01'}}, state)
        self.assertEqual(merged['rss_cursor'], state['rss_cursor'])

    def test_rss_body_score_after_redirect(self):
        candidate = {'url': 'https://news.google.com/rss/articles/opaque', 'source': 'google_news_rss', 'title': 'Uninformative feed title'}
        with patch.object(media, 'fetch_text', return_value=(PARTNER_ARTICLE, {'status': 'ok', 'final_url': 'https://physics.example.org/news/example'})):
            original, report = media.resolve_original(candidate['url'])
        self.assertEqual(original, 'https://physics.example.org/news/example')
        candidate['url'] = original
        record = media.build_record(candidate, media.article_meta(PARTNER_ARTICLE, original), CFG)
        self.assertEqual(record['status'], 'published')

    def test_repository_published_english_is_complete_or_explicitly_pending(self):
        published = Path(__file__).resolve().parents[2] / 'data/media/published.json'
        records = media.payload_records(json.loads(published.read_text(encoding='utf-8')))
        for record in records:
            state = record.get('translation_state') or {}
            pending = (set(state.get('fields') or []) & set(pending_translation_fields(record))
                       if state.get('status') == 'pending' and state.get('reason') else set())
            for field in ('title_en', 'description_en'):
                if not record.get(field) and field in pending:
                    continue
                self.assertTrue(record.get(field), (record['url'], field))
                self.assertIsNone(media.re.search('[А-Яа-яЁё]', record[field]), (record['url'], field))

    def test_checkpoint_merge_retains_backlog_and_success(self):
        old = {'initial_cutoff': '2026-06-01',
               'processed': {'done': {'processed_at': '2026-09-19', 'status': 'published'}},
               'pending': {'both': {'url': 'https://site.org/both', 'last_attempt_at': '2026-09-20', 'reason': 'newer'},
                           'candidate_done': {'url': 'https://site.org/candidate_done'}},
               'sitemap_pending': {'map': ['child-a']}}
        candidate = {'initial_cutoff': '2026-06-20',
                     'processed': {'candidate_done': {'processed_at': '2026-09-20', 'status': 'low_confidence'}},
                     'pending': {'both': {'url': 'https://site.org/both', 'last_attempt_at': '2026-09-18', 'reason': 'older'},
                                 'done': {'url': 'https://site.org/done'}, 'new': {'url': 'https://site.org/new'}},
                     'sitemap_pending': {'map': ['child-a', 'child-b']}}
        result = media.merge_discovery_state(old, candidate)
        self.assertEqual(set(result['pending']), {'both', 'new'})
        self.assertEqual(set(result['processed']), {'done', 'candidate_done'})
        self.assertEqual(result['pending']['both']['reason'], 'newer')
        self.assertEqual(result['sitemap_pending']['map'], ['child-a', 'child-b'])
        self.assertEqual(result['initial_cutoff'], '2026-06-01')
        self.assertEqual(media.merge_discovery_state(result, candidate), result)

    def test_google_signed_wrapper_lookup_and_consent(self):
        wrapper = '<div data-n-a-sg="public-signature" data-n-a-ts="123"></div>'
        response = Mock()
        response.text = "\n" + json.dumps([['wrb.fr', 'Fbv4je', json.dumps(['garturlres', 'https://publisher.org/news/story'])]])
        with patch.object(media, 'fetch_text', return_value=(wrapper, {'status': 'ok', 'final_url': 'https://news.google.com/articles/token'})), patch.object(media.requests, 'post', return_value=response):
            url, report = media.resolve_original('https://news.google.com/articles/token')
        self.assertEqual(url, 'https://publisher.org/news/story')
        self.assertEqual(report['resolution'], 'public_article_lookup')
        with patch.object(media, 'fetch_text', return_value=('<html>Consent</html>', {'status': 'ok', 'final_url': 'https://consent.google.com/ml?token=x'})):
            url, report = media.resolve_original('https://news.google.com/articles/token')
        self.assertIsNone(url)
        self.assertEqual(report, {'url': 'https://news.google.com/articles/token', 'status': 'consent_required'})

    def test_http_errors_and_retryable_rate_limit(self):
        for code in (401, 403):
            media.HOST_FAILURES.clear()
            response = Mock(status_code=code, url='https://source.org')
            with patch.object(media.requests, 'get', return_value=response) as get:
                raw, report = media.fetch_text('https://source.org')
            self.assertIsNone(raw)
            self.assertEqual(report['http_status'], code)
            self.assertEqual(get.call_count, 1)
        media.HOST_FAILURES.clear()
        denied = Mock(status_code=429, url='https://source.org')
        ok = Mock(status_code=200, url='https://source.org', encoding='utf-8')
        ok.iter_content.return_value = [b'<html>ok</html>']
        with patch.object(media.requests, 'get', side_effect=[denied, ok]), patch.object(media.time, 'sleep'):
            raw, report = media.fetch_text('https://source.org')
        self.assertEqual(report['status'], 'ok')
        self.assertIn('ok', raw)

    def test_translation_failure_and_cached_manual_priority(self):
        cache = self.root / 'translation.json'
        translator = MediaTranslator(cache)
        record = {'title_ru': 'Русский текст', 'title_en': 'Reviewed', 'description_ru': 'Описание'}
        with patch.object(translator, 'ensure', return_value=False):
            translator.enrich(record)
        self.assertEqual(record['title_en'], 'Reviewed')
        self.assertEqual(record['description_ru'], 'Описание')
        self.assertNotIn('description_en', record)
        translator._translation = Mock()
        translator._translation.translate.return_value = 'Description'
        with patch.object(translator, 'ensure', return_value=True):
            translator.enrich(record)
        translator.save()
        again = {'description_ru': 'Описание'}
        cached = MediaTranslator(cache)
        with patch.object(cached, 'ensure', side_effect=AssertionError('No model call needed')):
            cached.enrich(again)
        self.assertEqual(again['description_en'], 'Description')

    def test_new_russian_card_survives_translation_outage_and_later_recovers(self):
        old = {'id': 'manual', 'url': 'https://site.org/manual', 'title_ru': 'Ручной заголовок',
               'title_en': 'Reviewed title', 'description_ru': 'Ручное описание',
               'description_en': 'Reviewed description', 'source_name': 'Источник', 'source_name_en': 'Reviewed source'}
        media.write_json(media.OUT / 'published.json', {'records': [old]})

        def unavailable(translator):
            translator.status = 'unavailable_ru_en_model'
            return False

        with patch.object(media, 'fetch_text', side_effect=self.fake_fetch), patch.object(MediaTranslator, 'ensure', unavailable):
            media.run(CFG, ['listings'], mirror=False)
        records = self.read('published.json')['records']
        self.assertEqual(next(r for r in records if r['id'] == 'manual'), old)
        new = [r for r in records if r['id'] != 'manual']
        self.assertEqual(len(new), 2)
        self.assertTrue(all(r['translation_state']['status'] == 'pending' for r in new))
        self.assertTrue(all(r['title_ru'] and r['description_ru'] for r in new))
        self.assertTrue(all(r['source_name_en'] for r in new))
        errors = []
        for name in ('published.json', 'news_mentions.json'):
            media.write_json(self.root / 'data/media' / name, {'records': records})
        with patch.object(seo, 'ROOT', self.root), patch.object(seo, 'ERRORS', errors):
            seo.check_media_english_localization()
        self.assertEqual(errors, [])

        def available(translator):
            translator._translation = Mock()
            translator._translation.translate.return_value = 'Translated academic news'
            return True

        with patch.object(media, 'fetch_text', side_effect=self.fake_fetch), patch.object(MediaTranslator, 'ensure', available):
            media.run(CFG, ['listings'], mirror=False)
        recovered = self.read('published.json')['records']
        self.assertEqual(next(r for r in recovered if r['id'] == 'manual'), old)
        self.assertTrue(all(r['translation_state']['status'] == 'complete' for r in recovered if r['id'] != 'manual'))
        # Concurrent publication must clear stale pending state after filling EN.
        merged = media.merge_records(records, recovered)
        self.assertTrue(all(r['translation_state']['status'] == 'complete' for r in merged if r['id'] != 'manual'))

    def test_pending_marker_cannot_excuse_invalid_english_or_missing_original(self):
        cases = [
            {'title_ru': 'Русский заголовок', 'description_ru': 'Русское описание'},
            {'title_en': 'Русский заголовок', 'description_en': 'Reviewed description',
             'translation_state': {'status': 'pending', 'fields': ['title_en'], 'reason': 'unavailable'}},
            {'title_en': 'Reviewed title',
             'translation_state': {'status': 'pending', 'fields': ['description_en'], 'reason': 'unavailable'}},
        ]
        for record in cases:
            for name in ('published.json', 'news_mentions.json'):
                media.write_json(self.root / 'data/media' / name, {'records': [record]})
            errors = []
            with patch.object(seo, 'ROOT', self.root), patch.object(seo, 'ERRORS', errors):
                seo.check_media_english_localization()
            self.assertTrue(errors, record)

    def test_source_name_english_uses_config_or_cached_translation(self):
        cfg = {'sitemap_sources': [{'name': 'Институт', 'name_en': 'Research Institute', 'sitemap_url': 'https://site.org/map.xml'}]}
        with patch.object(media, 'fetch_text', return_value=('<urlset><url><loc>https://site.org/news</loc></url></urlset>', {'status': 'ok'})):
            found = media.discover_sitemaps(cfg, [], {})
        self.assertEqual(found[0]['source_name_en'], 'Research Institute')
        translator = MediaTranslator(self.root / 'translation.json')
        translator._translation = Mock()
        translator._translation.translate.return_value = 'Research Institute'
        record = {'title_en': 'Reviewed title', 'description_en': 'Reviewed description', 'source_name': 'Научный институт'}
        with patch.object(translator, 'ensure', return_value=True):
            translator.enrich(record)
        self.assertEqual(record['source_name_en'], 'Research Institute')
        self.assertNotIn('translation_state', record)
        translator.save()
        cached = MediaTranslator(self.root / 'translation.json')
        restored = {'source_name': 'Научный институт'}
        with patch.object(cached, 'ensure', side_effect=AssertionError('Use source-name cache')):
            cached.enrich(restored)
        self.assertEqual(restored['source_name_en'], 'Research Institute')

    def test_image_transport_or_invalid_content_keeps_previous(self):
        old = {'id': 'image', 'image': 'https://site.org/old.jpg', 'url': 'https://site.org/story'}
        for response in [(None, {'status': 'http_error'}), (b'<html>denied</html>', {'status': 'ok', 'content_type': 'image/jpeg'})]:
            record = copy.deepcopy(old)
            with patch.object(images, 'fetch_bytes', return_value=response):
                images.mirror_image(record, 'https://site.org/new.jpg')
            self.assertEqual(record, old)


if __name__ == '__main__':
    unittest.main()
