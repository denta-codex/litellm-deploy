"""Offline diagnostic for Gemini signature round-trips through the pinned LiteLLM bridge.

Uses fake credentials and an in-memory HTTP transport; no inference is performed.
Prints observations rather than asserting the current lossy behavior is correct.
"""
import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'
import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
from litellm.responses.litellm_completion_transformation.transformation import TOOL_CALLS_CACHE

litellm.telemetry = False
litellm.turn_off_message_logging = True
SIG_TOOL = 'ZmFrZS10b29sLXNpZ25hdHVyZQ=='
SIG_TEXT = 'ZmFrZS10ZXh0LXNpZ25hdHVyZQ=='

async def run(streaming, custom=False):
    requests = []
    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            name = body['tools'][0]['function_declarations'][0]['name']
            parts = [{'text': 'Checking the fixture.', 'thought': True},
                     {'functionCall': {'name': name, 'args': {'content': 'fixture code'} if custom else {}},
                      'thoughtSignature': SIG_TOOL}]
        else:
            parts = [{'text': 'FIXTURE_OK', 'thoughtSignature': SIG_TEXT}]
        value = {'candidates': [{'content': {'role': 'model', 'parts': parts},
                                'finishReason': 'STOP', 'index': 0}],
                 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5,
                                   'thoughtsTokenCount': 3, 'totalTokenCount': 18},
                 'modelVersion': 'gemini-4-carbon'}
        if 'streamGenerateContent' in str(request.url):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  content='data: ' + json.dumps(value) + '\n\n')
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with patch.object(AsyncHTTPHandler, 'create_client', return_value=client):
            handler = AsyncHTTPHandler()
        handler.client = client
        router = litellm.Router(model_list=[{'model_name': 'vertex/gemini-4-carbon', 'litellm_params': {
            'model': 'vertex_ai/gemini-4-carbon', 'vertex_project': 'fixture-project',
            'vertex_location': 'global', 'vertex_credentials': 'fixture-only',
            'reasoning_effort': 'medium', 'use_chat_completions_api': True}}], num_retries=0)
        tool = ({'type': 'custom', 'name': 'exec', 'description': 'Run fixture JavaScript', 'format': {'type': 'text'}}
                if custom else {'type': 'function', 'name': 'probe', 'description': 'Probe fixture',
                                'parameters': {'type': 'object', 'properties': {}}})
        async def response(history):
            result = await router.aresponses(model='vertex/gemini-4-carbon', input=history,
                tools=[tool], stream=streaming, reasoning={'effort': 'high'}, store=False)
            if not streaming:
                return result.model_dump(), []
            events = [event.model_dump() async for event in result]
            final = next(e['response'] for e in events if e['type'] == 'response.completed')
            items = [e['item'] for e in events if e['type'] == 'response.output_item.done']
            return dict(final, output=items), events

        with patch.object(VertexBase, '_ensure_access_token_async', new=AsyncMock(return_value=('fixture-token', 'fixture-project'))), \
             patch('litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini.get_async_httpx_client', return_value=handler), \
             patch.object(litellm, 'module_level_aclient', handler):
            history = [{'role': 'user', 'content': 'Run the fixture tool, then report the result.'}]
            first, events = await response(history)
            call = next(i for i in first['output'] if i['type'] in ('function_call', 'custom_tool_call'))
            history += first['output'] + [{'type': 'custom_tool_call_output' if custom else 'function_call_output',
                                          'call_id': call['call_id'], 'output': 'FIXTURE_OK'}]
            TOOL_CALLS_CACHE.flush_cache()
            second, _ = await response(history)
            native_calls = [p for c in requests[-1]['contents'] for p in c['parts'] if 'function_call' in p or 'functionCall' in p]
            tool_signature_preserved = any(p.get('thoughtSignature') == SIG_TOOL for p in native_calls)
            history += second['output'] + [{'role': 'user', 'content': 'Continue from your last answer.'}]
            TOOL_CALLS_CACHE.flush_cache()
            await response(history)
            native_texts = [p for c in requests[-1]['contents'] for p in c['parts'] if p.get('text') == 'FIXTURE_OK']
            print(json.dumps({'stream': streaming, 'custom': custom,
                'thinking_config': requests[0].get('generationConfig', requests[0].get('generation_config', {})).get('thinkingConfig'),
                'output_types': [i['type'] for i in first['output']],
                'reasoning_summary_events': sum('reasoning_summary' in e['type'] for e in events),
                'tool_signature_preserved_without_cache': tool_signature_preserved,
                'final_text_signature_in_response': SIG_TEXT in json.dumps(second),
                'final_text_signature_preserved_in_next_request': any(p.get('thoughtSignature') == SIG_TEXT for p in native_texts)}))

async def main():
    for streaming in (False, True):
        for custom in (False, True):
            litellm.in_memory_llm_clients_cache.flush_cache()
            await run(streaming, custom)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
def tick():
    loop.call_later(0.01, tick)
tick()
try:
    loop.run_until_complete(asyncio.wait_for(main(), timeout=60))
finally:
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
