#!/usr/bin/env python3
"""Encrypted, provider-scoped browser checkpoints. Never print session material.

Artifacts are transport, not proof of authentication. Only a collector that has
verified the current account and target author may create a confirmed checkpoint.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import urlparse
import zipfile

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEMA = 'browser-session/v1'
REPOSITORY = 'Arseniy24RUS/personal-website-Baigutlin'
MAX_BYTES = 4 * 1024 * 1024
PROVIDERS = ('elibrary', 'wos')
HOSTS = {
    'elibrary': {'elibrary.ru', 'www.elibrary.ru'},
    'wos': {'webofscience.com', 'www.webofscience.com', 'access.clarivate.com',
            'signin.clarivate.com', 'orcid.org', 'www.orcid.org'},
}
AUTH_COOKIES = {
    'elibrary.ru': {'SCookieGUID', 'SUserID'},
    'www.elibrary.ru': {'SCookieGUID', 'SUserID'},
    'webofscience.com': {'WOSSID', 'dotmatics.elementalKey', 'group', '__cf_bm'},
    'www.webofscience.com': {'WOSSID', 'dotmatics.elementalKey', 'group', '__cf_bm'},
}
TARGET_ENV = {'elibrary': 'ELIBRARY_AUTHOR_ID', 'wos': 'WOS_RESEARCHER_ID'}
DEFAULT_TARGET = {'elibrary': '1170779', 'wos': 'AAN-4717-2020'}
WORKFLOW_PATH = '.github/workflows/refresh-data.yml'
TRUSTED_WORKFLOWS = {WORKFLOW_PATH, '.github/workflows/test-wos-dom.yml', '.github/workflows/refresh-media-corpus.yml'}
HYDRATION_MARKER = '__portfolio_session_hydrated_v1'


class SessionError(ValueError):
    """The reason is a fixed marker safe to report, never the original exception."""


def repository_for():
    value = os.environ.get('GITHUB_REPOSITORY', REPOSITORY)
    if value.lower() != REPOSITORY.lower():
        raise SessionError('wrong_session_repository')
    return REPOSITORY


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def target_for(provider):
    if provider not in PROVIDERS:
        raise SessionError('unknown_provider')
    return os.environ.get(TARGET_ENV[provider]) or DEFAULT_TARGET[provider]


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError):
        raise SessionError('invalid_timestamp') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _decode(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise SessionError('invalid_encoding') from None


def _key(value=None):
    result = _decode(value if value is not None else os.environ.get('BROWSER_SESSION_KEY', ''))
    if len(result) != 32:
        raise SessionError('invalid_session_key')
    return result


def _origin_allowed(provider, origin):
    parsed = urlparse(origin)
    return (parsed.scheme == 'https' and parsed.hostname in HOSTS[provider]
            and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)


def scoped_state(provider, state):
    """Copy only browser fields supported by Playwright and approved hosts."""
    target_for(provider)
    if not isinstance(state, dict):
        raise SessionError('invalid_storage_state')
    cookies = []
    for cookie in state.get('cookies', []):
        if not isinstance(cookie, dict):
            raise SessionError('invalid_cookie')
        host = str(cookie.get('domain', '')).lstrip('.').lower()
        if host not in HOSTS[provider]:
            continue
        if host in AUTH_COOKIES and cookie.get('name') not in AUTH_COOKIES[host]:
            continue
        if not isinstance(cookie.get('name'), str) or not isinstance(cookie.get('value'), str):
            raise SessionError('invalid_cookie')
        row = {key: cookie[key] for key in ('name', 'value', 'domain', 'path', 'expires',
                                           'httpOnly', 'secure', 'sameSite', 'partitionKey') if key in cookie}
        row.setdefault('path', '/')
        if row.get('expires') is not None and not isinstance(row['expires'], (int, float)):
            raise SessionError('invalid_cookie_expiry')
        if row.get('sameSite') not in (None, 'Strict', 'Lax', 'None'):
            raise SessionError('invalid_cookie_samesite')
        # CDP uses -1 for session cookies; Playwright also accepts -1.
        cookies.append(row)
    origins = []
    for origin in state.get('origins', []):
        if not isinstance(origin, dict):
            raise SessionError('invalid_origin')
        if _origin_allowed(provider, origin.get('origin', '')):
            origins.append({key: origin[key] for key in ('origin', 'localStorage', 'indexedDB') if key in origin})
    return {'cookies': cookies, 'origins': origins}


def validate_payload(payload, provider, *, allow_bootstrap=False):
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA:
        raise SessionError('invalid_session_schema')
    if payload.get('repository') != repository_for():
        raise SessionError('wrong_session_repository')
    if payload.get('provider') != provider or str(payload.get('target_id', '')) != target_for(provider):
        raise SessionError('wrong_session_target')
    kind = payload.get('kind')
    if kind != 'confirmed' and not (allow_bootstrap and kind == 'bootstrap'):
        raise SessionError('unconfirmed_session')
    created = _timestamp(payload.get('created_at'))
    if created > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise SessionError('future_session')
    if kind == 'confirmed':
        checked = _timestamp(payload.get('validated_at'))
        if checked > created or checked < datetime.now(timezone.utc) - timedelta(days=90):
            raise SessionError('stale_session_checkpoint')
    elif created < datetime.now(timezone.utc) - timedelta(days=1):
        raise SessionError('stale_bootstrap')
    result = {key: payload[key] for key in ('schema', 'repository', 'provider', 'target_id', 'kind', 'created_at', 'validated_at') if key in payload}
    result['storage_state'] = scoped_state(provider, payload.get('storage_state'))
    result['session_storage'] = {origin: items for origin, items in payload.get('session_storage', {}).items()
                                 if _origin_allowed(provider, origin) and isinstance(items, dict)
                                 and all(isinstance(k, str) and isinstance(v, str) for k, v in items.items())}
    if not result['storage_state']['cookies'] and not result['storage_state']['origins']:
        raise SessionError('empty_session')
    return result


def cookie_expiry(payload):
    provider = payload['provider']
    names = {'WOSSID'} if provider == 'wos' else {'SCookieGUID', 'SUserID'}
    values = [cookie.get('expires', -1) for cookie in payload['storage_state']['cookies'] if cookie['name'] in names]
    values = [value for value in values if isinstance(value, (int, float)) and value > 0]
    if not values:
        return None
    try:
        return datetime.fromtimestamp(min(values), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def encrypt_payload(payload, key=None):
    provider = payload.get('provider')
    payload = validate_payload(payload, provider, allow_bootstrap=True)
    header = {'schema': SCHEMA, 'repository': repository_for(), 'provider': provider, 'target_id': target_for(provider)}
    nonce = secrets.token_bytes(12)
    raw = _json(payload)
    if len(raw) > MAX_BYTES:
        raise SessionError('session_too_large')
    ciphertext = AESGCM(_key(key)).encrypt(nonce, raw, _json(header))
    return {**header, 'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(ciphertext).decode()}


def decrypt_payload(envelope, provider, *, key=None, allow_bootstrap=False):
    try:
        if not isinstance(envelope, dict) or len(_json(envelope)) > MAX_BYTES * 2:
            raise SessionError('invalid_envelope')
        header = {name: envelope.get(name) for name in ('schema', 'repository', 'provider', 'target_id')}
        if header != {'schema': SCHEMA, 'repository': repository_for(), 'provider': provider, 'target_id': target_for(provider)}:
            raise SessionError('wrong_session_target')
        raw = AESGCM(_key(key)).decrypt(_decode(envelope['nonce']), _decode(envelope['ciphertext']), _json(header))
        if len(raw) > MAX_BYTES:
            raise SessionError('session_too_large')
        return validate_payload(json.loads(raw), provider, allow_bootstrap=allow_bootstrap)
    except SessionError:
        raise
    except Exception:
        raise SessionError('session_decryption_failed') from None


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(_json(value) + b'\n')
    os.replace(temp, path)
    path.chmod(0o600)


def hydrate_session_storage(context, saved):
    """Hydrate each allowed origin once per tab without replacing renewed values."""
    if not saved:
        return
    encoded = json.dumps(json.dumps(saved, ensure_ascii=True))
    marker = json.dumps(HYDRATION_MARKER)
    context.add_init_script(script=f'(() => {{ const saved = JSON.parse({encoded}); const data = saved[location.origin]; const marker = {marker}; if (data && sessionStorage.getItem(marker) !== "1") {{ for (const [k,v] of Object.entries(data)) if (k !== marker) sessionStorage.setItem(k,v); sessionStorage.setItem(marker,"1"); }} }})();')


def restore_context(browser, provider, **options):
    """Missing/corrupt state still allows the collector's ordinary login path."""
    info = {'status': 'missing'}
    path = os.environ.get(f'BROWSER_SESSION_INPUT_{provider.upper()}')
    payload = None
    if path and Path(path).exists():
        try:
            payload = validate_payload(json.loads(Path(path).read_text(encoding='utf-8')), provider, allow_bootstrap=True)
            options['storage_state'] = payload['storage_state']
            info = {'status': 'restored', 'kind': payload['kind'], 'validated_at': payload.get('validated_at'),
                    'cookie_expires_at': cookie_expiry(payload)}
        except Exception:
            info = {'status': 'invalid', 'reason': 'unusable_session_checkpoint'}
    context = None
    try:
        context = browser.new_context(**options)
        if payload:
            hydrate_session_storage(context, payload.get('session_storage'))
    except Exception:
        if not payload:
            raise
        if context:
            context.close()
        options.pop('storage_state', None)
        context = browser.new_context(**options)
        info = {'status': 'invalid', 'reason': 'browser_rejected_session_checkpoint'}
    return context, info


