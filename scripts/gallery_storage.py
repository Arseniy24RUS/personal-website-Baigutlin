"""Transactional, additive storage shared by the two document galleries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> dict:
    if not path.exists():
        return {'items': []}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise ValueError(f'Invalid existing gallery: {path}')
    if any(not isinstance(item, dict) or not item.get('id') for item in payload['items']):
        raise ValueError(f'Invalid existing gallery items: {path}')
    return payload


def prepare_sources(input_dir: Path, work: Path, extensions: set[str]) -> list[Path]:
    """Extract into a new private staging directory, never into published assets."""
    input_dir.mkdir(parents=True, exist_ok=True)
    direct = work / 'direct-files'
    direct.mkdir(parents=True, exist_ok=True)
    for source in sorted(input_dir.rglob('*')):
        if source.is_file() and source.suffix.lower() in extensions:
            target = direct / source.relative_to(input_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    archives = sorted(input_dir.glob('*.zip'))
    for index, archive in enumerate(archives):
        root = (work / f'archive-{index}').resolve()
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = member.filename.replace('\\', '/')
                target = (root / name).resolve()
                if not target.is_relative_to(root) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Unsafe ZIP member')
            bundle.extractall(root)
    return archives


def publish(manifest_path: Path, payload: dict, staged_assets: list[tuple[Path, Path]]) -> None:
    """All inputs must render successfully before any manifest change is made."""
    for staged, target in staged_assets:
        if target.exists() and staged.read_bytes() != target.read_bytes():
            raise ValueError(f'Refusing to replace an existing gallery asset: {target}')
    for staged, target in staged_assets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.replace(staged, target)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, manifest_path)
