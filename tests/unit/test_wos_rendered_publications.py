"""Observed WoS zero-height shells rendered by an actual IntersectionObserver.

The container/tag structure follows the live page; all record values are fixtures.
No provider requests, session data, or copied private page HTML are used.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_wos_authenticated as wos
from provider_auth import AuthFailure


def lazy_html(*, paginate=False, fail_next=False):
    return '''<!doctype html><html><head><style>
        app-record, app-records-list {display:block}
        .record-body {padding:12px}
        </style></head><body>
        <div class="wat-author-metric-inline-block"><div>3</div><div>Publications</div></div>
        <div class="wat-author-metric-inline-block"><div>0</div><div>Sum of Times Cited</div></div>
        <div class="wat-author-metric-inline-block"><div>0</div><div>H-Index</div></div>
        <button class="selected-round-chip" aria-label="Web of Science Core Collection (3)">Web of Science Core Collection (3)</button>
        <input type="hidden" value="private-not-diagnostic">
        <div style="height:1300px"></div><app-records-list></app-records-list>
        <button id="next" data-ta="next-page-button" onclick="nextPage()">Next Page</button>
        <div style="height:1000px"></div><script>
        const paginate = PAGINATE, failNext = FAIL_NEXT;
        const list = document.querySelector('app-records-list');
        let observer;
        function load(ids, fill) {
            if (observer) observer.disconnect();
            list.innerHTML = ids.map(() => '<app-record></app-record>').join('');
            observer = new IntersectionObserver(entries => {
                if (!fill || !entries.some(entry => entry.isIntersecting)) return;
                observer.disconnect();
                list.querySelectorAll('app-record').forEach((el, index) => {
                    const id = ids[index];
                    el.innerHTML = `<div class="record-body"><app-summary-title>
                        <h3><a data-ta="summary-record-title-link" href="/wos/woscc/full-record/WOS:${id}">Fixture scientific publication ${id}</a></h3>
                        </app-summary-title><app-summary-authors>Fixture Author</app-summary-authors>
                        <span class="summary-source-title">Fixture Journal</span>
                        <span data-ta="summary-record-pubdate">2026</span></div>`;
                });
            });
            observer.observe(list.querySelector('app-record'));
        }
        function nextPage() {
            document.getElementById('next').disabled = true;
            window.scrollTo(0, 0);
            load([3], !failNext);
        }
        document.getElementById('next').disabled = !paginate;
        load(paginate ? [1, 2] : [1, 2, 3], true);
        </script></body></html>'''.replace('PAGINATE', json.dumps(paginate)).replace('FAIL_NEXT', json.dumps(fail_next))


class RenderedRecordUnitTests(unittest.TestCase):
    def test_existing_snapshot_distinguishes_metric_success_from_empty_shells(self):
        snapshot = Path(__file__).resolve().parents[1] / 'fixtures/wos_profile_shell.html'
        parsed = wos.parse_wos_author_profile_html(snapshot.read_text(encoding='utf-8'))
        self.assertEqual(parsed['summary']['publications'], 10)
        self.assertEqual(parsed['records'], [])

    def test_ready_records_are_returned_without_scrolling(self):
        page = MagicMock()
        data = {'summary': {'publications': 1}, 'records': [{'wos_uid': 'WOS:1', 'title': 'Fixture'}]}
        with patch.object(wos, 'assert_no_challenge'), patch.object(wos, 'parse_wos_author_profile_html', return_value=data):
            self.assertEqual(wos.read_records(page), data)
        page.locator.assert_not_called()

    def test_scroll_error_is_explicit_and_diagnostic_without_exception_text(self):
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.first.scroll_into_view_if_needed.side_effect = RuntimeError('private error content')
        data = {'summary': {'publications': 1}, 'records': []}
        with patch.object(wos, 'assert_no_challenge'), patch.object(wos, 'parse_wos_author_profile_html', return_value=data), patch.object(wos, 'safe_record_dom_diagnostics', return_value={'app_record_count': 1}):
            with self.assertRaisesRegex(AuthFailure, '^publication_list_scroll_failed$') as caught:
                wos.read_records(page)
        self.assertEqual(caught.exception.profile_diagnostics['app_record_count'], 1)
        self.assertNotIn('private', str(caught.exception.profile_diagnostics))


class RenderedRecordBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.playwright = sync_playwright().start()
            options = {'headless': True}
            if not Path(cls.playwright.chromium.executable_path).exists():
                edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
                if not edge.exists():
                    cls.playwright.stop()
                    raise unittest.SkipTest('Install Playwright Chromium for rendered-list fixtures')
                options['executable_path'] = str(edge)
            cls.browser = cls.playwright.chromium.launch(**options)
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 800, 'height': 500})
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)

    def test_zero_height_lazy_shells_render_after_ordinary_scroll(self):
        self.page.set_content(lazy_html())
        self.page.wait_for_timeout(150)
        before = wos.safe_record_dom_diagnostics(self.page)
        self.assertEqual(before['app_record_count'], 3)
        self.assertEqual(before['nonempty_app_record_count'], 0)
        self.assertEqual(before['first_record_height'], 0)
        self.assertGreater(before['first_record_top'], before['viewport_height'])
        self.assertEqual(wos.parse_wos_author_profile_html(self.page.content())['records'], [])
        with patch.object(wos, 'WAIT_SEC', 8):
            data = wos.read_records(self.page)
        self.assertEqual({row['wos_uid'] for row in data['records']}, {'WOS:1', 'WOS:2', 'WOS:3'})
        self.assertEqual(wos.safe_record_dom_diagnostics(self.page)['nonempty_app_record_count'], 3)

    def test_next_page_lazy_shells_are_scrolled_and_merged_without_duplicates(self):
        self.page.set_content(lazy_html(paginate=True))
        batches = []
        with patch.object(wos, 'WAIT_SEC', 8):
            data = wos.collect_publications(self.page, on_batch=lambda rows, complete: batches.append((rows, complete)))
        self.assertEqual([row['wos_uid'] for row in data['records']], ['WOS:1', 'WOS:2', 'WOS:3'])
        self.assertEqual([complete for _, complete in batches], [False, True])

    def test_partial_pagination_preserves_metrics_previous_rows_and_diagnostics(self):
        self.page.set_content(lazy_html(paginate=True, fail_next=True))
        old = {'metrics': {}, 'publications': [{'wos_uid': 'WOS:99', 'title': 'Retained work', 'observed_at': '2025-01-01'}], 'details': {}}
        with patch.object(wos, 'WAIT_SEC', 2), patch.object(wos, 'read_profile_metrics', return_value={'summary': {'publications': 3, 'citations': 0, 'h_index': 0}}):
            report, payload = wos.collect_from_page(self.page, previous=old)
        self.assertTrue(report['components']['metrics']['complete'])
        self.assertEqual(payload['metrics']['summary']['citations'], 0)
        publications = report['components']['publications']
        self.assertEqual(publications['status'], 'partial')
        self.assertEqual(publications['reason'], 'profile_records_not_ready')
        self.assertEqual(publications['observed_count'], 2)
        self.assertFalse(publications['complete'])
        self.assertEqual(publications['profile_diagnostics']['app_record_count'], 1)
        self.assertEqual(publications['profile_diagnostics']['nonempty_app_record_count'], 0)
        self.assertEqual({row['wos_uid'] for row in payload['publications']}, {'WOS:99', 'WOS:1', 'WOS:2'})
        self.assertEqual(payload['publications'][0], old['publications'][0])
        self.assertNotIn('private-not-diagnostic', json.dumps(report))

    def test_timeout_diagnostics_contain_counts_and_geometry_without_page_values(self):
        self.page.set_content(lazy_html())
        with patch.object(wos, 'WAIT_SEC', 0):
            with self.assertRaisesRegex(AuthFailure, '^profile_records_not_ready$') as caught:
                wos.read_records(self.page)
        diagnostic = caught.exception.profile_diagnostics
        self.assertEqual(diagnostic['app_record_count'], 3)
        self.assertEqual(diagnostic['nonempty_app_record_count'], 0)
        self.assertEqual(diagnostic['record_title_link_count'], 0)
        self.assertEqual(diagnostic['first_record_intersects_viewport'], 0)
        self.assertNotIn('private-not-diagnostic', json.dumps(diagnostic))
        self.assertNotIn('https://', json.dumps(diagnostic))

    def test_explicit_challenge_prevents_scrolling(self):
        self.page.set_content(lazy_html().replace('<body>', '<body><p>Please verify you are human</p>'))
        with self.assertRaisesRegex(AuthFailure, '^human_verification_required$'):
            wos.read_records(self.page)
        self.assertEqual(self.page.evaluate('scrollY'), 0)
        self.assertEqual(wos.safe_record_dom_diagnostics(self.page)['nonempty_app_record_count'], 0)


if __name__ == '__main__':
    unittest.main()
