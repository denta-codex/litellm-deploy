"""Offline integration: actual shell/uv/server, synthetic auth and SSE backend."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import verify_review

ROOT = Path(__file__).resolve().parent.parent


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, payload))
        if payload.get('prompt_cache_key') == 'reject-probe':
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"synthetic backend rejection","type":"invalid_request_error"}}')
            return
        if payload.get('tools'):
            item = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe',
                    'name': 'deployment_probe', 'arguments': '{}', 'status': 'completed'}
        else:
            text = '{"status":"REVIEW_ROUTE_OK"}' if payload['model'] == 'codex-auto-review' else 'STREAM_OK'
            if any(i.get('type') == 'function_call_output' for i in payload['input']):
                text = 'TOOL_CONTINUATION_OK'
            item = {'type': 'message', 'id': 'msg_probe', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
        response = {'id': 'resp_probe', 'object': 'response', 'created_at': 1, 'model': payload['model'],
                    'status': 'in_progress', 'output': [], 'error': None, 'incomplete_details': None,
                    'service_tier': ('default' if payload.get('prompt_cache_key') == 'downgrade-priority'
                                     else payload.get('service_tier', 'default'))}
        events = [{'type': 'response.created', 'response': response},
                  {'type': 'response.output_item.added', 'output_index': 0, 'item': item}]
        if item['type'] == 'function_call':
            events += [{'type': 'response.function_call_arguments.delta', 'item_id': item['id'],
                        'output_index': 0, 'delta': '{}'},
                       {'type': 'response.function_call_arguments.done', 'item_id': item['id'],
                        'output_index': 0, 'arguments': '{}'}]
        else:
            events += [{'type': 'response.output_text.delta', 'item_id': item['id'],
                        'output_index': 0, 'content_index': 0, 'delta': item['content'][0]['text']}]
        events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                   {'type': 'response.completed', 'response': response | {
                       'status': 'completed', 'output': [item],
                       'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for index, event in enumerate(events):
            event['sequence_number'] = index
            self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='litellm-startup-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        cls.backend.requests = []
        thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.backend.server_close)
        cls.addClassCleanup(cls.backend.shutdown)
        (root / 'litellm-proxy-key').write_text('sk-isolated-proxy-key')
        (root / 'auth.json').write_text(json.dumps({
            'access_token': 'isolated-backend-token', 'account_id': 'isolated-account',
            'expires_at': time.time() + 3600,
        }))
        template = Environment(loader=FileSystemLoader(ROOT / 'deploy/templates'),
                               undefined=StrictUndefined).get_template('config.yaml.j2')
        config = root / 'config.yaml'
        config.write_text(template.render(litellm_model='chatgpt/gpt-6-astra'))
        cls.port = unused_port()
        cls.base = f'http://127.0.0.1:{cls.port}'
        env = dict(os.environ, CREDENTIALS_DIRECTORY=str(root), CHATGPT_TOKEN_DIR=str(root),
                   CHATGPT_API_BASE=f'http://127.0.0.1:{cls.backend.server_port}',
                   LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False',
                   UV_OFFLINE='1', UV_PYTHON_DOWNLOADS='never', UV_CACHE_DIR=str(root / 'uv-cache'))
        cls.log = (root / 'server.log').open('w+')
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([
            str(ROOT / 'scripts/start-litellm'), shutil.which('uv'), str(ROOT), str(config), str(cls.port),
        ], env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop)
        for _ in range(300):
            if cls.process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(cls.base + '/health/liveliness', timeout=1):
                    return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        cls.log.seek(0)
        raise RuntimeError('Isolated proxy failed to start: ' + cls.log.read()[-6000:])

    @classmethod
    def stop(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def request(self, payload):
        request = urllib.request.Request(self.base + '/v1/responses', data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer sk-isolated-proxy-key'})
        with urllib.request.urlopen(request, timeout=30) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                return json.load(response)
            events = [json.loads(line[6:]) for line in response
                      if line.startswith(b'data: ') and line.strip() != b'data: [DONE]']
        self.assertTrue(any(e['type'] == 'response.completed' for e in events), events)
        return events

    def payload(self, **extra):
        return {'model': 'chatgpt/gpt-6-astra', 'input': [{'role': 'user', 'content': 'Hello'}],
                'stream': True, **extra}

    def test_fields_and_instructions_reach_wire_through_shell_uv_and_router(self):
        schema = {'type': 'json_schema', 'name': 'probe', 'strict': True, 'schema': {
            'type': 'object', 'properties': {'status': {'type': 'string'}},
            'required': ['status'], 'additionalProperties': False}}
        for instructions in (' Client\n\n\t\u03bb ', ''):
            payload = self.payload(instructions=instructions, text={'format': schema, 'verbosity': 'low'},
                                   parallel_tool_calls=False, prompt_cache_key='isolated-cache-key')
            self.request(payload)
            path, headers, outgoing = self.backend.requests[-1]
            self.assertEqual(path, '/responses')
            self.assertEqual(headers['authorization'], 'Bearer isolated-backend-token')
            for field in ('instructions', 'text', 'parallel_tool_calls', 'prompt_cache_key'):
                self.assertEqual(outgoing[field], payload[field])
            self.assertEqual(outgoing['model'], 'gpt-6-astra')
            self.assertFalse(outgoing['store'])
            self.assertIn('reasoning.encrypted_content', outgoing['include'])

    def test_ordinary_streaming_and_nonstreaming_fallback(self):
        from litellm.llms.chatgpt.common_utils import get_chatgpt_default_instructions
        events = self.request(self.payload())
        self.assertTrue(any(e['type'] == 'response.output_text.delta' for e in events))
        outgoing = self.backend.requests[-1][2]
        self.assertEqual(outgoing['instructions'], get_chatgpt_default_instructions())
        for name in ('text', 'parallel_tool_calls', 'prompt_cache_key', 'service_tier'):
            self.assertNotIn(name, outgoing)
        result = self.request(self.payload(stream=False))
        # The pinned ChatGPT provider forces streaming even for stream=False.
        self.assertTrue(any(e['type'] == 'response.completed' for e in result))

    def test_streamed_tool_and_continuation(self):
        events = self.request(self.payload(
            service_tier='priority',
            parallel_tool_calls=False,
            tools=[{'type': 'function', 'name': 'deployment_probe', 'parameters': {
                'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}],
            tool_choice={'type': 'function', 'name': 'deployment_probe'},
        ))
        self.assertTrue(any(e['type'] == 'response.function_call_arguments.delta' for e in events))
        calls = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        self.assertEqual(calls[0]['name'], 'deployment_probe')
        self.assertEqual(self.backend.requests[-1][2]['service_tier'], 'priority')
        continuation = self.payload(service_tier='priority', input=[{'role': 'user', 'content': 'Use the tool result.'}] + calls + [{
            'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': 'TOOL_CONTINUATION_OK'}])
        events = self.request(continuation)
        self.assertIn('TOOL_CONTINUATION_OK', ''.join(e.get('delta', '') for e in events
                                                     if e['type'] == 'response.output_text.delta'))
        outgoing = self.backend.requests[-1][2]
        self.assertEqual(outgoing['input'][-1]['call_id'], calls[0]['call_id'])
        self.assertEqual(outgoing['service_tier'], 'priority')

    def test_tiers_reach_backend_and_returned_tier_is_not_rewritten(self):
        for tier, cache_key, expected in (('priority', 'accept-priority', 'priority'),
                                          ('default', 'accept-default', 'default'),
                                          ('priority', 'downgrade-priority', 'default')):
            with self.subTest(requested=tier, returned=expected):
                events = self.request(self.payload(service_tier=tier, prompt_cache_key=cache_key))
                self.assertEqual(self.backend.requests[-1][2]['service_tier'], tier)
                completed = next(e['response'] for e in events if e['type'] == 'response.completed')
                self.assertEqual(completed['service_tier'], expected)

    def test_reserved_reviewer_with_existing_verifier(self):
        original_open = urllib.request.urlopen

        def isolated_open(request, **kwargs):
            request.full_url = request.full_url.replace('http://127.0.0.1:4000', self.base)
            return original_open(request, **kwargs)

        with patch('verify_review.subprocess.check_output', return_value='sk-isolated-proxy-key'), \
                patch('verify_review.urllib.request.urlopen', side_effect=isolated_open):
            verify_review.verify(Path('/unused-isolated-credential'))
        outgoing = self.backend.requests[-1][2]
        self.assertEqual(outgoing['model'], 'codex-auto-review')
        self.assertEqual(outgoing['instructions'],
                         'Return only the requested JSON object for this deployment transport probe.')
        self.assertEqual(outgoing['text']['format']['type'], 'json_schema')

    def test_backend_errors_are_not_hidden(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(self.payload(prompt_cache_key='reject-probe'))
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('synthetic backend rejection', caught.exception.read().decode())
        caught.exception.close()


if __name__ == '__main__':
    unittest.main()
