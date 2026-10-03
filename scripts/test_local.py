"""Offline local-route ownership and acceptance-fixture regression tests."""
import copy
import json
import unittest

from configure_local import MANIFEST, generate
from verify_local import image_fixture


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
        self.assertEqual(entry['input_modalities'], ['text', 'image'])
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

    def test_image_fixture_is_png(self):
        import base64
        self.assertTrue(base64.b64decode(image_fixture().split(',', 1)[1]).startswith(b'\x89PNG\r\n\x1a\n'))


if __name__ == '__main__':
    unittest.main()
