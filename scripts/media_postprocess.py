#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit
import hashlib
import html
import io
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

OUT = Path('data/media')
PUBLISHED = OUT / 'published.json'
NEWS = OUT / 'news_mentions.json'
REJECTED = OUT / 'rejected_or_low_confidence.json'
REPORT = OUT / 'postprocess_report.json'
IMAGE_DIR = Path('assets/media/mentions')

BLOCKED_URL_PATTERNS = [
    # Dissertation candidate profiles belong to academic activity, not news.
    re.compile(r'csu\.ru/science/diss-sovet/.*/phd-candidates/', re.I),
]

BLOCKED_TITLES = {
    'байгутлин данил расулович',
    'danil rasulovich baigutlin',
}

STATIC_EXT = re.compile(
    r'\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|otf|pdf|docx?|xlsx?|pptx?|zip|rar|7z|mp3|mp4|avi|mov)(\?.*)?$',
    re.I,
)
BAD_IMAGE_HINTS = re.compile(r'logo|icon|sprite|avatar|emoji|telegram\.org/img/emoji|mc\.yandex|/ad-link/|counter|pixel|favicon|greenline|spacer|transparent', re.I)
IMAGE_EXT_BY_TYPE = {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/gif': '.gif',
}
RU_MONTHS = {
    'января': '01',
    'февраля': '02',
    'марта': '03',
    'апреля': '04',
    'мая': '05',
    'июня': '06',
    'июля': '07',
    'августа': '08',
    'сентября': '09',
    'октября': '10',
    'ноября': '11',
    'декабря': '12',
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(text: str | None) -> str:
    return re.sub(r'\s+', ' ', html.unescape(text or '').replace('\xa0', ' ')).strip()


def canonical(url: str | None) -> str:
    if not url:
        return ''
    try:
        p = urlparse(str(url).strip())
        return p._replace(fragment='').geturl().rstrip('/')
    except Exception:
        return str(url).split('#')[0].rstrip('/')


def title_key(record: dict) -> str:
    text = str(record.get('title') or record.get('title_ru') or record.get('title_en') or '')
    return clean(text).lower().replace('ё', 'е')


def is_blocked_record(record: dict) -> bool:
    url = canonical(record.get('url'))
    if not url:
        return True
    if STATIC_EXT.search(url):
        return True
    if any(rx.search(url) for rx in BLOCKED_URL_PATTERNS):
        return True
    return title_key(record) in BLOCKED_TITLES


def safe_url(url: str) -> str:
    if url.startswith('//'):
        url = 'https:' + url
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe='/%')
    query = quote(unquote(parts.query), safe='=&?/:;%+,+')
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _fetch_image_bytes(url: str, max_bytes: int, deadline: float) -> tuple[bytes | None, dict]:
    info = {'url': url}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, {**info, 'status': 'timeout'}
        req = urllib.request.Request(
            safe_url(url),
            headers={
                'User-Agent': 'Mozilla/5.0 media-postprocess/1.0',
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            },
        )
        with urllib.request.urlopen(req, timeout=remaining) as resp:
            # read1 returns after one underlying read, so a drip-fed response
            # cannot keep an unbounded read(max_bytes) alive indefinitely.
            chunks, size = [], 0
            while size <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, {**info, 'status': 'timeout'}
                read = getattr(resp, 'read1', resp.read)
                chunk = read(min(65_536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b''.join(chunks)
            raw_ctype = resp.headers.get('content-type') or ''
            ctype = raw_ctype.split(';')[0].lower().strip()
            info.update({'status': 'ok', 'http_status': resp.status, 'content_type': ctype, 'raw_content_type': raw_ctype, 'bytes': len(data)})
            if len(data) > max_bytes:
                return None, {**info, 'status': 'too_large'}
            return data, info
    except urllib.error.HTTPError as exc:
        return None, {**info, 'status': 'http_error', 'http_status': exc.code}
    except Exception as exc:
        return None, {**info, 'status': 'error', 'error': repr(exc)[:240]}


def fetch_bytes(url: str, *, max_bytes: int = 8_000_000, deadline: float | None = None) -> tuple[bytes | None, dict]:
    """Bound the whole optional download, including DNS and redirects.

    urllib's socket timeout does not bound DNS, redirect chains or a slow
    response. A daemon worker may finish its read after the deadline, but it
    only returns bytes: it never writes assets or modifies a published record.
    """
    started = time.monotonic()
    if deadline is not None and deadline <= started:
        return None, {'url': url, 'status': 'budget_exhausted'}
    request_deadline = min(started + 35, deadline) if deadline is not None else started + 35
    result = queue.Queue(maxsize=1)

    def download():
        result.put(_fetch_image_bytes(url, max_bytes, request_deadline))

    threading.Thread(target=download, daemon=True, name='optional-media-image').start()
    try:
        response = result.get(timeout=max(0, request_deadline - time.monotonic()))
    except queue.Empty:
        response = (None, {'url': url, 'status': 'timeout'})
    if deadline is not None and time.monotonic() >= deadline:
        return None, {'url': url, 'status': 'budget_exhausted'}
    if time.monotonic() >= request_deadline:
        return None, {'url': url, 'status': 'timeout'}
    return response


def fetch_text(url: str) -> tuple[str, dict]:
    info = {'url': url}
    try:
        req = urllib.request.Request(
            safe_url(url),
            headers={
                'User-Agent': 'Mozilla/5.0 media-postprocess/1.0',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            },
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = resp.read(4_000_001)
            raw_ctype = resp.headers.get('content-type') or ''
            ctype = raw_ctype.split(';')[0].lower().strip()
            info.update({'status': 'ok', 'http_status': resp.status, 'content_type': ctype, 'raw_content_type': raw_ctype, 'bytes': len(data)})
            if len(data) > 4_000_000:
                return '', {**info, 'status': 'too_large'}
    except urllib.error.HTTPError as exc:
        return '', {**info, 'status': 'http_error', 'http_status': exc.code}
    except Exception as exc:
        return '', {**info, 'status': 'error', 'error': repr(exc)[:240]}
    try:
        enc_match = re.search(r'charset=([A-Za-z0-9._-]+)', str(info.get('raw_content_type') or ''), re.I)
        text = data.decode(enc_match.group(1) if enc_match else 'utf-8', errors='replace')
    except Exception:
        text = data.decode(errors='replace')
    return text, info


def meta_content(soup, *names: str) -> str | None:
    if not soup:
        return None
    for name in names:
        tag = (
            soup.find('meta', attrs={'property': name})
            or soup.find('meta', attrs={'name': name})
            or soup.find('meta', attrs={'itemprop': name})
            or soup.find('link', attrs={'itemprop': name})
        )
        if tag and tag.get('content'):
            return clean(tag.get('content'))
        if tag and tag.get('href'):
            return clean(tag.get('href'))
    return None


def usable_image_url(url: str | None) -> bool:
    if not url:
        return False
    if url.startswith('data:'):
        return False
    if BAD_IMAGE_HINTS.search(url):
        return False
    parsed = urlparse(url)
    if parsed.path.lower().endswith('.svg'):
        return False
    return True


def image_candidates_from_html(html_text: str, page_url: str) -> list[str]:
    out: list[str] = []
    if BeautifulSoup:
        soup = BeautifulSoup(html_text or '', 'html.parser')
        for selector in [
            '.post .text-content img',
            '.post-head img',
            '.page-detail img',
            '.page-detail-content img',
            'article img',
            'main img',
            '.content img',
            'img',
        ]:
            for img in soup.select(selector):
                raw = img.get('src') or img.get('data-src') or img.get('data-original') or img.get('data-lazy-src')
                cand = urljoin(page_url, clean(raw)) if raw else ''
                if usable_image_url(cand) and cand not in out:
                    out.append(cand)
    else:
        for match in re.finditer(r'<img[^>]+(?:src|data-src|data-original)=["\']([^"\']+)["\']', html_text or '', re.I):
            cand = urljoin(page_url, clean(match.group(1)))
            if usable_image_url(cand) and cand not in out:
                out.append(cand)
    return out


def parse_date_value(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    if re.match(r'^\d{4}-\d{2}-\d{2}(?:T.*)?$', value):
        return value
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    match = re.search(r'(\d{1,2})\s+([А-Яа-яЁё]+)\s+(20\d{2}|19\d{2})', value)
    if match:
        day, month, year = match.groups()
        month_num = RU_MONTHS.get(month.lower().replace('ё', 'е'))
        if month_num:
            return f'{year}-{month_num}-{int(day):02d}'
    match = re.search(r'(\d{2})\.(\d{2})\.(20\d{2}|19\d{2})', value)
    if match:
        day, month, year = match.groups()
        return f'{year}-{month}-{day}'
    return None


def generic_meta(html_text: str, page_url: str) -> dict:
    if not html_text:
        return {}
    if BeautifulSoup:
        soup = BeautifulSoup(html_text, 'html.parser')
        title = meta_content(soup, 'og:title', 'twitter:title', 'headline', 'name') or (clean(soup.title.get_text(' ')) if soup.title else '')
        heading = soup.select_one('article h1, main h1, h1, .page-detail__title, .b-post-view__title')
        if heading:
            title = clean(heading.get_text(' '))
        desc = meta_content(soup, 'og:description', 'twitter:description', 'description')
        image = meta_content(soup, 'og:image', 'twitter:image', 'vk:image', 'image')
        date = (
            meta_content(soup, 'article:published_time', 'datePublished', 'publish_date', 'date')
            or clean(node.get_text(' ') if (node := soup.select_one('.post .post-head__date, .page-detail-nib__item--date, time')) else '')
        )
    else:
        def meta_rx(name):
            rx = re.compile(r'<meta[^>]+(?:property|name)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(name) + r'["\']', re.I)
            m = rx.search(html_text)
            return clean(m.group(1) or m.group(2)) if m else None
        title = meta_rx('og:title') or meta_rx('twitter:title') or ''
        desc = meta_rx('og:description') or meta_rx('twitter:description') or meta_rx('description')
        image = meta_rx('og:image') or meta_rx('twitter:image') or meta_rx('vk:image')
        date = meta_rx('article:published_time')
    candidates = image_candidates_from_html(html_text, page_url)
    if not usable_image_url(image):
        image = candidates[0] if candidates else None
    return {
        'title': clean(title),
        'description': clean(desc),
        'image': urljoin(page_url, image) if image else None,
        'published_at': parse_date_value(date),
    }


def telegram_meta(record: dict) -> dict:
    url = canonical(record.get('url'))
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip('/').split('/') if p]
    if len(parts) < 2:
        return {}
    channel, post_id = parts[0], parts[1]
    direct_html, _ = fetch_text(url)
    meta = generic_meta(direct_html, url)
    thread_url = f'https://t.me/s/{channel}/{post_id}'
    thread_html, _ = fetch_text(thread_url)
    if BeautifulSoup and thread_html:
        soup = BeautifulSoup(thread_html, 'html.parser')
        block = None
        for item in soup.select('.tgme_widget_message'):
            data_post = item.get('data-post') or ''
            if data_post.startswith(f'{channel}/{post_id}'):
                block = item
                break
        if block:
            time_tag = block.select_one('a.tgme_widget_message_date time')
            if time_tag and time_tag.get('datetime'):
                meta['published_at'] = parse_date_value(time_tag.get('datetime')) or meta.get('published_at')
            photo = block.select_one('.tgme_widget_message_photo_wrap')
            if photo and photo.get('style'):
                match = re.search(r'background-image:url\([\'"]?([^\'")]+)', photo.get('style') or '')
                if match and usable_image_url(match.group(1)):
                    meta['image'] = urljoin(thread_url, match.group(1))
    elif thread_html:
        idx = thread_html.find(f'data-post="{channel}/{post_id}')
        if idx >= 0:
            chunk = thread_html[idx:idx + 20000]
            match = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', chunk)
            if match:
                meta['published_at'] = parse_date_value(match.group(1)) or meta.get('published_at')
            match = re.search(r'tgme_widget_message_photo_wrap[^>]+background-image:url\([\'"]?([^\'")]+)', chunk)
            if match and usable_image_url(match.group(1)):
                meta['image'] = urljoin(thread_url, match.group(1))
    return meta


def record_meta(record: dict) -> dict:
    url = canonical(record.get('url'))
    if urlparse(url).netloc.lower().endswith('t.me'):
        return telegram_meta(record)
    html_text, _ = fetch_text(url)
    return generic_meta(html_text, url)


def detect_lang(text: str | None) -> str | None:
    if re.search(r'[А-Яа-яЁё]', text or ''):
        return 'ru'
    if re.search(r'[A-Za-z]', text or ''):
        return 'en'
    return None


def update_localized(record: dict, title: str | None = None, desc: str | None = None) -> None:
    if title:
        if not record.get('title'):
            record['title'] = title
        lang = detect_lang(title)
        if lang and not record.get(f'title_{lang}'):
            record[f'title_{lang}'] = title
    if desc:
        if not record.get('description'):
            record['description'] = desc
        lang = detect_lang(desc)
        if lang and not record.get(f'description_{lang}'):
            record[f'description_{lang}'] = desc
    if not record.get('language'):
        record['language'] = detect_lang(record.get('title')) or detect_lang(record.get('description')) or 'und'


def image_ext(url: str, ctype: str, data: bytes) -> str:
    if ctype in IMAGE_EXT_BY_TYPE:
        return IMAGE_EXT_BY_TYPE[ctype]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        return '.jpg' if suffix == '.jpeg' else suffix
    if data.startswith(b'\x89PNG'):
        return '.png'
    if data.startswith(b'RIFF') and b'WEBP' in data[:16]:
        return '.webp'
    if data.startswith(b'GIF'):
        return '.gif'
    return '.jpg'


def mirror_image(record: dict, image_url: str | None, *, deadline: float | None = None) -> dict:
    """Keep a usable previous image for every transport/validation failure."""
    previous = record.get('image')
    if previous and previous.startswith('assets/') and Path(previous).exists():
        return {'status': 'cached', 'local': previous}
    if deadline is not None and time.monotonic() >= deadline:
        return {'status': 'budget_exhausted'}
    if not image_url:
        return {'status': 'missing'}
    image_url = urljoin(record.get('url') or '', image_url)
    if not usable_image_url(image_url):
        return {'status': 'invalid_url'}
    data, info = fetch_bytes(image_url, deadline=deadline)
    if not data:
        return info
    signature_ok = data.startswith((b'\xff\xd8', b'\x89PNG', b'GIF')) or (data.startswith(b'RIFF') and b'WEBP' in data[:16])
    if not signature_ok:
        return {'status': 'not_image'}
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except ImportError:
        return {'status': 'validation_unavailable'}
    except Exception:
        return {'status': 'invalid_image'}
    if deadline is not None and time.monotonic() >= deadline:
        return {'status': 'budget_exhausted'}
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    rec_id = record.get('id') or hashlib.sha256((record.get('url') or image_url).encode()).hexdigest()[:16]
    ext = image_ext(image_url, str(info.get('content_type') or ''), data)
    target = IMAGE_DIR / f'{rec_id}{ext}'
    staged = target.with_suffix(target.suffix + '.tmp')
    staged.write_bytes(data)
    staged.replace(target)
    record.update(image_original_url=image_url, image=target.as_posix(), image_status='cached', image_checked_at=now())
    return {**info, 'status': 'cached', 'local': target.as_posix()}


def sort_key(record: dict) -> tuple[str, str]:
    published = parse_date_value(record.get('published_at')) or ''
    return (published, str(record.get('title') or ''))


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    records = payload if isinstance(payload, list) else payload.get('records', [])
    if not isinstance(records, list):
        raise ValueError('invalid_media_records')
    return records


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'generated_at': now(), 'records': records}, ensure_ascii=False, indent=2), encoding='utf-8')


def postprocess_records(records: list[dict], *, enrich: bool = True, mirror: bool = True, reports: list[dict] | None = None) -> list[dict]:
    out: list[dict] = []
    seen = set()
    reports = reports if reports is not None else []
    for original in records:
        if not original:
            continue
        record = dict(original)
        key = canonical(record.get('url')) or record.get('id')
        if not key or key in seen:
            continue
        seen.add(key)
        meta = {}
        if enrich and record.get('url'):
            try:
                meta = record_meta(record)
            except Exception as exc:
                reports.append({'url': record.get('url'), 'status': 'metadata_error', 'error': repr(exc)[:240]})
        if meta:
            # Published text is reviewed; localized legacy fields also win.
            update_localized(record,
                             record.get('title') or record.get('title_ru') or record.get('title_en') or meta.get('title'),
                             record.get('description') or record.get('description_ru') or record.get('description_en') or meta.get('description'))
            if not record.get('published_at') and meta.get('published_at'):
                record['published_at'] = record.get('date') or meta.get('published_at')
            if meta.get('image') and not record.get('image'):
                record['image'] = meta.get('image')
        if mirror:
            reports.append({'record': record.get('id'), 'url': record.get('url'), 'image': mirror_image(record, record.get('image'))})
            time.sleep(0.03)
        out.append(record)
    out.sort(key=sort_key, reverse=True)
    return out


def postprocess_media_files(*, enrich: bool = True, mirror: bool = True) -> dict:
    reports: list[dict] = []
    published = postprocess_records(read_records(PUBLISHED), enrich=enrich, mirror=mirror, reports=reports)
    rejected = postprocess_records(read_records(REJECTED), enrich=False, mirror=False, reports=reports)
    write_records(PUBLISHED, published)
    write_records(NEWS, published)
    write_records(OUT / 'published-fallback.json', published)
    write_records(REJECTED, rejected)
    payload = {
        'generated_at': now(),
        'published': len(published),
        'rejected_or_low_confidence': len(rejected),
        'image_cache_dir': IMAGE_DIR.as_posix(),
        'reports': reports,
    }
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def main() -> int:
    print(json.dumps(postprocess_media_files(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
