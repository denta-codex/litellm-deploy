"""Kimi state, tools, reasoning and streaming through the pinned Modal bridge."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import httpx
import litellm
from litellm.responses.litellm_completion_transformation.transformation import TOOL_CALLS_CACHE
from claude_context import REQUEST
from compaction_fallback import portable_input
from chat_native import install
from refresh_models import configured_models


MODEL = 'modal/kimi-k3.1-pro'
THINKING = 'Inspect the current state before continuing.'


def plain(value):
    return value.model_dump(exclude_none=True) if hasattr(value, 'model_dump') else copy.deepcopy(value)


class KimiBridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick(): loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.requests = []
        self.calls = 0
        self.mode = 'text'
        self.token = REQUEST.set({'caller': 'fixture-owner', 'headers': {'thread-id': 'fixture'}, 'request': {}})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        self.session_patch = patch('litellm.aclient_session', self.client)
        self.session_patch.start()
        specs = [{'name': MODEL, 'litellm': {
            'model': 'openai/kimi-k3.1-pro', 'api_base': 'https://modal.invalid/v1',
            'api_key': 'fixture-only', 'use_chat_completions_api': True},
            'model_info': {'catalog_pricing': {'provider': 'moonshotai', 'model': 'kimi-k3.1-pro'}},
            'codex': {'display_name': 'Kimi', 'context_window': 1048576, 'input_modalities': ['text', 'image'],
                      'supported_reasoning_levels': ['low', 'high', 'max'], 'default_reasoning_level': 'high'}}]
        routes, self.rows, _ = zip(*configured_models(specs))
        self.router = litellm.Router(model_list=list(routes), num_retries=0, timeout=10)
        async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
        self.integration = install()
        self.addCleanup(self.integration.uninstall)

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.client.aclose()
        REQUEST.reset(self.token)
        TOOL_CALLS_CACHE.flush_cache()
        litellm.in_memory_llm_clients_cache.flush_cache()

    def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.calls += 1
        self.assertEqual(request.headers['authorization'], 'Bearer fixture-only')
        tool = self.mode in ('function', 'custom', 'parallel', 'search') and self.calls % 2 == 1
        message = {'role': 'assistant', 'reasoning_content': THINKING}
        if tool:
            name = {'function': 'probe', 'custom': 'apply_patch', 'parallel': 'probe',
                    'search': 'web_search'}[self.mode]
            calls = [{'index': 0, 'id': 'call_probe', 'type': 'function',
                      'function': {'name': name, 'arguments': '{"value":"fixture"}'}}]
            if self.mode == 'parallel':
                calls.append({'index': 1, 'id': 'call_second', 'type': 'function',
                              'function': {'name': 'probe', 'arguments': '{"value":"second"}'}})
            message['tool_calls'] = calls
            finish = 'tool_calls'
        else:
            message['content'] = 'KIMI_OK'
            finish = 'stop'
        value = {'id': 'chatcmpl-kimi', 'object': 'chat.completion', 'created': 1,
                 'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        if body.get('stream'):
            chunks = []
            for key in ('reasoning_content', 'content', 'tool_calls'):
                if key not in message: continue
                delta = {'role': 'assistant', key: message[key]}
                chunks.append({'id': value['id'], 'object': 'chat.completion.chunk', 'created': 1,
                               'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})
            chunks.append({'id': value['id'], 'object': 'chat.completion.chunk', 'created': 1,
                           'model': body['model'], 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})
            content = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=content)
        return httpx.Response(200, json=value)

    async def response(self, history, streaming=False, tools=None):
        data = dict(model=MODEL, input=history, stream=streaming, store=False,
                    reasoning={'effort': 'high', 'summary': 'auto'}, tools=tools or [])
        REQUEST.get()['request'] = data
        result = await self.router.aresponses(**data)
        if not streaming: return plain(result), []
        events = [plain(event) async for event in result]
        final = next(e['response'] for e in events if e['type'] == 'response.completed')
        self.assertFalse(any(e['type'] == 'response.output_text.delta' and e['item_id'].startswith('rs_') for e in events))
        done = {e['item']['id']: e['item'] for e in events if e['type'] == 'response.output_item.done'}
        self.assertEqual({i['id']: i for i in final['output']}, done)
        return final, events

    async def test_reasoning_state_survives_json_stream_restart_and_cache_flush(self):
        for streaming in (False, True):
            self.requests.clear(); self.calls = 0
            first, events = await self.response('Hello', streaming)
            reasoning = next(i for i in first['output'] if i['type'] == 'reasoning')
            self.assertEqual(next(i for i in reasoning['content'] if i['type'] == 'output_text')['text'], THINKING)
            if streaming:
                self.assertFalse(any(e['type'] == 'response.reasoning_summary_text.delta' for e in events))
            TOOL_CALLS_CACHE.flush_cache()
            litellm.in_memory_llm_clients_cache.flush_cache()
            self.router = litellm.Router(model_list=self.router.model_list, num_retries=0, timeout=10)
            await self.response([{'role': 'user', 'content': 'Hello'}, *first['output'],
                                 {'role': 'user', 'content': 'Continue'}], streaming)
            assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
            self.assertEqual(assistant['reasoning_content'], THINKING)
            self.assertEqual(assistant['content'], [{'type': 'text', 'text': 'KIMI_OK'}])
            self.assertEqual(self.requests[-1]['reasoning_effort'], 'high')

    async def test_tool_batches_and_reasoning_stay_attached(self):
        for mode in ('function', 'custom', 'parallel'):
            with self.subTest(mode=mode):
                self.requests.clear(); self.calls = 0; self.mode = mode
                tool = {'type': 'custom', 'name': 'apply_patch', 'description': 'Patch', 'format': {'type': 'text'}} if mode == 'custom' else {
                    'type': 'function', 'name': 'probe', 'description': 'Probe',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}
                first, _ = await self.response('Use tools', mode == 'parallel', [tool])
                calls = [i for i in first['output'] if i['type'] in ('function_call', 'custom_tool_call')]
                self.assertEqual(len(calls), 2 if mode == 'parallel' else 1)
                history = [{'role': 'user', 'content': 'Use tools'}, *first['output']]
                for call in calls:
                    history.append({'type': 'custom_tool_call_output' if mode == 'custom' else 'function_call_output',
                                    'call_id': call['call_id'], 'output': 'DONE'})
                await self.response(history, mode == 'parallel', [tool])
                assistant = next(m for m in self.requests[-1]['messages'] if m.get('tool_calls'))
                self.assertEqual(assistant['reasoning_content'], THINKING)
                self.assertEqual([c['id'] for c in assistant['tool_calls']], [c['call_id'] for c in calls])
                self.assertEqual(sum(m['role'] == 'tool' for m in self.requests[-1]['messages']), len(calls))

    async def test_reasoning_summary_can_be_hidden_without_losing_replay(self):
        data = dict(model=MODEL, input='Hello', stream=False, store=False,
                    reasoning={'effort': 'high', 'summary': 'none'}, tools=[])
        first = plain(await self.router.aresponses(**data))
        self.assertTrue(any(i['type'] == 'reasoning' for i in first['output']))
        await self.response([{'role': 'user', 'content': 'Hello'}, *first['output'],
                             {'role': 'user', 'content': 'Continue'}])
        assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
        self.assertEqual(assistant['reasoning_content'], THINKING)

    async def test_delayed_answer_streams_before_provider_completion(self):
        # The production hook normalizes upstream events without adding a
        # completed-response buffering layer.
        stream = await self.router.aresponses(model=MODEL, input='Hello', stream=True,
            store=False, reasoning={'effort': 'high'}, tools=[])
        first = plain(await asyncio.wait_for(anext(stream), 2))
        self.assertEqual(first['type'], 'response.created')
        remaining = [plain(e) async for e in stream]
        self.assertTrue(any(e['type'] == 'response.output_text.delta' for e in remaining))

    async def test_compaction_checkpoint_does_not_replay_stale_thinking(self):
        first, _ = await self.response('Hello')
        checkpoint = [{'role': 'user', 'content': 'Readable checkpoint'}, {'role': 'user', 'content': 'Continue'}]
        await self.response(checkpoint)
        self.assertFalse(any(m.get('reasoning_content') for m in self.requests[-1]['messages']))
        portable = portable_input({'input': [{'role': 'user', 'content': 'Summarize'}, *first['output']]})
        # Readable reasoning can be quoted for the compactor, but it must not
        # become native reasoning_content on a rewritten assistant checkpoint.
        self.assertIn(THINKING, json.dumps(portable))
        self.assertIn('KIMI_OK', json.dumps(portable))

    async def test_stock_codex_accepts_kimi_reasoning_catalog(self):
        row = self.rows[0]
        self.assertFalse(row['supports_reasoning_summaries'])
        self.assertEqual(row['default_reasoning_summary'], 'none')
        self.assertIn('Codex', row['base_instructions'])
        self.assertIn('apply_patch', row['base_instructions'])
        with tempfile.TemporaryDirectory(prefix='kimi-catalog-') as directory:
            catalog = Path(directory) / 'models.json'
            catalog.write_text(json.dumps({'models': [row]}))
            result = subprocess.run(['codex', 'debug', 'models', '-c', 'model_catalog_json=' + json.dumps(str(catalog))],
                                    env=os.environ | {'CODEX_HOME': directory}, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = next(r for r in json.loads(result.stdout)['models'] if r['slug'] == MODEL)
            self.assertFalse(parsed.get('supports_reasoning_summaries', False))

    async def test_search_stream_retains_reasoning_for_next_turn(self):
        from shared_search import SearchJournal, SearchResponses, SearchResult, SharedSearchInterceptor
        class Backend:
            async def search(self, query, settings):
                raise AssertionError('Hosted search was declared but not called')
        interceptor = SharedSearchInterceptor()
        litellm.callbacks = [interceptor]
        self.addCleanup(setattr, litellm, 'callbacks', [])
        with tempfile.TemporaryDirectory(prefix='kimi-search-') as directory:
            async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
            bridge = SearchResponses(dispatch, interceptor, Backend(), SearchJournal(directory), self.router)
            data = dict(model=MODEL, input='Hello', stream=True, store=False,
                        reasoning={'effort': 'high'}, tools=[{'type': 'web_search'}])
            REQUEST.get()['request'] = data
            result = await (await bridge.route(data, 'aresponses'))
            events = [plain(event) async for event in result]
            final = next(e['response'] for e in events if e['type'] == 'response.completed')
            reasoning = next(i for i in final['output'] if i['type'] == 'reasoning')
            self.assertEqual(reasoning['content'][0]['text'], THINKING)
            self.assertFalse(any(e['type'] == 'response.output_text.delta' and e['item_id'].startswith('rs_') for e in events))
            data.update(input=[{'role': 'user', 'content': 'Hello'}, *final['output'],
                               {'role': 'user', 'content': 'Continue'}], stream=False)
            REQUEST.get()['request'] = data
            await (await bridge.route(data, 'aresponses'))
            assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
            self.assertEqual(assistant['reasoning_content'], THINKING)


if __name__ == '__main__':
    unittest.main()