def capture_session_storage(context, provider, verified_page=None, *, excluded_hosts=()):
    """Resolve tab-local storage without choosing an arbitrary last tab.

    The verified page is authoritative for its origin. Other origins must agree
    across tabs; a conflict must not create a supposedly confirmed checkpoint.
    """
    pages = list(context.pages)
    if verified_page is not None and not any(page is verified_page for page in pages):
        raise SessionError('verified_page_unavailable')

    def origin_of(page):
        parsed = urlparse(page.url)
        port = f':{parsed.port}' if parsed.port not in (None, 443) else ''
        return f'{parsed.scheme}://{parsed.hostname}{port}'

    def read_page(page, origin):
        values = page.evaluate('() => Object.fromEntries(Object.entries(sessionStorage))')
        if origin_of(page) != origin:
            raise SessionError('session_storage_origin_changed')
        if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
            raise SessionError('invalid_session_storage')
        return {key: value for key, value in values.items() if key != HYDRATION_MARKER}

    captured = {}
    verified_origin = None
    if verified_page is not None:
        verified_origin = origin_of(verified_page)
        if not _origin_allowed(provider, verified_origin) or urlparse(verified_origin).hostname in excluded_hosts:
            raise SessionError('verified_page_origin_invalid')
        captured[verified_origin] = read_page(verified_page, verified_origin)
    for page in pages:
        origin = origin_of(page)
        if origin == verified_origin or not _origin_allowed(provider, origin) or urlparse(origin).hostname in excluded_hosts:
            continue
        values = read_page(page, origin)
        if origin in captured and captured[origin] != values:
            raise SessionError('session_storage_conflict')
        captured[origin] = values
    if verified_page is not None and origin_of(verified_page) != verified_origin:
        raise SessionError('session_storage_origin_changed')
    return captured


