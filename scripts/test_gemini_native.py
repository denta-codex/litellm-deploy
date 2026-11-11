"""Offline fidelity regressions through the actual pinned Router/Vertex bridge."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
from litellm.responses.litellm_completion_transformation.transformation import TOOL_CALLS_CACHE
from claude_context import REQUEST
from gemini_native import PREFIX, StateCodec, install, provision_key, read_key


MODEL = 'vertex/gemini-4-carbon'
SIGNATURE = 'synthetic-final-signature'
TOOL_SIGNATURE = 'synthetic-tool-signature'


class KeyTests(unittest.TestCase):
    def test_private_atomic_idempotent_provisioning_and_missing_key(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'gemini/state.key'
            self.assertTrue(provision_key(path))
            original = read_key(path)
            self.assertFalse(provision_key(path))
            self.assertEqual(read_key(path), original)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                read_key(path)
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'restore it'):
                provision_key(path)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                StateCodec(path).seal({'version': 1}, 'caller')
            self.assertFalse(path.exists())

    def test_ciphertext_is_caller_bound_and_authenticated(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'key'; provision_key(path)
            codec = StateCodec(path)
            token = codec.seal({'version': 1, 'secret': SIGNATURE}, 'owner')
            self.assertNotIn(SIGNATURE, token)
            self.assertEqual(codec.open(token, 'owner')['secret'], SIGNATURE)
            for corrupted, owner in ((token, 'someone-else'), (token[:-8] + 'abcdefgh', 'owner')):
                with self.assertRaisesRegex(ValueError, 'invalid or belongs'):
                    codec.open(corrupted, owner)
            path.write_bytes(os.urandom(32))
            with self.assertRaises(ValueError):
                codec.open(token, 'owner')


class NativeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick(): loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / 'state.key'; provision_key(self.key)
        self.codec = StateCodec(self.key)
        self.requests, self.responses, self.parts = [], [], []
        self.token = REQUEST.set({'caller': 'fixture-owner', 'headers': {'thread-id': 'fixture'}, 'request': {}})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        with patch.object(AsyncHTTPHandler, 'create_client', return_value=self.client):
            handler = AsyncHTTPHandler()
        handler.client = self.client
        self.patches = [
            patch.object(VertexBase, '_ensure_access_token_async', new=AsyncMock(return_value=('fixture-token', 'fixture-project'))),
            patch('litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini.get_async_httpx_client', return_value=handler),
            patch.object(litellm, 'module_level_aclient', handler)]
        for p in self.patches: p.start()
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.router = litellm.Router(model_list=[{'model_name': MODEL, 'litellm_params': {
            'model': 'vertex_ai/gemini-4-carbon', 'vertex_project': 'fixture-project',
            'vertex_location': 'global', 'vertex_credentials': 'fixture-only',
            'reasoning_effort': 'medium', 'use_chat_completions_api': True}}], num_retries=0)
        self.integration = install(self.router, self.codec)
        async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
        self.integration.dispatch = dispatch

    async def asyncTearDown(self):
        self.integration.uninstall()
        for p in reversed(self.patches): p.stop()
        await self.client.aclose()
        REQUEST.reset(self.token)
        litellm.in_memory_llm_clients_cache.flush_cache()
        TOOL_CALLS_CACHE.flush_cache()

    def upstream(self, request):
        self.requests.append(json.loads(request.content))
        parts = copy.deepcopy(self.parts[len(self.requests)-1] if self.parts else
                              [{'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}])
        self.responses.append(parts)
        value = {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP', 'index': 0}],
                 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 3, 'totalTokenCount': 18},
                 'modelVersion': 'gemini-4-carbon'}
        if 'streamGenerateContent' in str(request.url):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content='data: ' + json.dumps(value) + '\n\n')
        return httpx.Response(200, json=value)

    async def response(self, history, streaming=False, tools=None, summary='auto'):
        data = dict(model=MODEL, input=history, tools=tools or [], stream=streaming,
                    reasoning={'effort': 'high', 'summary': summary}, store=False)
        REQUEST.get()['request'] = data
        call = await self.integration.route(data, 'aresponses')
        result = await call
        if not streaming: return result.model_dump(exclude_none=True), []
        events = [e.model_dump(exclude_none=True) async for e in result]
        final = next(e['response'] for e in events if e['type'] == 'response.completed')
        self.assertEqual(final['output'], [e['item'] for e in events if e['type'] == 'response.output_item.done'])
        self.assertFalse(any(e['type'] == 'response.output_text.delta' and e['item_id'].startswith('rs_') for e in events))
        return final, events

    async def test_signed_final_text_json_stream_restart_and_cache_flush(self):
        for streaming in (False, True):
            self.requests.clear()
            first, _ = await self.response('Hello', streaming)
            carrier = next(i for i in first['output'] if str(i.get('encrypted_content', '')).startswith(PREFIX))
            self.assertNotIn(SIGNATURE, json.dumps(first))
            self.assertEqual(self.codec.open(carrier['encrypted_content'], 'fixture-owner')['parts'], self.responses[-1])
            # New hook instance and codec, same durable key, no live tool cache.
            self.integration.uninstall()
            dispatch = self.integration.dispatch
            self.integration = install(self.router, StateCodec(self.key)); self.integration.dispatch = dispatch
            TOOL_CALLS_CACHE.flush_cache()
            await self.response([{'role': 'user', 'content': 'Hello'}, *first['output'], {'role': 'user', 'content': 'Continue'}], streaming)
            native = [c for c in self.requests[-1]['contents'] if c['role'] == 'model']
            self.assertEqual(native[0]['parts'], self.responses[-2])

    async def test_parallel_tools_custom_tools_and_sequential_calls(self):
        for custom, streaming in ((False, False), (True, True)):
            self.requests.clear()
            tool = ({'type': 'custom', 'name': 'probe', 'description': 'Fixture', 'format': {'type': 'text'}} if custom else
                    {'type': 'function', 'name': 'probe', 'description': 'Fixture', 'parameters': {'type': 'object', 'properties': {}}})
            # Literal provider function name is also its declared name for these tools.
            args = {'content': 'fixture code'} if custom else {}
            first_parts = [{'text': 'Provider summary.', 'thought': True},
                           {'functionCall': {'name': 'probe', 'args': args}, 'thoughtSignature': TOOL_SIGNATURE}]
            if not custom: first_parts.append({'functionCall': {'name': 'probe', 'args': {'second': True}}})
            self.parts = [first_parts, [{'functionCall': {'name': 'probe', 'args': args}, 'thoughtSignature': 'sequential-signature'}],
                          [{'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}], [{'text': 'NEXT'}]]
            history = [{'role': 'user', 'content': 'Use fixture tools'}]
            for step in range(3):
                result, events = await self.response(history, streaming, [tool])
                if step:
                    native = [c for c in self.requests[-1]['contents'] if c['role'] == 'model']
                    self.assertEqual(native[-1]['parts'], self.parts[step-1])
                history.extend(result['output'])
                for call in result['output']:
                    if call['type'] in ('function_call', 'custom_tool_call'):
                        history.append({'type': 'custom_tool_call_output' if custom else 'function_call_output',
                                        'call_id': call['call_id'], 'output': 'DONE'})
                TOOL_CALLS_CACHE.flush_cache()
            history.append({'role': 'user', 'content': 'Continue'})
            await self.response(history, streaming, [tool])
            self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][-1]['parts'], self.parts[2])

    async def test_changed_batch_tampering_and_caller_isolation(self):
        first, _ = await self.response('Hello')
        for mode in ('text', 'id', 'cipher', 'caller', 'missing'):
            history = copy.deepcopy(first['output'])
            if mode == 'text': next(i for i in history if i['type'] == 'message')['content'][0]['text'] = 'CHANGED'
            if mode == 'id': next(i for i in history if i['type'] == 'reasoning')['id'] = 'rs_changed'
            if mode == 'cipher': history[-1]['encrypted_content'] = history[-1]['encrypted_content'][:-8] + 'abcdefgh'
            if mode == 'missing': history = history[-1:]
            owner = REQUEST.get()['caller']; REQUEST.get()['caller'] = 'other' if mode == 'caller' else owner
            with self.assertRaises(Exception): await self.response([*history, {'role': 'user', 'content': 'Continue'}])
            REQUEST.get()['caller'] = owner
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__': unittest.main()
