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
import yaml

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
        if (payload.get('prompt_cache_key') == 'historical-search-probe'
                and not any(tool.get('type') == 'web_search' for tool in payload.get('tools', []))):
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"response protection is unavailable","type":"server_error"}}')
            return
        compact = 'CONTEXT CHECKPOINT COMPACTION' in json.dumps(payload.get('input', []))
        if compact and payload['model'] != 'fixture-compactor':
            response = {'id': 'resp_failed_compaction', 'object': 'response', 'created_at': 1,
                        'model': payload['model'], 'status': 'failed', 'output': [],
                        'error': {'code': 'server_error', 'message': 'response protection is unavailable'}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'type': 'response.failed',
                             'sequence_number': 0, 'response': response}) + '\n\n').encode())
            self.wfile.flush()
            return
        if payload.get('prompt_cache_key') == 'reject-probe':
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"synthetic backend rejection","type":"invalid_request_error"}}')
            return
        if any(tool.get('type') == 'function' for tool in payload.get('tools', [])):
            item = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe',
                    'name': 'deployment_probe', 'arguments': '{}', 'status': 'completed'}
        else:
            text = '{"status":"REVIEW_ROUTE_OK"}' if payload['model'] == 'codex-auto-review' else 'STREAM_OK'
            if payload['model'] == 'fixture-compactor':
                text = 'The user asked to remember COMPACTION_CONTEXT_OK. Continue with that marker.'
            elif 'COMPACTION_CONTEXT_OK' in json.dumps(payload.get('input', [])):
                text = 'COMPACTION_CONTEXT_OK retained'
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
                       'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for index, event in enumerate(events):
            event['sequence_number'] = index
            self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()


