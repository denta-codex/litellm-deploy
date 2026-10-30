"""Native Bedrock Responses search must never round-trip through Chat messages."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'
import httpx
import litellm
from claude_context import REQUEST
from shared_search import SEARCH_NAME, SearchJournal, SearchResponses, SharedSearchInterceptor, plain
from test_shared_search import Backend


class NativeBedrockSearchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.requests = []
        self.mixed = False
        self.parallel = 1
        self.backend = Backend()
        self.interceptor = SharedSearchInterceptor()
        self.old_callbacks = litellm.callbacks
        litellm.callbacks = [self.interceptor]
        litellm.in_memory_llm_clients_cache.flush_cache()
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        with patch.object(AsyncHTTPHandler, 'create_client', return_value=self.client):
            self.handler = AsyncHTTPHandler()
        self.handler.client = self.client
        self.http_patch = patch('litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client', return_value=self.handler)
        self.http_patch.start()
        self.router = litellm.Router(model_list=[{'model_name': 'bedrock/gpt-6.1-sol', 'model_info': {'mode': 'responses'},
            'litellm_params': {'model': 'bedrock/us.openai.gpt-6.1-sol', 'aws_region_name': 'us-east-1',
                               'aws_access_key_id': 'AKIA' + 'A' * 16, 'aws_secret_access_key': 'B' * 40,
                               'store': False}}], num_retries=0)
        async def dispatch(data, route_type, **kwargs):
            return self.router.aresponses(**data)
        self.bridge = SearchResponses(dispatch, self.interceptor, self.backend, SearchJournal(self.temporary.name))
        self.token = REQUEST.set({'caller': 'caller-one', 'headers': {}, 'request': {}})

    async def asyncTearDown(self):
        REQUEST.reset(self.token)
        self.http_patch.stop()
        await self.client.aclose()
        litellm.callbacks = self.old_callbacks
        litellm.in_memory_llm_clients_cache.flush_cache()

    def upstream(self, request):
        self.assertEqual(str(request.url), 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses')
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertNotIn('messages', body)
        self.assertFalse(body['store'])
        result_present = any(i.get('type') == 'function_call_output' and i.get('call_id', '').startswith('call_search')
                             for i in body['input'])
        if result_present:
            output = [{'type': 'message', 'id': 'msg_final', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'BLUE: https://example.com/fact', 'annotations': []}]}]
        else:
            output = [{'type': 'reasoning', 'id': 'rs_signed', 'summary': [], 'encrypted_content': 'signed-reasoning'}]
            output += [{'type': 'function_call', 'id': 'fc_search' + str(i), 'call_id': 'call_search' + str(i),
                        'name': SEARCH_NAME, 'arguments': json.dumps({'query': 'fixture fact' + str(i)}),
                        'status': 'completed'} for i in range(self.parallel)]
            if self.mixed:
                output.append({'type': 'function_call', 'id': 'fc_image', 'call_id': 'call_image',
                               'name': 'view_image', 'arguments': '{}', 'status': 'completed'})
        return httpx.Response(200, json={'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
            'model': body['model'], 'status': 'completed', 'output': output,
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}})

    async def call(self, **extra):
        request = {'model': 'bedrock/gpt-6.1-sol', 'input': [{'role': 'user', 'content': 'Find the fact'}],
                   'stream': False, 'tools': [{'type': 'web_search'}, {'type': 'function', 'name': 'view_image',
                       'parameters': {'type': 'object', 'properties': {}}}]} | extra
        async with asyncio.timeout(20):
            coroutine = await self.bridge.route(data=request, route_type='aresponses')
            result = await coroutine
            return [plain(e) async for e in result] if request['stream'] else plain(result)

    async def test_native_parallel_search_replays_signed_reasoning_and_counts_usage(self):
        self.parallel = 2
        image = {'type': 'function_call_output', 'call_id': 'prior_image',
                 'output': [{'type': 'input_image', 'image_url': 'data:image/png;base64,fixture'}]}
        request_input = [{'role': 'user', 'content': 'Find the fact'}, image]
        original = copy.deepcopy(request_input)
        result = await self.call(input=request_input)
        self.assertEqual(request_input, original)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.backend.queries), 2)
        followup = self.requests[-1]['input']
        self.assertIn(image, followup)
        self.assertTrue(any(i.get('encrypted_content') == 'signed-reasoning' for i in followup))
        self.assertEqual(result['usage']['input_tokens'], 20)
        self.assertEqual(result['usage']['output_tokens'], 10)
        self.assertTrue(result['output'][-1]['content'][0]['annotations'])

    async def test_identical_retry_does_not_repeat_search_or_model_call(self):
        first = await self.call()
        count = len(self.requests)
        again = await self.call()
        self.assertEqual(again, first)
        self.assertEqual(len(self.requests), count)
        self.assertEqual(len(self.backend.queries), 1)

    async def test_no_search_request_also_disables_storage_without_mutating_caller(self):
        data = {'model': 'bedrock/gpt-6.1-sol', 'input': [], 'store': True}
        captured = []
        async def dispatch(data, route_type, **kwargs):
            captured.append(data)
            return 'direct'
        bridge = SearchResponses(dispatch, self.interceptor, self.backend, self.bridge.journal)
        self.assertEqual(await bridge.route(data=data, route_type='aresponses'), 'direct')
        self.assertFalse(captured[0]['store'])
        self.assertTrue(data['store'])

    async def test_mixed_search_image_tool_stream_and_continuation_is_caller_isolated(self):
        self.mixed = True
        events = await self.call(stream=True)
        result = next(e['response'] for e in events if e['type'] == 'response.completed')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['usage']['input_tokens'], 10)
        self.assertEqual(sum(e['type'] == 'response.output_item.done' for e in events), 3)
        search = next(i for i in result['output'] if i['type'] == 'web_search_call')
        image_result = {'type': 'function_call_output', 'call_id': 'call_image',
                        'output': [{'type': 'input_image', 'image_url': 'data:image/png;base64,fixture'}]}
        history = [{'role': 'user', 'content': 'Find the fact'}] + result['output'] + [image_result]
        self.mixed = False
        await self.call(input=history)
        self.assertEqual(len(self.requests), 2)
        self.assertIn(image_result, self.requests[-1]['input'])
        record = self.bridge.journal.read('caller-one', search['id'])
        self.assertEqual(record['pending'], [])
        with self.assertRaises(ValueError):
            self.bridge.journal.expand('caller-two', history)


if __name__ == '__main__':
    unittest.main()
