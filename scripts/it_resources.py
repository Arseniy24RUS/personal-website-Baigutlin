"""Append-only IT catalog and the bounded set of files its publisher may merge."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit, urlunsplit

CATALOG = 'data/it/resources.json'
STATE = 'data/it/discovery_state.json'
CONFIG = 'data/it/config.json'
CACHE = 'data/it/translation_cache.json'
ASSETS = 'assets/it/thumbs'
FIELDS = ('id', 'title_ru', 'title_en', 'description_ru', 'description_en', 'url', 'thumb')


def normalized_url(value):
    parsed = urlsplit(value or '')
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.query, ''))


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else copy.deepcopy(default)


def atomic_json(path, value):
    path = Path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if path.is_file() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return True


def asset_path(root, value):
    if not isinstance(value, str) or '\\' in value:
        raise ValueError('invalid_it_asset_path')
    path = Path(value)
    if not value.startswith(ASSETS + '/') or path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid_it_asset_path')
    resolved = (Path(root) / path).resolve()
    if not resolved.is_relative_to((Path(root) / ASSETS).resolve()):
        raise ValueError('invalid_it_asset_path')
    return resolved


def merge_catalog(published, incoming, featured_ids=()):
    """Current objects win in full; never edit fields of a published card."""
    previous = published.get('items', [])
    additions, seen = [], {row['id'] for row in previous}
    urls = {normalized_url(row.get('url')) for row in previous if row.get('url')}
    for row in incoming.get('items', []):
        if row['id'] not in seen and normalized_url(row.get('url')) not in urls:
            additions.append(copy.deepcopy(row))
            seen.add(row['id'])
            if row.get('url'):
                urls.add(normalized_url(row['url']))
    if not additions:
        return copy.deepcopy(published)
    featured_ids = published.get('featured_ids', featured_ids)
    at = 0
    while at < len(previous) and previous[at]['id'] in featured_ids:
        at += 1
    result = copy.deepcopy(published)
    result['items'] = copy.deepcopy(previous[:at]) + additions + copy.deepcopy(previous[at:])
    if incoming.get('generated_at'):
        result['generated_at'] = incoming['generated_at']
    return result


def merge_discovery_state(published, incoming, catalog):
    result = copy.deepcopy(published or {'schema': 'it-discovery/v1', 'repositories': {}})
    target = result.setdefault('repositories', {})
    visible = {row['id'] for row in catalog.get('items', [])}
    for repo_id, record in (incoming or {}).get('repositories', {}).items():
        if not re.fullmatch(r'[1-9][0-9]*', str(repo_id)) or not isinstance(record, dict):
            raise ValueError('invalid_it_repository_identity')
        old = target.get(str(repo_id), {})
        if old.get('resource_id') in visible and record.get('resource_id') != old['resource_id']:
            raise ValueError('it_repository_alias_conflict')
        # A completed current-main card cannot become pending in an older run.
        chosen = old if old.get('resource_id') in visible else {**old, **record}
        chosen = copy.deepcopy(chosen)
        names = sorted(set(old.get('aliases', [])) | set(record.get('aliases', [])))
        if names:
            chosen['aliases'] = names
        if chosen.get('resource_id') in visible:
            chosen['status'] = 'published'
            for key in ('reason', 'candidate', 'retry_after', 'image_pending'):
                chosen.pop(key, None)
        target[str(repo_id)] = chosen
    return result


def validate_it_resources(root):
    root = Path(root)
    issues = []
    try:
        catalog = read_json(root / CATALOG, {})
        rows = catalog.get('items')
        if not isinstance(rows, list):
            return [{'code': 'invalid_it_catalog'}]
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in FIELDS):
                issues.append({'code': 'invalid_it_card'})
                continue
            if row['id'] in seen:
                issues.append({'code': 'duplicate_it_card', 'id': row['id']})
            seen.add(row['id'])
            url = urlsplit(row['url'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                issues.append({'code': 'invalid_it_url', 'id': row['id']})
            if not isinstance(row.get('tags', []), list):
                issues.append({'code': 'invalid_it_tags', 'id': row['id']})
            if not asset_path(root, row['thumb']).is_file():
                issues.append({'code': 'missing_it_image', 'id': row['id']})
        state = read_json(root / STATE, {'repositories': {}})
        featured = catalog.get('featured_ids', read_json(root / CONFIG, {}).get('featured_ids', []))
        if len(featured) != len(set(featured)) or [r.get('id') for r in rows[:len(featured)]] != featured:
            issues.append({'code': 'invalid_it_featured_order'})
        for repo_id, record in state.get('repositories', {}).items():
            resource_id = record.get('resource_id')
            # Multiple repository IDs may legitimately point to one application.
            if not resource_id:
                continue
            if record.get('status') == 'published' and resource_id not in seen:
                issues.append({'code': 'missing_published_it_card', 'id': resource_id})
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        issues.append({'code': 'invalid_it_data'})
    return issues


def merge_it_resources(candidate, destination):
    """Merge only IT publishable files; execution reports stay in the artifact."""
    candidate, destination = Path(candidate), Path(destination)
    if not (candidate / CATALOG).exists() and not (destination / CATALOG).exists():
        return {'added': 0, 'record_count': 0}
    config = read_json(destination / CONFIG, read_json(candidate / CONFIG, {}))
    old = read_json(destination / CATALOG, {'items': []})
    new = read_json(candidate / CATALOG, {'items': []})
    merged = merge_catalog(old, new, config.get('featured_ids', []))
    incoming_state = read_json(candidate / STATE, {})
    by_url = {normalized_url(row.get('url')): row['id'] for row in merged.get('items', [])}
    aliases = {row['id']: by_url[normalized_url(row.get('url'))] for row in new.get('items', [])
               if normalized_url(row.get('url')) in by_url}
    for entry in incoming_state.get('repositories', {}).values():
        if entry.get('resource_id') in aliases:
            entry['resource_id'] = aliases[entry['resource_id']]
    state = merge_discovery_state(read_json(destination / STATE, {}), incoming_state, merged)
    cache = read_json(candidate / CACHE, {})
    cache.update(read_json(destination / CACHE, {}))
    # Check all collisions before writing anything. Previously committed assets,
    # including unreferenced ones, must survive byte for byte.
    copies = []
    for source in sorted((candidate / ASSETS).rglob('*')) if (candidate / ASSETS).is_dir() else []:
        if not source.is_file():
            continue
        relative = source.relative_to(candidate).as_posix()
        target = asset_path(destination, relative)
        if target.exists():
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                raise ValueError('it_published_asset_conflict')
        else:
            copies.append((source, target))
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    if merged != old:
        atomic_json(destination / CATALOG, merged)
    if state:
        atomic_json(destination / STATE, state)
    if cache:
        atomic_json(destination / CACHE, cache)
    if not (destination / CONFIG).exists() and config:
        atomic_json(destination / CONFIG, config)
    issues = validate_it_resources(destination)
    if issues:
        raise ValueError('invalid_merged_it_resources')
    return {'added': len(merged['items']) - len(old.get('items', [])), 'record_count': len(merged['items'])}
