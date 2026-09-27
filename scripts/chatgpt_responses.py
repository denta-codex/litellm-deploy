"""Narrow request customization for the pinned subscription adapter."""
from importlib.metadata import version

import litellm
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig


class SubscriptionResponsesConfig(ChatGPTResponsesAPIConfig):
    def transform_responses_api_request(
        self, model, input, response_api_optional_request_params, litellm_params, headers,
    ):
        params = response_api_optional_request_params
        request = super().transform_responses_api_request(
            model, input, params, litellm_params, headers,
        )
        # Upstream owns all normalization and transport constraints. Undo only
        # its instruction prepend and these entries missing from its allowlist.
        for name in ('instructions', 'text', 'parallel_tool_calls', 'prompt_cache_key', 'service_tier'):
            if name in params:
                request[name] = params[name]
        return request


def install():
    """ProviderConfigManager resolves this export when selecting ChatGPT."""
    if version('litellm') != '1.102.1':
        raise RuntimeError('ChatGPT customization requires LiteLLM 1.102.1; revalidate before upgrading')
    litellm.ChatGPTResponsesAPIConfig = SubscriptionResponsesConfig
