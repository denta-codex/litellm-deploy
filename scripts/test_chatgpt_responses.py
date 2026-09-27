"""Offline checks against the actual pinned provider selection and transformer."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

import litellm
from litellm.llms.chatgpt.common_utils import get_chatgpt_default_instructions
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.types.router import GenericLiteLLMParams
from litellm.utils import ProviderConfigManager

from chatgpt_responses import SubscriptionResponsesConfig, install, observe_tier


class RequestTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'CHATGPT_TOKEN_DIR': directory}))
        self.enterContext(patch.object(litellm, 'ChatGPTResponsesAPIConfig', ChatGPTResponsesAPIConfig))
        install()
        self.config = ProviderConfigManager.get_provider_responses_api_config('chatgpt', 'gpt-6-astra')

    def transform(self, params, config=None):
        return (config or self.config).transform_responses_api_request(
            'gpt-6-astra', [{'role': 'user', 'content': 'Hello'}], params,
            GenericLiteLLMParams(), {},
        )

    def test_real_provider_selection_and_upstream_methods(self):
        self.assertIs(type(self.config), SubscriptionResponsesConfig)
        self.assertIs(type(ProviderConfigManager.get_provider_responses_api_config('openai')),
                      litellm.OpenAIResponsesAPIConfig)
        for name in ('validate_environment', 'get_complete_url', 'transform_response_api_response',
                     'get_supported_openai_params', 'map_openai_params', 'supports_native_websocket'):
            self.assertIs(getattr(SubscriptionResponsesConfig, name), getattr(ChatGPTResponsesAPIConfig, name))

    def test_instructions_are_preserved_exactly(self):
        for instructions in ('Client only.\n\n Preserve whitespace. ', '', '\u03bb\n\t',
                             get_chatgpt_default_instructions() + '\nClient suffix'):
            with self.subTest(instructions=instructions[:20]):
                self.assertEqual(self.transform({'instructions': instructions})['instructions'], instructions)

    def test_absent_instructions_and_ordinary_requests_match_upstream(self):
        for params in ({}, {'stream': False, 'store': True, 'include': ['reasoning.encrypted_content'],
                           'reasoning': {'effort': 'medium'}, 'temperature': 1}):
            with self.subTest(params=params):
                expected = self.transform(copy.deepcopy(params), ChatGPTResponsesAPIConfig())
                actual = self.transform(copy.deepcopy(params))
                self.assertEqual(actual, expected)
                self.assertEqual(actual['instructions'], get_chatgpt_default_instructions())
                self.assertTrue(actual['stream'])
                self.assertFalse(actual['store'])
                self.assertNotIn('temperature', actual)

    def test_supported_fields_survive_mapping_and_transform(self):
        schema = {'type': 'json_schema', 'name': 'probe', 'strict': True, 'schema': {
            'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
            'required': ['ok'], 'additionalProperties': False,
        }}
        for parallel in (False, True):
            for format in ({'type': 'text'}, schema):
                params = {'text': {'format': format, 'verbosity': 'low'},
                          'parallel_tool_calls': parallel, 'prompt_cache_key': 'isolated-cache-key'}
                original = copy.deepcopy(params)
                mapped = self.config.map_openai_params(params, 'gpt-6-astra', False)
                actual = self.transform(mapped)
                for key, value in original.items():
                    self.assertIn(key, self.config.get_supported_openai_params('gpt-6-astra'))
                    self.assertEqual(actual[key], value)
                self.assertEqual(params, original)

    def test_version_guard_before_override_and_idempotence(self):
        install()
        self.assertIs(litellm.ChatGPTResponsesAPIConfig, SubscriptionResponsesConfig)
        with patch('chatgpt_responses.version', return_value='1.102.2'), \
                patch.object(litellm, 'ChatGPTResponsesAPIConfig', ChatGPTResponsesAPIConfig):
            with self.assertRaisesRegex(RuntimeError, 'revalidate'):
                install()
            self.assertIs(litellm.ChatGPTResponsesAPIConfig, ChatGPTResponsesAPIConfig)

    def test_service_tiers_survive_mapping_without_enabling_priority_by_default(self):
        self.assertNotIn('service_tier', self.transform({}))
        for tier in ('priority', 'default', 'auto'):
            with self.subTest(tier=tier):
                params = {'service_tier': tier}
                mapped = self.config.map_openai_params(params, 'gpt-6-astra', False)
                outgoing = self.transform(mapped)
                self.assertEqual(outgoing['service_tier'], tier)
                self.assertEqual(params, {'service_tier': tier})
                self.assertFalse(outgoing['store'])
                self.assertTrue(outgoing['stream'])
                self.assertIn('reasoning.encrypted_content', outgoing['include'])

    def test_observation_is_opt_in_metadata_only_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'observation'
            with patch('chatgpt_responses.OBSERVATION', target):
                observe_tier({'service_tier': 'priority'})
                self.assertFalse(target.exists())
                target.touch(mode=0o600)
                observe_tier({'service_tier': 'priority', 'input': 'PRIVATE', 'authorization': 'SECRET'})
                observe_tier({'service_tier': 'PRIVATE'})
                entries = [json.loads(line) for line in target.read_text().splitlines()]
                self.assertEqual(set(entries[0]), {'time', 'requested_tier', 'session'})
                self.assertIsNone(entries[0]['session'])
                self.assertEqual(entries[0]['requested_tier'], 'priority')
                self.assertEqual(entries[1]['requested_tier'], 'other')
                self.assertNotIn('PRIVATE', target.read_text())
                observe_tier({'prompt_cache_key': '00000000-0000-0000-0000-000000000001'})
                last = json.loads(target.read_text().splitlines()[-1])
                self.assertEqual(len(last['session']), 16)
                self.assertNotIn('00000000-0000', target.read_text())
                target.unlink()
                other = Path(directory) / 'other'
                other.touch(mode=0o600)
                target.symlink_to(other)
                observe_tier({'service_tier': 'priority'})
                self.assertEqual(other.read_text(), '')


if __name__ == '__main__':
    unittest.main()
