"""Offline checks against the actual pinned provider selection and transformer."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

import litellm
from litellm.llms.chatgpt.common_utils import get_chatgpt_default_instructions
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.types.router import GenericLiteLLMParams
from litellm.utils import ProviderConfigManager

from chatgpt_responses import SubscriptionResponsesConfig, install


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


if __name__ == '__main__':
    unittest.main()
