import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from gallery_storage import prepare_sources


class GalleryTest(unittest.TestCase):
    def test_additive_hashes_idempotence_and_corrupt_input_are_transactional(self):
        for module_name in ('build_diplomas_gallery', 'build_dpo_gallery'):
            with self.subTest(builder=module_name), tempfile.TemporaryDirectory() as temporary:
                module = importlib.import_module(module_name)
                root = Path(temporary)
                source = root / 'content'
                source.mkdir()
                out = root / 'gallery.json'
                old_asset = root / 'legacy.webp'
                old_asset.write_bytes(b'existing-original-asset')
                old = {'id': 'legacy', 'title': 'Manual title', 'title_en': 'Manual English', 'thumb': str(old_asset)}
                out.write_text(json.dumps({'count': 1, 'items': [old]}))
                replacements = {'INPUT_DIR': source, 'OUT': out, 'THUMBS': root / 'thumbs'}
                replacements['FULL' if module_name == 'build_diplomas_gallery' else 'PAGES'] = root / 'full'
                with patch.multiple(module, **replacements):
                    original_bytes = out.read_bytes()
                    module.main()
                    self.assertEqual(out.read_bytes(), original_bytes)
                    Image.new('RGB', (30, 20), 'red').save(source / '2026-new.png')
                    module.main()
                    manifest = json.loads(out.read_text(encoding='utf-8'))
                    self.assertEqual(manifest['count'], 2)
                    self.assertIn(old, manifest['items'])
                    new = next(item for item in manifest['items'] if item['id'] != 'legacy')
                    self.assertEqual(len(new['source_hash']), 64)
                    saved_bytes = out.read_bytes()
                    (source / '2026-new.png').rename(source / '2026-renamed.png')
                    module.main()
                    self.assertEqual(out.read_bytes(), saved_bytes)
                    (source / '2027-broken.png').write_bytes(b'not-an-image')
                    with self.assertRaises(ValueError):
                        module.main()
                    self.assertEqual(out.read_bytes(), saved_bytes)
                    self.assertEqual(old_asset.read_bytes(), b'existing-original-asset')

    def test_zip_traversal_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            with zipfile.ZipFile(source / 'new.zip', 'w') as archive:
                archive.writestr('../escape.png', b'bad')
            with self.assertRaises(ValueError):
                prepare_sources(source, root / 'work', {'.png'})
            self.assertFalse((root / 'work/escape.png').exists())


if __name__ == '__main__':
    unittest.main()
