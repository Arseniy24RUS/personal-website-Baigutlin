#!/usr/bin/env python3
"""Append-only discovery of public GitHub projects; README is never executed."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import html
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
import requests

from it_resources import (ASSETS, CACHE, CATALOG, CONFIG, STATE, atomic_json, asset_path,
                          merge_catalog, normalized_url, read_json, validate_it_resources)
from it_translation import ITTranslator

REPORT = 'data/it/audit/harvest_report.json'
MAX_SCREENSHOTS = 10
MAX_BYTES = 8_000_000
NEUTRAL = b'''<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="810" viewBox="0 0 1440 810"><rect width="1440" height="810" fill="#eeeeee"/><rect x="360" y="195" width="720" height="420" rx="24" fill="#d7d7d7"/><rect x="400" y="235" width="640" height="48" rx="8" fill="#bdbdbd"/><path d="M570 340l-70 70 70 70m300-140l70 70-70 70m-110-170l-80 200" fill="none" stroke="#4d4d53" stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/></svg>'''
APP_LABEL = re.compile(r'\b(live|demo|website|web app|application|launch|try it|visit|open (?:app|the platform))\b|сайт|приложени|демо|открыть|попробовать', re.I)
BAD_IMAGE = re.compile(r'shields\.io|badge|badgen\.|travis-ci|codecov|actions/workflows|visitor-badge', re.I)
CHALLENGE_TEXT = re.compile(r'verify (?:that )?you are human|checking your browser|unusual traffic|just a moment|проверка браузера|подтвердите, что вы человек', re.I)


class ITFailure(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def require_public_network():
    if os.environ.get('IT_PUBLIC_NETWORK_REQUIRED') == '1':
        try:
            result = subprocess.run(['bash', str(Path(__file__).with_name('it_public_network.sh')), 'check'],
                                    capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            raise ITFailure('public_network_guard_unavailable') from None
        if result.returncode:
            raise ITFailure('public_network_guard_unavailable')


def public_url(value, *, resolver=socket.getaddrinfo):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        host = parsed.hostname.rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '%' in host:
            raise ValueError()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError()
        addresses = resolver(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError()
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, parsed.fragment))
    except (ValueError, TypeError):
        raise ITFailure('unsafe_public_url') from None
    except OSError:
        raise ITFailure('public_dns_unavailable') from None


class PublicFetcher:
    """Independent anonymous session. A worker bounds DNS/redirect/body latency."""
    def __init__(self, session=None, resolver=socket.getaddrinfo):
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.resolver = resolver

    def fetch(self, url, *, max_bytes=MAX_BYTES):
        results = queue.Queue(maxsize=1)
        def perform():
            try:
                results.put(self._fetch(url, max_bytes))
            except ITFailure as exc:
                results.put(exc)
            except Exception:
                results.put(ITFailure('public_fetch_failed'))
        threading.Thread(target=perform, daemon=True).start()
        try:
            result = results.get(timeout=35)
        except queue.Empty:
            raise ITFailure('public_fetch_timeout') from None
        if isinstance(result, ITFailure):
            raise result
        return result

    def _fetch(self, url, max_bytes):
        deadline = time.monotonic() + 35
        for _ in range(6):
            url = public_url(url, resolver=self.resolver)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ITFailure('public_fetch_timeout')
            try:
                response = self.session.get(url, headers={'User-Agent': 'PortfolioITCollector/1.0',
                    'Accept': '*/*'}, timeout=(min(10, remaining), min(10, remaining)),
                    allow_redirects=False, stream=True)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if not response.headers.get('Location'):
                            raise ITFailure('invalid_public_redirect')
                        url = urljoin(url, response.headers['Location'])
                        continue
                    if response.status_code >= 400:
                        raise ITFailure('public_http_error', response.status_code)
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > max_bytes:
                            raise ITFailure('public_content_too_large')
                        if time.monotonic() >= deadline:
                            raise ITFailure('public_fetch_timeout')
                        chunks.append(chunk)
                    return b''.join(chunks), response.headers.get('Content-Type', ''), url
                finally:
                    response.close()
            except requests.RequestException:
                raise ITFailure('public_fetch_failed') from None
        raise ITFailure('public_redirect_limit')


class GitHubClient:
    def __init__(self, token=None, session=None, clock=time.monotonic, sleep=time.sleep):
        self.session = session or requests.Session()
        self.token = token
        self.clock, self.sleep = clock, sleep
        if session is None:
            self.session.trust_env = False

    def get(self, path, params=None):
        if not path.startswith(('/users/', '/repos/')) or '://' in path or '?' in path or '\\' in path:
            raise ITFailure('invalid_github_api_path')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10',
                   'User-Agent': 'PortfolioITCollector/1.0'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        deadline = self.clock() + 45
        for attempt in range(3):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ITFailure('github_timeout')
            try:
                response = self.session.get('https://api.github.com' + path, params=params,
                    headers=headers, timeout=min(20, remaining), allow_redirects=False)
            except requests.RequestException:
                if attempt < 2 and self.clock() + 2 ** attempt < deadline:
                    self.sleep(2 ** attempt)
                    continue
                raise ITFailure('github_transport_failed') from None
            status = response.status_code
            rate = status == 429 or status == 403 and ('Retry-After' in response.headers or response.headers.get('X-RateLimit-Remaining') == '0')
            if rate or status >= 500:
                try:
                    delay = float(response.headers.get('Retry-After', 2 ** attempt))
                except (ValueError, TypeError):
                    delay = 2 ** attempt
                response.close()
                if attempt < 2 and 0 <= delay <= 20 and self.clock() + delay < deadline:
                    self.sleep(delay)
                    continue
                raise ITFailure('github_rate_limited' if rate else 'github_unavailable', status)
            if status != 200:
                response.close()
                raise ITFailure('github_http_error', status)
            try:
                result = response.json()
            except ValueError:
                raise ITFailure('github_invalid_json') from None
            finally:
                response.close()
            return result
        raise ITFailure('github_unavailable')

    def repositories(self, owner):
        for page in range(1, 101):
            rows = self.get('/users/' + quote(owner, safe='') + '/repos',
                {'type': 'owner', 'sort': 'full_name', 'per_page': 100, 'page': page})
            if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get('id')) is not int or r['id'] <= 0 for r in rows):
                raise ITFailure('github_repository_schema_changed')
            yield rows
            if len(rows) < 100:
                return
        raise ITFailure('github_pagination_limit')

    def readme(self, repo):
        path = '/repos/' + '/'.join(quote(v, safe='') for v in repo['full_name'].split('/')) + '/readme'
        try:
            value = self.get(path)
        except ITFailure as exc:
            if exc.status == 404:
                return '', None
            raise
        if not isinstance(value, dict) or value.get('encoding') != 'base64' or not isinstance(value.get('content'), str):
            raise ITFailure('github_readme_schema_changed')
        try:
            text = base64.b64decode(value['content']).decode('utf-8-sig')
        except (ValueError, UnicodeError):
            raise ITFailure('github_readme_encoding_changed') from None
        if len(text) > 1_000_000:
            raise ITFailure('readme_too_large')
        repo['_readme_path'] = value.get('path', 'README.md')
        return text, value.get('sha')

    def pages(self, repo):
        if not repo.get('has_pages'):
            return None
        try:
            result = self.get('/repos/' + '/'.join(quote(v, safe='') for v in repo['full_name'].split('/')) + '/pages')
        except ITFailure as exc:
            if exc.status in (401, 403, 404) and exc.reason != 'github_rate_limited':
                return None  # Public Pages URL can still be confirmed anonymously.
            raise
        if not isinstance(result, dict):
            raise ITFailure('github_pages_schema_changed')
        return result.get('html_url') or ('https://' + result['cname'] + '/' if result.get('cname') else None)


def clean_prose(value):
    value = re.sub(r'!\[[^\]]*\]\([^\n]*?\)|!\[[^\]]*\]\[[^\]]*\]', '', value)
    value = re.sub(r'\[([^\]]+)\]\([^\n]*?\)|\[([^\]]+)\]\[[^\]]*\]', lambda m: m[1] or m[2], value)
    value = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'[`*_~]', '', value))).strip()


def language(value):
    cyrillic = len(re.findall('[А-Яа-яЁё]', value))
    latin = len(re.findall('[A-Za-z]', value))
    return 'ru' if cyrillic > latin / 3 else 'en'


def brief_description(value, limit=320):
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZА-ЯЁ0-9])', value.strip())
    result = ''
    for sentence in sentences[:2]:
        if re.search(r'\b(?:backend|source tree|published tree|HTML, CSS|not the full|JSON shards)\b|серверн\w* часть|опубликованн\w* ветк|исходн\w* выгруз', sentence, re.I):
            continue
        if len((result + ' ' + sentence).strip()) > limit:
            break
        result = (result + ' ' + sentence).strip()
    if result:
        return result
    if len(value) <= limit:
        return value
    return value[:limit - 1].rsplit(' ', 1)[0].rstrip('.,;:') + '…'


def readme_url(value, repo):
    value = html.unescape(value).strip().strip('<>')
    if value.startswith('//'):
        value = 'https:' + value
    base = 'https://raw.githubusercontent.com/' + repo['full_name'] + '/' + quote(repo.get('default_branch') or 'main', safe='') + '/' + repo.get('_readme_path', 'README.md')
    value = urljoin(base, value)
    parsed = urlsplit(value)
    if parsed.hostname == 'github.com':
        match = re.match(r'^/([^/]+/[^/]+)/blob/(.+)$', parsed.path)
        if match:
            value = 'https://raw.githubusercontent.com/' + match[1] + '/' + match[2]
    return value


def read_readme_document(text, repo):
    text = re.sub(r'```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~', '', text, flags=re.S)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    refs = {m[1].strip().lower(): m[2] for m in re.finditer(r'^\s*\[([^\]]+)\]:\s*<?([^\s>]+)>?', text, re.M)}
    images, links = [], []
    for match in re.finditer(r'!\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)', text):
        images.append((match.start(), match[2], match[1]))
    for match in re.finditer(r'!\[([^\]]*)\](?:\[([^\]]*)\])?', text):
        target = refs.get((match[2] or match[1]).strip().lower())
        if target:
            images.append((match.start(), target, match[1]))
    soup = BeautifulSoup(text, 'html.parser')
    for match in re.finditer(r'<img\b[^>]*>', text, re.I):
        tag = BeautifulSoup(match[0], 'html.parser').find('img')
        if tag.get('src'):
            images.append((match.start(), tag['src'], tag.get('alt', '')))
    for match in re.finditer(r'(?<!!)\[([^\]]+)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)', text):
        if APP_LABEL.search(match[1]):
            links.append(readme_url(match[2], repo))
    for tag in soup.find_all('a', href=True):
        if APP_LABEL.search(tag.get_text(' ', strip=True)):
            links.append(readme_url(tag['href'], repo))
    for line in text.splitlines():
        if APP_LABEL.search(clean_prose(line)):
            for value in re.findall(r'https?://[^\s<>\])]+', line):
                links.append(value.rstrip('.,;`'))
    fields = {}
    def title(value):
        value = clean_prose(value)
        if value and value.lower() not in ('english', 'русский', 'описание', 'description', 'about', 'о проекте', 'installation', 'установка'):
            brand = re.match(r'^([A-Z]{2,}[A-Za-z0-9]*|[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|[A-Za-z0-9]+\.[A-Za-z0-9]+)(?:\s*[·:—–]|\s+(?:public|static)\b)', value)
            if brand:
                value = brand[1]
            fields.setdefault('title_' + language(value), value[:160])
            if language(value) == 'en' and re.fullmatch(r'[A-Za-z][A-Za-z0-9.+]*', value):
                fields.setdefault('title_ru', value[:160])  # Brand names are not translated.
    for tag in soup.find_all('h1'):
        title(tag.get_text(' ', strip=True))
    # Keep explicit language sections authoritative, including English prose
    # containing examples in Russian. Rank a purpose paragraph above captions,
    # navigation and installation instructions.
    explicit_language, section, block, candidates = None, '', [], []
    purpose = re.compile(r'purpose|назначени|о проекте|about|overview|обзор|what (?:is|this)|что (?:это|представляет)', re.I)
    technical = re.compile(r'install|setup|quick ?start|usage|license|requirements|запуск|установ|лицензи|требован|структура|architecture', re.I)
    notes = re.compile(r'^(?:live (?:site|demo)|website|demo|сайт|демо|open the platform|english\b|русский\b|interface language|the application ui|the interface|язык интерфейса|интерфейс приложения|screenshot|скриншот|preview|caption|изображение|figure|рисунок|source code|license)\s*[:—–.-]?', re.I)
    def consume():
        raw = '\n'.join(block).strip()
        block.clear()
        if not raw or raw.startswith(('|', '>', '![', '[!')) or re.match(r'^(?:[-*+]\s|\d+\.\s|\[.*\]:)', raw):
            return
        if re.fullmatch(r'\*[^*]+\*|_[^_]+_', raw, re.S):
            return  # Standalone italic image/interface captions.
        paragraphs = BeautifulSoup(raw, 'html.parser').find_all('p')
        for part in [str(p) for p in paragraphs] if paragraphs else [raw]:
            value = clean_prose(part)
            if len(value) < 35 or notes.match(value) or value.count(' · ') >= 2 or value.startswith(('http://', 'https://')):
                continue
            if technical.search(section):
                continue
            score = 100 if purpose.search(section) else 50 if not section or section.lower() in ('english', 'русский') else 20
            candidates.append((score, explicit_language or language(value), value))
    for line in text.splitlines():
        heading = re.match(r'^\s*(#{1,6})\s+(.+)', line)
        if heading:
            consume()
            value = clean_prose(heading[2])
            if len(heading[1]) == 1:
                title(value)
            if re.fullmatch(r'(?:english|en)(?: version)?|🇬🇧\s*english', value, re.I):
                explicit_language, section = 'en', 'english'
            elif re.fullmatch(r'русский(?: язык)?|ru|🇷🇺\s*русский', value, re.I):
                explicit_language, section = 'ru', 'русский'
            else:
                section = value if len(heading[1]) > 1 else ''
        elif not line.strip():
            consume()
        else:
            block.append(line)
    consume()
    for _, lang, value in sorted(candidates, key=lambda x: -x[0]):
        fields.setdefault('description_' + lang, brief_description(value))
    description = clean_prose(repo.get('description') or '')
    if description:
        fields.setdefault('description_' + language(description), brief_description(description))
    if not any(k.startswith('title_') for k in fields):
        title(repo.get('name', '').replace('-', ' ').replace('_', ' '))
    ordered, seen = [], set()
    for _, value, alt in sorted(images):
        url = readme_url(value, repo)
        if not BAD_IMAGE.search(url + ' ' + alt) and url not in seen:
            ordered.append(url)
            seen.add(url)
    return {'fields': fields, 'images': ordered[:20], 'site_links': list(dict.fromkeys(links))[:8]}


def website_candidates(repo, document, override=None):
    urls = [override] if override else []
    if repo.get('_pages_url'):
        urls.append(repo['_pages_url'])
    if repo.get('has_pages'):
        owner, name = repo['full_name'].split('/')
        urls.append('https://' + owner.lower() + '.github.io/' + ('' if name.lower() == owner.lower() + '.github.io' else quote(name, safe='') + '/'))
    urls += document['site_links']
    if repo.get('homepage'):
        urls.append(repo['homepage'])
    output = []
    for value in urls:
        parsed = urlsplit(value)
        try:
            local = not ipaddress.ip_address(parsed.hostname or '').is_global
        except ValueError:
            local = (parsed.hostname or '').lower() == 'localhost'
        if local or parsed.username or parsed.password or parsed.scheme not in ('http', 'https'):
            continue
        if parsed.hostname and parsed.hostname.lower() not in ('github.com', 'raw.githubusercontent.com', 'api.github.com') and value not in output:
            output.append(value)
    return output


def select_website(repo, document, fetcher, override=None):
    candidates = website_candidates(repo, document, override)
    for value in candidates:
        try:
            data, content_type, final = fetcher.fetch(value, max_bytes=3_000_000)
            if 'html' not in content_type.lower() and not data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                continue
            return final
        except ITFailure:
            continue
    if candidates:
        raise ITFailure('website_not_confirmed')
    return None


def verified_image(data):
    if len(data) > MAX_BYTES:
        raise ITFailure('image_too_large')
    if data.lstrip().startswith((b'<svg', b'<?xml')):
        try:
            if re.search(br'<\?(?!xml\s)|<!DOCTYPE|<!ENTITY', data, re.I):
                raise ValueError()
            element = ET.fromstring(data)
            if element.tag.split('}')[-1] != 'svg':
                raise ValueError()
            allowed = {'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'defs', 'linearGradient', 'radialGradient', 'stop', 'clipPath', 'title', 'desc'}
            for node in element.iter():
                if node.tag.split('}')[-1] not in allowed:
                    raise ValueError()
                for key, value in node.attrib.items():
                    if key.lower().startswith('on') or key.split('}')[-1].lower() in ('href', 'src', 'base', 'style') or '\\' in value or '@import' in value.lower() or 'url(' in value.lower() and not re.fullmatch(r'url\(#[\w-]+\)', value):
                        raise ValueError()
            width = float(re.sub(r'px$', '', element.get('width', '0')))
            height = float(re.sub(r'px$', '', element.get('height', '0')))
            if not math.isfinite(width) or not math.isfinite(height) or width < 240 or height < 120 or width / height > 5:
                raise ValueError()
            return data, '.svg'
        except (ET.ParseError, ValueError, TypeError):
            raise ITFailure('image_invalid_or_small') from None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width < 240 or image.height < 120 or image.width / image.height > 5 or image.width * image.height > 40_000_000:
                raise ITFailure('image_invalid_or_small')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert('RGB')
            image.thumbnail((2400, 1600))
            output = io.BytesIO()
            image.save(output, format='PNG')
            return output.getvalue(), '.png'
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ITFailure('image_invalid_or_small') from None


def save_new_image(root, repo_id, data):
    encoded, suffix = verified_image(data)
    name = ASSETS + '/github-' + str(repo_id) + '-' + hashlib.sha256(encoded).hexdigest()[:16] + suffix
    target = asset_path(root, name)
    if target.exists() and target.read_bytes() != encoded:
        raise ITFailure('it_image_collision')
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_bytes(encoded)
        temporary.replace(target)
    return name


def page_readiness(page):
    body = page.locator('body').inner_text(timeout=3000)
    if CHALLENGE_TEXT.search(body[:3000]):
        raise ITFailure('website_human_verification')
    for frame in page.locator('iframe[src*="hcaptcha"],iframe[src*="recaptcha"]').all():
        if frame.is_visible():
            raise ITFailure('website_human_verification')
    if page.locator('input[type="password"]:visible').count():
        raise ITFailure('website_login_required')
    if re.match(r'^\s*(?:404\b|403\b|502\b|503\b|Access denied|This site can.t be reached)', body, re.I):
        raise ITFailure('website_error_page')
    if len(body.strip()) < 35 or page.locator('[aria-busy="true"]:visible,[role="progressbar"]:visible').count():
        raise ITFailure('website_not_ready')
    return {'text_length': len(body), 'ready_state': page.evaluate('document.readyState')}


def capture_site_screenshot(url, target, *, browser=None, wait_seconds=60,
                            clock=time.monotonic, sleep=None, resolver=socket.getaddrinfo):
    """One ordinary visit; wait from DOMContentLoaded, never click challenges."""
    require_public_network()
    public_url(url, resolver=resolver)
    if wait_seconds < 60:
        raise ITFailure('screenshot_wait_too_short')
    manager = None
    if browser is None:
        from playwright.sync_api import sync_playwright
        manager = sync_playwright().start()
        environment = {k: v for k, v in os.environ.items()
                       if not re.search(r'PASSWORD|TOKEN|SECRET|API_KEY|COOKIE|BROWSER_SESSION', k, re.I)}
        browser = manager.chromium.launch(headless=True, env=environment)
    context = None
    try:
        context = browser.new_context(viewport={'width': 1440, 'height': 810}, device_scale_factor=1,
                                      service_workers='block', accept_downloads=False)
        def route_request(route):
            try:
                value = route.request.url
                if value.startswith(('data:', 'blob:')):
                    route.continue_()
                    return
                public_url(value, resolver=resolver)
                route.continue_()
            except ITFailure:
                route.abort()
        context.route('**/*', route_request)
        page = context.new_page()
        response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
        loaded = clock()
        if response is None or response.status >= 400:
            raise ITFailure('website_navigation_failed')
        deadline = loaded + wait_seconds
        while clock() < deadline:
            interval = min(1, deadline - clock())
            if sleep is None:
                page.wait_for_timeout(interval * 1000)
            else:
                sleep(interval)
        public_url(page.url, resolver=resolver)
        metadata = page_readiness(page)
        encoded = page.screenshot(type='png', full_page=False, timeout=15000)
        verified_image(encoded)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_bytes(encoded)
        temporary.replace(target)
        return {'origin': 'site_screenshot', 'wait_seconds': round(clock() - loaded, 3),
                'viewport_width': 1440, 'viewport_height': 810, **metadata}
    except ITFailure:
        raise
    except Exception:
        raise ITFailure('website_screenshot_failed') from None
    finally:
        if context:
            context.close()
        if manager:
            browser.close()
            manager.stop()


def harvest(root, *, github=None, fetcher=None, translator=None, screenshot=capture_site_screenshot):
    root = Path(root)
    attempted = now()
    require_public_network()
    old_report = read_json(root / REPORT, {})
    config = read_json(root / CONFIG, {})
    owner = config.get('owner', '')
    if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9-]+', owner):
        raise ITFailure('invalid_it_owner')
    catalog = read_json(root / CATALOG, {'items': []})
    state = read_json(root / STATE, {'schema': 'it-discovery/v1', 'repositories': {}})
    repositories = state.setdefault('repositories', {})
    visible = {row['id'] for row in catalog['items']}
    github = github or GitHubClient(os.environ.get('IT_GITHUB_TOKEN'))
    fetcher = fetcher or PublicFetcher()
    translator = translator or ITTranslator(root / CACHE)
    config_repos = config.get('repositories', {})
    excluded = set(config.get('excluded_repo_ids', []))
    discovered, added, screenshots = set(), 0, 0
    discovery_complete, failure = False, None
    try:
        for batch in github.repositories(owner):
            for repo in batch:
                repo_id = str(repo['id'])
                if repo['id'] in excluded or repo.get('private') or (repo.get('owner') or {}).get('login', owner).lower() != owner.lower():
                    continue
                if not re.fullmatch(re.escape(owner) + r'/[A-Za-z0-9_.-]+', repo.get('full_name', ''), re.I):
                    raise ITFailure('github_repository_schema_changed')
                discovered.add(repo_id)
                setting = config_repos.get(repo_id, {})
                old = repositories.get(repo_id, {})
                resource_id = old.get('resource_id') or setting.get('resource_id', 'github-' + repo_id)
                if old.get('resource_id') and setting.get('resource_id') and setting['resource_id'] != old['resource_id']:
                    raise ITFailure('it_repository_alias_conflict')
                if resource_id in visible:
                    entry = repositories.setdefault(repo_id, {'resource_id': resource_id, 'aliases': [repo['full_name']]})
                    entry['status'] = 'published'
                    for key in ('reason', 'repository'):
                        entry.pop(key, None)
                    continue
                repositories[repo_id] = {**old, 'resource_id': resource_id, 'status': 'pending',
                    'aliases': sorted(set(old.get('aliases', [])) | {repo['full_name']}),
                    'repository': {k: repo.get(k) for k in ('id', 'full_name', 'name', 'description', 'default_branch', 'homepage', 'has_pages', 'html_url')}}
            atomic_json(root / STATE, state)
        discovery_complete = True
    except ITFailure as exc:
        failure = exc.reason
    # A partial last page never loses pending repositories. A rate limit stops
    # further API work for this run rather than retrying once per repository.
    for repo_id, entry in list(repositories.items()):
        if failure == 'github_rate_limited':
            break
        if entry.get('resource_id') in visible or entry.get('status') == 'published' or int(repo_id) in excluded:
            continue
        if repo_id not in discovered:
            entry['reason'] = 'repository_not_public_current'
            atomic_json(root / STATE, state)
            continue
        repo = entry.get('repository')
        if not isinstance(repo, dict):
            continue
        try:
            setting = config_repos.get(repo_id, {})
            text, sha = github.readme(repo)
            document = read_readme_document(text, repo)
            row = {'id': entry['resource_id'], **document['fields']}
            for key in ('title_ru', 'title_en', 'description_ru', 'description_en'):
                if setting.get(key):
                    row[key] = setting[key]
            if not translator.enrich(row):
                raise ITFailure('it_translation_pending')
            if not setting.get('site_url'):
                repo['_pages_url'] = github.pages(repo)
            site = select_website(repo, document, fetcher, setting.get('site_url'))
            row['url'] = site or 'https://github.com/' + repo['full_name']
            same = next((r for r in catalog['items'] if normalized_url(r['url']) == normalized_url(row['url'])), None)
            if same:
                entry.update(resource_id=same['id'], status='published', readme_sha=sha)
                entry.pop('reason', None)
                entry.pop('repository', None)
                atomic_json(root / STATE, state)
                continue
            row['tags'] = setting.get('tags', ['GitHub'])
            image, temporary_image_failure = None, False
            for source in document['images']:
                try:
                    data, _, _ = fetcher.fetch(source)
                    image = save_new_image(root, repo_id, data)
                    entry['image_origin'] = 'readme'
                    break
                except ITFailure as exc:
                    if exc.status not in (404, 410) and exc.reason not in ('image_invalid_or_small', 'unsafe_public_url', 'public_content_too_large'):
                        temporary_image_failure = True
            if image is None and site:
                if screenshots >= MAX_SCREENSHOTS:
                    raise ITFailure('screenshot_backlog')
                screenshots += 1
                temporary = root / 'data/it/audit' / ('screenshot-' + repo_id + '.png')
                metadata = screenshot(site, temporary, wait_seconds=60)
                image = save_new_image(root, repo_id, temporary.read_bytes())
                temporary.unlink(missing_ok=True)
                entry['image_origin'] = 'site_screenshot'
                entry['screenshot_wait_seconds'] = metadata['wait_seconds']
            if image is None:
                if temporary_image_failure:
                    raise ITFailure('readme_image_temporarily_unavailable')
                image = save_new_image(root, repo_id, NEUTRAL)
                entry['image_origin'] = 'neutral_no_site_or_image'
            row['thumb'] = image
            catalog = merge_catalog(catalog, {'generated_at': attempted, 'items': [row]}, config.get('featured_ids', []))
            atomic_json(root / CATALOG, catalog)
            visible.add(row['id'])
            added += 1
            entry.update(status='published', readme_sha=sha)
            for key in ('reason', 'repository'):
                entry.pop(key, None)
        except ITFailure as exc:
            entry.update(status='pending', reason=exc.reason)
            if exc.reason == 'github_rate_limited':
                failure = exc.reason
        except Exception:
            entry.update(status='pending', reason='it_candidate_processing_failed')
        atomic_json(root / STATE, state)
        if hasattr(translator, 'flush'):
            translator.flush()
    translator.save()
    pending = sum(1 for row in repositories.values() if row.get('status') != 'published')
    issues = validate_it_resources(root)
    status = 'success' if discovery_complete and not pending and not issues else 'partial' if discovered or added else 'error'
    report = {'status': status, 'attempted_at': attempted,
        'last_success_at': attempted if status == 'success' else old_report.get('last_success_at'),
        'origin': 'live' if discovered or discovery_complete else 'snapshot',
        'complete': status == 'success', 'record_count': len(catalog['items']),
        'reason': 'invalid_it_data' if issues else failure or ('it_candidates_pending' if pending else None),
        'discovery_complete': discovery_complete, 'discovered_count': len(discovered),
        'pending': pending, 'published_new': added, 'screenshots_attempted': screenshots,
        'pending_reasons': sorted({row.get('reason', 'not_processed') for row in repositories.values() if row.get('status') != 'published'})}
    atomic_json(root / REPORT, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    report = harvest(args.root)
    print(json.dumps({key: report[key] for key in ('status', 'record_count', 'pending', 'published_new', 'reason')}))
    return 0 if report['status'] == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
