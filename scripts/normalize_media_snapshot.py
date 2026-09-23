#!/usr/bin/env python3
"""Offline append-only normalization; never replace reviewed existing fields."""
import argparse
import json
import subprocess
from pathlib import Path
from harvest_media_mentions import merge_records, read_json, write_json, now, payload_records, normalize_legacy_record


def normalize():
    root = Path('data/media')
    groups = []
    for name in ('published.json', 'news_mentions.json', 'published-fallback.json'):
        try:
            committed = subprocess.check_output(['git', 'show', 'HEAD:data/media/' + name], stderr=subprocess.DEVNULL)
            groups.append(payload_records(json.loads(committed)))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            pass
        groups.append(payload_records(read_json(root / name, {})))
    records = []
    for group in groups:
        records = merge_records(records, [normalize_legacy_record(record) for record in group])
    payload = {'generated_at': now(), 'records': records}
    for name in ('published.json', 'news_mentions.json', 'published-fallback.json'):
        write_json(root / name, payload)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--normalize-only', action='store_true')
    parser.parse_args()
    print(json.dumps({'published': len(normalize())}))


if __name__ == '__main__':
    main()
