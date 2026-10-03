"""Offline local-route ownership and acceptance-fixture regression tests."""
import copy
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from configure_local import LocalSetup, MANIFEST, generate
from refresh_models import Refresh, encode


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads(MANIFEST.read_text())
        self.config = {'model_list': [
            {'model_name': 'chatgpt/astra', 'litellm_params': {'model': 'chatgpt/astra'}},
            {'model_name': 'codex-auto-review', 'litellm_params': {'model': 'chatgpt/codex-auto-review'}},
            {'model_name': 'modal/example', 'litellm_params': {'model': 'openai/example'}}],
            'general_settings': {'master_key': 'os.environ/LITELLM_MASTER_KEY'}}
        self.catalog = {'models': [{'slug': 'chatgpt/astra'}, {'slug': 'codex-auto-review', 'visibility': 'hide'}]}

    def test_preserves_other_providers_and_converges(self):
        originals = copy.deepcopy((self.config, self.catalog))
        config, catalog, summary = generate(self.spec, self.config, self.catalog, 'chatgpt/astra')
        self.assertEqual((self.config, self.catalog), originals)
        self.assertEqual(config['model_list'][:-1], self.config['model_list'])
        self.assertEqual(catalog['models'][:-1], self.catalog['models'])
        self.assertEqual(config['general_settings'], self.config['general_settings'])
        self.assertEqual(summary['test_models'], ['local/qwen3.8-27b'])
        again_config, again_catalog, again = generate(self.spec, config, catalog, 'local/qwen3.8-27b')
        self.assertEqual((config, catalog), (again_config, again_catalog))
        self.assertEqual(again['test_models'], [])

    def test_explicit_endpoint_timeout_and_capabilities(self):
        config, catalog, _ = generate(self.spec, self.config, self.catalog, '')
        params = config['model_list'][-1]['litellm_params']
        self.assertEqual(params['api_base'], 'http://127.0.0.1:18743/v1')
        self.assertEqual(params['timeout'], 300)
        self.assertEqual(params['max_retries'], 0)
        self.assertTrue(params['use_chat_completions_api'])
        entry = catalog['models'][-1]
        self.assertEqual(entry['context_window'], 16384)
        self.assertEqual(entry['input_modalities'], ['text'])
        self.assertTrue(entry['supports_search_tool'])
        self.assertFalse(entry['prefer_websockets'])

    def test_route_or_capability_edits_require_revalidation(self):
        config, catalog, _ = generate(self.spec, self.config, self.catalog, '')
        config['model_list'][-1]['litellm_params']['timeout'] = 30
        catalog['models'][-1]['supports_search_tool'] = False
        _, _, result = generate(self.spec, config, catalog, '')
        self.assertEqual(result['test_models'], ['local/qwen3.8-27b'])
        self.assertIn('supports_search_tool', result['changed_fields']['local/qwen3.8-27b'])

    def test_unknown_saved_manifest_or_selected_model_rejected(self):
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            generate(self.spec | {'port': 8081}, self.config, self.catalog, '')
        with self.assertRaisesRegex(ValueError, 'Selected local model disappeared'):
            generate(self.spec, self.config, self.catalog, 'local/other')


class LocalActivationTests(LocalTests):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.refresh = LocalSetup(self.home, self.home / 'runtime', 'uv')
        self.refresh.state.mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump(self.config))
        self.refresh.paths['catalog'].write_text(encode(self.catalog))
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': self.spec}
        self.originals = self.refresh.originals()

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            return self.refresh.prepare()

    def test_failure_blocks_publication_and_rollback_restores_files(self):
        self.prepare()
        self.refresh.activate()
        with patch('configure_local.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['verify_local'])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.refresh.validate()
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_shared_lock_identifies_local_recovery_owner(self):
        self.prepare()
        other = Refresh(self.home, self.home / 'runtime', 'uv')
        with self.assertRaisesRegex(ValueError, 'local.yml'):
            other.rollback()

    def test_publishes_only_after_all_probes_and_then_converges(self):
        self.prepare()
        self.refresh.activate()
        self.assertEqual(self.refresh.paths['catalog'].read_text(), self.originals['catalog'])
        with patch('configure_local.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.refresh.validate()
        self.assertEqual(run.call_count, 3)
        self.refresh.finish()
        self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
        with patch.object(self.refresh, 'discover', return_value=self.snapshot):
            self.assertFalse(self.refresh.prepare()['any_changes'])

    def test_changed_artifacts_revalidate_even_if_route_unchanged(self):
        config, catalog, _ = generate(self.spec, self.config, self.catalog, '')
        self.refresh.paths['config'].write_text(yaml.safe_dump(config))
        self.refresh.paths['catalog'].write_text(encode(catalog))
        old = copy.deepcopy(self.snapshot)
        old['catalog']['revision'] = 'old'
        self.refresh.paths['snapshot'].write_text(encode(old))
        candidate = self.refresh.candidate(self.snapshot)
        self.assertFalse(candidate['summary']['routing_changed'])
        self.assertEqual(candidate['summary']['test_models'], ['local/qwen3.8-27b'])


class LocalFailureTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
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
        config, _, _ = generate(json.loads(MANIFEST.read_text()), {}, {}, '')
        router = litellm.Router(model_list=config['model_list'])
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
                with patch('litellm.aclient_session', client):
                    async with asyncio.timeout(20):
                        with self.assertRaises(litellm.ServiceUnavailableError):
                            await router.aresponses(model='local/qwen3.8-27b', input='fixture', stream=False)
            self.assertEqual(len(calls), 1)
        finally:
            litellm.in_memory_llm_clients_cache.flush_cache()


if __name__ == '__main__':
    unittest.main()
