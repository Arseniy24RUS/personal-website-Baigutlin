"""Normal interactive website login using a fresh Playwright browser context.

Secrets are filled only into the official provider form. No session, HTML,
request URL, exception text or screenshot is written to public diagnostics.
Verification challenges are reported, never solved or bypassed.
"""
from __future__ import annotations

import os
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse


class AuthFailure(RuntimeError):
    def __init__(self, reason, authentication_evidence=None, verification_evidence=None):
        self.reason = reason
        self.authentication_evidence = authentication_evidence
        self.verification_evidence = verification_evidence
        super().__init__(reason)


def browser_initialization_diagnostics(exc):
    """Classify launch logs privately; publish only fixed allowlisted markers."""
    message = str(exc).lower()
    patterns = {
        'display_unavailable': ('missing x server', 'cannot open display', 'failed to open display', 'unable to open x display'),
        'crashpad_initialization_failed': ('crashpad', 'crash_report_database'),
        'filesystem_permission_denied': ('permission denied', 'eacces', 'access is denied'),
        'browser_executable_missing': ("executable doesn't exist", 'executable not found', 'please run the following command to download new browsers'),
        'browser_library_missing': ('error while loading shared libraries', 'host system is missing dependencies'),
        'browser_sandbox_failed': ('no usable sandbox', 'failed to move to new namespace', 'running as root without --no-sandbox'),
        'browser_process_crashed': ('signal=sigtrap', 'signal=sigsegv', 'trace/breakpoint trap', 'segmentation fault'),
        'browser_process_closed': ('target page, context or browser has been closed', 'target closed'),
        'storage_full': ('no space left on device',),
    }
    signals = [label for label, needles in patterns.items() if any(needle in message for needle in needles)]
    if isinstance(exc, PermissionError) and 'filesystem_permission_denied' not in signals:
        signals.append('filesystem_permission_denied')
    if isinstance(exc, ModuleNotFoundError):
        signals.append('python_dependency_missing')
    priorities = ['display_unavailable', 'filesystem_permission_denied', 'crashpad_initialization_failed', 'browser_executable_missing', 'browser_library_missing', 'browser_sandbox_failed', 'storage_full', 'python_dependency_missing', 'browser_process_crashed', 'browser_process_closed']
    reason = next((label for label in priorities if label in signals), 'browser_initialization_failed')

    def accessible(key, mode):
        path = os.environ.get(key)
        return bool(path and Path(path).is_dir() and os.access(path, mode))

    return {
        'reason': reason,
        'signals': signals,
        'error_type': type(exc).__name__ if type(exc).__name__ in {'TargetClosedError', 'Error', 'TimeoutError', 'PermissionError', 'FileNotFoundError', 'ModuleNotFoundError', 'OSError'} else 'OtherError',
        'environment': {
            'display_configured': bool(os.environ.get('DISPLAY')),
            'home_writable': accessible('HOME', os.W_OK | os.X_OK),
            'runtime_writable': accessible('RUNNER_TEMP', os.W_OK | os.X_OK),
            'xdg_config_writable': accessible('XDG_CONFIG_HOME', os.W_OK | os.X_OK),
            'xdg_cache_writable': accessible('XDG_CACHE_HOME', os.W_OK | os.X_OK),
            'browser_store_readable': accessible('PLAYWRIGHT_BROWSERS_PATH', os.R_OK | os.X_OK),
        },
    }