class StartupTests(unittest.TestCase):
    def test_voice_route_is_registered_by_production_startup(self):
        # A healthy text proxy can still have lost its voice startup hook.
        # GET never creates a call; a registered POST route returns 405, not 404.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + '/v1/live', timeout=5)
        self.assertEqual(caught.exception.code, 405)
        caught.exception.close()

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
        (root / 'modal-inference-token').write_text('WK_SECRET=wk-isolated\nWS_SECRET=ws-isolated\n')
        (root / 'auth.json').write_text(json.dumps({
            'access_token': 'isolated-backend-token', 'account_id': 'isolated-account',
            'expires_at': time.time() + 3600,
        }))
        template = Environment(loader=FileSystemLoader(ROOT / 'deploy/templates'),
                               undefined=StrictUndefined).get_template('config.yaml.j2')
        config = root / 'config.yaml'
        settings = yaml.safe_load(template.render(litellm_model='chatgpt/gpt-6-astra'))
        settings['model_list'].append({'model_name': 'fixture/compactor', 'model_info': {'mode': 'responses'},
            'litellm_params': {'model': 'openai/fixture-compactor', 'api_key': 'synthetic-fixture',
                              'api_base': f'http://127.0.0.1:{cls.backend.server_port}/v1'}})
        config.write_text(yaml.safe_dump(settings))
        cls.catalog = root / 'catalog.json'
        cls.catalog.write_text(json.dumps({'models': [
            {'slug': 'chatgpt/gpt-6-astra', 'visibility': 'list', 'priority': 1, 'context_window': 272000,
             'comp_hash': '3000'},
            {'slug': 'fixture/compactor', 'visibility': 'list', 'priority': 90, 'context_window': 1000000,
             'input_modalities': ['text']}]}))
        cls.port = unused_port()
        cls.base = f'http://127.0.0.1:{cls.port}'
        env = dict(os.environ, CREDENTIALS_DIRECTORY=str(root), CHATGPT_TOKEN_DIR=str(root),
                   CHATGPT_API_BASE=f'http://127.0.0.1:{cls.backend.server_port}',
                   LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False',
                   COMPACTION_FALLBACK_ENABLED='true', COMPACTION_MODEL_CATALOG=str(cls.catalog),
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

    def request(self, payload, headers=None):
        request = urllib.request.Request(self.base + '/v1/responses', data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer sk-isolated-proxy-key', **(headers or {})})
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
        # Upstream now aggregates its subscription stream for stream=False.
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['output'])

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

    def test_compaction_recovers_through_real_proxy_and_other_provider(self):
        before = len(self.backend.requests)
        events = self.request(self.payload(input=[{'role': 'user', 'content': 'CONTEXT CHECKPOINT COMPACTION'}]),
                              headers={'x-codex-turn-metadata': '{"request_kind":"compaction"}'})
        outgoing = [r[2] for r in self.backend.requests[before:]]
        self.assertEqual([r['model'] for r in outgoing], ['gpt-6-astra', 'fixture-compactor'])
        self.assertEqual(outgoing[-1]['tools'], [])
        self.assertEqual(outgoing[-1]['tool_choice'], 'none')
        self.assertEqual(sum(e['type'] == 'response.completed' for e in events), 1)
        final = events[-1]['response']
        self.assertEqual(final['metadata']['compaction_model'], 'fixture/compactor')
        self.assertIn('COMPACTION_CONTEXT_OK', json.dumps(final['output']))

    def test_compaction_declares_historical_search_without_executing_tools(self):
        payload = self.payload(prompt_cache_key='historical-search-probe', tools=[], input=[
            {'role': 'user', 'content': 'Remember SEARCH_HISTORY_OK'},
            {'type': 'web_search_call', 'id': 'ws_historical', 'status': 'completed',
             'action': {'type': 'search', 'query': 'synthetic historical query'}}])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(payload)
        self.assertEqual(caught.exception.code, 500)
        self.assertIn('response protection is unavailable', caught.exception.read().decode())
        caught.exception.close()
        before = len(self.backend.requests)
        events = self.request(payload,
            headers={'x-codex-turn-metadata': '{"request_kind":"compaction"}'})
        outgoing = [r[2] for r in self.backend.requests[before:]]
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]['model'], 'gpt-6-astra')
        self.assertEqual(outgoing[0]['tools'], [{'type': 'web_search'}])
        self.assertEqual(outgoing[0]['tool_choice'], 'none')
        self.assertFalse(outgoing[0]['parallel_tool_calls'])
        self.assertEqual(events[-1]['response']['metadata']['compaction_model'], 'chatgpt/gpt-6-astra')

    @unittest.skipUnless(os.environ.get('CODEX_COMPACTION_TEST_BINARY'),
                         'set CODEX_COMPACTION_TEST_BINARY for installed Codex acceptance')
    def test_installed_codex_compacts_and_resumes_with_selected_model(self):
        # A repository-local disposable home avoids changing the desktop's sessions/auth.
        with tempfile.TemporaryDirectory(prefix='.compaction-acceptance-', dir=ROOT) as temporary:
            task_home = Path(temporary)
            (task_home / 'config.toml').write_text(f'''model = "chatgpt/gpt-6-astra"
model_provider = "fixture"
model_auto_compact_token_limit = 1
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
shell_snapshot = false
[model_providers.fixture]
name = "Offline compaction fixture"
base_url = "{self.base}/v1"
wire_api = "responses"
env_key = "COMPACTION_FIXTURE_KEY"
request_max_retries = 0
stream_max_retries = 0
''')
            env = os.environ | {'CODEX_HOME': str(task_home), 'COMPACTION_FIXTURE_KEY': 'sk-isolated-proxy-key'}
            binary = os.environ['CODEX_COMPACTION_TEST_BINARY']
            def run(arguments):
                completed = subprocess.run([binary, 'exec', *arguments], env=env,
                                           capture_output=True, text=True, timeout=60)
                if completed.returncode:
                    self.log.flush()
                    self.log.seek(0)
                    logs = self.log.read()
                    recovery = '\n'.join(line for line in logs.splitlines() if 'compaction_recovery' in line)
                    headers = [{k: v for k, v in r[1].items() if 'codex' in k} for r in self.backend.requests[-4:]]
                    kinds = [[{'type': i.get('type'), 'role': i.get('role'), 'keys': list(i)}
                              for i in r[2].get('input', [])] for r in self.backend.requests[-4:]]
                    self.fail(completed.stderr[-2000:] + completed.stdout[-2000:] + '\n' + recovery + '\n' + json.dumps(headers) + '\n' + json.dumps(kinds))
                return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            first = run(['--skip-git-repo-check', '-C', temporary, '--json',
                         'Remember COMPACTION_CONTEXT_OK and acknowledge it.'])
            thread = next(e['thread_id'] for e in first if e['type'] == 'thread.started')
            before = len(self.backend.requests)
            second = run(['resume', thread, '--skip-git-repo-check', '--json',
                          'Report the marker I asked you to remember.'])
            self.assertTrue(any(e['type'] == 'turn.completed' for e in second), second)
            self.assertIn('COMPACTION_CONTEXT_OK', json.dumps(second))
            requests = [r[2] for r in self.backend.requests[before:]]
            self.assertIn('fixture-compactor', [r['model'] for r in requests])
            self.assertEqual(requests[-1]['model'], 'gpt-6-astra')
            # Verify Codex actually persisted the replacement summary, not just a reply.
            rollouts = list((task_home / 'sessions').rglob('*.jsonl'))
            checkpoints = [json.loads(line) for path in rollouts for line in path.read_text().splitlines()
                           if json.loads(line).get('type') == 'compacted']
            self.assertTrue(checkpoints, 'Codex did not persist a compaction checkpoint')
            self.assertIn('COMPACTION_CONTEXT_OK', json.dumps(checkpoints))


if __name__ == '__main__':
    unittest.main()
