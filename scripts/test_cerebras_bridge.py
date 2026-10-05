"""Exercise stock Cerebras transport and retry policy without network or real credentials."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'
import litellm
from configure_cerebras import MODEL, ROUTE, generate

litellm.telemetry = False
litellm.turn_off_message_logging = True


class CerebrasBridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.requests = []
        self.reject = False
        config, _, _ = generate({'model': MODEL}, {}, {'models': []}, '')
        route = config['model_list'][0]
        route['litellm_params'].update(api_key='fixture-cerebras-key', api_base='https://cerebras.invalid/v1')
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        self.session_patch = patch('litellm.aclient_session', self.http)
        self.session_patch.start()
        self.router = litellm.Router(model_list=config['model_list'], num_retries=2,
                                    model_group_retry_policy=config['router_settings']['model_group_retry_policy'])

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.http.aclose()
        litellm.in_memory_llm_clients_cache.flush_cache()

    def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.reject:
            return httpx.Response(503, json={'error': {'message': 'fixture provider unavailable', 'type': 'server_error'}})
        return httpx.Response(200, json={'id': 'fixture-completion', 'object': 'chat.completion', 'created': 1,
            'model': MODEL, 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': '{"suggestions":["Show me a layout","What about speed?","Keep my draft"]}'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130}})

    async def test_reasoning_schema_and_key_reach_only_the_provider_wire(self):
        schema = {'type': 'json_schema', 'json_schema': {'name': 'followups', 'strict': True,
            'schema': {'type': 'object', 'properties': {'suggestions': {'type': 'array', 'items': {'type': 'string'}}},
                       'required': ['suggestions'], 'additionalProperties': False}}}
        result = await self.router.acompletion(model=ROUTE, messages=[{'role': 'user', 'content': 'Synthetic context'}],
            reasoning_effort='none', response_format=schema, max_tokens=128, stream=False)
        self.assertEqual(result.choices[0].finish_reason, 'stop')
        request, body = self.requests[0]
        self.assertEqual(str(request.url), 'https://cerebras.invalid/v1/chat/completions')
        self.assertEqual(request.headers['Authorization'], 'Bearer fixture-cerebras-key')
        self.assertEqual(body['model'], MODEL)
        self.assertEqual(body['reasoning_effort'], 'none')
        self.assertEqual(body['response_format'], schema)
        self.assertNotIn('tools', body)
        self.assertEqual(len(self.requests), 1)

    async def test_suggestions_route_does_not_retry_provider_failures(self):
        self.reject = True
        with self.assertRaises(litellm.ServiceUnavailableError):
            async with asyncio.timeout(5):
                await self.router.acompletion(model=ROUTE, messages=[{'role': 'user', 'content': 'Synthetic context'}])
        self.assertEqual(len(self.requests), 1)
