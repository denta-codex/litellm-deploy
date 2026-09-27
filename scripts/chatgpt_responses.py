"""Narrow request customization for the pinned subscription adapter."""
from importlib.metadata import version
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

import litellm
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig


OBSERVATION = Path('/home/agent/.local/state/litellm/fast-observation.jsonl')


def observe_tier(request):
    """Opt-in, bounded, metadata-only desktop acceptance observation.

    Never create the file; deleting it immediately disables observation.
    Refuse symlinks, nonregular files, and permissions other than owner-only.
    Observation failures must never interrupt inference.
    """
    try:
        fd = os.open(OBSERVATION, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600 or info.st_uid != os.getuid() or info.st_size > 262144:
            return
        tier = request.get('service_tier')
        # A strict allowlist prevents arbitrary request contents entering logs.
        tier = tier if tier in (None, 'priority', 'default', 'auto') else 'other'
        # Correlate desktop turns without recording prompts or raw task IDs.
        try:
            session = str(uuid.UUID(request.get('prompt_cache_key', '')))
            session = hashlib.sha256(session.encode()).hexdigest()[:16]
        except (ValueError, TypeError, AttributeError):
            session = None
        os.write(fd, (json.dumps({'time': time.time(), 'requested_tier': tier,
                                 'session': session}) + '\n').encode())
    except OSError:
        pass
    finally:
        os.close(fd)


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
        observe_tier(request)
        return request


def install():
    """ProviderConfigManager resolves this export when selecting ChatGPT."""
    if version('litellm') != '1.102.1':
        raise RuntimeError('ChatGPT customization requires LiteLLM 1.102.1; revalidate before upgrading')
    litellm.ChatGPTResponsesAPIConfig = SubscriptionResponsesConfig
