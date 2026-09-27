"""Offline regression tests against the installed, locked upstream adapter."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

from litellm.llms.chatgpt.responses import transformation
from litellm.llms.custom_httpx.http_handler import HTTPHandler
import httpx
import litellm
from patch_litellm import VERSION, apply_patch, patched_source


class ServiceTierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'transformation.py'
        self.source = Path(transformation.__file__).read_bytes()
        self.path.write_bytes(self.source)

    def adapter(self, source):
        self.path.write_bytes(source)
        name = 'litellm.llms.chatgpt.responses._service_tier_test'
        spec = importlib.util.spec_from_file_location(name, self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, 'Authenticator'):
            return module.ChatGPTResponsesAPIConfig()

    def test_preserve_tiers_without_enabling_priority_by_default(self):
        adapter = self.adapter(patched_source(self.source))
        for tier in (None, 'priority', 'auto', 'default', 'flex'):
            with self.subTest(tier=tier):
                params = {'truncation': 'auto', 'temperature': 1}
                if tier is not None:
                    params['service_tier'] = tier
                mapped = adapter.map_openai_params(params, 'gpt-6-astra', False)
                result = adapter.transform_responses_api_request('gpt-6-astra', 'probe', mapped, {}, {})
                if tier is None:
                    self.assertNotIn('service_tier', result)
                else:
                    self.assertEqual(result['service_tier'], tier)
                self.assertNotIn('temperature', result)
                self.assertFalse(result['store'])
                self.assertTrue(result['stream'])
                self.assertIn('reasoning.encrypted_content', result['include'])

    def test_tool_continuation_keeps_priority_and_items(self):
        adapter = self.adapter(patched_source(self.source))
        items = [{'type': 'function_call_output', 'call_id': 'call_probe', 'output': 'OK'}]
        result = adapter.transform_responses_api_request(
            'gpt-6-astra', items, {'service_tier': 'priority', 'previous_response_id': 'resp_probe'}, {}, {})
        self.assertEqual(result['input'], items)
        self.assertEqual(result['service_tier'], 'priority')
        self.assertEqual(result['previous_response_id'], 'resp_probe')

    def test_responses_sends_tier_to_http_transport(self):
        adapter = self.adapter(patched_source(self.source))
        adapter.authenticator.get_access_token.return_value = 'test-token'
        adapter.authenticator.get_account_id.return_value = None
        adapter.authenticator.get_api_base.return_value = 'https://subscription.invalid'

        def probe():
            for tier in (None, 'priority', 'default'):
                sent = []

                def backend(request):
                    sent.append(json.loads(request.content))
                    event = {'type': 'response.completed', 'sequence_number': 0, 'response': {
                        'id': 'resp_probe', 'created_at': 0, 'object': 'response', 'output': [],
                        'model': 'gpt-6-astra', 'status': 'completed', 'service_tier': tier or 'default'}}
                    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                          content='data: ' + json.dumps(event) + '\n\n')

                with httpx.Client(transport=httpx.MockTransport(backend)) as client:
                    handler = HTTPHandler(client=client)
                    options = {} if tier is None else {'service_tier': tier}
                    with patch('litellm.utils.ProviderConfigManager.get_provider_responses_api_config', return_value=adapter):
                        stream = litellm.responses(model='chatgpt/gpt-6-astra', input='probe',
                                                         stream=True, client=handler, timeout=5, num_retries=0, **options)
                        events = list(stream)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]['model'], 'gpt-6-astra')
                if tier is None:
                    self.assertNotIn('service_tier', sent[0])
                else:
                    self.assertEqual(sent[0]['service_tier'], tier)
                self.assertEqual(events[-1].type, 'response.completed')

        with patch('litellm.llms.chatgpt.chat.transformation.Authenticator', return_value=adapter.authenticator):
            probe()

    def test_preview_idempotence_and_permissions(self):
        self.path.chmod(0o640)
        preview = apply_patch(self.path, VERSION, check=True)
        self.assertEqual(self.path.read_bytes(), self.source)
        self.assertEqual(apply_patch(self.path, VERSION)['changed'], preview['changed'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(apply_patch(self.path, VERSION)['changed'])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_source_drift_and_version_change_fail_without_writing(self):
        for source, version in ((self.source + b'\n# changed\n', VERSION), (self.source, 'next')):
            with self.subTest(version=version):
                self.path.write_bytes(source)
                with self.assertRaises(ValueError):
                    apply_patch(self.path, version)
                self.assertEqual(self.path.read_bytes(), source)

    def test_restore_original_without_recovery_copies(self):
        apply_patch(self.path, VERSION)
        modified = self.path.read_bytes()
        self.assertTrue(apply_patch(self.path, VERSION, check=True, restore=True)['changed'])
        self.assertEqual(self.path.read_bytes(), modified)
        self.assertTrue(apply_patch(self.path, VERSION, restore=True)['changed'])
        self.assertFalse(apply_patch(self.path, VERSION, restore=True)['changed'])
        self.assertEqual(patched_source(self.path.read_bytes()), modified)


if __name__ == '__main__':
    unittest.main()
