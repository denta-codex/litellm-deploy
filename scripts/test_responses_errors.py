"""Regression for quota failures becoming generic Codex stream disconnects."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_context import CaptureRequest, ResponsesErrorStream


def frame(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


ERROR = {'error': {'code': 429, 'type': 'None', 'message': 'Claude subscription limit reached'}}


class ErrorTests(unittest.TestCase):
    def test_split_coalesced_frames_preserve_success_and_report_quota(self):
        created = frame({'type': 'response.created', 'sequence_number': 2,
                         'response': {'id': 'resp_test', 'status': 'in_progress', 'output': []}})
        delta = frame({'type': 'response.output_text.delta', 'sequence_number': 3, 'delta': 'hello'})
        source = created + delta + frame(ERROR)
        for size in (1, 7, len(source)):
            stream = ResponsesErrorStream()
            result = b''.join(stream.feed(source[i:i+size]) for i in range(0, len(source), size))
            self.assertTrue(result.startswith(created + delta))
            failed = json.loads(result.split(b'data: ')[-1])
            self.assertEqual(failed['type'], 'response.failed')
            self.assertEqual(failed['sequence_number'], 4)
            self.assertEqual(failed['response']['id'], 'resp_test')
            self.assertEqual(failed['response']['error'], {
                'code': 'rate_limit_exceeded', 'message': 'Claude subscription limit reached'})

    def test_native_events_comments_done_and_invalid_json_unchanged(self):
        for value in (frame({'type': 'response.failed', 'response': {'status': 'failed'}}),
                      frame({'type': 'response.completed'}), frame({'type': 'error', 'error': ERROR['error']}),
                      b': keepalive\n\n', b'data: [DONE]\n\n', b'data: invalid\n\n'):
            self.assertEqual(ResponsesErrorStream().feed(value, final=True), value)

    def test_error_before_response_created_and_final_unterminated_frame(self):
        result = ResponsesErrorStream().feed(frame(ERROR).rstrip(), final=True)
        event = json.loads(result.split(b'data: ')[1])
        self.assertEqual(event['response']['status'], 'failed')
        self.assertTrue(event['response']['id'].startswith('resp_'))

    def test_status_codes_and_crlf(self):
        for status, code in ((401, 'authentication_error'), ('403', 'permission_denied'),
                             (500, 'server_error'), ('custom_error', 'custom_error')):
            value = frame({'error': {'code': status, 'message': 'upstream explanation'}}).replace(b'\n', b'\r\n')
            event = json.loads(ResponsesErrorStream().feed(value).split(b'data: ')[1])
            self.assertEqual(event['response']['error']['code'], code)

    def test_only_successful_responses_sse_is_translated(self):
        async def run(path, status, content_type):
            sent = []
            async def app(scope, receive, send):
                await send({'type': 'http.response.start', 'status': status,
                            'headers': [(b'content-type', content_type)]})
                await send({'type': 'http.response.body', 'body': frame(ERROR)[:12], 'more_body': True})
                await send({'type': 'http.response.body', 'body': frame(ERROR)[12:]})
            async def send(message):
                sent.append(message)
            await CaptureRequest(app)({'type': 'http', 'path': path}, None, send)
            return b''.join(m.get('body', b'') for m in sent)
        for path, status, content_type, translated in (
                ('/v1/responses', 200, b'text/event-stream; charset=utf-8', True),
                ('/responses', 200, b'text/event-stream', True),
                ('/v1/chat/completions', 200, b'text/event-stream', False),
                ('/v1/responses', 429, b'application/json', False)):
            result = asyncio.run(run(path, status, content_type))
            self.assertEqual(b'response.failed' in result, translated)
            if not translated:
                self.assertEqual(result, frame(ERROR))

    @unittest.skipUnless(os.environ.get('CODEX_ERROR_TEST_BINARY'), 'set CODEX_ERROR_TEST_BINARY for installed Codex acceptance')
    def test_installed_codex_surfaces_provider_error(self):
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body = ResponsesErrorStream().feed(frame(ERROR), final=True)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        state = Path.home() / '.local/state/litellm'
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='error-acceptance-', dir=state) as root:
            home = Path(root)
            (home / 'config.toml').write_text(f'''model = "gpt-6.1-sol"
model_provider = "fixture"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[model_providers.fixture]
name = "Offline error fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
env_key = "ERROR_FIXTURE_KEY"
request_max_retries = 0
stream_max_retries = 0
''')
            result = subprocess.run([os.environ['CODEX_ERROR_TEST_BINARY'], 'exec', '--skip-git-repo-check',
                                     '-C', root, '--json', 'Say hello'],
                                    env=os.environ | {'CODEX_HOME': root, 'ERROR_FIXTURE_KEY': 'synthetic'},
                                    capture_output=True, text=True, timeout=40)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output[-3000:])
            self.assertIn('Claude subscription limit reached', output)
            self.assertNotIn('stream closed before response.completed', output)


if __name__ == '__main__':
    unittest.main()
