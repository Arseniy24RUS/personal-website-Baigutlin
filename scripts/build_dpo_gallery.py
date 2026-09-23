#!/usr/bin/env python3
"""Build a static continuing-professional-education gallery.

The workflow mirrors the diplomas gallery builder, but keeps every rendered PDF
page for the modal view while using the first page as the thumbnail.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from gallery_storage import prepare_sources, publish, read_manifest, source_hash

from PIL import Image, ImageChops, ImageOps

ROOT = Path('.')
INPUT_DIR = ROOT / 'content' / 'dpo'
WORK = ROOT / '.tmp_dpo'
THUMBS = ROOT / 'assets' / 'dpo' / 'thumbs'
PAGES = ROOT / 'assets' / 'dpo' / 'pages'
OUT = ROOT / 'data' / 'dpo' / 'gallery.json'
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}
PDF_EXTS = {'.pdf'}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

DPO_FIXUPS = {}

TRANSLIT = str.maketrans({
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'y',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f',
    'х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
})


def slugify(value: str) -> str:
    value = value.lower().translate(TRANSLIT)
    value = re.sub(r'[^a-z0-9]+', '-', value).strip('-')
    return value[:90] or 'dpo'


def year_from_name(name: str):
    if 'master of public policy' in name.lower():
        return 2022
    years = re.findall(r'(20\d{2}|19\d{2})', name)
    return int(years[-1]) if years else None


def title_from_name(path: Path) -> str:
    name = re.sub(r'[_-]+', ' ', path.stem)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:1].upper() + name[1:] if name else 'Document'


def prepare_workdir():
    return prepare_sources(INPUT_DIR, WORK, SUPPORTED_EXTS)


def collect_source_files():
    files = []
    for p in WORK.rglob('*'):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            if '__MACOSX' in p.parts or p.name.startswith('._'):
                continue
            files.append(p)
    files.sort(key=lambda p: (-(year_from_name(p.as_posix()) or -1), p.as_posix().lower()))
    return files


def resize_max(img: Image.Image, max_side: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def crop_white_margins(img: Image.Image, threshold: int = 12, padding: int = 8) -> Image.Image:
    rgb = img.convert('RGB')
    diff = ImageChops.difference(rgb, Image.new('RGB', rgb.size, 'white')).convert('L')
    mask = diff.point(lambda value: 255 if value > threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return rgb
    pixels = mask.load()
    min_col_pixels = max(4, int(rgb.height * 0.01))
    min_row_pixels = max(4, int(rgb.width * 0.01))
    columns = [
        x for x in range(rgb.width)
        if sum(1 for y in range(rgb.height) if pixels[x, y]) >= min_col_pixels
    ]
    rows = [
        y for y in range(rgb.height)
        if sum(1 for x in range(rgb.width) if pixels[x, y]) >= min_row_pixels
    ]
    if columns and rows:
        left, right = columns[0], columns[-1] + 1
        top, bottom = rows[0], rows[-1] + 1
    else:
        left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(rgb.width, right + padding)
    bottom = min(rgb.height, bottom + padding)
    return rgb.crop((left, top, right, bottom))


def fixup_for_base(base: str):
    for key, fixup in DPO_FIXUPS.items():
        if base.startswith(key):
            return fixup
    return {}


def apply_page_fixup(base: str, source_page_number: int, img: Image.Image):
    fixup = fixup_for_base(base)
    if source_page_number in fixup.get('skip_pages', set()):
        return None
    page_fixup = fixup.get('pages', {}).get(source_page_number, {})
    rotate = page_fixup.get('rotate', fixup.get('rotate'))
    trim = page_fixup.get('trim', fixup.get('trim', False))
    fixed = img.convert('RGB')
    if rotate:
        fixed = fixed.rotate(rotate, expand=True)
    if trim:
        fixed = crop_white_margins(fixed)
    return fixed


def render_pdf_pages(path: Path):
    import fitz
    doc = fitz.open(path)
    try:
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            yield Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
    finally:
        doc.close()


def load_source_pages(path: Path):
    if path.suffix.lower() in PDF_EXTS:
        return list(render_pdf_pages(path))
    img = Image.open(path)
    return [ImageOps.exif_transpose(img).convert('RGB')]


def build_additions():
    archives = prepare_workdir()
    files = collect_source_files()
    previous = read_manifest(OUT)
    if not archives and not files:
        print('No new DPO inputs; existing gallery retained.')
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)

    items = list(previous['items'])
    known_hashes = {item.get('source_hash') for item in items if item.get('source_hash')}
    staged_assets = []
    stage = WORK / 'rendered'
    stage.mkdir(parents=True, exist_ok=True)
    for path in files:
        year = year_from_name(path.as_posix())
        digest = source_hash(path)
        if digest in known_hashes:
            continue
        base = f"{year or 'nd'}-{slugify(path.stem)}-{digest[:16]}"
        known_hashes.add(digest)
        try:
            images = load_source_pages(path)
        except Exception as exc:
            raise ValueError(f'Cannot render new document {path.name}; existing gallery retained') from exc
        if not images:
            raise ValueError(f'No pages in new document {path.name}; existing gallery retained')

        page_records = []
        first_page_image = None
        for source_page_index, img in enumerate(images, start=1):
            img = apply_page_fixup(base, source_page_index, img)
            if img is None:
                continue
            width, height = img.size
            page_number = len(page_records) + 1
            page_path = PAGES / f"{base}-p{page_number:02d}.webp"
            staged_page = stage / page_path.name
            resize_max(img, 1800).save(staged_page, 'WEBP', quality=84, method=6)
            staged_assets.append((staged_page, page_path))
            if first_page_image is None:
                first_page_image = img
            page_records.append({
                'page': page_number,
                'src': str(page_path).replace('\\', '/'),
                'width': width,
                'height': height,
                'orientation': 'landscape' if width > height else 'portrait',
            })

        if not page_records or first_page_image is None:
            raise ValueError(f'No renderable pages in {path.name}; existing gallery retained')

        thumb_path = THUMBS / f"{base}.webp"
        staged_thumb = stage / (base + '-thumb.webp')
        resize_max(first_page_image, 520).save(staged_thumb, 'WEBP', quality=76, method=6)
        staged_assets.append((staged_thumb, thumb_path))
        first = page_records[0]
        orientation = first['orientation']
        items.append({
            'id': base,
            'title': title_from_name(path),
            'year': year,
            'kind': 'pdf' if path.suffix.lower() in PDF_EXTS else 'image',
            'source_filename': path.name,
            'source_hash': digest,
            'page_count': len(page_records),
            'width': first['width'],
            'height': first['height'],
            'orientation': orientation,
            'span': 2 if orientation == 'landscape' else 1,
            'thumb': str(thumb_path).replace('\\', '/'),
            'full': first['src'],
            'download': first['src'],
            'pages': page_records,
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
    print(f'Built DPO gallery: {len(items)} items')

    return 0


def main():
    global WORK
    with TemporaryDirectory(prefix='portfolio-gallery-') as temporary:
        WORK = Path(temporary)
        return build_additions()


if __name__ == '__main__':
    raise SystemExit(main())
