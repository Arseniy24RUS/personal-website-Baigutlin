#!/usr/bin/env python3
"""Exercise a real unseeded README, translation and image in a disposable catalog."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from harvest_it_resources import GitHubClient, harvest
from it_resources import CATALOG, CONFIG, STATE, atomic_json, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--repository', help='Optional owner/repository to probe; defaults to the first public repository.')
    args = parser.parse_args()
    owner = read_json(args.root / CONFIG, {}).get('owner')
    if not owner:
        raise RuntimeError('Set owner in data/it/config.json before running the live probe.')
    client = GitHubClient(os.environ.get('IT_GITHUB_TOKEN'))
    repositories = [repo for batch in client.repositories(owner) for repo in batch if not repo.get('private')]
    selected = [repo for repo in repositories if repo.get('full_name') == args.repository] if args.repository else repositories[:1]
    if len(selected) != 1:
        raise RuntimeError('The public live acceptance repository is unavailable.')
    target_id = selected[0]['id']
    with tempfile.TemporaryDirectory(prefix='portfolio-it-live-') as temporary:
        root = Path(temporary)
        atomic_json(root / CATALOG, {'items': []})
        atomic_json(root / CONFIG, {'owner': owner, 'repositories': {},
            'excluded_repo_ids': [repo['id'] for repo in repositories if repo['id'] != target_id]})
        first = harvest(root, github=client)
        catalog = read_json(root / CATALOG)
        if first['status'] != 'success' or first['published_new'] != 1 or len(catalog['items']) != 1:
            raise RuntimeError('Live unseeded IT collection did not produce one complete card: ' + str(first.get('reason')))
        card = catalog['items'][0]
        source = read_json(root / STATE)['repositories'][str(target_id)]
        if source.get('image_origin') not in ('readme', 'site_screenshot', 'neutral_no_site_or_image'):
            raise RuntimeError('Live discovery did not publish a verified illustration or explicit fallback.')
        if not all(card.get(field) for field in ('title_ru', 'title_en', 'description_ru', 'description_en')):
            raise RuntimeError('Live bilingual card is incomplete.')
        cache = read_json(root / 'data/it/translation_cache.json', {})
        verified_pairs = sorted({entry['pair'] for entry in cache.values()
                                 if entry.get('model', {}).get('probe_passed') and entry.get('pair')})
        before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
                  if path.is_file() and 'audit' not in path.parts}
        second = harvest(root, github=client)
        after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
                 if path.is_file() and 'audit' not in path.parts}
        if second['status'] != 'success' or second['published_new'] != 0 or before != after:
            raise RuntimeError('Live repeated discovery changed a previously published card or its files.')
        result = {'status': 'success', 'repository': selected[0]['full_name'],
                  'public_repositories_seen': len(repositories), 'first_run_added': 1,
                  'second_run_added': 0, 'manual_overrides_used': False,
                  'bilingual_card_complete': True, 'verified_translation_pairs': verified_pairs,
                  'image_origin': source['image_origin'], 'published_files_unchanged_on_repeat': True}
        atomic_json(args.report, result)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