def create_wos_reauthentication_context(browser, context, **options):
    """Discard expired WoS state while retaining the approved identity-provider SSO.

    This helper is only for a positively expired WoS session. It must not be used
    to retry challenges or to turn an unreadable SSO state into an empty login.
    """
    wos_hosts = {'webofscience.com', 'www.webofscience.com'}
    try:
        state = scoped_state('wos', context.storage_state(indexed_db=True))
        state['cookies'] = [cookie for cookie in state['cookies']
                            if cookie['domain'].lstrip('.').lower() not in wos_hosts]
        state['origins'] = [origin for origin in state['origins']
                            if urlparse(origin['origin']).hostname not in wos_hosts]
        session_storage = capture_session_storage(context, 'wos', excluded_hosts=wos_hosts)
    except Exception:
        raise SessionError('wos_sso_state_capture_failed') from None
    try:
        context.close()
    except Exception:
        raise SessionError('wos_expired_context_close_failed') from None
    replacement = None
    try:
        options['storage_state'] = state
        replacement = browser.new_context(**options)
        hydrate_session_storage(replacement, session_storage)
        return replacement
    except Exception:
        if replacement is not None:
            try:
                replacement.close()
            except Exception:
                pass
        raise SessionError('wos_sso_state_restore_failed') from None


