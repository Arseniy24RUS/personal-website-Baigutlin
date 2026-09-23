#!/usr/bin/env python3
"""Reject destructive refreshes by comparing published identities with a Git baseline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from report_safety import canonical_wos_url

DATASETS = {
    'data/public/publications.json': 'publications',
    'data/media/published.json': 'media',
    'data/media/published-fallback.json': 'media',
    'data/risi/articles.json': 'risi',
    'data/diplomas/gallery.json': 'gallery',
    'data/dpo/gallery.json': 'gallery',
    'data/it/resources.json': 'it',
}
PROTECTED = {
    'title', 'title_ru', 'title_en', 'venue', 'venue_ru', 'venue_en',
    'authors_raw', 'doi', 'url', 'year', 'volume', 'issue', 'pages',
    'gost_ru', 'apa_en', 'description_ru', 'description_en',
    'source_name_ru', 'source_name_en', 'html', 'docx', 'thumb', 'full',
    'download', 'source_hash', 'original_filename', 'source_filename',
}


def present(value):
    return value is not None and value != '' and value != [] and value != {}


def normalized_url(value):
    parts = urlsplit(canonical_wos_url(str(value or '').strip()))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), parts.query, ''))


def records(payload):
    if isinstance(payload, list):
        result = payload
    elif isinstance(payload, dict):
        result = payload.get('records', payload.get('items'))
    else:
        result = None
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ValueError('Expected a list of record objects')
    return result


def identity_tokens(row):
    tokens = set()
    for key in ('id', 'elibrary_item_id', 'wos_uid', 'dedupe_fingerprint', 'source_hash'):
        if present(row.get(key)):
            tokens.add((key, str(row[key])))
    if row.get('doi'):
        tokens.add(('doi', re.sub(r'^https?://(?:dx\.)?doi\.org/', '', str(row['doi']).lower()).rstrip('.,;')))
    if row.get('url'):
        tokens.add(('url', normalized_url(row['url'])))
    if not tokens:
        title = row.get('title') or row.get('title_ru') or row.get('title_en')
        tokens.add(('title_year', re.sub(r'\s+', ' ', str(title or '')).strip().lower(), str(row.get('year') or '')))
    return tokens


def local_assets(value):
    found = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(local_assets(child))
    elif isinstance(value, list):
        for child in value:
            found.update(local_assets(child))
    elif isinstance(value, str):
        path = value.split('?', 1)[0].split('#', 1)[0].lstrip('/')
        if path.startswith(('assets/', 'content/', 'data/risi/articles/')) and '\n' not in path:
            found.add(path)
    return found


def compare_records(before, after, kind):
    """One-to-one matching deliberately preserves separately published duplicates."""
    if kind == 'it':
        issues = []
        positions = []
        for index, old in enumerate(before):
            matches = [i for i, row in enumerate(after) if row.get('id') == old.get('id')]
            if len(matches) != 1:
                issues.append({'code': 'record_removed' if not matches else 'duplicate_identity',
                               'index': index, 'id': old.get('id')})
                continue
            position = matches[0]
            positions.append(position)
            # Entire objects, including empty fields, tags and future metadata,
            # are immutable once a resource has been published.
            if old != after[position]:
                issues.append({'code': 'published_it_card_changed', 'index': index,
                               'id': old.get('id')})
        if positions != sorted(positions):
            issues.append({'code': 'published_it_order_changed'})
        return issues
    issues = []
    available = set(range(len(after)))
    after_tokens = [identity_tokens(row) for row in after]
    for index, old in enumerate(before):
        tokens = identity_tokens(old)
        candidates = [i for i in available if tokens & after_tokens[i]]
        # Prefer an exact stable identity to a shared DOI, then preserved metadata.
        def score(i):
            overlap = tokens & after_tokens[i]
            stable = sum(1 for token in overlap if token[0] in {'id', 'elibrary_item_id', 'dedupe_fingerprint', 'source_hash', 'wos_uid'})
            same = sum(old.get(k) == after[i].get(k) for k in PROTECTED if present(old.get(k)))
            return stable, same, len(overlap)
        if not candidates:
            issues.append({'code': 'record_removed', 'index': index, 'identity': sorted(tokens)})
            continue
        target = max(candidates, key=score)
        available.remove(target)
        new = after[target]
        # All existing bibliography and curated localized copy are protected.
        # Provider metrics/raw metadata may change after verified collection.
        protected = PROTECTED if kind != 'media' or old.get('seed_metadata_locked') else {
            'title_ru', 'title_en', 'description_ru', 'description_en', 'url'
        }
        for key in protected:
            if not present(old.get(key)):
                continue
            current = new.get(key)
            equal = normalized_url(old[key]) == normalized_url(current) if key == 'url' else old[key] == current
            if not equal:
                issues.append({'code': 'protected_metadata_changed', 'index': index, 'field': key})
        for key in ('id', 'elibrary_item_id', 'dedupe_fingerprint', 'wos_uid', 'source_hash'):
            if present(old.get(key)) and old[key] != new.get(key):
                issues.append({'code': 'identity_changed', 'index': index, 'field': key})
        # Once published, the observation time for a provider cannot disappear
        # or move backwards during a concurrent merge or partial refresh.
        for provider, stamp in (old.get('citation_observed_at') or {}).items():
            try:
                prior = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                current = datetime.fromisoformat(new['citation_observed_at'][provider].replace('Z', '+00:00'))
                valid = current >= prior
            except (ValueError, TypeError, KeyError, AttributeError):
                valid = False
            if not valid:
                issues.append({'code': 'citation_observation_regressed', 'index': index, 'provider': provider})
        missing_assets = local_assets(old) - local_assets(new)
        if missing_assets:
            issues.append({'code': 'record_asset_reference_removed', 'index': index, 'assets': sorted(missing_assets)})
    return issues


def git_output(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], stderr=subprocess.PIPE)


def baseline_blobs(root, commit):
    """NUL-delimited tree output preserves spaces and non-ASCII asset names."""
    blobs = {}
    for entry in git_output(root, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        _mode, kind, object_id = metadata.split(b' ')
        if kind == b'blob':
            blobs[path.decode('utf-8')] = object_id.decode('ascii')
    return blobs


def working_blob_hashes(root, paths):
    if not paths:
        return {}
    # Git applies the same clean filters/line-ending normalization as git add.
    # C-style quoted paths also support spaces, quotes and embedded newlines.
    names = ''.join(json.dumps(path, ensure_ascii=False) + '\n' for path in paths)
    output = subprocess.check_output(
        ['git', '-C', str(root), 'hash-object', '--stdin-paths'],
        input=names.encode('utf-8'), stderr=subprocess.PIPE)
    hashes = output.decode('ascii').splitlines()
    if len(hashes) != len(paths):
        raise ValueError('Git did not return a hash for every published asset')
    return dict(zip(paths, hashes))


def validate(root: Path, baseline_ref: str, scope='portfolio'):
    commit = git_output(root, 'rev-parse', '--verify', baseline_ref + '^{commit}').decode().strip()
    blobs = baseline_blobs(root, commit)
    baseline_files = set(blobs)
    issues = []
    checked = {}
    for path, kind in DATASETS.items():
        if scope == 'it' and kind != 'it':
            continue
        if path not in baseline_files:
            continue
        try:
            before_payload = json.loads(git_output(root, 'show', f'{commit}:{path}'))
            before = records(before_payload)
            after_payload = json.loads((root / path).read_text(encoding='utf-8'))
            after = records(after_payload)
            checked[path] = {'before': len(before), 'after': len(after)}
            issues.extend({'path': path, **issue} for issue in compare_records(before, after, kind))
            if kind == 'it' and before_payload.get('featured_ids'):
                if before_payload['featured_ids'] != after_payload.get('featured_ids'):
                    issues.append({'path': path, 'code': 'published_it_featured_order_changed'})
            for asset in local_assets(before_payload):
                if asset in baseline_files and not (root / asset).is_file():
                    issues.append({'path': path, 'code': 'referenced_asset_missing', 'asset': asset})
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            issues.append({'path': path, 'code': 'invalid_or_missing_dataset', 'error': str(exc)})
    # A baseline asset must survive even when it has no current card reference.
    prefixes = ('assets/it/',) if scope == 'it' else ('assets/', 'data/risi/articles/', 'content/risi/')
    assets = sorted(path for path in baseline_files if path.startswith(prefixes))
    available_assets = []
    for path in assets:
        if not (root / path).is_file():
            issues.append({'path': path, 'code': 'published_asset_removed'})
        else:
            available_assets.append(path)
    hashes = {}
    try:
        hashes = working_blob_hashes(root, available_assets)
        for path, object_id in hashes.items():
            if object_id != blobs[path]:
                issues.append({'path': path, 'code': 'published_asset_modified',
                               'baseline_blob': blobs[path], 'current_blob': object_id})
    except (OSError, ValueError, subprocess.CalledProcessError):
        issues.append({'code': 'published_asset_content_check_failed'})
    return {'status': 'success' if not issues else 'error', 'baseline_ref': baseline_ref,
            'baseline_commit': commit, 'datasets': checked, 'assets_checked': len(assets),
            'assets_content_checked': len(hashes), 'issues': issues}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--report', type=Path)
    parser.add_argument('--scope', choices=['portfolio', 'it'], default='portfolio')
    args = parser.parse_args(argv)
    try:
        result = validate(args.root, args.baseline_ref, args.scope)
    except subprocess.CalledProcessError:
        result = {'status': 'error', 'issues': [{'code': 'baseline_unavailable'}]}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + '\n', encoding='utf-8')
    print(output)
    return 0 if result['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
