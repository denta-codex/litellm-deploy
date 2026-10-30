"""Tool-image acceptance fails closed on stream errors or a false visual answer."""
import base64
import io
import json
import struct
import unittest
from unittest.mock import patch
import zlib

from verify_bedrock import check, image_fixture


class BedrockImageAcceptanceTests(unittest.TestCase):
    def check_stream(self, events, expected='blue'):
        lines = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)
        with patch('verify_bedrock.urllib.request.urlopen', return_value=io.BytesIO(lines)):
            check('http://127.0.0.1:4000', 'fixture', {'stream': True}, expected, exact=True)

    def test_correct_deltas_and_completed_are_required(self):
        self.check_stream([{'type': 'response.output_text.delta', 'delta': 'Blue'},
                           {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}])
        for events in ([{'type': 'response.output_text.delta', 'delta': 'Blue'}],
                       [{'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}],
                       [{'type': 'response.output_text.delta', 'delta': 'red'},
                        {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}]):
            with self.subTest(events=events), self.assertRaises(AssertionError):
                self.check_stream(events)

    def test_stream_failure_never_passes_even_after_correct_color(self):
        for kind in ('error', 'response.failed', 'response.incomplete'):
            with self.subTest(kind=kind), self.assertRaises(AssertionError):
                self.check_stream([{'type': 'response.output_text.delta', 'delta': 'Blue'}, {'type': kind},
                                   {'type': 'response.completed', 'response': {'status': 'completed'}}])

    def test_fixture_contains_blue_pixels_without_external_assets(self):
        png = base64.b64decode(image_fixture().split(',', 1)[1])
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        size = struct.unpack('>I', png[33:37])[0]
        self.assertEqual(png[37:41], b'IDAT')
        self.assertEqual(zlib.decompress(png[41:41 + size]), (b'\x00' + b'\x00\x00\xff' * 128) * 128)


if __name__ == '__main__':
    unittest.main()
