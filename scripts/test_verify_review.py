"""Check that failed reviewer transports cannot pass the deployment gate."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from verify_review import verify


def message(text='{"status":"REVIEW_ROUTE_OK"}'):
    return {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}


def completed(output=None, status='completed'):
    return {'type': 'response.completed', 'response': {'status': status, 'output': output or []}}


class ReviewProbeTests(unittest.TestCase):
    def run_probe(self, events):
        stream = io.BytesIO(b'event: response.created\n' + b''.join(
            b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events) + b'data: [DONE]\n')
        output = io.StringIO()
        with patch('verify_review.subprocess.check_output', return_value='test-key\n'), \
                patch('verify_review.urllib.request.urlopen', return_value=stream) as send, \
                contextlib.redirect_stdout(output):
            verify(Path('/test/proxy-key.cred'))
        self.assertNotIn('test-key', output.getvalue())
        request = send.call_args.args[0]
        self.assertEqual(json.loads(request.data)['model'], 'codex-auto-review')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        return output.getvalue()

    def test_completed_output_is_checked(self):
        self.assertIn('PASS', self.run_probe([completed([message()])]))

    def test_streamed_items_survive_empty_completion_output(self):
        events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': message()}, completed()]
        self.assertIn('PASS', self.run_probe(events))

    def test_failed_or_incomplete_stream_cannot_pass(self):
        cases = [[], [completed(status='incomplete')],
                 [{'type': 'response.output_item.done', 'output_index': 0, 'item': message()}]]
        cases += [[{'type': kind}, completed([message()])] for kind in
                  ['error', 'response.failed', 'response.incomplete']]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.run_probe(events)

    def test_invalid_missing_or_wrong_json_cannot_pass(self):
        for text in ('', 'REVIEW_ROUTE_OK', '{', '{}', '{"status":"WRONG"}',
                     '{"status":"REVIEW_ROUTE_OK","extra":true}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.run_probe([completed([message(text)])])


if __name__ == '__main__':
    unittest.main()
