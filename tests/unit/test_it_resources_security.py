"""Deterministic URL, redirect and image boundaries for public IT discovery.

No fixture performs DNS, HTTP, credential access or browser interaction.
"""
from __future__ import annotations

import os
import ipaddress
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import harvest_it_resources as collector


def public_resolver(host, port, *, type):
    try:
        address = ipaddress.ip_address(host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        return [(family, type, 6, '', (str(address), port))]
    except ValueError:
        address = '127.0.0.1' if host.lower().endswith('localhost.localdomain') else '93.184.216.34'
        return [(socket.AF_INET, type, 6, '', (address, port))]


def response(status=200, *, headers=None, content=b'public content'):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = iter([content])
    return result


def svg(content='<rect width="480" height="240" fill="#ddd"/>', *, width=480, height=240):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width}" height="{height}">{content}</svg>').encode()


class PublicURLTests(unittest.TestCase):
    def assert_rejected(self, value, *, resolver=public_resolver):
        with self.assertRaises(collector.ITFailure) as caught:
            collector.public_url(value, resolver=resolver)
        self.assertIsInstance(caught.exception.reason, str)
        self.assertTrue(caught.exception.reason)
        return caught.exception

    def test_public_url_uses_only_the_injected_dns_resolver(self):
        resolver = Mock(side_effect=public_resolver)
        with patch.object(socket, 'getaddrinfo', side_effect=AssertionError('real DNS forbidden')):
            value = collector.public_url('https://example.org/projects/demo', resolver=resolver)
        self.assertEqual(value, 'https://example.org/projects/demo')
        resolver.assert_called_with('example.org', 443, type=socket.SOCK_STREAM)

    def test_credentials_nonweb_schemes_and_localhost_are_rejected(self):
        for value in (
            'https://fixture-user:fixture-secret@example.org/',
            'https://fixture-user@example.org/',
            'file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,fixture',
            'ftp://example.org/file', 'https://localhost/', 'http://LOCALHOST/',
            'https://localhost.localdomain/',
        ):
            with self.subTest(value=value):
                failure = self.assert_rejected(value)
                self.assertNotIn('fixture-secret', failure.reason)

    def test_literal_internal_ipv4_and_ipv6_are_rejected_before_fetch(self):
        for host in (
            '127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1',
            '169.254.169.254', '0.0.0.0', '[::1]', '[::]',
            '[fc00::1]', '[fe80::1]', '[::ffff:127.0.0.1]',
        ):
            with self.subTest(host=host):
                self.assert_rejected(f'https://{host}/')

    def test_dns_private_and_mixed_answers_are_rejected(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        private4 = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 443))
        private6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0))
        for answers in ([private4], [private6], [public, private4], [private6, public]):
            with self.subTest(answers=answers):
                self.assert_rejected('https://example.org/', resolver=Mock(return_value=answers))

    def test_public_ipv6_dns_answer_is_not_rejected_as_private(self):
        resolver = Mock(return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, '',
                                       ('2606:4700:4700::1111', 443, 0, 0))])
        self.assertEqual(collector.public_url('https://example.org/', resolver=resolver),
                         'https://example.org/')