def safe_browser_diagnostics(context):
    """Allowlist visible form structure, never input values, body or URL queries."""
    output = []
    secrets = [os.environ.get(key, '') for key in ('ELIBRARY_USERNAME', 'ELIBRARY_PASSWORD', 'WOS_ORCID_USERNAME', 'WOS_ORCID_PASSWORD')]

    def clean(value):
        text = str(value or '')[:120]
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return re.sub(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', '[REDACTED]', text, flags=re.I)

    for page in context.pages[-3:]:
        try:
            address = urlparse(page.url)
            controls = page.eval_on_selector_all('input,button,a,[role="button"]', """els => els.filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)).slice(0,60).map(el => ({tag:el.tagName.toLowerCase(),type:el.getAttribute('type')||'',name:el.getAttribute('name')||'',id:el.id||'',label:el.tagName==='INPUT' ? (el.getAttribute('aria-label')||el.getAttribute('placeholder')||'') : (el.innerText||el.getAttribute('aria-label')||el.title||'').trim().slice(0,120)}))""")
            output.append({'host': clean(address.hostname), 'path': clean(address.path), 'controls': [{key: clean(value) for key, value in control.items()} for control in controls]})
        except Exception:
            continue
    return output


def diagnostic_login(function):
    def wrapped(context, *args, **kwargs):
        try:
            return function(context, *args, **kwargs)
        except Exception as exc:
            failure = exc if isinstance(exc, AuthFailure) else AuthFailure(type(exc).__name__)
            failure.diagnostics = safe_browser_diagnostics(context)
            raise failure from None
    return wrapped


def page_text(page, *, timeout=10000):
    return page.locator('body').inner_text(timeout=timeout)


def in_visible_viewport(locator, *, timeout=None):
    """Playwright is_visible also accepts offscreen and transparent elements."""
    return locator.evaluate("""el => {
        const box = el.getBoundingClientRect();
        let left = Math.max(0, box.left), top = Math.max(0, box.top);
        let right = Math.min(innerWidth, box.right), bottom = Math.min(innerHeight, box.bottom);
        for (let node = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
            if (node !== el) {
                const clip = node.getBoundingClientRect();
                if (/hidden|clip|scroll|auto/.test(style.overflowX)) {
                    left = Math.max(left, clip.left); right = Math.min(right, clip.right);
                }
                if (/hidden|clip|scroll|auto/.test(style.overflowY)) {
                    top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
                }
            }
        }
        return right > left && bottom > top;
    }""", timeout=timeout)


HUMAN_MARKERS = {
    'turing_test_ru': 'тест тьюринга',
    'verify_you_are_human': 'verify you are human',
    'verify_that_you_are_human': 'verify that you are human',
    'unusual_activity': 'unusual activity',
    'challenge_expired': 'challenge has expired',
    'not_robot_ru': 'проверка, что вы не робот',
}

# A known hCaptcha frame may briefly display automatic verification before
# disappearing. This is only an observation budget, never challenge interaction.
CHALLENGE_SETTLE_SECONDS = 10.0
CHALLENGE_POLL_SECONDS = 0.25
CHALLENGE_TIMELINE_SECONDS = (0, 2, 5, 10, 20, 40, 60)
CHALLENGE_TIMELINE_LIMIT = 16


class _ChallengeObservationTimeout(RuntimeError):
    pass


class _ChallengeObservationReadError(RuntimeError):
    def __init__(self, error_type, read_phase, pending=None):
        # Keep only fixed categories, never a provider URL or exception message.
        super().__init__(error_type)
        self.error_type = error_type
        self.read_phase = read_phase
        self.pending = pending
        self.retryable = error_type in {'timeout', 'navigation'}


def _observation_error_type(exc):
    playwright_error = type(exc).__module__.startswith('playwright.')
    if isinstance(exc, TimeoutError) or (playwright_error and type(exc).__name__ == 'TimeoutError'):
        return 'timeout'
    if playwright_error:
        if type(exc).__name__ == 'TargetClosedError':
            return 'closed'
        message = str(exc).lower()
        if 'target page, context or browser has been closed' in message:
            return 'closed'
        if any(marker in message for marker in (
            'execution context was destroyed, most likely because of a navigation',
            'cannot find context with specified id',
            'frame was detached',
            'element is not attached to the dom',
        )):
            return 'navigation'
    return 'unexpected'


def _observation_timeout(deadline):
    """Bound locator auto-waiting and check the shared observation deadline."""
    remaining = 1.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise _ChallengeObservationTimeout()
    return min(1000.0, remaining * 1000)


def human_marker_ids(text, url=''):
    markers = [key for key, value in HUMAN_MARKERS.items() if value in str(text).lower()]
    if 'page_captcha' in url:
        markers.append('page_captcha_url')
    return markers


def challenge_frame_evidence(locator, *, deadline=None):
    """Read geometry and fixed challenge signals; never emit frame text/values."""
    result = {'frame_dom_observed': False, 'checkbox_present': None, 'checkbox_visible': None, 'checkbox_checked': None, 'active_challenge_controls': None}
    try:
        # Only known static provider paths are retained. In particular, do not
        # export src query parameters, fragments, arbitrary paths or frame names.
        src = locator.get_attribute('src', timeout=_observation_timeout(deadline)) or ''
        address = urlparse(src)
        host = (address.hostname or '').lower()
        allowed = ('google.com', 'recaptcha.net', 'hcaptcha.com')
        result['provider_host'] = host if any(provider_host(host, domain) for domain in allowed) else 'other'
        result['provider_path'] = address.path if re.fullmatch(r'/recaptcha/(?:api2|enterprise)/(?:anchor|bframe)', address.path) else 'other'
        result['title_challenge'] = 'challenge' in (locator.get_attribute('title', timeout=_observation_timeout(deadline)) or '').lower()
        result['src_recaptcha'] = 'recaptcha' in src
        result['size_normal'] = 'size=normal' in src
        result['in_visible_viewport'] = in_visible_viewport(locator, timeout=_observation_timeout(deadline))
        result.update(locator.evaluate("""el => {
            const box = el.getBoundingClientRect();
            const number = value => Math.round(Math.max(-1000000, Math.min(1000000, value)));
            let opacity = 1, hidden = false, undisplayed = false, clipped = false, modal = false;
            for (let node = el; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                opacity *= Number(style.opacity);
                hidden ||= style.visibility !== 'visible';
                undisplayed ||= style.display === 'none';
                clipped ||= style.clipPath !== 'none' || style.clip !== 'auto';
                modal ||= node.getAttribute('aria-modal') === 'true' || node.getAttribute('role') === 'dialog' || node.tagName === 'DIALOG';
            }
            const left = Math.max(0, box.left), right = Math.min(innerWidth, box.right);
            const top = Math.max(0, box.top), bottom = Math.min(innerHeight, box.bottom);
            const intersects = right > left && bottom > top;
            const hit = intersects ? document.elementFromPoint((left + right) / 2, (top + bottom) / 2) : null;
            return {rect: {x:number(box.x), y:number(box.y), width:number(box.width), height:number(box.height)},
                viewport: {width:innerWidth, height:innerHeight}, effective_opacity: Math.round(opacity * 1000) / 1000,
                visibility_hidden:hidden, display_none:undisplayed, css_clip_present:clipped, ancestor_modal:modal,
                viewport_intersects:intersects, center_hit_iframe:hit === el};
        }""", timeout=_observation_timeout(deadline)))
        handle = locator.element_handle(timeout=_observation_timeout(deadline))
        frame = handle.content_frame() if handle else None
        if frame is not None:
            result['frame_dom_observed'] = True
            result['frame_host_matches_provider'] = (urlparse(frame.url).hostname or '').lower() == host
            checkboxes = frame.locator('#recaptcha-anchor, .recaptcha-checkbox, [role="checkbox"], input[type="checkbox"]')
            count = checkboxes.count()
            result['checkbox_present'] = count > 0
            result['checkbox_visible'] = any(in_visible_viewport(checkboxes.nth(i), timeout=_observation_timeout(deadline)) for i in range(min(count, 10)))
            result['checkbox_checked'] = any(checkboxes.nth(i).evaluate("el => el.checked === true || el.getAttribute('aria-checked') === 'true'", timeout=_observation_timeout(deadline)) for i in range(min(count, 10))) if count else None
            control_selector = '.rc-imageselect, #recaptcha-verify-button, #audio-response, .rc-audiochallenge-input, .hcaptcha-challenge'
            if result['provider_host'] != 'other' and result['frame_host_matches_provider']:
                # Provider markup changes; ordinary visible controls inside a
                # known challenge frame must never receive loading grace.
                control_selector += ', button, [role="button"]'
            controls = frame.locator(control_selector)
            control_count = controls.count()
            result['active_challenge_controls'] = any(in_visible_viewport(controls.nth(i), timeout=_observation_timeout(deadline)) for i in range(min(control_count, 10)))
            # A capped/incomplete scan must never qualify for settling grace.
            if count > 10 or control_count > 10:
                result['observation_incomplete'] = True
            body = frame.locator('body')
            if body.count() == 1:
                result['marker_ids'] = human_marker_ids(body.inner_text(timeout=_observation_timeout(deadline)), frame.url)
                result.update(body.evaluate("""el => {
                    const count = selector => Math.min(1000000, el.querySelectorAll(selector).length);
                    return {ready_state: el.ownerDocument.readyState,
                        frame_text_length: Math.min(1000000, (el.innerText || '').length),
                        frame_tag_count: count('*'), frame_button_count: count('button'),
                        frame_role_button_count: count('[role="button"]'),
                        frame_input_count: count('input'), frame_canvas_count: count('canvas')};
                }""", timeout=_observation_timeout(deadline)))
            else:
                result['observation_incomplete'] = True
    except _ChallengeObservationTimeout:
        raise
    except Exception:
        result['observation_incomplete'] = True
    return result


def challenge_reason(text, url=''):
    text = str(text).lower()
    if human_marker_ids(text, url):
        return 'human_verification_required'
    if any(x in text for x in ('two-factor', 'two factor', 'authentication code', 'verification code', 'одноразовый код', 'двухфактор')):
        return 'mfa_required'
    if any(x in text for x in ('link your account', 'link an existing account', 'associate your account')):
        return 'account_link_required'
    if any(x in text for x in ('неверный пароль', 'неверный логин', 'invalid username', 'incorrect password', 'incorrect email', 'bad username or password', 'invalid credentials', 'invalid sign in details', 'please check your orcid sign in details')):
        return 'invalid_credentials'
    if 'please enter a valid email address or orcid' in text:
        return 'invalid_username_format'
    if 'you will need to reactivate the account' in text:
        return 'account_reactivation_required'
    if 'ip_blocked' in url or 'заблокирован из-за нарушения' in text:
        return 'ip_blocked'
    return None


def _challenge_observation(page, *, form_submitted, deadline):
    """One read-only pass; return an error and whether it may settle naturally."""
    pending = None

    def read(phase, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except AuthFailure:
            raise
        except _ChallengeObservationTimeout:
            raise
        except Exception as exc:
            raise _ChallengeObservationReadError(_observation_error_type(exc), phase, pending) from None

    text = read('page_text', page_text, page, timeout=_observation_timeout(deadline))
    reason = challenge_reason(text, page.url)
    evidence = {'trigger': 'page_marker', 'marker_ids': human_marker_ids(text, page.url)} if reason == 'human_verification_required' else None
    if not form_submitted and reason in {'invalid_credentials', 'invalid_username_format'}:
        reason = None
    elif reason in {'invalid_credentials', 'invalid_username_format'} and provider_host(urlparse(page.url).hostname or '', 'orcid.org'):
        # ORCID's initial help text is not a server rejection. Its actual
        # validation messages live in mat-error / app-alert-message nodes.
        errors = page.locator('mat-error, app-alert-message, [role="alert"], #dialogTitle')
        reason = None
        for index in range(read('validation_errors', errors.count)):
            _observation_timeout(deadline)
            error = errors.nth(index)
            if read('validation_errors', error.is_visible):
                reason = challenge_reason(read('validation_errors', error.inner_text, timeout=_observation_timeout(deadline)))
                if reason:
                    break
    # An embedded challenge script alone is not a challenge. Only visible forms count.
    # The ubiquitous invisible reCAPTCHA badge is not an interactive challenge.
    selector = 'iframe[title*="challenge" i], iframe[src*="recaptcha"][src*="size=normal"]'
    if reason:
        return reason, evidence, False
    counts = {'hcaptcha_frame_count': 0, 'recaptcha_frame_count': 0, 'other_frame_count': 0}
    frames = page.locator(selector)
    for index in range(read('frame_list', frames.count)):
        frame = frames.nth(index)
        if not read('frame_visibility', in_visible_viewport, frame, timeout=_observation_timeout(deadline)):
            continue
        observed = read('frame_evidence', challenge_frame_evidence, frame, deadline=deadline)
        host = observed.get('provider_host', '')
        category = 'hcaptcha' if provider_host(host, 'hcaptcha.com') else 'recaptcha' if any(provider_host(host, domain) for domain in ('google.com', 'recaptcha.net')) else 'other'
        counts[f'{category}_frame_count'] += 1
        evidence = {'trigger': 'iframe_title' if observed.get('title_challenge') else 'recaptcha_normal_widget', 'frame': observed, 'frame_counts': dict(counts)}
        loading = (
            provider_host(observed.get('provider_host', ''), 'hcaptcha.com')
            and observed.get('title_challenge') is True
            and observed.get('frame_dom_observed') is True
            and observed.get('frame_host_matches_provider') is True
            and observed.get('checkbox_present') is False
            and observed.get('checkbox_visible') is False
            and observed.get('active_challenge_controls') is False
            and observed.get('marker_ids') == []
            and not observed.get('observation_incomplete')
        )
        if not loading:
            return 'human_verification_required', evidence, False
        # Inspect every other visible frame before waiting: an interactive
        # challenge must fail immediately even beside an automatic loader.
        pending = pending or evidence
    if pending:
        pending['frame_counts'] = counts
    return ('human_verification_required', pending, True) if pending else (None, None, False)


def _verification_sample(elapsed, reason, evidence, may_settle, *, terminal=None):
    """Fixed categories and numeric observations; never copy arbitrary strings."""
    evidence = evidence or {}
    frame = evidence.get('frame') or {}
    if terminal:
        category = terminal
    elif not reason:
        category = 'clear'
    elif may_settle:
        category = 'passive'
    elif evidence.get('trigger') == 'page_marker' or frame.get('marker_ids'):
        category = 'marker'
    elif frame.get('checkbox_present') or frame.get('active_challenge_controls'):
        category = 'interactive'
    elif frame.get('observation_incomplete') or reason == 'challenge_observation_incomplete':
        category = 'incomplete'
    else:
        category = 'blocked'
    sample = {'elapsed_seconds': round(max(0, elapsed), 3), 'category': category,
              'ready_state': frame.get('ready_state') if frame.get('ready_state') in {'loading', 'interactive', 'complete'} else 'unavailable'}
    numeric = ('frame_text_length', 'frame_tag_count', 'frame_button_count', 'frame_role_button_count', 'frame_input_count', 'frame_canvas_count')
    for key in numeric:
        value = frame.get(key)
        sample[key] = min(1000000, value) if type(value) in (int, float) and 0 <= value <= 1000000 else None
    for key in ('frame_dom_observed', 'checkbox_present', 'checkbox_visible', 'active_challenge_controls', 'observation_incomplete'):
        sample[key] = int(frame[key]) if type(frame.get(key)) is bool else None
    for key in ('hcaptcha_frame_count', 'recaptcha_frame_count', 'other_frame_count'):
        value = 0 if not reason else (evidence.get('frame_counts') or {}).get(key)
        sample[key] = value if type(value) is int and 0 <= value <= 1000000 else None
    if evidence.get('error_type') in {'timeout', 'navigation', 'closed', 'unexpected'}:
        sample['error_type'] = evidence['error_type']
    if evidence.get('read_phase') in {'page_text', 'validation_errors', 'frame_list', 'frame_visibility', 'frame_evidence', 'observation', 'passive_wait'}:
        sample['read_phase'] = evidence['read_phase']
    return sample


def assert_no_challenge(page, *, form_submitted=True, passive_wait_seconds=10.0, passive_deadline=None):
    """Allow only bounded, passive observation of known automatic verification.

    A single deadline covers every frame and every observation in this call.
    Disappearance is readiness only; callers must still prove authentication.
    """
    if type(passive_wait_seconds) not in (int, float) or not 0 < passive_wait_seconds <= 60:
        raise AuthFailure('challenge_observation_budget_invalid')
    started = time.monotonic()
    deadline = started + passive_wait_seconds
    if passive_deadline is not None:
        if type(passive_deadline) not in (int, float) or not math.isfinite(passive_deadline):
            raise AuthFailure('challenge_observation_budget_invalid')
        deadline = min(deadline, passive_deadline)
    timeline = []
    next_sample = 0
    previous_category = None
    pending = None
    retried_read = False

    def observe(reason, evidence, may_settle, *, terminal=None):
        nonlocal next_sample, previous_category
        elapsed = max(0, time.monotonic() - started)
        sample = _verification_sample(elapsed, reason, evidence, may_settle, terminal=terminal)
        signature = (sample['category'], sample['ready_state'], sample.get('error_type'), sample.get('read_phase'))
        due = next_sample < len(CHALLENGE_TIMELINE_SECONDS) and elapsed >= CHALLENGE_TIMELINE_SECONDS[next_sample]
        if terminal or due or signature != previous_category:
            if len(timeline) < CHALLENGE_TIMELINE_LIMIT:
                timeline.append(sample)
            else:
                timeline[-1] = sample
        while next_sample < len(CHALLENGE_TIMELINE_SECONDS) and elapsed >= CHALLENGE_TIMELINE_SECONDS[next_sample]:
            next_sample += 1
        previous_category = signature
        return {'elapsed_seconds': round(elapsed, 3), 'observation_budget_seconds': round(max(0, deadline - started), 3), 'observation_timeline': list(timeline)}

    while True:
        read_failure = False
        try:
            reason, evidence, may_settle = _challenge_observation(page, form_submitted=form_submitted, deadline=deadline)
        except AuthFailure:
            raise
        except _ChallengeObservationTimeout:
            reason, may_settle, read_failure = 'challenge_observation_incomplete', True, True
            evidence = {'trigger': 'observation_deadline', 'error_type': 'timeout', 'read_phase': 'observation'}
        except Exception as exc:
            error = exc if isinstance(exc, _ChallengeObservationReadError) else _ChallengeObservationReadError(
                _observation_error_type(exc), 'observation')
            if error.pending is not None:
                pending = error.pending
            evidence = {'trigger': 'observation_incomplete', 'error_type': error.error_type,
                        'read_phase': error.read_phase}
            if not error.retryable:
                trace = observe('challenge_observation_incomplete', evidence, False, terminal='incomplete')
                raise AuthFailure('challenge_observation_incomplete', verification_evidence={**evidence, **trace}) from None
            reason, may_settle, read_failure = 'challenge_observation_incomplete', True, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure_reason = (('human_verification_required' if pending else 'challenge_observation_incomplete')
                              if read_failure else reason or ('human_verification_required' if pending else 'challenge_observation_incomplete'))
            observed = pending if read_failure and pending else evidence or pending or {'trigger': 'observation_deadline'}
            if read_failure:
                observed = {**observed, **{key: evidence[key] for key in ('error_type', 'read_phase') if key in evidence}}
            trace = observe(failure_reason, observed, False, terminal='timed_out')
            raise AuthFailure(failure_reason, verification_evidence={**observed, **trace, 'settling': 'timed_out'}) from None
        if not reason:
            if pending or retried_read:
                return {'settling': 'cleared', **observe(None, None, False, terminal='clear')}
            return
        if not may_settle:
            evidence = {**(evidence or {}), **observe(reason, evidence, False)}
            if pending is not None and evidence is not None:
                evidence = {**evidence, 'settling': 'stopped_by_guard'}
            raise AuthFailure(reason, verification_evidence=evidence)
        observe(reason, evidence, not read_failure)
        if read_failure:
            retried_read = True
        else:
            pending = evidence
        # No click, submission, navigation, token mutation or challenge solving.
        try:
            page.wait_for_timeout(min(CHALLENGE_POLL_SECONDS, remaining) * 1000)
        except Exception as exc:
            evidence = {'trigger': 'observation_incomplete', 'error_type': _observation_error_type(exc),
                        'read_phase': 'passive_wait'}
            trace = observe('challenge_observation_incomplete', evidence, False, terminal='incomplete')
            raise AuthFailure('challenge_observation_incomplete', verification_evidence={**evidence, **trace}) from None


def verify_browser_egress(context):
    if os.environ.get('HOME_VPN_REQUIRED') != '1':
        return
    try:
        expected = Path(os.environ['HOME_VPN_EXPECTED_IP_FILE']).read_text().strip()
        page = context.new_page()
        try:
            response = page.goto('https://api.ipify.org?format=json', wait_until='domcontentloaded', timeout=30000)
            observed = response.json().get('ip') if response else None
        finally:
            page.close()
    except Exception:
        raise AuthFailure('vpn_verification_failed') from None
    if not expected or observed != expected:
        raise AuthFailure('vpn_route_mismatch')


def visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                return item
    return None


def click_named(page, pattern):
    for role in ('button', 'link'):
        locator = page.get_by_role(role, name=re.compile(pattern, re.I))
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible() and item.is_enabled():
                item.click(timeout=15000)
                return True
    return False


def wait_navigation(page):
    try:
        page.wait_for_load_state('domcontentloaded', timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1000)
    except Exception:
        if not page.is_closed():
            raise


def elibrary_authenticated(page):
    text = page_text(page)
    session = page.locator('#win_session')
    session_text = session.text_content() if session.count() else ''
    anonymous = re.search(r'Незарегистрированный пользователь|Вы не авторизованы', text + (session_text or ''), re.I)
    named_session = bool(re.search(r'Имя пользователя:\s*\S', session_text or ''))
    return not anonymous and (named_session or bool(re.search(r'\b(?:Выход|Выйти)\b', text, re.I)))


@diagnostic_login
def login_elibrary(context):
    username = os.environ.get('ELIBRARY_USERNAME', '')
    password = os.environ.get('ELIBRARY_PASSWORD', '')
    if not username or not password:
        raise AuthFailure('credentials_missing')
    page = context.new_page()
    page.goto('https://elibrary.ru/defaultx.asp', wait_until='domcontentloaded', timeout=90000)
    wait_navigation(page)
    assert_no_challenge(page)
    user = visible(page, ['input[name="login"]', '#login', 'input[autocomplete="username"]'])
    if user is None:
        toggle = visible(page, ['span[title*="Вход в библиотеку"]'])
        if toggle is not None:
            toggle.click()
            page.wait_for_timeout(600)
        user = visible(page, ['input[name="login"]', '#login'])
    secret = visible(page, ['input[name="password"]', 'input[type="password"]'])
    if user is None or secret is None:
        raise AuthFailure('login_form_changed')
    if urlparse(page.url).hostname not in {'elibrary.ru', 'www.elibrary.ru'}:
        raise AuthFailure('unexpected_login_origin')
    user.fill(username)
    secret.fill(password)
    if not click_named(page, r'^Вход$|^Войти$|^Login$|^Sign in$'):
        submit = visible(page, ['[onclick="check_all()"]', 'input[type="submit"]', 'input[type="image"][alt*="Вход"]', '[onclick*="login()"]'])
        if submit is None:
            raise AuthFailure('login_submit_changed')
        submit.click()
    for _ in range(30):
        wait_navigation(page)
        assert_no_challenge(page)
        if elibrary_authenticated(page):
            return page
    raise AuthFailure('login_not_confirmed')


def wos_identity_configuration():
    """The authorized login and the collected researcher can be different people."""
    import yaml
    config = Path(__file__).resolve().parents[1] / 'config' / 'profile.yml'
    try:
        profile = (yaml.safe_load(config.read_text(encoding='utf-8')) or {}).get('profile', {})
    except (OSError, ValueError, yaml.YAMLError):
        return {}, {}
    return profile, (profile.get('authentication') or {}).get('wos') or {}


def wos_account_names():
    """Account labels are login evidence, never collected-author identity."""
    profile, authentication = wos_identity_configuration()
    configured = authentication.get('account_names')
    if isinstance(configured, list):
        names = {name.strip() for name in configured if isinstance(name, str)}
    else:
        names = {str(profile.get(key) or '').strip() for key in ('display_name_en', 'display_name_ru')}
        names.update(str(name).strip() for name in profile.get('aliases', []) if isinstance(name, str))
    # WoS commonly omits the middle initial displayed by the portfolio.
    names.update(re.sub(r'\s+[A-Za-zА-Яа-яЁё]\.\s+', ' ', name) for name in tuple(names))
    return {name for name in names if name}


def wos_cv_export_allowed(target):
    """WoS exports the login's own CV, so a shared account must use target DOM/API."""
    _, authentication = wos_identity_configuration()
    if authentication:
        return authentication.get('researcher_id') == str(target)
    return True


def wos_logout_visible(page):
    # Guest WoS menus also expose "End session". Only an explicit sign-out
    # action is evidence of account authentication, never that bare label.
    pattern = re.compile(r'^\s*(?:(?:logout|exit_to_app)\s+)?(?:Sign out|Log out|Выйти|Выход|Завершить сеанс и выйти)\s*$', re.I)
    for role in ('button', 'link', 'menuitem'):
        controls = page.get_by_role(role, name=pattern)
        if any(controls.nth(index).is_visible() for index in range(controls.count())):
            return True
    bare_session_end = re.compile(r'^\s*(?:(?:logout|exit_to_app)\s+)?(?:End session|Завершить сеанс)\s*$', re.I)
    links = page.locator('a[href*="signout"], a[href*="logout"]')
    for index in range(links.count()):
        link = links.nth(index)
        if link.is_visible():
            labels = (link.inner_text(timeout=1000).strip(), (link.get_attribute('aria-label') or '').strip())
            if not any(bare_session_end.fullmatch(label) for label in labels):
                return True
    return False


def _is_playwright_timeout(exc):
    return type(exc).__name__ == 'TimeoutError' and type(exc).__module__.startswith('playwright.')


WOS_INTRO_MODAL_SELECTOR = ':is([role="dialog"], dialog, mat-dialog-container, .mat-mdc-dialog-container, .mat-dialog-container):not(#onetrust-banner-sdk):not(#onetrust-banner-sdk *)'


def _wos_intro_modal_visible(page):
    dialogs = page.locator(WOS_INTRO_MODAL_SELECTOR)
    return any(dialogs.nth(index).is_visible() for index in range(dialogs.count()))


def dismiss_wos_cookie_banner(page, evidence):
    """Use only the observed OneTrust consent controls, never a generic Close."""
    assert_no_challenge(page)
    if _wos_intro_modal_visible(page):
        return False
    control = visible(page, ['#onetrust-accept-btn-handler'])
    if control is None:
        return False
    evidence['cookie_banner_observed'] = True
    if not control.is_enabled():
        return False
    control.click(timeout=10000)
    assert_no_challenge(page)
    control.wait_for(state='hidden', timeout=10000)
    assert_no_challenge(page)
    evidence['cookie_banner_dismissed'] = True
    evidence['consent_accepted'] = True
    return True


def prepare_wos_profile_login(page, evidence, deadline):
    """Wait for the rendered profile UI and use its two explicit entry prompts."""
    ready_deadline = deadline
    stable_since = None
    while time.monotonic() < ready_deadline:
        remaining = ready_deadline - time.monotonic()
        assert_no_challenge(page, passive_wait_seconds=min(10.0, remaining), passive_deadline=ready_deadline)
        if not provider_host(urlparse(page.url).hostname or '', 'webofscience.com'):
            return
        loading = visible(page, ['[role="progressbar"]', '[aria-busy="true"]',
                                 'mat-spinner', 'mat-progress-spinner', '.mat-mdc-progress-spinner'])
        if loading is not None or page.evaluate('document.readyState') != 'complete':
            stable_since = None
            remaining = ready_deadline - time.monotonic()
            if remaining > 0:
                page.wait_for_timeout(min(250, remaining * 1000))
            continue
        modal_present = _wos_intro_modal_visible(page)
        controls = page.locator(WOS_INTRO_MODAL_SELECTOR + ' :is(button, [role="button"]):not(#onetrust-banner-sdk *)')
        modal_controls = [controls.nth(index) for index in range(controls.count()) if controls.nth(index).is_visible()]
        acknowledgement = controls.filter(has_text=re.compile(r'^\s*Got it!?\s*$', re.I))
        acknowledgements = [acknowledgement.nth(index) for index in range(acknowledgement.count())
                            if acknowledgement.nth(index).is_visible() and acknowledgement.nth(index).is_enabled()]
        if len(modal_controls) == len(acknowledgements) == 1:
            control = acknowledgements[0]
            assert_no_challenge(page)
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0:
                break
            control.click(timeout=min(10000, remaining * 1000))
            assert_no_challenge(page)
            control.wait_for(state='hidden', timeout=min(10000, max(1, (ready_deadline - time.monotonic()) * 1000)))
            assert_no_challenge(page)
            evidence['onboarding_acknowledged'] = True
            stable_since = None
            continue
        # Do not dismiss a cookie overlay over an unresolved onboarding dialog.
        if not modal_present and dismiss_wos_cookie_banner(page, evidence):
            stable_since = None
            continue
        modal_present = _wos_intro_modal_visible(page)
        account = visible(page, ['button[data-ta="wos-header-user_name"]', '[data-ta="user-menu"]',
                                 '[data-ta="user-menu-button"]', 'button[aria-label*="user menu" i]',
                                 'button[aria-label*="account menu" i]'])
        named_account = any(button.is_visible() for name in wos_account_names()
                            for button in page.get_by_role('button', name=name, exact=True).all())
        signin = any(control.is_visible() for role in ('button', 'link')
                     for control in page.get_by_role(role, name=re.compile(r'^\s*(?:Sign in|Войти)\s*$', re.I)).all())
        ready = not modal_present and (account is not None or named_account or signin or wos_logout_visible(page))
        if ready:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.75:
                assert_no_challenge(page)
                # The guard itself can span a late render. Recheck prompts at
                # this return boundary so they cannot be mistaken for ready UI.
                if (_wos_intro_modal_visible(page)
                        or visible(page, ['#onetrust-accept-btn-handler', '[role="progressbar"]', '[aria-busy="true"]',
                                          'mat-spinner', 'mat-progress-spinner', '.mat-mdc-progress-spinner']) is not None
                        or page.evaluate('document.readyState') != 'complete'):
                    stable_since = None
                    continue
                return
        else:
            stable_since = None
        remaining = ready_deadline - time.monotonic()
        if remaining > 0:
            page.wait_for_timeout(min(250, remaining * 1000))
    raise AuthFailure('wos_login_form_changed')


def _wos_target_profile_current(page, profile_url):
    try:
        current, target = urlparse(page.url), urlparse(profile_url)
        return (current.scheme == target.scheme == 'https'
                and current.hostname == target.hostname == 'www.webofscience.com'
                and current.port in (None, 443) and target.port in (None, 443)
                and current.username is None and current.password is None
                and target.username is None and target.password is None
                and current.path.rstrip('/') == target.path.rstrip('/'))
    except ValueError:
        return False


def _finish_wos_profile_login(page, profile_url, evidence, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AuthFailure('wos_login_not_confirmed')
    evidence.update(stage='profile_navigation', wos_session_confirmed=True)
    if _wos_target_profile_current(page, profile_url):
        evidence['returned_profile_reused'] = True
        return page
    evidence['profile_requested'] = True
    _goto_wos_login(page, profile_url, evidence, wait_until='domcontentloaded', timeout=min(90000, remaining * 1000))
    evidence['profile_loaded'] = True
    return page


def wos_authenticated(page):
    evidence = {'account_click_timed_out': False, 'cookie_banner_observed': False, 'cookie_banner_dismissed': False}
    try:
        return _wos_authenticated(page, evidence)
    except AuthFailure as failure:
        failure.authentication_evidence = {**(failure.authentication_evidence or {}), **evidence}
        raise
    except Exception as exc:
        if _is_playwright_timeout(exc):
            raise AuthFailure('TimeoutError', authentication_evidence=evidence) from None
        raise


def _wos_authenticated(page, evidence):
    # The user menu is positive session proof; SID existence is not.
    dismiss_wos_cookie_banner(page, evidence)
    if wos_logout_visible(page):
        return True

    def open_and_verify(account):
        assert_no_challenge(page)
        try:
            account.click(timeout=15000)
        except Exception as exc:
            if not _is_playwright_timeout(exc):
                raise
            evidence['account_click_timed_out'] = True
            # The banner can mount after the initial scan. One ordinary retry
            # is allowed only after a visible, explicit OneTrust consent action.
            if not dismiss_wos_cookie_banner(page, evidence):
                raise
            account.click(timeout=15000)
        try:
            for _ in range(10):
                if wos_logout_visible(page):
                    return True
                page.wait_for_timeout(200)
            return False
        finally:
            # Dismiss only a menu opened by this check, using the normal UI.
            # Leaving its modal backdrop open can block the next Export click.
            # An already-visible logout above belongs to the caller's UI state.
            if not page.is_closed():
                # A challenge appearing during menu rendering must remain open.
                assert_no_challenge(page)
                page.keyboard.press('Escape')

    account = visible(page, [
        'button[data-ta="wos-header-user_name"]',
        '[data-ta="user-menu"]', '[data-ta="user-menu-button"]',
        'button[aria-label*="user menu" i]', 'button[aria-label*="account menu" i]',
    ])
    if account is not None and account.is_enabled():
        return open_and_verify(account)
    # A matching public author label alone is insufficient: open only the
    # configured user's account button and confirm the session's logout action.
    for name in sorted(wos_account_names()):
        buttons = page.get_by_role('button', name=name, exact=True)
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible() or not button.is_enabled():
                continue
            return open_and_verify(button)
    return False


def provider_host(host, provider):
    return host == provider or host.endswith('.' + provider)


def normalize_orcid_username(value):
    """Undo the user's Markdown escape only; never transform the password."""
    return value.strip().replace('\\@', '@')


def valid_orcid_username(value):
    return bool(re.fullmatch(r'[^@\s\\]+@[^@\s\\]+\.[^@\s\\]+', value) or re.fullmatch(r'(?:\d{4}-){3}\d{3}[\dXx]|\d{15}[\dXx]', value))


def orcid_auth_response_evidence(status, payload):
    """ORCID's public SignIn interface: retain only status and boolean flags."""
    evidence = {'http_status': int(status), 'response_observed': True}
    if not 200 <= status < 300:
        evidence['reason'] = f'orcid_auth_http_{status}'
        return evidence
    if not isinstance(payload, dict):
        evidence['reason'] = 'orcid_auth_response_unrecognized'
        return evidence
    mapping = {'verificationCodeRequired': 'mfa_required', 'disabled': 'account_reactivation_required', 'unclaimed': 'account_claim_required', 'deprecated': 'account_deprecated', 'invalidUserType': 'account_type_unsupported'}
    for key in ('success', *mapping):
        if key in payload:
            evidence[key] = payload[key] is True or str(payload[key]).lower() == 'true'
    evidence['reason'] = next((reason for key, reason in mapping.items() if evidence.get(key)), None)
    if not evidence['reason'] and evidence.get('success') is False:
        # A negative result alone does not distinguish credentials from other
        # server-side failures. Only an explicit UI marker establishes that.
        evidence['reason'] = 'orcid_signin_rejected'
    return evidence


def choose_orcid_signin(page, host):
    """Never confuse the author's public ORCID link with the SSO login option."""
    if provider_host(host, 'clarivate.com'):
        control = visible(page, ['a[href*="orcid" i]', 'button[title*="orcid" i]', '[aria-label*="orcid" i]'])
        if control is not None:
            control.click()
            return True
        return click_named(page, r'ORCID')
    # In a WoS inline login dialog only an explicit sign-in affordance counts.
    return click_named(page, r'sign in (?:with|using) ORCID|ORCID sign in')


WOS_LOGIN_HOSTS = frozenset({'webofscience.com', 'www.webofscience.com',
    'access.clarivate.com', 'signin.clarivate.com', 'orcid.org', 'www.orcid.org'})


def _wos_login_page(context, existing_pages, evidence=None):
    """Follow this attempt's same-tab redirects or its latest still-open popup."""
    pages = [page for page in context.pages if page not in existing_pages and not page.is_closed()]
    if not pages:
        return None
    page = pages[-1]
    _check_browser_error_page(page, evidence)
    if page.url != 'about:blank':
        try:
            address = urlparse(page.url)
            allowed = (address.scheme == 'https' and address.hostname in WOS_LOGIN_HOSTS
                       and address.port in (None, 443) and address.username is None and address.password is None)
        except ValueError:
            allowed = False
        if not allowed:
            raise AuthFailure('unexpected_login_origin')
    return page


def _wait_wos_login_navigation(page, deadline):
    """Navigation waiting belongs to the original login budget, including popups."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or page.is_closed():
        return
    try:
        page.wait_for_load_state('domcontentloaded', timeout=min(15000, remaining * 1000))
    except Exception:
        pass
    remaining = deadline - time.monotonic()
    if remaining > 0 and not page.is_closed():
        try:
            page.wait_for_timeout(min(1000, remaining * 1000))
        except Exception:
            if not page.is_closed():
                raise


WOS_LOGIN_STAGES = frozenset({
    'initialization', 'credentials', 'homepage', 'initial_profile', 'signin', 'orcid_selection',
    'orcid_page', 'orcid_form', 'orcid_submit', 'orcid_response', 'orcid_consent',
    'wos_return', 'profile_navigation', 'complete',
})
WOS_LOGIN_PROGRESS_FLAGS = frozenset({
    'homepage_requested', 'homepage_loaded', 'signin_clicked', 'clarivate_observed',
    'initial_profile_requested', 'initial_profile_loaded', 'onboarding_acknowledged',
    'consent_accepted', 'returned_profile_reused',
    'orcid_selected', 'orcid_page_observed', 'orcid_form_observed', 'submit_clicked',
    'orcid_consent_clicked', 'wos_return_observed', 'wos_session_confirmed',
    'profile_requested', 'profile_loaded', 'input_matches_configured',
})
WOS_LOGIN_BOOLEAN_FIELDS = WOS_LOGIN_PROGRESS_FLAGS | frozenset({
    'username_format_valid', 'username_normalized', 'response_observed', 'success',
    'verificationCodeRequired', 'disabled', 'unclaimed', 'deprecated', 'invalidUserType',
    'browser_error_page_observed',
    'account_click_timed_out', 'cookie_banner_observed', 'cookie_banner_dismissed',
    'canonical_home_probe_attempted', 'canonical_home_probe_loaded',
    'initial_profile_retry_attempted', 'initial_profile_retry_loaded',
})
WOS_LOGIN_RESPONSE_REASONS = frozenset({
    'mfa_required', 'account_reactivation_required', 'account_claim_required',
    'account_deprecated', 'account_type_unsupported', 'orcid_signin_rejected',
    'orcid_auth_response_unrecognized',
})
WOS_NETWORK_ERROR_CODES = frozenset({
    'ERR_ABORTED', 'ERR_FAILED', 'ERR_CACHE_MISS', 'ERR_EMPTY_RESPONSE',
    'ERR_CONNECTION_CLOSED', 'ERR_CONNECTION_RESET', 'ERR_CONNECTION_REFUSED',
    'ERR_CONNECTION_ABORTED', 'ERR_CONNECTION_FAILED', 'ERR_CONNECTION_TIMED_OUT',
    'ERR_TIMED_OUT', 'ERR_NAME_NOT_RESOLVED', 'ERR_NAME_RESOLUTION_FAILED',
    'ERR_INTERNET_DISCONNECTED', 'ERR_NETWORK_CHANGED', 'ERR_ADDRESS_UNREACHABLE',
    'ERR_SSL_PROTOCOL_ERROR', 'ERR_SSL_VERSION_OR_CIPHER_MISMATCH',
    'ERR_CERT_AUTHORITY_INVALID', 'ERR_CERT_DATE_INVALID', 'ERR_CERT_COMMON_NAME_INVALID',
    'ERR_CERT_INVALID', 'ERR_CERT_REVOKED', 'ERR_TOO_MANY_REDIRECTS', 'ERR_INVALID_REDIRECT',
    'ERR_HTTP_RESPONSE_CODE_FAILURE', 'ERR_HTTP2_PROTOCOL_ERROR', 'ERR_QUIC_PROTOCOL_ERROR',
    'ERR_TUNNEL_CONNECTION_FAILED', 'ERR_PROXY_CONNECTION_FAILED',
    'ERR_BLOCKED_BY_CLIENT', 'ERR_BLOCKED_BY_RESPONSE', 'ERR_BLOCKED_BY_ADMINISTRATOR',
})
WOS_NAVIGATION_FAILURE_LIMIT = 8
WOS_NAVIGATION_KINDS = frozenset({'request_failed', 'http_error', 'browser_error'})
WOS_NAVIGATION_PROVIDERS = frozenset({'wos', 'clarivate', 'orcid', 'browser', 'other'})


def _network_error_code(value):
    if isinstance(value, str):
        return next((code for code in re.findall(r'\bERR_[A-Z0-9_]+\b', value)
                     if code in WOS_NETWORK_ERROR_CODES), 'unknown')
    return 'unknown'


def _safe_navigation_failure(value):
    if not isinstance(value, dict):
        return None
    kind, provider = value.get('kind'), value.get('provider')
    if (not isinstance(kind, str) or kind not in WOS_NAVIGATION_KINDS
            or not isinstance(provider, str) or provider not in WOS_NAVIGATION_PROVIDERS):
        return None
    result = {'kind': kind, 'provider': provider}
    if isinstance(value.get('stage'), str) and value['stage'] in WOS_LOGIN_STAGES:
        result['stage'] = value['stage']
    code = value.get('network_error_code')
    if isinstance(code, str) and (code == 'unknown' or code in WOS_NETWORK_ERROR_CODES):
        result['network_error_code'] = code
    status = value.get('http_status')
    if type(status) is int and 400 <= status <= 599:
        result['http_status'] = status
    return result


def _record_navigation_failure(evidence, kind, provider, *, code=None, status=None):
    if evidence is None:
        return
    event = _safe_navigation_failure({'kind': kind, 'provider': provider,
        'stage': evidence.get('stage'), 'network_error_code': code, 'http_status': status})
    if event is None:
        return
    failures = evidence.setdefault('navigation_failures', [])
    failures.append(event)
    del failures[:-WOS_NAVIGATION_FAILURE_LIMIT]
    evidence['navigation_failure_count'] = min(1000, evidence.get('navigation_failure_count', 0) + 1)


def _check_browser_error_page(page, evidence=None):
    # This internal page is failure evidence, never an allowed login origin.
    if page.url != 'chrome-error://chromewebdata/':
        return
    evidence = evidence if evidence is not None else {}
    evidence['browser_error_page_observed'] = True
    try:
        code = _network_error_code(page.locator('body').inner_text(timeout=1000))
    except Exception:
        code = 'unknown'
    _record_navigation_failure(evidence, 'browser_error', 'browser', code=code)
    raise AuthFailure('login_navigation_failed', authentication_evidence=safe_wos_login_evidence(evidence))


def _navigation_provider(url):
    try:
        address = urlparse(url)
        if address.scheme == 'https' and address.port in (None, 443) and not address.username and not address.password:
            for host, category in (('webofscience.com', 'wos'), ('clarivate.com', 'clarivate'), ('orcid.org', 'orcid')):
                if provider_host(address.hostname or '', host):
                    return category
    except (TypeError, ValueError):
        pass
    return 'other'


def _goto_wos_login(page, url, evidence, **options):
    try:
        return page.goto(url, **options)
    except Exception as exc:
        _check_browser_error_page(page, evidence)
        code = _network_error_code(str(exc)) if type(exc).__module__.startswith('playwright.') else 'unknown'
        if code not in {'unknown', 'ERR_ABORTED'}:
            # This exception belongs to this exact goto, not an earlier failed
            # request. An aborted redirect alone is not a transport diagnosis.
            last = evidence.get('navigation_failures', [])[-1:]
            if not last or last[0].get('kind') != 'request_failed' or last[0].get('network_error_code') != code:
                _record_navigation_failure(evidence, 'request_failed', _navigation_provider(url), code=code)
            raise AuthFailure('login_navigation_failed') from None
        raise


def safe_wos_login_evidence(value):
    """Keep fixed progress and existing ORCID status evidence, never body/URLs."""
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in WOS_LOGIN_BOOLEAN_FIELDS if type(value.get(key)) is bool}
    stage = value.get('stage')
    if isinstance(stage, str) and stage in WOS_LOGIN_STAGES:
        result['stage'] = stage
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    reason = value.get('reason')
    if isinstance(reason, str) and (reason in WOS_LOGIN_RESPONSE_REASONS or re.fullmatch(r'orcid_auth_http_[1-5][0-9]{2}', reason)):
        result['reason'] = reason
    failures = value.get('navigation_failures')
    if isinstance(failures, list):
        result['navigation_failures'] = [event for item in failures[-WOS_NAVIGATION_FAILURE_LIMIT:]
                                         if (event := _safe_navigation_failure(item)) is not None]
    count = value.get('navigation_failure_count')
    if type(count) is int and 0 <= count <= 1000:
        result['navigation_failure_count'] = count
    return result


class _WosLoginNavigationObserver:
    """Observe only top-level documents belonging to this one login attempt."""
    def __init__(self, context, evidence):
        self.context, self.evidence = context, evidence
        self.existing_pages = tuple(context.pages)
        self.last_document_failure = None
        self.authorization_denied = False

    def owned_page(self, request):
        try:
            frame = request.frame
            page = frame.page
            if (frame.parent_frame is None and frame == page.main_frame
                    and page not in self.existing_pages and page.context == self.context):
                return page
        except Exception:
            pass
        return None

    def provider(self, request):
        try:
            if not request.is_navigation_request() or request.resource_type != 'document':
                return None
            if self.owned_page(request) is None:
                return None
            return _navigation_provider(request.url)
        except Exception:
            return None

    def failed(self, request):
        provider = self.provider(request)
        if provider is not None:
            try:
                code = _network_error_code(request.failure)
            except Exception:
                code = 'unknown'
            self.last_document_failure = {'page': self.owned_page(request), 'provider': provider, 'code': code,
                                          'stage': self.evidence.get('stage')}
            _record_navigation_failure(self.evidence, 'request_failed', provider, code=code)

    def response(self, response):
        try:
            status = response.status
            if type(status) is not int or not 400 <= status <= 599:
                return
            provider = self.provider(response.request)
            if provider is not None:
                if status in {401, 403, 429}:
                    self.authorization_denied = True
                _record_navigation_failure(self.evidence, 'http_error', provider, status=status)
        except Exception:
            pass

    def start(self):
        self.context.on('requestfailed', self.failed)
        self.context.on('response', self.response)

    def stop(self):
        for event, callback in (('requestfailed', self.failed), ('response', self.response)):
            try:
                self.context.remove_listener(event, callback)
            except Exception:
                pass


def _retry_initial_wos_profile(page, profile_url, failure, evidence, navigation, deadline, *, initial_page):
    """Repeat only the failed initial canonical GET, before any login/UI action."""
    if (failure.reason != 'login_navigation_failed' or page is None or page is not initial_page
            or page.url != 'chrome-error://chromewebdata/' or navigation is None
            or navigation.authorization_denied or evidence.get('stage') != 'initial_profile'
            or evidence.get('initial_profile_requested') is not True
            or evidence.get('initial_profile_retry_attempted') or time.monotonic() >= deadline):
        return False
    acted = ('signin_clicked', 'orcid_selected', 'orcid_page_observed', 'orcid_form_observed',
             'submit_clicked', 'orcid_consent_clicked', 'response_observed', 'onboarding_acknowledged',
             'consent_accepted', 'cookie_banner_dismissed', 'wos_return_observed',
             'wos_session_confirmed', 'profile_requested', 'canonical_home_probe_attempted')
    if any(evidence.get(field) for field in acted):
        return False
    try:
        address = urlparse(profile_url)
        canonical = 'https://www.webofscience.com' + address.path
        if (profile_url != canonical
                or not re.fullmatch(r'/wos/author/record/[A-Za-z0-9-]+', address.path)):
            return False
    except (TypeError, ValueError):
        return False
    events = (failure.authentication_evidence or {}).get('navigation_failures', [])
    browser_error = events[-1] if events else {}
    last = navigation.last_document_failure or {}
    if (browser_error.get('kind') != 'browser_error'
            or browser_error.get('network_error_code') != 'ERR_INVALID_REDIRECT'
            or last.get('page') is not page or last.get('provider') != 'clarivate'
            or last.get('code') != 'ERR_INVALID_REDIRECT' or last.get('stage') != 'initial_profile'):
        return False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    evidence['initial_profile_retry_attempted'] = True
    _goto_wos_login(page, canonical, evidence, wait_until='domcontentloaded', timeout=min(90000, remaining * 1000))
    evidence['initial_profile_retry_loaded'] = True
    return True


def _probe_canonical_wos_home(page, failure, evidence, navigation, auth_responses, deadline):
    """One fixed-page probe after an observed malformed Clarivate callback only.

    Successful ORCID credentials do not prove WoS authorization. The caller must
    enter verification-only mode and retain its ordinary account/target checks.
    """
    if (failure.reason != 'login_navigation_failed' or page is None
            or page.url != 'chrome-error://chromewebdata/'
            or evidence.get('canonical_home_probe_attempted') or navigation is None
            or navigation.authorization_denied or time.monotonic() >= deadline):
        return False
    events = (failure.authentication_evidence or {}).get('navigation_failures', [])
    browser_error = events[-1] if events else {}
    last = navigation.last_document_failure or {}
    response = auth_responses[-1] if auth_responses else {}
    if (browser_error.get('kind') != 'browser_error' or browser_error.get('network_error_code') != 'ERR_INVALID_REDIRECT'
            or last.get('page') is not page or last.get('provider') != 'clarivate' or last.get('code') != 'ERR_INVALID_REDIRECT'
            or response.get('response_observed') is not True or response.get('success') is not True
            or response.get('http_status') != 200 or response.get('reason') is not None):
        return False
    # No Location replay, new context, cookie mutation, credential or consent
    # resubmission. This uses only the original attempt's remaining deadline.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    evidence['canonical_home_probe_attempted'] = True
    _goto_wos_login(page, 'https://www.webofscience.com/', evidence,
                    wait_until='domcontentloaded', timeout=min(90000, remaining * 1000))
    evidence['canonical_home_probe_loaded'] = True
    return True


@diagnostic_login
def login_wos(context, profile_url, timeout=180):
    evidence = {key: False for key in WOS_LOGIN_PROGRESS_FLAGS}
    evidence['stage'] = 'initialization'
    navigation = _WosLoginNavigationObserver(context, evidence)
    try:
        navigation.start()
        page = _login_wos(context, profile_url, timeout, evidence, navigation=navigation)
        evidence['stage'] = 'complete'
        page._wos_login_evidence = safe_wos_login_evidence(evidence)
        return page
    except Exception as exc:
        failure = exc if isinstance(exc, AuthFailure) else AuthFailure(type(exc).__name__)
        failure.authentication_evidence = safe_wos_login_evidence({**safe_wos_login_evidence(failure.authentication_evidence), **evidence})
        raise failure from None
    finally:
        navigation.stop()


def _login_wos(context, profile_url, timeout, evidence, *, navigation=None):
    evidence['stage'] = 'credentials'
    configured_username = os.environ.get('WOS_ORCID_USERNAME', '')
    username = normalize_orcid_username(configured_username)
    password = os.environ.get('WOS_ORCID_PASSWORD', '')
    if not username or not password:
        raise AuthFailure('credentials_missing')
    evidence['username_format_valid'] = valid_orcid_username(username)
    evidence['username_normalized'] = username != configured_username
    if not evidence['username_format_valid']:
        raise AuthFailure('username_configuration_invalid')
    existing_pages = tuple(context.pages)
    page = context.new_page()
    initial_page = page
    deadline = time.monotonic() + timeout
    evidence.update(stage='initial_profile', initial_profile_requested=True)
    try:
        _goto_wos_login(page, profile_url, evidence, wait_until='domcontentloaded', timeout=min(90000, timeout * 1000))
    except AuthFailure as failure:
        if not _retry_initial_wos_profile(page, profile_url, failure, evidence, navigation, deadline, initial_page=initial_page):
            raise
    evidence['initial_profile_loaded'] = True
    submitted = False
    selected_signin = False
    selected_orcid = False
    auth_responses = []

    def record_auth_response(response):
        try:
            address = urlparse(response.url)
            if not provider_host(address.hostname or '', 'orcid.org') or address.path not in {'/signin/auth.json', '/login'} or response.request.method != 'POST':
                return
            if navigation is not None and navigation.owned_page(response.request) is None:
                return
            try:
                payload = response.json()
            except Exception:
                payload = None
            observed = orcid_auth_response_evidence(response.status, payload)
            auth_responses.append(observed)
            evidence.update(observed)
        except Exception:
            pass

    context.on('response', record_auth_response)

    def select_page():
        try:
            return _wos_login_page(context, existing_pages, evidence)
        except AuthFailure as failure:
            pages = [current for current in context.pages if current not in existing_pages and not current.is_closed()]
            current = pages[-1] if pages else None
            if _retry_initial_wos_profile(current, profile_url, failure, evidence, navigation, deadline, initial_page=initial_page):
                return _wos_login_page(context, existing_pages, evidence)
            if _probe_canonical_wos_home(current, failure, evidence, navigation, auth_responses, deadline):
                return _wos_login_page(context, existing_pages, evidence)
            raise

    while time.monotonic() < deadline:
        page = select_page()
        if page is None:
            break
        _wait_wos_login_navigation(page, deadline)
        # A popup may arrive or close while the previous page was loading.
        page = select_page()
        if page is None or time.monotonic() >= deadline:
            break
        if page.url == 'about:blank':
            continue
        observed_host = urlparse(page.url).hostname or ''
        if provider_host(observed_host, 'orcid.org'):
            evidence.update(stage='orcid_response' if submitted else 'orcid_page', orcid_page_observed=True)
        elif provider_host(observed_host, 'clarivate.com'):
            evidence.update(stage='signin', clarivate_observed=True)
        elif provider_host(observed_host, 'webofscience.com') and evidence.get('orcid_page_observed'):
            evidence.update(stage='wos_return', wos_return_observed=True)
        if auth_responses and auth_responses[-1].get('reason'):
            reason = auth_responses[-1]['reason']
            if reason == 'orcid_signin_rejected':
                # The JSON response arrives before Angular renders its error.
                # Give the explicit UI message a bounded opportunity to appear.
                page.wait_for_timeout(1500)
                try:
                    assert_no_challenge(page)
                except AuthFailure as failure:
                    failure.authentication_evidence = auth_responses[-1]
                    raise
            raise AuthFailure(reason, authentication_evidence=auth_responses[-1])
        if evidence.get('canonical_home_probe_attempted'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthFailure('wos_login_not_confirmed')
            assert_no_challenge(page, form_submitted=submitted,
                                passive_wait_seconds=min(10.0, remaining), passive_deadline=deadline)
        else:
            assert_no_challenge(page, form_submitted=submitted)
        host = urlparse(page.url).hostname or ''
        if evidence.get('canonical_home_probe_attempted'):
            # A callback that did not establish WoS authorization must not cause
            # another Sign In, ORCID form submission or Authorize interaction.
            if not provider_host(host, 'webofscience.com'):
                raise AuthFailure('wos_login_not_confirmed')
            if not wos_authenticated(page):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AuthFailure('wos_login_not_confirmed')
                # A successfully loaded home can still be rendering its account
                # menu. Wait only; never restart sign-in or OAuth consent here.
                page.wait_for_timeout(min(250, remaining * 1000))
                continue
            return _finish_wos_profile_login(page, profile_url, evidence, deadline)
        if provider_host(host, 'webofscience.com'):
            if not selected_signin or evidence.get('wos_return_observed'):
                prepare_wos_profile_login(page, evidence, deadline)
                host = urlparse(page.url).hostname or ''
                if not provider_host(host, 'webofscience.com'):
                    # Rendering can complete an SSO redirect. Revalidate the next
                    # origin through the normal page-selection gate before actions.
                    continue
        else:
            dismiss = visible(page, ['#onetrust-reject-all-handler', '#onetrust-accept-btn-handler'])
            if dismiss is not None:
                dismiss.click()
        if provider_host(host, 'clarivate.com'):
            # WoS sometimes redirects the homepage straight to its sign-in
            # service. Do not submit the unused Clarivate password form first.
            selected_signin = True
        if provider_host(host, 'orcid.org'):
            user = visible(page, ['#username-input', '#userId', 'input[name="username"]', 'input[name="userId"]', 'input[autocomplete="username"]', 'input[type="email"]'])
            secret = visible(page, ['#password', 'input[type="password"]'])
            if user is not None and secret is not None and not submitted:
                evidence.update(stage='orcid_form', orcid_form_observed=True)
                user.fill(username)
                secret.fill(password)
                evidence['input_matches_configured'] = user.input_value() == username
                if not evidence['input_matches_configured']:
                    raise AuthFailure('username_input_not_applied')
                # Live ORCID form: button#signin-button, "Sign in to ORCID".
                # Its cookie banner may mount after the fields have appeared.
                consent = visible(page, ['#onetrust-reject-all-handler', '#onetrust-accept-btn-handler'])
                if consent is not None:
                    consent.click()
                submit = visible(page, ['button#signin-button[type="submit"]'])
                evidence['stage'] = 'orcid_submit'
                if submit is not None:
                    submit.click(timeout=15000)
                elif not click_named(page, r'^Sign in(?: to ORCID)?$|^Войти(?: в ORCID)?$'):
                    raise AuthFailure('orcid_submit_changed')
                submitted = True
                evidence['submit_clicked'] = True
                continue
            # The authorization page is the standard ORCID OAuth consent for WoS.
            if submitted or selected_orcid:
                evidence['stage'] = 'orcid_consent'
            if (submitted or selected_orcid) and click_named(page, r'^Authorize(?: access)?$|^Разрешить доступ$'):
                evidence['orcid_consent_clicked'] = True
                continue
        elif provider_host(host, 'webofscience.com') and wos_authenticated(page):
            return _finish_wos_profile_login(page, profile_url, evidence, deadline)
        elif not selected_signin:
            evidence['stage'] = 'signin'
            if click_named(page, r'^Sign in$|^Sign in.*Web of Science|^Войти$'):
                selected_signin = True
                evidence['signin_clicked'] = True
                continue
        if selected_signin and not selected_orcid:
            evidence['stage'] = 'orcid_selection'
            if choose_orcid_signin(page, host):
                selected_orcid = True
                evidence['orcid_selected'] = True
            else:
                # The actual submenu is an <a> without href in the current WoS
                # DOM, so its implicit accessibility role is not necessarily link.
                signin_link = page.locator('a').filter(has_text=re.compile(r'^\s*Sign in\s*$', re.I))
                for index in range(signin_link.count()):
                    item = signin_link.nth(index)
                    if item.is_visible():
                        item.click()
                        break
            if selected_orcid:
                continue
    raise AuthFailure('wos_login_not_confirmed' if submitted else 'wos_login_form_changed', authentication_evidence=auth_responses[-1] if auth_responses else {'response_observed': False, 'submit_clicked': submitted})