def checkpoint_session(context, provider, *, authenticated, target_verified, target_id, verified_page=None):
    """The caller must establish both proofs from the current live page."""
    if authenticated is not True or target_verified is not True or str(target_id) != target_for(provider):
        return {'status': 'rejected', 'reason': 'session_not_verified'}
    if not os.environ.get('BROWSER_SESSION_KEY') or not os.environ.get('BROWSER_SESSION_OUTBOX'):
        return {'status': 'disabled', 'reason': 'session_storage_not_configured'}
    try:
        state = scoped_state(provider, context.storage_state(indexed_db=True))
        session_storage = capture_session_storage(context, provider, verified_page)
        stamp = now()
        payload = {'schema': SCHEMA, 'repository': repository_for(), 'provider': provider, 'target_id': str(target_id),
                   'kind': 'confirmed', 'created_at': stamp, 'validated_at': stamp,
                   'storage_state': state, 'session_storage': session_storage}
        path = Path(os.environ['BROWSER_SESSION_OUTBOX']) / f'{provider}.enc.json'
        private_write(path, encrypt_payload(payload))
        expiry = cookie_expiry(payload)
        previous_expiry = None
        restored = os.environ.get(f'BROWSER_SESSION_INPUT_{provider.upper()}')
        if restored and Path(restored).is_file():
            try:
                previous_expiry = cookie_expiry(validate_payload(json.loads(Path(restored).read_text()), provider, allow_bootstrap=True))
            except Exception:
                pass
        return {'status': 'checkpointed', 'validated_at': stamp, 'cookie_expires_at': expiry,
                'cookie_expiry_extended': bool(expiry and previous_expiry and _timestamp(expiry) > _timestamp(previous_expiry))}
    except SessionError as exc:
        reason = str(exc)
        safe_reasons = {'session_storage_conflict', 'verified_page_unavailable', 'verified_page_origin_invalid',
                        'session_storage_origin_changed', 'invalid_session_storage'}
        return {'status': 'error', 'reason': reason if reason in safe_reasons else 'checkpoint_write_failed'}
    except Exception:
        return {'status': 'error', 'reason': 'checkpoint_write_failed'}


def trusted_run(run, repository_id):
    return (run.get('repository', {}).get('id') == repository_id
            and run.get('head_repository', {}).get('id') == repository_id
            and run.get('head_branch') == 'main'
            and run.get('path', '').split('@')[0] in TRUSTED_WORKFLOWS
            and run.get('event') in ('schedule', 'workflow_dispatch', 'workflow_call'))


