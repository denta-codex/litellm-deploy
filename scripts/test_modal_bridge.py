"""Exercise pinned stock LiteLLM using an in-memory Modal HTTP transport."""
import json
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import litellm
from configure_modal import MANIFEST, generate

litellm.telemetry = False
litellm.turn_off_message_logging = True


class ModalBridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        # Also wake for executor shutdown when a sandbox suppresses socket wakeups.
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    @classmethod
    def setUpClass(cls):
        config, cls.catalog, _ = generate(json.loads(MANIFEST.read_text()), {}, {}, '')
        cls.requests = []

        cls.routes = config['model_list']
        for route in cls.routes:
            route['litellm_params']['api_base'] = 'https://modal.invalid/v1'
            route['litellm_params']['api_key'] = 'wk-fake.ws-fake'

    def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, request.headers['Authorization'], body))
        self.assertEqual(str(request.url), 'https://modal.invalid/v1/chat/completions')
        output = body.get('tools', [])
        if output:
            name = output[0]['function']['name']
            if name == 'apply_patch':
                self.assertIn('content', output[0]['function']['parameters']['properties'])
            arguments = '{"content":"patch content"}' if name == 'apply_patch' else '{"value":"probe"}'
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call_probe', 'type': 'function',
                     'function': {'name': name, 'arguments': arguments}}]}
            reason = 'tool_calls'
        else:
            delta = {'role': 'assistant', 'content': 'MODAL_BRIDGE_OK'}
            reason = 'stop'
        events = []
        for choice in ({'index': 0, 'delta': delta, 'finish_reason': None},
                       {'index': 0, 'delta': {}, 'finish_reason': reason}):
            events.append({'id': 'chatcmpl-probe', 'object': 'chat.completion.chunk', 'created': 1,
                           'model': body['model'], 'choices': [choice]})
        events.append({'id': 'chatcmpl-probe', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': body['model'], 'choices': [],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
        content = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=content)

    async def asyncSetUp(self):
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        self.session_patch = patch('litellm.aclient_session', self.http)
        self.session_patch.start()
        self.router = litellm.Router(model_list=self.routes, num_retries=0, timeout=10)

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.http.aclose()
        litellm.in_memory_llm_clients_cache.flush_cache()

    async def response(self, model, **kwargs):
        async with asyncio.timeout(15):
            stream = await self.router.aresponses(model=model, stream=True, **kwargs)
            events = [event.model_dump() async for event in stream]
        self.assertTrue(any(e['type'] == 'response.completed' for e in events))
        self.assertFalse(any(e['type'] in ('error', 'response.failed') for e in events))
        return events

    async def test_all_models_stream_and_forward_reasoning(self):
        for route in self.routes:
            with self.subTest(model=route['model_name']):
                events = await self.response(route['model_name'], input='Say hello', reasoning={'effort': 'high'})
                self.assertIn('MODAL_BRIDGE_OK', ''.join(e.get('delta', '') for e in events if e['type'] == 'response.output_text.delta'))
                path, auth, body = self.requests[-1]
                self.assertEqual(path, '/v1/chat/completions')
                self.assertEqual(auth, 'Bearer wk-fake.ws-fake')
                self.assertEqual(body['model'], route['model_name'].removeprefix('modal/'))
                self.assertEqual(body['reasoning_effort'], 'high')
                self.assertTrue(body['stream_options']['include_usage'])

    async def test_namespaced_and_custom_tools_with_full_history_continuation(self):
        model = self.routes[0]['model_name']
        function = {'type': 'function', 'name': 'probe', 'description': 'Probe',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}}
        for tool in (function, {'type': 'namespace', 'name': 'functions', 'tools': [function]},
                     {'type': 'custom', 'name': 'apply_patch', 'description': 'Apply a patch', 'format': {'type': 'text'}}):
            with self.subTest(tool=tool['type']):
                first = await self.response(model, input='Use the tool', tools=[tool], tool_choice='required')
                items = [e['item'] for e in first if e['type'] == 'response.output_item.done']
                calls = [i for i in items if i['type'] in ('function_call', 'custom_tool_call')]
                self.assertEqual(len(calls), 1)
                call = calls[0]
                if tool['type'] == 'namespace':
                    self.assertEqual(call['namespace'], 'functions')
                    self.assertEqual(call['name'], 'probe')
                if tool['type'] == 'custom':
                    self.assertEqual(call['type'], 'custom_tool_call')
                    self.assertEqual(call['input'], 'patch content')
                result_type = 'custom_tool_call_output' if call['type'] == 'custom_tool_call' else 'function_call_output'
                await self.response(model, input=[{'role': 'user', 'content': 'Use the tool'}, *items,
                                    {'type': result_type, 'call_id': call['call_id'], 'output': 'PROBE_RESULT'}])
                messages = self.requests[-1][2]['messages']
                self.assertTrue(any(m['role'] == 'tool' and m['content'] == 'PROBE_RESULT' for m in messages))

    def test_stock_codex_accepts_catalog(self):
        with tempfile.TemporaryDirectory(prefix='modal-catalog-') as directory:
            catalog = Path(directory) / 'models.json'
            catalog.write_text(json.dumps(self.catalog))
            result = subprocess.run(['codex', 'debug', 'models', '-c', 'model_catalog_json=' + json.dumps(str(catalog))],
                                    env=os.environ | {'CODEX_HOME': directory}, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)['models']
            self.assertEqual({r['slug'] for r in rows if r.get('visibility') == 'list'}, {r['model_name'] for r in self.routes})


if __name__ == '__main__':
    unittest.main()
