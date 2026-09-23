"""Optional image work must not prevent a collected corpus from being saved."""
import copy
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import media_postprocess as images


class Response:
    headers = {'content-type': 'image/png'}
    status = 200

    def __init__(self, chunks=(), closed=None):
        self.chunks = iter(chunks)
        self.closed = closed or threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()

    def read1(self, size):
        return next(self.chunks, b'')[:size]

    read = read1


class ImageDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.record = {'id': 'new-story', 'url': 'https://source.example/story',
                       'image': 'https://source.example/photo.png'}

    def test_expired_budget_does_not_start_network_and_preserves_record(self):
        record = copy.deepcopy(self.record)
        with patch.object(images.urllib.request, 'urlopen') as request:
            result = images.mirror_image(record, record['image'], deadline=time.monotonic() - 1)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(record, self.record)
        request.assert_not_called()

    def test_blocked_connection_returns_by_shared_deadline(self):
        release, closed = threading.Event(), threading.Event()

        def blocked_connection(*args, **kwargs):
            release.wait(2)
            return Response([b'data'], closed)

        with patch.object(images.urllib.request, 'urlopen', side_effect=blocked_connection):
            started = time.monotonic()
            try:
                data, report = images.fetch_bytes(self.record['image'], deadline=started + .1)
                elapsed = time.monotonic() - started
                self.assertIsNone(data)
                self.assertEqual(report['status'], 'budget_exhausted')
                self.assertLess(elapsed, .8)
            finally:
                release.set()
                self.assertTrue(closed.wait(1), 'Timed-out download must close its response.')

    def test_drip_response_stops_between_reads_and_closes(self):
        clock = [0.0]
        response = Response()

        def drip(size):
            clock[0] += 1
            return b'x'

        response.read1 = drip
        with patch.object(images.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(images.urllib.request, 'urlopen', return_value=response) as request:
            data, report = images._fetch_image_bytes(self.record['image'], 100, 3)
        self.assertIsNone(data)
        self.assertEqual(report['status'], 'timeout')
        self.assertEqual(clock[0], 3)
        self.assertEqual(request.call_args.kwargs['timeout'], 3)
        self.assertTrue(response.closed.is_set())

    def test_successful_download_stays_size_limited(self):
        response = Response([b'12345'])
        with patch.object(images.urllib.request, 'urlopen', return_value=response):
            data, report = images.fetch_bytes(self.record['image'], max_bytes=4,
                                              deadline=time.monotonic() + 1)
        self.assertIsNone(data)
        self.assertEqual(report['status'], 'too_large')
        self.assertTrue(response.closed.is_set())

    def test_expiry_after_validation_cannot_write_asset_or_replace_remote_url(self):
        from PIL import Image
        raw = io.BytesIO()
        Image.new('RGB', (1, 1)).save(raw, format='PNG')
        original = self.directory / 'existing.png'
        original.write_bytes(b'previous published asset')
        record = copy.deepcopy(self.record)
        with patch.object(images, 'IMAGE_DIR', self.directory), \
             patch.object(images.time, 'monotonic', side_effect=[0, 10]), \
             patch.object(images, 'fetch_bytes', return_value=(raw.getvalue(), {'status': 'ok'})):
            report = images.mirror_image(record, record['image'], deadline=5)
        self.assertEqual(report['status'], 'budget_exhausted')
        self.assertEqual(record, self.record)
        self.assertEqual(list(self.directory.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b'previous published asset')

    def test_valid_image_can_be_saved_within_budget(self):
        from PIL import Image
        raw = io.BytesIO()
        Image.new('RGB', (1, 1)).save(raw, format='PNG')
        record = copy.deepcopy(self.record)
        deadline = time.monotonic() + 5
        with patch.object(images, 'IMAGE_DIR', self.directory), \
             patch.object(images, 'fetch_bytes', return_value=(raw.getvalue(), {'status': 'ok', 'content_type': 'image/png'})) as download:
            report = images.mirror_image(record, record['image'], deadline=deadline)
        self.assertEqual(report['status'], 'cached')
        self.assertEqual(Path(record['image']).read_bytes(), raw.getvalue())
        download.assert_called_once_with(self.record['image'], deadline=deadline)


if __name__ == '__main__':
    unittest.main()
