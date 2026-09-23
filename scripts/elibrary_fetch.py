from __future__ import annotations

from http.cookiejar import CookieJar
import os
import time
import urllib.error
import urllib.request

DEFAULT_PREFLIGHT_URL = 'https://elibrary.ru/defaultx.asp'
DEFAULT_TIMEOUT = 75
DEFAULT_HEADERS = {
    'User-Agent': os.environ.get('ELIBRARY_USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 YaBrowser/26.4.0.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'ru,en;q=0.9',
    'Cache-Control': 'max-age=0',
    'Connection': 'close',
    'Upgrade-Insecure-Requests': '1',
}


def build_opener() -> tuple[urllib.request.OpenerDirector, CookieJar]:
    jar = CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    proxy_url = os.environ.get('ELIBRARY_PROXY_URL')
    if proxy_url:
        handlers.insert(0, urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
    return urllib.request.build_opener(*handlers), jar


def fetch_once(opener: urllib.request.OpenerDirector, url: str, *, referer: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[str | None, dict]:
    headers = dict(DEFAULT_HEADERS)
    headers['Host'] = urllib.request.urlparse(url).netloc
    if referer:
        headers['Referer'] = referer
    # CookieJar owns the session; never override refreshed cookies with a stale header.
    started = time.time()
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get_content_charset() or 'utf-8'
            return raw.decode(enc, errors='replace'), {
                'status': 'ok',
                'http_status': resp.status,
                'elapsed_sec': round(time.time() - started, 3),
                'content_length': len(raw),
                'url': url,
                'final_url': resp.geturl(),
            }
    except urllib.error.HTTPError as exc:
        return None, {
            'status': 'http_error',
            'http_status': exc.code,
            'elapsed_sec': round(time.time() - started, 3),
            'url': url,
        }
    except Exception as exc:
        return None, {
            'status': 'error',
            'elapsed_sec': round(time.time() - started, 3),
            'url': url,
            'error_type': type(exc).__name__,
        }


def fetch_elibrary_page(url: str) -> tuple[str | None, dict]:
    opener, jar = build_opener()
    preflight_url = os.environ.get('ELIBRARY_PREFLIGHT_URL', DEFAULT_PREFLIGHT_URL)
    preflight_text, preflight_report = fetch_once(opener, preflight_url, referer='https://elibrary.ru/')
    text, target_report = fetch_once(opener, url, referer=os.environ.get('ELIBRARY_REFERER', preflight_url))
    target_report['preflight'] = preflight_report
    target_report['cookies_count'] = len(jar)
    target_report['manual_cookie_present'] = bool(os.environ.get('ELIBRARY_COOKIE'))
    if preflight_text:
        target_report['preflight_fingerprint'] = {'content_length': len(preflight_text.encode('utf-8', errors='replace'))}
    if text:
        lowered = text.lower()
        target_report['html_fingerprint'] = {
            'has_author_items': 'author_items' in text,
            'has_rows': 'arw' in text,
            'has_suspicious_ip_text': 'подозр' in lowered or 'suspicious' in lowered or 'ip_blocked' in lowered,
            'has_turing_test': 'тест тьюринга' in lowered or 'page_captcha' in lowered or 'recaptcha' in lowered,
            'has_cookie_text': 'cookie' in lowered or 'cookies' in lowered,
            'content_length': len(text.encode('utf-8', errors='replace')),
        }
    return text, target_report
