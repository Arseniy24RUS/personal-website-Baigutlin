#!/usr/bin/env python3
"""Compare deployed public JSON with the validated local publication."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request

FILES = ('data/public/profile.json', 'data/public/publications.json', 'data/media/published.json', 'data/media/published-fallback.json')


def verification_files(scope='portfolio', root=Path('.')):
    names = [] if scope == 'it' else list(FILES)
    catalog = root / 'data/it/resources.json'
    if catalog.exists():
        names.append('data/it/resources.json')
        if scope == 'it':
            names.extend(('it.html', 'en/it.html', 'assets/css/site.css', 'assets/js/site.js'))
            payload = json.loads(catalog.read_text(encoding='utf-8'))
            for item in payload.get('items', []):
                thumb = item.get('thumb', '')
                if not thumb.startswith('assets/it/thumbs/') or '..' in Path(thumb).parts:
                    raise ValueError('IT thumbnails must be local portfolio assets.')
                names.append(thumb)
    elif scope == 'it':
        raise ValueError('IT catalog is required for deployed verification.')
    return list(dict.fromkeys(names))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait', type=int, default=0)
    parser.add_argument('--base-url', default='https://baigutlin.ru')
    parser.add_argument('--scope', choices=['portfolio', 'it'], default='portfolio')
    args = parser.parse_args()
    expected = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in verification_files(args.scope)}
    deadline = time.monotonic() + args.wait
    while True:
        good = []
        for name, digest in expected.items():
            try:
                request = urllib.request.Request(f'{args.base_url.rstrip("/")}/{name}?verify={digest[:12]}', headers={'Cache-Control': 'no-cache', 'User-Agent': 'PortfolioDeploymentCheck/1.0'})
                with urllib.request.urlopen(request, timeout=20) as response:
                    actual = hashlib.sha256(response.read()).hexdigest()
                good.append(actual == digest)
            except Exception:
                good.append(False)
        if all(good):
            print(f'Published {args.scope} files match validated output ({len(expected)} files).')
            return
        if time.monotonic() >= deadline:
            raise SystemExit('Published data does not yet match this validated commit.')
        time.sleep(min(15, max(0, deadline - time.monotonic())))

if __name__ == '__main__':
    main()