class PublicFetcherTests(unittest.TestCase):
    def test_private_redirect_is_blocked_before_any_second_request(self):
        for target in ('http://127.0.0.1/internal', 'https://[::1]/internal',
                       'https://private.example.org/internal', '//169.254.169.254/latest'):
            with self.subTest(target=target):
                def resolver(host, port, *, type):
                    if host == 'private.example.org':
                        return [(socket.AF_INET, type, 6, '', ('192.168.1.4', port))]
                    return public_resolver(host, port, type=type)
                first = response(302, headers={'Location': target})
                session = Mock()
                session.get.return_value = first
                fetcher = collector.PublicFetcher(session=session, resolver=resolver)
                with self.assertRaises(collector.ITFailure):
                    fetcher.fetch('https://example.org/start')
                self.assertEqual(session.get.call_count, 1)
                self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
                first.close.assert_called()

    def test_valid_public_redirect_is_resolved_and_streamed_with_a_size_bound(self):
        first = response(302, headers={'Location': '/image.png'})
        second = response(200, headers={'Content-Type': 'image/png'}, content=b'fixture-image')
        session = Mock()
        session.get.side_effect = [first, second]
        result = collector.PublicFetcher(session=session, resolver=public_resolver).fetch(
            'https://example.org/start', max_bytes=100)
        self.assertEqual(result, (b'fixture-image', 'image/png', 'https://example.org/image.png'))
        self.assertEqual([call.args[0] for call in session.get.call_args_list],
                         ['https://example.org/start', 'https://example.org/image.png'])
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['stream'])
        first.close.assert_called()
        second.close.assert_called()

    def test_public_fetch_never_copies_the_github_environment_token_to_headers(self):
        session = Mock()
        session.get.return_value = response(headers={'Content-Type': 'text/plain'})
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-github-token', 'GH_TOKEN': 'fixture-gh-token'}):
            collector.PublicFetcher(session=session, resolver=public_resolver).fetch('https://example.org/')
        headers = session.get.call_args.kwargs['headers']
        self.assertNotIn('authorization', {key.lower() for key in headers})
        self.assertNotIn('cookie', {key.lower() for key in headers})
        self.assertNotIn('fixture-github-token', str(headers))
        self.assertNotIn('fixture-gh-token', str(headers))

    def test_oversized_stream_is_rejected_and_response_closed(self):
        streamed = response(content=b'x' * 101)
        session = Mock()
        session.get.return_value = streamed
        with self.assertRaises(collector.ITFailure):
            collector.PublicFetcher(session=session, resolver=public_resolver).fetch(
                'https://example.org/image.png', max_bytes=100)
        streamed.close.assert_called()


class VerifiedImageTests(unittest.TestCase):
    def test_static_large_svg_is_accepted_as_a_local_image(self):
        data, suffix = collector.verified_image(svg())
        self.assertTrue(data)
        self.assertIn(suffix, ('.svg', '.png'))
        if suffix == '.png':
            self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        else:
            self.assertIn(b'<svg', data)

    def test_tiny_badges_and_invalid_payloads_are_rejected(self):
        for data in (svg(width=120, height=20), b'not an image', b'<html>not SVG</html>'):
            with self.subTest(data=data):
                with self.assertRaises(collector.ITFailure) as caught:
                    collector.verified_image(data)
                self.assertEqual(caught.exception.reason, 'image_invalid_or_small')

    def test_svg_scripts_events_external_references_and_foreign_content_are_rejected(self):
        for content in (
            '<script>alert(1)</script>',
            '<rect width="480" height="240" onload="alert(1)"/>',
            '<image href="https://example.org/tracker.png" width="480" height="240"/>',
            '<image xlink:href="https://example.org/tracker.png" width="480" height="240"/>',
            '<a href="javascript:alert(1)"><text x="10" y="30">Click</text></a>',
            '<foreignObject width="480" height="240"><div xmlns="http://www.w3.org/1999/xhtml">HTML</div></foreignObject>',
            '<style>rect {fill:url(https://example.org/paint.svg)}</style><rect width="480" height="240"/>',
        ):
            with self.subTest(content=content):
                with self.assertRaises(collector.ITFailure) as caught:
                    collector.verified_image(svg(content))
                self.assertEqual(caught.exception.reason, 'image_invalid_or_small')

    def test_external_svg_references_cannot_hide_in_css_case_base_or_processing_instructions(self):
        cases = (
            svg('<rect width="480" height="240" fill="URL(https://example.org/paint.svg#p)"/>'),
            svg('<g xml:base="https://example.org/paint.svg"><rect width="480" height="240" fill="url(#p)"/></g>'),
            b'<?xml version="1.0"?><?xml-stylesheet type="text/css" href="https://example.org/style.css"?>' + svg(),
        )
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(collector.ITFailure) as caught:
                    collector.verified_image(data)
                self.assertEqual(caught.exception.reason, 'image_invalid_or_small')

    def test_svg_dimensions_must_be_finite(self):
        for dimensions in ({'width': 'NaN'}, {'height': 'inf'}):
            with self.subTest(dimensions=dimensions):
                with self.assertRaises(collector.ITFailure) as caught:
                    collector.verified_image(svg(**dimensions))
                self.assertEqual(caught.exception.reason, 'image_invalid_or_small')


if __name__ == '__main__':
    unittest.main()
