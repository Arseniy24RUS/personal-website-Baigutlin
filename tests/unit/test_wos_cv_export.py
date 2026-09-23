"""Ordinary CV UI jobs in a real browser; every request is routed locally."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import wos_cv_export as cv
from provider_auth import AuthFailure

ORIGIN = 'https://www.webofscience.com'
DOCUMENT = {'author': {'researcher_id': 'TEST-0001-2026'}, 'records': [{'uid': 'WOS:123'}]}


def result(document=DOCUMENT):
    return {'status': 'SUCCESS', 'results': {'file_name': 'fixture.json', 'file_content_type': 'text/json',
            'file_content': base64.b64encode(json.dumps(document).encode()).decode()}}


def cv_html(language='en', behavior='success'):
    ru = language == 'ru'
    labels = (['Экспортировать полный профиль', 'Идентификационный номер', 'Список авторов',
               'Число цитирований', 'Загрузить мой профиль'] if ru else
              ['Export full profile', 'Web of Science Accession Number', 'Author list', 'Citation count', 'Download my profile'])
    mode, accession, authors, citations, download = labels
    metric_labels = (['Общее число цитирований из набора статей Web of Science Core Collection, опубликованных в течение выбранного периода',
                      'h-index Web of Science статей, опубликованных в течение выбранного периода',
                      'Число статей, опубликованных в течение выбранного периода, которые были проиндексированы в Web of Science Core Collection'] if ru else
                     ['Total number of citations from the Web of Science Core Collection of papers published in the selected period',
                      'Web of Science h-index for papers published in the selected period',
                      'Number of papers published in the selected period which are indexed in the Web of Science Core Collection'])
    before = ''
    if behavior == 'unknown':
        before = "await fetch('https://www.webofscience.com.untrusted.invalid' + taskPath + '?task_id=fixture-task'); await fetch(taskPath + '?task_id=other-task');"
    elif behavior == 'challenge':
        before = "document.getElementById('notice').textContent = 'Verify you are human';"
    elif behavior == 'timeout':
        before = 'return;'
    return f'''<html><body><p id="notice"></p>
<label><input type="radio" checked>{mode}</label>
<label>Start date<input id="startDateId" value="2021-01-01"></label>
<label>End date<input id="endDateId" value="2026-01-01"></label>
<div role="combobox" aria-label="Filter by, PDF" tabindex="0" onclick="document.getElementById('formats').hidden=false">PDF</div>
<div id="formats" hidden><div role="option" onclick="const c=document.querySelector('[role=combobox]'); c.textContent='JSON'; c.setAttribute('aria-label','Filter by, JSON'); this.parentElement.hidden=true; window.pdfCheckboxState=Array.from(document.querySelectorAll('#pdf-settings input')).map(e=>e.checked); document.getElementById('pdf-settings').remove()">JSON</div></div>
<section id="pdf-settings">
<label><input type="checkbox" id="accession">{accession}</label>
<label><input type="checkbox" id="authors">{authors}</label>
<label><input type="checkbox" id="citations">{citations}</label>
<label><input type="checkbox" id="publication-date">{'Дата публикации' if ru else 'Publication date'}</label>
<label><input type="checkbox" id="doi">DOI</label>
<label><input type="checkbox" id="total-citations">{metric_labels[0]}</label>
<label><input type="checkbox" id="hindex">{metric_labels[1]}</label>
<label><input type="checkbox" id="total-publications">{metric_labels[2]}</label>
<fieldset disabled><label><input type="checkbox" id="preprint-accession">{accession}</label>
<label><input type="checkbox" id="preprint-authors">{authors}</label></fieldset>
</section>
<button onclick="download()">{download}</button>
<script>
async function download() {{
 window.downloadClicks = (window.downloadClicks || 0) + 1;
 const taskPath = {json.dumps(cv.TASK_PATH)};
 const creation = await fetch({json.dumps(cv.CREATE_PATH)}, {{method:'POST'}});
 const job = await creation.json();
 {before}
 await fetch(taskPath + '?task_id=' + encodeURIComponent(job.taskId));
}}
</script></body></html>'''


class CVUIBrowserTests(unittest.TestCase):
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
                    raise unittest.SkipTest('Install Playwright Chromium for CV export fixtures')
                options['executable_path'] = str(edge)
            cls.browser = cls.playwright.chromium.launch(**options)
        except ImportError:
            raise unittest.SkipTest('Playwright is not installed')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def fixture(self, *, language='en', behavior='success', response=None, profile_marker=False, job_status=200):
        context = self.browser.new_context(viewport={'width': 1440, 'height': 1100})
        self.addCleanup(context.close)
        page = context.new_page()
        calls = []
        old_requests = []
        def route(route):
            url = urlsplit(route.request.url)
            calls.append((url.hostname, url.path, route.request.method))
            if url.path == '/profile':
                label = 'Экспортировать резюме' if language == 'ru' else 'Export CV'
                marker = '<p>Verify you are human</p>' if profile_marker else ''
                route.fulfill(content_type='text/html; charset=utf-8', body=f'<body>{marker}<button onclick="location.href=\'{cv.CV_PATH}\'">{label}</button></body>')
            elif url.path == cv.CV_PATH:
                body = cv_html(language, behavior)
                if behavior == 'old_request':
                    body += '<script>fetch(' + json.dumps(cv.CREATE_PATH) + ',{method:"POST",body:"prior"}).then(r=>r.json()).then(j=>fetch(' + json.dumps(cv.TASK_PATH) + '+"?task_id="+j.taskId))</script>'
                route.fulfill(content_type='text/html; charset=utf-8', body=body)
            elif url.path == cv.CREATE_PATH:
                if route.request.post_data == 'prior':
                    old_requests.append(route)
                    return
                for old in old_requests:
                    old.fulfill(status=201, content_type='application/json', body=json.dumps({'taskId': 'old-task'}))
                route.fulfill(status=201, content_type='application/json', body=json.dumps({'taskId': 'fixture-task'}))
            elif url.path == cv.TASK_PATH:
                matching = url.hostname == 'www.webofscience.com' and parse_qs(url.query).get('task_id') == ['fixture-task']
                content = response if response is not None and matching else result() if matching else result({'wrong_export': True})
                route.fulfill(status=job_status if matching else 200, content_type='application/json', headers={'Access-Control-Allow-Origin': '*'}, body=json.dumps(content))
            else:
                route.abort()
        context.route('**/*', route)
        page.goto(ORIGIN + '/profile')
        return page, calls

    def test_ordinary_json_export_succeeds_after_ui_removes_pdf_only_fields(self):
        for language in ('en', 'ru'):
            with self.subTest(language=language):
                page, calls = self.fixture(language=language)
                with patch.object(page, 'remove_listener', wraps=page.remove_listener) as remove:
                    self.assertEqual(cv.fetch_wos_cv(page), DOCUMENT)
                self.assertEqual(remove.call_count, 4)
                self.assertEqual(page.locator('#startDateId').input_value(), '1900-01-01')
                self.assertEqual(page.locator('#endDateId').input_value(), datetime.now(timezone.utc).date().isoformat())
                self.assertEqual(page.get_by_role('combobox').get_attribute('aria-label'), 'Filter by, JSON')
                self.assertEqual(page.get_by_role('checkbox').count(), 0)
                self.assertEqual(page.evaluate('window.pdfCheckboxState'), [False] * 10)
                self.assertTrue(page.get_by_role('radio').is_checked())
                self.assertEqual(page.evaluate('window.downloadClicks'), 1)
                self.assertEqual(sum(path == cv.CREATE_PATH for _, path, _ in calls), 1)
                self.assertEqual(sum(path == cv.TASK_PATH for _, path, _ in calls), 1)

    def test_unknown_host_and_unrelated_task_responses_are_ignored(self):
        page, _ = self.fixture(behavior='unknown')
        self.assertEqual(cv.fetch_wos_cv(page), DOCUMENT)

    def test_rate_limit_stops_without_page_parser_fallback(self):
        page, _ = self.fixture(job_status=429)
        with self.assertRaises(AuthFailure) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(caught.exception.reason, 'rate_limited')

    def test_unexpected_profile_origin_is_auth_failure(self):
        page, calls = self.fixture()
        page.goto('https://www.webofscience.com.untrusted.invalid/profile')
        with self.assertRaises(AuthFailure) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(caught.exception.reason, 'unexpected_login_origin')
        self.assertFalse(any(path == cv.CREATE_PATH for _, path, _ in calls))

    def test_delayed_response_from_request_started_before_download_is_ignored(self):
        page, calls = self.fixture(behavior='old_request')
        self.assertEqual(cv.fetch_wos_cv(page), DOCUMENT)
        self.assertEqual(sum(path == cv.CREATE_PATH for _, path, _ in calls), 2)

    def test_failed_job_has_fixed_reason_and_no_raw_body(self):
        page, _ = self.fixture(response={'status': 'FAILURE', 'error': 'synthetic-private-token-and-url'})
        with self.assertRaises(cv.CVExportError) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(str(caught.exception), 'cv_export_job_failed')
        self.assertEqual(caught.exception.stage, 'read_job_response')

    def test_ui_failure_reports_fixed_stage_without_exception_text(self):
        page, _ = self.fixture()
        from playwright.sync_api import Locator
        fill = Locator.fill
        def failed_start(locator, value, **kwargs):
            if value == '1900-01-01':
                raise RuntimeError('synthetic-private-session-url-and-body')
            return fill(locator, value, **kwargs)
        with patch.object(Locator, 'fill', failed_start), self.assertRaises(cv.CVExportError) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(str(caught.exception), 'cv_export_ui_failed')
        self.assertEqual(caught.exception.stage, 'start_date')
        self.assertNotIn('synthetic-private', str(caught.exception))

    def test_timeout_is_bounded_and_does_not_make_polling_requests(self):
        from types import SimpleNamespace

        page, calls = self.fixture(behavior='timeout')
        clock = {'started': None, 'expired': False}
        ordinary_wait = page.wait_for_timeout
        waits = []

        def monotonic():
            current = time.monotonic()
            if clock['started'] is None:
                clock['started'] = current
            return clock['started'] + 121 if clock['expired'] else current

        def finish_job_wait(milliseconds):
            if any(path == cv.CREATE_PATH and method == 'POST' for _, path, method in calls):
                waits.append(milliseconds)
                # Expire after the ordinary UI starts its job, between guard
                # calls. A two-second wall-clock limit can instead expire
                # inside a legitimate challenge observation on a slower runner.
                clock['expired'] = True
                return ordinary_wait(0)
            return ordinary_wait(milliseconds)

        # Replace this module's clock only. provider_auth and Playwright keep
        # their real clocks and all production CAPTCHA checks remain active.
        with patch.object(cv, 'time', SimpleNamespace(monotonic=monotonic)), \
                patch.object(page, 'wait_for_timeout', side_effect=finish_job_wait), \
                self.assertRaises(cv.CVExportError) as caught:
            cv.fetch_wos_cv(page, timeout=120)
        self.assertEqual(caught.exception.reason, 'cv_export_timeout')
        self.assertTrue(clock['expired'])
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], cv.POLL_SECONDS * 1000)
        self.assertEqual(page.evaluate('window.downloadClicks'), 1)
        self.assertEqual(sum(path == cv.CREATE_PATH and method == 'POST' for _, path, method in calls), 1)
        self.assertFalse(any(path == cv.TASK_PATH for _, path, _ in calls))

    def test_visible_challenge_stops_before_export_button(self):
        page, calls = self.fixture(profile_marker=True)
        with self.assertRaises(AuthFailure) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        self.assertEqual(calls, [('www.webofscience.com', '/profile', 'GET')])

    def test_challenge_during_job_wait_stops_instead_of_accepting_result(self):
        page, _ = self.fixture(behavior='challenge')
        with self.assertRaises(AuthFailure) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(caught.exception.reason, 'human_verification_required')
        self.assertEqual(page.evaluate('window.downloadClicks'), 1)

    def test_invalid_document_is_a_fixed_export_error(self):
        page, _ = self.fixture(response={'status': 'SUCCESS', 'results': {'file_content_type': 'text/json',
                                                                       'file_content': 'synthetic-private-value'}})
        with self.assertRaises(cv.CVExportError) as caught:
            cv.fetch_wos_cv(page)
        self.assertEqual(str(caught.exception), 'cv_export_document_invalid')


class CVPayloadTests(unittest.TestCase):
    def test_exact_origin_only(self):
        self.assertIsNotNone(cv._address(ORIGIN + cv.TASK_PATH))
        for url in ('http://www.webofscience.com/', 'https://webofscience.com/',
                    'https://www.webofscience.com.attacker.invalid/', 'https://user:pass@www.webofscience.com/',
                    'https://www.webofscience.com:444/', 'invalid'):
            self.assertIsNone(cv._address(url))

    def test_export_size_is_checked_before_decoding(self):
        with patch.object(cv, 'MAX_DOCUMENT_BYTES', 3), patch.object(cv.base64, 'b64decode') as decode:
            with self.assertRaises(cv.CVExportError) as caught:
                cv._decode_export({'file_content_type': 'text/json', 'file_content': 'a' * 100})
            self.assertEqual(caught.exception.reason, 'cv_export_response_too_large')
            decode.assert_not_called()

    def test_content_length_guard_runs_before_body_read(self):
        response = Mock()
        response.header_value.return_value = str(cv.MAX_BODY_BYTES + 1)
        with self.assertRaises(cv.CVExportError) as caught:
            cv._response_document(response)
        self.assertEqual(caught.exception.reason, 'cv_export_response_too_large')
        response.body.assert_not_called()

    def test_invalid_json_shapes_mime_and_nonfinite_numbers_are_rejected(self):
        for raw, mime in [(b'[]', 'text/json'), (b'{}', 'text/json'), (b'{"number": NaN}', 'text/json'),
                          (b'{"number": 1}', 'text/html'), (b'not-json-secret', 'text/json')]:
            with self.subTest(mime=mime), self.assertRaises(cv.CVExportError) as caught:
                cv._decode_export({'file_content_type': mime, 'file_content': base64.b64encode(raw).decode()})
            self.assertEqual(caught.exception.reason, 'cv_export_document_invalid')

    def test_timeout_validation_does_not_touch_page(self):
        page = Mock()
        for timeout in (0, -1, 121, True, float('nan'), float('inf'), '120'):
            with self.subTest(timeout=timeout), self.assertRaises(cv.CVExportError) as caught:
                cv.fetch_wos_cv(page, timeout=timeout)
            self.assertEqual(caught.exception.reason, 'cv_export_timeout_invalid')
        self.assertFalse(page.mock_calls)

    def test_exception_does_not_copy_original_reason(self):
        self.assertEqual(str(cv.CVExportError('private-token')), 'cv_export_ui_failed')
        self.assertIsNone(cv.CVExportError('cv_export_ui_failed', stage='private-session-url').stage)


if __name__ == '__main__':
    unittest.main()
