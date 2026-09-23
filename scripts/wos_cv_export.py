"""Read a CV JSON produced by the ordinary, authenticated WoS export UI.

No endpoint is called directly: the website owns creation and polling of its
download job. Responses are observed only after the user-facing Download button
is clicked. Raw exports and session URLs never leave memory here.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from provider_auth import AuthFailure, assert_no_challenge

CV_PATH = '/wos/op/cv-export'
CREATE_PATH = '/wos-researcher/dashboard/cv/download/'
TASK_PATH = '/wos-researcher/dashboard/cv/download-task/'
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_RESPONSES = 128
POLL_SECONDS = 0.25
REASONS = frozenset({
    'cv_export_timeout', 'cv_export_timeout_invalid', 'cv_export_controls_missing',
    'cv_export_ui_failed', 'cv_export_origin_changed', 'cv_export_dates_not_applied',
    'cv_export_format_not_applied', 'cv_export_job_failed', 'cv_export_http_error',
    'cv_export_task_invalid', 'cv_export_response_invalid', 'cv_export_response_too_large',
    'cv_export_document_invalid', 'cv_export_response_limit',
})
CV_STAGES = frozenset({
    'entry', 'open_cv', 'wait_cv_page', 'full_profile', 'start_date', 'end_date',
    'verify_dates', 'format_open', 'format_select_json', 'format_confirm_json',
    'download', 'wait_job', 'read_job_response', 'decode_export', 'verify_result',
})


class CVExportError(RuntimeError):
    """Only a fixed, credential-free reason is public."""
    def __init__(self, reason, *, stage=None):
        self.reason = reason if reason in REASONS else 'cv_export_ui_failed'
        self.stage = stage if isinstance(stage, str) and stage in CV_STAGES else None
        super().__init__(self.reason)


def _address(url):
    try:
        address = urlsplit(url)
        if (address.scheme == 'https' and address.hostname == 'www.webofscience.com'
                and address.port in (None, 443) and not address.username and not address.password):
            return address
    except (TypeError, ValueError):
        pass
    return None


def _reject_constant(value):
    raise ValueError('nonfinite_json_number')


def _json(data):
    return json.loads(data, parse_constant=_reject_constant)


def _response_document(response):
    try:
        length = response.header_value('content-length')
        if length is not None and (not length.isdecimal() or int(length) > MAX_BODY_BYTES):
            raise CVExportError('cv_export_response_too_large')
        # Called only after requestfinished, so this does not wait for a body
        # from a stalled website request beyond the export's absolute deadline.
        body = response.body()
        if len(body) > MAX_BODY_BYTES:
            raise CVExportError('cv_export_response_too_large')
        document = _json(body)
        if not isinstance(document, dict):
            raise CVExportError('cv_export_response_invalid')
        return document
    except CVExportError:
        raise
    except Exception:
        raise CVExportError('cv_export_response_invalid') from None


def _decode_export(result):
    try:
        if not isinstance(result, dict):
            raise ValueError()
        content_type = result.get('file_content_type')
        if not isinstance(content_type, str) or content_type.split(';')[0].strip().lower() not in {'text/json', 'application/json'}:
            raise ValueError()
        encoded = result.get('file_content')
        if not isinstance(encoded, str):
            raise ValueError()
        if len(encoded) > ((MAX_DOCUMENT_BYTES + 2) // 3) * 4:
            raise CVExportError('cv_export_response_too_large')
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise CVExportError('cv_export_response_too_large')
        document = _json(raw.decode('utf-8-sig'))
        if not isinstance(document, dict) or not document:
            raise ValueError()
        return document
    except CVExportError:
        raise
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise CVExportError('cv_export_document_invalid') from None


def fetch_wos_cv(page, *, timeout=120):
    """Return an in-memory export from an already verified author's profile.

    AuthFailure (including CAPTCHA/MFA) is deliberately propagated unchanged.
    A CVExportError permits the caller to try its ordinary page parser instead.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise CVExportError('cv_export_timeout_invalid')
    deadline = time.monotonic() + timeout
    pending, finished, eligible = {}, [], set()
    armed = False
    overflow = False
    response_count = 0
    stage = 'entry'

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise CVExportError('cv_export_timeout')
        return seconds

    def guard():
        remaining()
        assert_no_challenge(page, passive_wait_seconds=min(10.0, remaining()), passive_deadline=deadline)
        remaining()
        if _address(page.url) is None:
            raise AuthFailure('unexpected_login_origin')

    def action(locator, operation, *args):
        guard()
        value = getattr(locator, operation)(*args, timeout=min(10000, remaining() * 1000))
        remaining()
        return value

    def requested(request):
        if armed:
            address = _address(request.url)
            if address and ((address.path == CREATE_PATH and request.method == 'POST')
                            or (address.path == TASK_PATH and request.method == 'GET')):
                eligible.add(request)

    def observed(response):
        nonlocal overflow, response_count
        if not armed or response.request not in eligible:
            return
        address = _address(response.url)
        if address is None:
            return
        method = response.request.method
        if (address.path == CREATE_PATH and method == 'POST') or (address.path == TASK_PATH and method == 'GET'):
            response_count += 1
            if response_count > MAX_RESPONSES:
                overflow = True
                return
            pending[response.request] = (response, address.path, address.query)

    def completed(request):
        eligible.discard(request)
        response = pending.pop(request, None)
        if response is not None:
            finished.append(response)

    def failed(request):
        eligible.discard(request)
        pending.pop(request, None)

    page.on('request', requested)
    page.on('response', observed)
    page.on('requestfinished', completed)
    page.on('requestfailed', failed)
    try:
        guard()
        stage = 'open_cv'
        action(page.get_by_role('button', name=re.compile(r'^(?:Export CV|Экспортировать резюме)$', re.I)), 'click')
        stage = 'wait_cv_page'
        while not ((address := _address(page.url)) and address.path == CV_PATH):
            guard()
            page.wait_for_timeout(min(POLL_SECONDS, remaining()) * 1000)
        stage = 'full_profile'
        action(page.get_by_role('radio', name=re.compile(r'^(?:Export full profile|Экспортировать полный профиль)$', re.I)), 'check')
        start, end = page.locator('#startDateId'), page.locator('#endDateId')
        today = datetime.now(timezone.utc).date().isoformat()
        stage = 'start_date'
        action(start, 'fill', '1900-01-01')
        action(start, 'press', 'Tab')
        stage = 'end_date'
        action(end, 'fill', today)
        action(end, 'press', 'Tab')
        stage = 'verify_dates'
        if start.input_value(timeout=remaining() * 1000) != '1900-01-01' or end.input_value(timeout=remaining() * 1000) != today:
            raise CVExportError('cv_export_dates_not_applied')
        combo = page.get_by_role('combobox', name=re.compile(r'^Filter by,\s*(?:PDF|JSON)$', re.I))
        stage = 'format_open'
        action(combo, 'click')
        stage = 'format_select_json'
        action(page.get_by_role('option', name='JSON', exact=True), 'click')
        stage = 'format_confirm_json'
        label = (combo.get_attribute('aria-label', timeout=remaining() * 1000) or '') + ' ' + combo.inner_text(timeout=remaining() * 1000)
        if not re.search(r'\bJSON\b', label):
            raise CVExportError('cv_export_format_not_applied')
        # JSON exports the full profile. Its UI removes PDF-only field settings;
        # the caller validates the actual returned identity, fields and coverage.
        stage = 'download'
        guard()
        armed = True
        page.get_by_role('button', name=re.compile(r'^(?:Download my profile|Загрузить мой профиль)$', re.I)).click(
            timeout=min(10000, remaining() * 1000))
        task_id = None
        while True:
            stage = 'wait_job'
            guard()
            if overflow:
                raise CVExportError('cv_export_response_limit')
            # A poll may finish very quickly; process the creation first without
            # accepting a polling response belonging to any previous export.
            finished.sort(key=lambda item: item[1] != CREATE_PATH)
            backlog, finished[:] = list(finished), []
            for response, path, query in backlog:
                if path == TASK_PATH:
                    ids = parse_qs(query).get('task_id', [])
                    if task_id is None:
                        finished.append((response, path, query))
                        continue
                    if ids != [task_id]:
                        continue
                if response.status == 401:
                    raise AuthFailure('session_expired')
                if response.status == 429:
                    raise AuthFailure('rate_limited')
                if response.status != (201 if path == CREATE_PATH else 200):
                    raise CVExportError('cv_export_http_error')
                stage = 'read_job_response'
                value = _response_document(response)
                remaining()
                if path == CREATE_PATH:
                    identifier = value.get('taskId')
                    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
                        raise CVExportError('cv_export_task_invalid')
                    if task_id is not None and task_id != identifier:
                        raise CVExportError('cv_export_task_invalid')
                    task_id = identifier
                elif value.get('status') == 'SUCCESS':
                    stage = 'decode_export'
                    document = _decode_export(value.get('results'))
                    stage = 'verify_result'
                    guard()
                    return document
                elif value.get('status') in {'FAILURE', 'FAILED', 'ERROR', 'CANCELLED', 'CANCELED'}:
                    raise CVExportError('cv_export_job_failed')
                elif value.get('status') not in {'PENDING', 'STARTED', 'RUNNING', 'PROCESSING', 'QUEUED', 'RETRY'}:
                    raise CVExportError('cv_export_response_invalid')
            page.wait_for_timeout(min(POLL_SECONDS, remaining()) * 1000)
    except AuthFailure:
        raise
    except CVExportError as exc:
        if exc.stage is None:
            exc.stage = stage
        raise
    except Exception:
        raise CVExportError('cv_export_timeout' if time.monotonic() >= deadline else 'cv_export_ui_failed', stage=stage) from None
    finally:
        page.remove_listener('request', requested)
        page.remove_listener('response', observed)
        page.remove_listener('requestfinished', completed)
        page.remove_listener('requestfailed', failed)
