#!/usr/bin/env python3
"""Build a static diplomas/certificates gallery from user-uploaded files.

Universal workflow for future scientist portfolios:

1. Put one or more ZIP archives with any file names into content/diplomas/.
2. Optionally put standalone PDF/JPG/PNG/WebP files into content/diplomas/.
3. Run GitHub Action "Build diplomas gallery" manually.

The script recursively extracts all ZIP archives, processes images and the first
page of PDFs, creates lightweight thumbnails and full-screen WebP versions, and
writes data/diplomas/gallery.json for diplomas.html.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import re
from tempfile import TemporaryDirectory

from gallery_storage import prepare_sources, publish, read_manifest, source_hash

from PIL import Image, ImageOps

ROOT = Path('.')
INPUT_DIR = ROOT / 'content' / 'diplomas'
WORK = ROOT / '.tmp_diplomas'
THUMBS = ROOT / 'assets' / 'diplomas' / 'thumbs'
FULL = ROOT / 'assets' / 'diplomas' / 'full'
OUT = ROOT / 'data' / 'diplomas' / 'gallery.json'
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}
PDF_EXTS = {'.pdf'}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS


def slugify(value: str) -> str:
    value = value.lower().replace('ё', 'e')
    table = str.maketrans({
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ж':'zh','з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya'
    })
    value = value.translate(table)
    value = re.sub(r'[^a-z0-9]+', '-', value).strip('-')
    return value[:90] or 'diploma'


def year_from_name(name: str):
    years = re.findall(r'(20\d{2}|19\d{2})', name)
    return int(years[-1]) if years else None


def title_from_name(path: Path) -> str:
    name = re.sub(r'[_-]+', ' ', path.stem)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:1].upper() + name[1:] if name else 'Диплом / сертификат'


def prepare_workdir():
    return prepare_sources(INPUT_DIR, WORK, SUPPORTED_EXTS)


def collect_source_files():
    files = []
    for p in WORK.rglob('*'):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            # Ignore macOS service files and temporary artefacts inside archives.
            if '__MACOSX' in p.parts or p.name.startswith('._'):
                continue
            files.append(p)
    # Newest first by inferred year. Unknown years go last. Stable title order inside a year.
    files.sort(key=lambda p: (-(year_from_name(p.as_posix()) or -1), p.as_posix().lower()))
    return files


def load_image(path: Path) -> Image.Image:
    if path.suffix.lower() in PDF_EXTS:
        import fitz  # PyMuPDF is needed only for PDF inputs
        doc = fitz.open(path)
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        doc.close()
        return img
    img = Image.open(path)
    return ImageOps.exif_transpose(img).convert('RGB')


def resize_max(img: Image.Image, max_side: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def build_additions():
    archives = prepare_workdir()
    files = collect_source_files()
    previous = read_manifest(OUT)
    if not archives and not files:
        print('No new diploma inputs; existing gallery retained.')
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)

    items = list(previous['items'])
    known_hashes = {item.get('source_hash') for item in items if item.get('source_hash')}
    staged_assets = []
    stage = WORK / 'rendered'
    stage.mkdir(parents=True, exist_ok=True)
    for idx, path in enumerate(files, start=1):
        year = year_from_name(path.as_posix())
        digest = source_hash(path)
        if digest in known_hashes:
            continue
        base = f"{year or 'nd'}-{slugify(path.stem)}-{digest[:16]}"
        known_hashes.add(digest)
        full_path = FULL / f"{base}.webp"
        thumb_path = THUMBS / f"{base}.webp"
        try:
            img = load_image(path)
        except Exception as exc:
            raise ValueError(f'Cannot render new document {path.name}; existing gallery retained') from exc
        width, height = img.size
        orientation = 'landscape' if width > height else 'portrait'
        staged_full = stage / (base + '-full.webp')
        resize_max(img, 1800).save(staged_full, 'WEBP', quality=84, method=6)
        staged_assets.append((staged_full, full_path))
        # Smaller thumbnails: the page now displays roughly twice as many items per screen.
        staged_thumb = stage / (base + '-thumb.webp')
        resize_max(img, 340).save(staged_thumb, 'WEBP', quality=74, method=6)
        staged_assets.append((staged_thumb, thumb_path))
        items.append({
            'id': base,
            'title': title_from_name(path),
            'year': year,
            'width': width,
            'height': height,
            'orientation': orientation,
            'span': 2 if orientation == 'landscape' else 1,
            'thumb': str(thumb_path).replace('\\', '/'),
            'full': str(full_path).replace('\\', '/'),
            'download': str(full_path).replace('\\', '/'),
            'download_filename': full_path.name,
            'source_filename': path.name,
            'source_hash': digest,
        })

    if len(items) == len(previous['items']):
        print('All source hashes already present; existing gallery retained.')
        return 0
    items.sort(key=lambda item: (-(item.get('year') or 0), str(item.get('title') or item['id'])))
    publish(OUT, {
        **previous,
        'generated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'count': len(items),
        'sort': 'year_desc_name_asc',
        'input_archives': sorted(set(previous.get('input_archives', [])) | {str(p).replace('\\', '/') for p in archives}),
        'items': items,
    }, staged_assets)
    print(f'Built diplomas gallery: {len(items)} items from {len(archives)} archive(s) and/or direct files')

    return 0


def main():
    global WORK
    with TemporaryDirectory(prefix='portfolio-gallery-') as temporary:
        WORK = Path(temporary)
        return build_additions()


if __name__ == '__main__':
    raise SystemExit(main())
