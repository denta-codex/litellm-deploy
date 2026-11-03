"""Offline local-runtime manifest and local-route failure regression tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from refresh_models import configured_model

MANIFEST = Path(__file__).with_name('local-model.json')


def local_spec():
    """A configured route for the shipped local runtime, as config.example.yml describes it."""
    manifest = json.loads(MANIFEST.read_text())
    return {'name': 'local/' + manifest['name'],
            'litellm': {'model': 'openai/' + manifest['name'], 'api_base': f"http://127.0.0.1:{manifest['port']}/v1",
                        'api_key': 'local-loopback', 'use_chat_completions_api': True,
                        'timeout': 300, 'stream_timeout': 300, 'max_retries': 0, 'num_retries': 0},
            'codex': {'display_name': manifest['display_name'], 'context_window': manifest['context_window'],
                      'supported_reasoning_levels': [{'effort': 'none', 'description': 'Direct answers'}],
                      'default_reasoning_level': 'none', 'supports_parallel_tool_calls': False,
                      'truncation_policy': {'mode': 'tokens', 'limit': 2000}},
            'verify': {'validators': [['verify_local.py', '--proxy']]}}


class LocalTests(unittest.TestCase):
    def test_chat_template_override_matches_manifest(self):
        spec = json.loads(MANIFEST.read_text())
        override = Path(__file__).parents[1] / 'deploy/files' / spec['chat_template']['name']
        data = override.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), spec['chat_template']['sha256'])
        self.assertNotIn(b'System message must be at the beginning', data)

    def test_configured_local_route_is_plain_litellm(self):
        route, row, verify = configured_model(local_spec())
        self.assertEqual(route['litellm_params']['api_base'], 'http://127.0.0.1:18743/v1')
        self.assertEqual(route['litellm_params']['max_retries'], 0)
        self.assertEqual(row['input_modalities'], ['text'])
        self.assertEqual(row['max_context_window'], 16384)
        self.assertEqual(verify['validators'], [['verify_local.py', '--proxy']])


class LocalFailureTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        # A periodic tick keeps LiteLLM's background executor handoffs from stalling the test loop.
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    async def test_failed_load_is_not_retried_by_router_or_client(self):
        os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
        import httpx
        import litellm
        litellm.in_memory_llm_clients_cache.flush_cache()
        calls = []
        def fail(request):
            calls.append(request)
            return httpx.Response(503, json={'error': {'message': 'fixture: model load failed', 'type': 'server_error'}})
        route, _, _ = configured_model(local_spec())
        router = litellm.Router(model_list=[route])
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
                with patch('litellm.aclient_session', client):
                    async with asyncio.timeout(20):
                        with self.assertRaises(litellm.ServiceUnavailableError):
                            await router.aresponses(model=route['model_name'], input='fixture', stream=False)
            self.assertEqual(len(calls), 1)
        finally:
            litellm.in_memory_llm_clients_cache.flush_cache()


if __name__ == '__main__':
    unittest.main()