class ArtifactStore:
    def __init__(self, repository, token):
        if not repository or repository.count('/') != 1 or not token:
            raise SessionError('artifact_access_not_configured')
        self.repository = repository
        self.http = requests.Session()
        self.http.headers.update({'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                  'X-GitHub-Api-Version': '2022-11-28'})
        self.base = f'https://api.github.com/repos/{repository}'

    def _get(self, suffix, **kwargs):
        for attempt in range(3):
            try:
                response = self.http.get(self.base + suffix, timeout=25, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == 2:
                    raise SessionError('artifact_request_failed') from None
                time.sleep(2 ** attempt)
        raise SessionError('artifact_request_failed')

    def candidates(self, provider):
        repository_id = self._get('').json()['id']
        found = []
        for page in range(1, 5):
            rows = self._get('/actions/artifacts', params={'name': f'browser-session-v1-{provider}', 'per_page': 100, 'page': page}).json()['artifacts']
            found.extend(row for row in rows if not row.get('expired'))
            if len(rows) < 100:
                break
        # Do not require a successful conclusion: another provider may have failed.
        for row in sorted(found, key=lambda r: r.get('created_at', ''), reverse=True)[:10]:
            run_id = row.get('workflow_run', {}).get('id')
            if not isinstance(run_id, int):
                continue
            run = self._get(f'/actions/runs/{run_id}').json()
            if trusted_run(run, repository_id):
                yield row

    def download(self, artifact, provider):
        if artifact.get('size_in_bytes', 0) > MAX_BYTES * 2:
            raise SessionError('artifact_too_large')
        response = self._get(f'/actions/artifacts/{int(artifact["id"])}/zip')
        if len(response.content) > MAX_BYTES * 2:
            raise SessionError('artifact_too_large')
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                names = archive.namelist()
                expected = f'{provider}.enc.json'
                if names != [expected] or archive.getinfo(expected).file_size > MAX_BYTES * 2:
                    raise SessionError('invalid_session_artifact')
                return json.loads(archive.read(expected))
        except SessionError:
            raise
        except Exception:
            raise SessionError('invalid_session_artifact') from None


def restore_files(destination, store=None, bootstrap=''):
    destination = Path(destination)
    summary = {}
    bootstrap_data = {}
    if bootstrap:
        try:
            bootstrap_data = json.loads(base64.b64decode(bootstrap, validate=True))
            if not isinstance(bootstrap_data, dict) or set(bootstrap_data) - set(PROVIDERS):
                raise ValueError()
        except Exception:
            raise SessionError('invalid_bootstrap_package') from None
    for provider in PROVIDERS:
        payload = None
        reason = 'no_saved_session'
        # An explicit newly authorized seed takes precedence over old checkpoints.
        if provider in bootstrap_data:
            payload = decrypt_payload(bootstrap_data[provider], provider, allow_bootstrap=True)
            reason = 'bootstrap_loaded'
        elif store:
            try:
                for artifact in store.candidates(provider):
                    try:
                        envelope = store.download(artifact, provider)
                        keys = [os.environ.get('BROWSER_SESSION_KEY')]
                        if os.environ.get('BROWSER_SESSION_PREVIOUS_KEY'):
                            keys.append(os.environ['BROWSER_SESSION_PREVIOUS_KEY'])
                        for key in keys:
                            try:
                                candidate = decrypt_payload(envelope, provider, key=key)
                                if payload is None or _timestamp(candidate['validated_at']) > _timestamp(payload['validated_at']):
                                    payload = candidate
                                break
                            except SessionError:
                                continue
                    except SessionError:
                        continue
                reason = 'confirmed_checkpoint_loaded' if payload else 'no_valid_saved_session'
            except SessionError:
                reason = 'artifact_restore_unavailable'
        if payload:
            private_write(destination / f'{provider}.json', payload)
        summary[provider] = {'status': 'restored' if payload else 'missing', 'reason': reason,
                             'validated_at': payload.get('validated_at') if payload else None}
    return summary


def maintain(stage, reports):
    """Run auth-only collectors, keep reports outside the publication candidate."""
    stage, reports = Path(stage), Path(reports)
    reports.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'BROWSER_SESSION_MAINTENANCE': '1', 'BROWSER_SESSION_REPORT_DIR': str(reports)}
    summary = {}
    for provider, script in [('elibrary', 'harvest_elibrary_browser.py'), ('wos', 'harvest_wos_authenticated.py')]:
        attempted = now()
        try:
            result = subprocess.run([sys.executable, str(stage / 'scripts' / script)], cwd=stage, env=env,
                                    capture_output=True, timeout=600)
            code = result.returncode
            verified = False
            try:
                report = json.loads((reports / f'{provider}.json').read_text(encoding='utf-8'))
                verified = (report.get('status') == 'success' and report.get('target_verified') is True
                            and report.get('session_checkpoint', {}).get('status') == 'checkpointed'
                            and _timestamp(report.get('attempted_at')) >= _timestamp(attempted)
                            and _timestamp(report['session_checkpoint'].get('validated_at')) >= _timestamp(attempted))
            except Exception:
                pass
            summary[provider] = {'status': 'success' if code == 0 and verified else 'error', 'exit_code': code,
                                 'reason': 'confirmed_session_checkpoint' if code == 0 and verified else 'maintenance_not_confirmed'}
        except subprocess.TimeoutExpired:
            summary[provider] = {'status': 'error', 'reason': 'maintenance_timeout'}
    private_write(reports / 'maintenance.json', {'attempted_at': now(), 'providers': summary})
    print(json.dumps(summary))
    return 0 if all(row['status'] == 'success' for row in summary.values()) else 2


def export_diagnostics(source, destination):
    """Export metadata only: neither arbitrary filenames nor browser state."""
    from report_safety import sanitize
    from provider_auth import safe_wos_login_evidence
    allowed = {'provider', 'attempted_at', 'status', 'reason', 'stage', 'authentication', 'authentication_mode',
               'validated_at', 'cookie_expires_at', 'cookie_expiry_extended', 'target_verified',
               'session_restore', 'session_checkpoint', 'checkpoint', 'restoration', 'kind',
               'providers', 'elibrary', 'wos', 'exit_code', 'origin', 'complete', 'schema',
               'authentication_evidence', 'login_confirmed', 'target_author_id', 'target_researcher_id',
               'profile_diagnostics', 'profile_entry_observation', 'verification_evidence'}

    def observation_fields(value):
        """Fixed trace vocabulary only; never copy frame text, paths or tokens."""
        if not isinstance(value, dict):
            return {}
        result = {}
        enums = {
            'settling': {'cleared', 'timed_out', 'stopped_by_guard'},
            'trigger': {'page_marker', 'iframe_title', 'recaptcha_normal_widget',
                        'observation_incomplete', 'observation_deadline'},
            'error_type': {'timeout', 'navigation', 'closed', 'unexpected'},
            'read_phase': {'page_text', 'validation_errors', 'frame_list', 'frame_visibility',
                           'frame_evidence', 'observation', 'passive_wait'},
        }
        for key, choices in enums.items():
            if isinstance(value.get(key), str) and value[key] in choices:
                result[key] = value[key]
        for key in ('elapsed_seconds', 'observation_budget_seconds'):
            item = value.get(key)
            if type(item) in (int, float) and math.isfinite(item) and item >= 0:
                result[key] = item
        markers = value.get('marker_ids')
        if isinstance(markers, list):
            allowed_markers = {'turing_test_ru', 'verify_you_are_human', 'verify_that_you_are_human',
                               'unusual_activity', 'challenge_expired', 'not_robot_ru', 'page_captcha_url'}
            result['marker_ids'] = [item for item in markers[:16] if isinstance(item, str) and item in allowed_markers]
        timeline = value.get('observation_timeline')
        if isinstance(timeline, list):
            samples = []
            # Keep the terminal observation as the producer does when capped.
            bounded = timeline[:15] + timeline[-1:] if len(timeline) > 16 else timeline
            for row in bounded:
                if not isinstance(row, dict):
                    continue
                sample = {}
                for key, choices in (
                    ('category', {'clear', 'passive', 'marker', 'interactive', 'incomplete', 'blocked', 'timed_out'}),
                    ('ready_state', {'loading', 'interactive', 'complete', 'unavailable'}),
                    ('error_type', enums['error_type']),
                    ('read_phase', enums['read_phase']),
                ):
                    if isinstance(row.get(key), str) and row[key] in choices:
                        sample[key] = row[key]
                elapsed = row.get('elapsed_seconds')
                if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                    sample['elapsed_seconds'] = elapsed
                for key in ('frame_dom_observed', 'checkbox_present', 'checkbox_visible',
                            'active_challenge_controls', 'observation_incomplete'):
                    if key in row and (row[key] is None or (type(row[key]) is int and row[key] in (0, 1))):
                        sample[key] = row[key]
                for key in ('frame_text_length', 'frame_tag_count', 'frame_button_count',
                            'frame_role_button_count', 'frame_input_count', 'frame_canvas_count',
                            'hcaptcha_frame_count', 'recaptcha_frame_count', 'other_frame_count'):
                    if key in row and (row[key] is None or (type(row[key]) is int and 0 <= row[key] <= 1000000)):
                        sample[key] = row[key]
                if sample:
                    samples.append(sample)
            result['observation_timeline'] = samples
        return result

    def profile_fields(value):
        if not isinstance(value, dict):
            return {}
        result = {}
        for name in ('parsed_record_count', 'summary_metric_count', 'core_metric_count'):
            if type(value.get(name)) is int:
                result[name] = value[name]
        if isinstance(value.get('parser_failed'), bool):
            result['parser_failed'] = value['parser_failed']
        summary = value.get('summary')
        if isinstance(summary, dict):
            result['summary'] = {name: summary[name] for name in (
                'publications', 'citations', 'h_index', 'total_documents', 'indexed_publications', 'core_collection_publications')
                if name in summary and (summary[name] is None or (type(summary[name]) in (int, float) and math.isfinite(summary[name])))}
        for name in ('schema_fields', 'record_fields'):
            values = value.get(name)
            if isinstance(values, list):
                result[name] = [field for field in values[:64] if isinstance(field, str) and re.fullmatch(r'[a-z][a-z_]{0,60}', field)]
        return result

    def keep(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in {'profile_entry_observation', 'verification_evidence'}:
                    result[key] = observation_fields(item)
                elif key == 'profile_diagnostics':
                    result[key] = profile_fields(item)
                elif key == 'authentication_evidence':
                    result[key] = safe_wos_login_evidence(item)
                elif key in allowed:
                    result[key] = keep(item)
            return result
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    for name in ('restore.json', 'maintenance.json', 'elibrary.json', 'wos.json'):
        path = Path(source) / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                private_write(Path(destination) / name, sanitize(keep(data)))
            except Exception:
                private_write(Path(destination) / name, {'status': 'error', 'reason': 'invalid_session_diagnostics'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--destination', required=True)
    restore.add_argument('--report', required=True)
    seed = commands.add_parser('encrypt-bootstrap')
    seed.add_argument('--provider', choices=PROVIDERS, required=True)
    seed.add_argument('--input', required=True, help='Private JSON storage_state file; values are never printed.')
    seed.add_argument('--output', required=True)
    pack = commands.add_parser('pack-bootstrap')
    pack.add_argument('--elibrary')
    pack.add_argument('--wos')
    pack.add_argument('--output', required=True)
    maintenance = commands.add_parser('maintain')
    maintenance.add_argument('--stage', required=True)
    maintenance.add_argument('--reports', required=True)
    diagnostics = commands.add_parser('diagnostics')
    diagnostics.add_argument('--source', required=True)
    diagnostics.add_argument('--destination', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'restore':
            _key()  # Misconfiguration must remain visible, not silently disable persistence.
            store = ArtifactStore(os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GH_TOKEN'))
            result = restore_files(args.destination, store, os.environ.get('BROWSER_SESSION_BOOTSTRAP', ''))
            private_write(args.report, {'attempted_at': now(), 'providers': result})
            print(json.dumps(result))
        elif args.command == 'encrypt-bootstrap':
            state = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
            payload = {'schema': SCHEMA, 'repository': repository_for(), 'kind': 'bootstrap', 'provider': args.provider,
                       'target_id': target_for(args.provider), 'created_at': now(),
                       'storage_state': state, 'session_storage': {}}
            private_write(args.output, encrypt_payload(payload))
            print('Encrypted bootstrap written; session values were not printed.')
        elif args.command == 'pack-bootstrap':
            package = {p: json.loads(Path(getattr(args, p)).read_text()) for p in PROVIDERS if getattr(args, p)}
            encoded = base64.b64encode(_json(package)).decode()
            if not package or len(encoded) > 60000:
                raise SessionError('bootstrap_input_size_invalid')
            Path(args.output).write_text(encoded, encoding='ascii')
            print('Encrypted dispatch input written.')
        elif args.command == 'diagnostics':
            export_diagnostics(args.source, args.destination)
        else:
            return maintain(args.stage, args.reports)
    except SessionError as exc:
        print(f'Browser session operation failed: {exc}', file=sys.stderr)
        return 2
    except Exception:
        print('Browser session operation failed: internal_error', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
