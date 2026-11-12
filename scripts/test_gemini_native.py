"""Offline fidelity regressions through the actual pinned Router/Vertex bridge."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import shutil
import multiprocessing
from unittest.mock import AsyncMock, patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
from litellm.responses.litellm_completion_transformation.transformation import TOOL_CALLS_CACHE
from claude_context import REQUEST
from gemini_native import PREFIX, StateCodec, install, provision_key, read_key


MODEL = 'vertex/gemini-4-carbon'
SIGNATURE = 'synthetic-final-signature'
TOOL_SIGNATURE = 'synthetic-tool-signature'


def open_in_new_process(key, token, connection):
    """Spawn, not fork: the process has no adapter cache or inherited codec."""
    try:
        connection.send(StateCodec(key).open(token, 'fixture-owner'))
    finally:
        connection.close()


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, parts, owner):
        self.parts, self.owner = parts, owner

    async def __aiter__(self):
        try:
            for index, part in enumerate(self.parts):
                value = {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': [part]}}]}
                yield ('data: ' + json.dumps(value) + '\n\n').encode()
                if index == 0 and self.owner.gate is not None:
                    await self.owner.gate.wait()
            if self.owner.stream_error:
                yield ('data: ' + json.dumps({'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED',
                                                        'message': 'Synthetic quota failure'}}) + '\n\n').encode()
                return
            value = {'candidates': [{'index': 0, 'finishReason': 'STOP'}],
                     'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 3, 'totalTokenCount': 18},
                     'modelVersion': 'gemini-4-carbon'}
            self.owner.provider_completed = True
            yield ('data: ' + json.dumps(value) + '\n\n').encode()
        finally:
            self.owner.provider_closed = True

    async def aclose(self):
        self.owner.provider_closed = True


class KeyTests(unittest.TestCase):
    def test_sealed_state_survives_a_fresh_process(self):
        with tempfile.TemporaryDirectory() as root:
            key = Path(root) / 'key'; provision_key(key)
            value = {'version': 1, 'parts': [{'text': 'FIXTURE', 'thoughtSignature': SIGNATURE}]}
            token = StateCodec(key).seal(value, 'fixture-owner')
            ctx = multiprocessing.get_context('spawn')
            receive, send = ctx.Pipe(duplex=False)
            process = ctx.Process(target=open_in_new_process, args=(key, token, send))
            process.start(); send.close()
            try:
                self.assertTrue(receive.poll(20), 'Fresh process did not restore sealed state')
                self.assertEqual(receive.recv(), value)
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive(): process.terminate(); process.join()
                receive.close(); process.close()

    def test_private_atomic_idempotent_provisioning_and_missing_key(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'gemini/state.key'
            self.assertTrue(provision_key(path))
            original = read_key(path)
            self.assertFalse(provision_key(path))
            self.assertEqual(read_key(path), original)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                read_key(path)
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'restore it'):
                provision_key(path)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                StateCodec(path).seal({'version': 1}, 'caller')
            self.assertFalse(path.exists())

    def test_ciphertext_is_caller_bound_and_authenticated(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'key'; provision_key(path)
            codec = StateCodec(path)
            token = codec.seal({'version': 1, 'secret': SIGNATURE}, 'owner')
            self.assertNotIn(SIGNATURE, token)
            self.assertEqual(codec.open(token, 'owner')['secret'], SIGNATURE)
            for corrupted, owner in ((token, 'someone-else'), (token[:-8] + 'abcdefgh', 'owner')):
                with self.assertRaisesRegex(ValueError, 'invalid or belongs'):
                    codec.open(corrupted, owner)
            path.write_bytes(os.urandom(32))
            with self.assertRaises(ValueError):
                codec.open(token, 'owner')


class NativeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick(): loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / 'state.key'; provision_key(self.key)
        self.codec = StateCodec(self.key)
        self.requests, self.responses, self.parts = [], [], []
        self.live_stream = False
        self.fixture_factory = None
        self.gate = None
        self.provider_completed = self.provider_closed = self.stream_error = False
        self.token = REQUEST.set({'caller': 'fixture-owner', 'headers': {'thread-id': 'fixture'}, 'request': {}})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        with patch.object(AsyncHTTPHandler, 'create_client', return_value=self.client):
            handler = AsyncHTTPHandler()
        handler.client = self.client
        self.patches = [
            patch.object(VertexBase, '_ensure_access_token_async', new=AsyncMock(return_value=('fixture-token', 'fixture-project'))),
            patch('litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini.get_async_httpx_client', return_value=handler),
            patch.object(litellm, 'module_level_aclient', handler)]
        for p in self.patches: p.start()
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.router = litellm.Router(model_list=[{'model_name': MODEL, 'litellm_params': {
            'model': 'vertex_ai/gemini-4-carbon', 'vertex_project': 'fixture-project',
            'vertex_location': 'global', 'vertex_credentials': 'fixture-only',
            'reasoning_effort': 'medium', 'additional_drop_params': ['prompt_cache_key'],
            'use_chat_completions_api': True}}], num_retries=0)
        self.integration = install(self.router, self.codec)
        async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
        self.integration.dispatch = dispatch
        self.original_callbacks = litellm.callbacks

    async def asyncTearDown(self):
        self.integration.uninstall()
        litellm.callbacks = self.original_callbacks
        for p in reversed(self.patches): p.stop()
        await self.client.aclose()
        REQUEST.reset(self.token)
        litellm.in_memory_llm_clients_cache.flush_cache()
        TOOL_CALLS_CACHE.flush_cache()

    def upstream(self, request):
        self.requests.append(json.loads(request.content))
        parts = copy.deepcopy(self.fixture_factory() if self.fixture_factory else self.parts[len(self.requests)-1] if self.parts else
                              [{'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}])
        self.responses.append(parts)
        value = {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP', 'index': 0}],
                 'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 3, 'totalTokenCount': 18},
                 'modelVersion': 'gemini-4-carbon'}
        if 'streamGenerateContent' in str(request.url):
            if self.live_stream:
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=FixtureStream(parts, self))
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content='data: ' + json.dumps(value) + '\n\n')
        return httpx.Response(200, json=value)

    async def response(self, history, streaming=False, tools=None, summary='auto', bridge=None):
        data = dict(model=MODEL, input=history, tools=tools or [], stream=streaming,
                    reasoning={'effort': 'high', 'summary': summary}, store=False)
        REQUEST.get()['request'] = data
        call = await (bridge or self.integration).route(data, 'aresponses')
        result = await call
        if not streaming: return result.model_dump(exclude_none=True), []
        events = [e.model_dump(exclude_none=True) async for e in result]
        final = next(e['response'] for e in events if e['type'] == 'response.completed')
        self.assertEqual(final['output'], [e['item'] for e in events if e['type'] == 'response.output_item.done'])
        self.assertFalse(any(e['type'] == 'response.output_text.delta' and e['item_id'].startswith('rs_') for e in events))
        return final, events

    def shared_bridge(self):
        from shared_search import SearchResponses, SearchJournal, SharedSearchInterceptor, SearchResult
        class Backend:
            def __init__(self): self.queries = []
            async def search(self, query, settings):
                self.queries.append(query)
                return SearchResult('BLUE', [{'title': 'Fixture', 'url': 'https://example.com/fact'}])
        interceptor = SharedSearchInterceptor()
        litellm.callbacks = [interceptor]
        backend = Backend()
        return SearchResponses(self.integration.route, interceptor, backend,
                               SearchJournal(Path(self.directory.name) / 'search'), self.router), backend

    async def test_delayed_summaries_and_signature_only_chunks_direct_and_hosted_unused(self):
        for hosted, summary_first in ((False, True), (True, True), (False, False), (True, False)):
            self.requests.clear(); self.provider_completed = self.provider_closed = False
            self.live_stream = True; self.gate = asyncio.Event()
            signed = [{'thought': True, 'text': 'Provider summary.'}, {'text': 'FIX'},
                      {'text': 'TURE_OK'}, {'thoughtSignature': SIGNATURE}]
            if not summary_first: signed = [signed[1], signed[2], signed[0], signed[3]]
            self.parts = [signed, [{'text': 'NEXT'}]]
            bridge, backend = self.shared_bridge() if hosted else (self.integration, None)
            tools = [{'type': 'web_search'}] if hosted else []
            data = dict(model=MODEL, input=f'Hello; summary_first={summary_first}', tools=tools, stream=True,
                        reasoning={'effort': 'medium', 'summary': 'auto'}, store=False)
            REQUEST.get()['request'] = data
            stream = await (await bridge.route(data, 'aresponses'))
            events = []
            while True:
                event = (await asyncio.wait_for(anext(stream), 5)).model_dump(exclude_none=True)
                events.append(event)
                expected = 'response.reasoning_summary_text.delta' if summary_first else 'response.output_text.delta'
                if event['type'] == expected: break
            self.assertFalse(self.provider_completed)
            self.assertEqual(events[-1]['delta'], 'Provider summary.' if summary_first else 'FIX')
            self.gate.set()
            events.extend([e.model_dump(exclude_none=True) async for e in stream])
            final = next(e['response'] for e in events if e['type'] == 'response.completed')
            self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
            self.assertEqual(final['output'], [e['item'] for e in events if e['type'] == 'response.output_item.done'])
            carrier = next(i for i in final['output'] if str(i.get('encrypted_content', '')).startswith(PREFIX))
            self.assertEqual(self.codec.open(carrier['encrypted_content'], 'fixture-owner')['parts'], signed)
            await self.response([*final['output'], {'role': 'user', 'content': 'Continue'}], True, tools, bridge=bridge)
            self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][0]['parts'], signed)
            self.assertTrue(self.provider_closed)
            if backend is not None: self.assertEqual(backend.queries, [])

    async def test_stream_cancellation_closes_provider_and_quota_is_terminal(self):
        self.live_stream = True; self.gate = asyncio.Event()
        self.parts = [[{'text': 'First'}, {'text': 'Last'}]]
        data = dict(model=MODEL, input='Hello', stream=True, tools=[], store=False)
        REQUEST.get()['request'] = data
        stream = await (await self.integration.route(data, 'aresponses'))
        while (await anext(stream)).type != 'response.output_text.delta': pass
        with self.assertLogs('gemini_native', level='INFO') as logs:
            await stream.aclose()
        timing = json.loads(logs.output[-1].partition('gemini_timing ')[2])
        self.assertEqual(timing['outcome'], 'cancelled')
        self.assertIsNotNone(timing['first_public_ms'])
        self.assertNotIn('First', str(logs.output))
        self.assertTrue(self.provider_closed)
        self.assertFalse(self.provider_completed)
        self.requests.clear(); self.parts = []; self.gate = None; self.stream_error = True
        stream = await (await self.integration.route(data, 'aresponses'))
        events = [e.model_dump(exclude_none=True) async for e in stream]
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertEqual(events[-1]['response']['error']['code'], 'rate_limit_exceeded')
        self.assertFalse(any(e['type'] == 'response.completed' for e in events))

    async def test_actual_search_mixed_client_tools_and_native_state_replay(self):
        from shared_search import SEARCH_NAME
        for mixed in (False, True):
            self.requests.clear(); self.live_stream = True
            bridge, backend = self.shared_bridge()
            first = [{'text': 'Checking.'}, {'functionCall': {'name': SEARCH_NAME, 'args': {'query': 'fixture fact'}},
                                           'thoughtSignature': TOOL_SIGNATURE}]
            if mixed: first.append({'functionCall': {'name': 'probe', 'args': {}}})
            self.parts = [first, [{'text': 'BLUE: https://example.com/fact', 'thoughtSignature': SIGNATURE}], [{'text': 'NEXT'}]]
            tools = [{'type': 'web_search'}, {'type': 'function', 'name': 'probe', 'description': 'Fixture',
                                             'parameters': {'type': 'object', 'properties': {}}}]
            prompt = f'Research fixture fact; mixed={mixed}'
            final, events = await self.response(prompt, True, tools, bridge=bridge)
            self.assertEqual(backend.queries, ['fixture fact'])
            self.assertFalse(any(i.get('name') == SEARCH_NAME for i in final['output']))
            self.assertTrue(any(i['type'] == 'web_search_call' for i in final['output']))
            history = [{'role': 'user', 'content': prompt}, *final['output']]
            if mixed:
                client = next(i for i in final['output'] if i['type'] == 'function_call')
                history.append({'type': 'function_call_output', 'call_id': client['call_id'], 'output': 'DONE'})
            else:
                self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][0]['parts'], first)
                history.append({'role': 'user', 'content': 'Continue'})
                annotations = next(i for i in final['output'] if i['type'] == 'message' and 'BLUE' in json.dumps(i))['content'][0]['annotations']
                self.assertTrue(annotations)
            TOOL_CALLS_CACHE.flush_cache()
            await self.response(history, True, tools, bridge=bridge)
            self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][0]['parts'], first)

    async def test_signed_final_text_json_stream_restart_and_cache_flush(self):
        for streaming in (False, True):
            self.requests.clear()
            first, _ = await self.response('Hello', streaming)
            carrier = next(i for i in first['output'] if str(i.get('encrypted_content', '')).startswith(PREFIX))
            self.assertNotIn(SIGNATURE, json.dumps(first))
            self.assertEqual(self.codec.open(carrier['encrypted_content'], 'fixture-owner')['parts'], self.responses[-1])
            # New hook instance and codec, same durable key, no live tool cache.
            self.integration.uninstall()
            dispatch = self.integration.dispatch
            self.integration = install(self.router, StateCodec(self.key)); self.integration.dispatch = dispatch
            TOOL_CALLS_CACHE.flush_cache()
            await self.response([{'role': 'user', 'content': 'Hello'}, *first['output'], {'role': 'user', 'content': 'Continue'}], streaming)
            native = [c for c in self.requests[-1]['contents'] if c['role'] == 'model']
            self.assertEqual(native[0]['parts'], self.responses[-2])

    async def test_parallel_tools_custom_tools_and_sequential_calls(self):
        for custom, streaming in ((False, False), (True, True)):
            self.requests.clear()
            tool = ({'type': 'custom', 'name': 'probe', 'description': 'Fixture', 'format': {'type': 'text'}} if custom else
                    {'type': 'function', 'name': 'probe', 'description': 'Fixture', 'parameters': {'type': 'object', 'properties': {}}})
            # Literal provider function name is also its declared name for these tools.
            args = {'content': 'fixture code'} if custom else {}
            first_parts = [{'text': 'Provider summary.', 'thought': True},
                           {'functionCall': {'name': 'probe', 'args': args}, 'thoughtSignature': TOOL_SIGNATURE}]
            if not custom: first_parts.append({'functionCall': {'name': 'probe', 'args': {'second': True}}})
            self.parts = [first_parts, [{'functionCall': {'name': 'probe', 'args': args}, 'thoughtSignature': 'sequential-signature'}],
                          [{'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}], [{'text': 'NEXT'}]]
            history = [{'role': 'user', 'content': 'Use fixture tools'}]
            for step in range(3):
                result, events = await self.response(history, streaming, [tool])
                self.assertNotIn(TOOL_SIGNATURE, json.dumps(result))
                self.assertNotIn('__thought__', json.dumps(result))
                if step:
                    native = [c for c in self.requests[-1]['contents'] if c['role'] == 'model']
                    self.assertEqual(native[-1]['parts'], self.parts[step-1])
                history.extend(result['output'])
                for call in result['output']:
                    if call['type'] in ('function_call', 'custom_tool_call'):
                        history.append({'type': 'custom_tool_call_output' if custom else 'function_call_output',
                                        'call_id': call['call_id'], 'output': 'DONE'})
                TOOL_CALLS_CACHE.flush_cache()
            history.append({'role': 'user', 'content': 'Continue'})
            await self.response(history, streaming, [tool])
            self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][-1]['parts'], self.parts[2])

    async def test_late_repeated_and_orphan_tool_results_reach_the_provider(self):
        """Codex can append a background command's result after a newer call."""
        tools = [{'type': 'custom', 'name': 'exec', 'description': 'Fixture', 'format': {'type': 'text'}},
                 {'type': 'function', 'name': 'wait', 'description': 'Fixture',
                  'parameters': {'type': 'object', 'properties': {}}}]
        call = {'type': 'custom_tool_call', 'call_id': 'call_exec', 'name': 'exec', 'input': 'sleep 30'}
        first = {'type': 'custom_tool_call_output', 'call_id': 'call_exec', 'output': 'running'}
        late = {'type': 'custom_tool_call_output', 'call_id': 'call_exec', 'output': 'LATE'}
        between = {'role': 'user', 'content': 'Also wait'}
        wait = [{'type': 'function_call', 'call_id': 'call_wait', 'name': 'wait', 'arguments': '{}'},
                {'type': 'function_call_output', 'call_id': 'call_wait', 'output': 'waited'}]
        user = {'role': 'user', 'content': 'Run it'}
        orphan = {'type': 'function_call_output', 'call_id': 'call_missing', 'output': 'ORPHAN'}
        def responses(contents):
            return [p['function_response']['name'] for c in contents for p in c['parts'] if 'function_response' in p]
        for streaming in (False, True):
            self.requests.clear()
            await self.response(copy.deepcopy([user, call, first, *wait]), streaming, tools)
            control = self.requests[-1]['contents']
            self.assertEqual(responses(control), ['exec', 'wait'])
            for history in ([user, call, first, *wait, late], [user, call, first, *wait, orphan],
                            [user, call, between, *wait, late]):
                self.requests.clear()
                with self.assertLogs('gemini_native', level='INFO') as logs:
                    final, _ = await self.response(copy.deepcopy(history), streaming, tools)
                self.assertEqual(final['status'], 'completed')
                self.assertEqual(len(self.requests), 1)
                contents = self.requests[-1]['contents']
                repairs = [line for line in logs.output if 'gemini_history_repair' in line]
                self.assertEqual(len(repairs), 1, logs.output)
                self.assertNotIn('LATE', repairs[0])
                self.assertNotIn('ORPHAN', json.dumps(contents))
                if history[2] is between:
                    # The first result for exec moves directly after its call.
                    self.assertEqual(responses(contents), ['exec', 'wait'])
                    self.assertIn('LATE', json.dumps(contents[2]))
                else:
                    self.assertEqual(contents, control)

    async def test_changed_batch_tampering_and_caller_isolation(self):
        first, _ = await self.response('Hello')
        for mode in ('text', 'id', 'cipher', 'caller', 'missing'):
            history = copy.deepcopy(first['output'])
            if mode == 'text': next(i for i in history if i['type'] == 'message')['content'][0]['text'] = 'CHANGED'
            if mode == 'id': next(i for i in history if i['type'] == 'reasoning')['id'] = 'rs_changed'
            if mode == 'cipher': history[-1]['encrypted_content'] = history[-1]['encrypted_content'][:-8] + 'abcdefgh'
            if mode == 'missing': history = history[-1:]
            owner = REQUEST.get()['caller']; REQUEST.get()['caller'] = 'other' if mode == 'caller' else owner
            with self.assertRaises(Exception): await self.response([*history, {'role': 'user', 'content': 'Continue'}])
            REQUEST.get()['caller'] = owner
        self.assertEqual(len(self.requests), 1)
        corrupted = copy.deepcopy(first['output'])
        corrupted[-1]['encrypted_content'] = corrupted[-1]['encrypted_content'][:-8] + 'abcdefgh'
        data = dict(model=MODEL, input=corrupted, stream=True, store=False, tools=[])
        REQUEST.get()['request'] = data
        stream = await (await self.integration.route(data, 'aresponses'))
        events = [e.model_dump(exclude_none=True) async for e in stream]
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertEqual(events[-1]['response']['error']['code'], 'invalid_request_error')
        self.assertIn('Gemini native state', events[-1]['response']['error']['message'])
        self.assertEqual(len(self.requests), 1)

    async def test_late_summary_and_suppressed_summary_keep_output_consistent(self):
        self.live_stream = True
        self.parts = [[{'text': 'FIXTURE_OK'}, {'thought': True, 'text': 'Late summary.'},
                       {'thoughtSignature': SIGNATURE}]]
        final, events = await self.response('Hello', True)
        self.assertEqual(final['output'][0]['type'], 'message')
        self.requests.clear()
        final, events = await self.response('Hello', True, summary='none')
        self.assertFalse(any('reasoning_summary' in e['type'] for e in events))
        carrier = next(i for i in final['output'] if i['type'] == 'reasoning')
        self.assertEqual(carrier['summary'], [])
        self.assertEqual(self.codec.open(carrier['encrypted_content'], 'fixture-owner')['parts'], self.parts[0])

    async def test_model_switch_keeps_readable_history_without_opaque_state(self):
        self.parts = [[{'functionCall': {'name': 'probe', 'args': {}, 'id': 'native-call-id'},
                        'thoughtSignature': TOOL_SIGNATURE}],
                      [{'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}], [{'text': 'OTHER'}]]
        tool = {'type': 'function', 'name': 'probe', 'parameters': {'type': 'object', 'properties': {}}}
        call, _ = await self.response('Use probe', tools=[tool])
        public_call = next(i for i in call['output'] if i['type'] == 'function_call')
        self.assertNotIn(TOOL_SIGNATURE, json.dumps(call))
        self.assertNotIn('__thought__', public_call['call_id'])
        history = call['output'] + [{'type': 'function_call_output', 'call_id': public_call['call_id'], 'output': 'DONE'}]
        first, _ = await self.response(history, tools=[tool])
        history += first['output']
        from gemini_native import GeminiResponses
        calls = []
        async def foreign_dispatch(**kwargs):
            calls.append(kwargs['data'])
            async def execute(): return 'FOREIGN'
            return execute()
        foreign = GeminiResponses(foreign_dispatch, self.router)
        data = {'model': 'other/native', 'input': history}
        self.assertEqual(await (await foreign.route(data, 'aresponses')), 'FOREIGN')
        self.assertNotIn(PREFIX, json.dumps(calls[-1]))
        self.assertIn('FIXTURE_OK', json.dumps(calls[-1]))
        self.assertIn(PREFIX, json.dumps(data))  # Source chat is not mutated.
        # Switching to another Gemini deployment also strips incompatible native
        # state before dispatch, while preserving the readable assistant turn.
        second = copy.deepcopy(self.router.model_list[0])
        second['model_name'] = 'vertex/gemini-4-other'
        second['litellm_params']['model'] = 'vertex_ai/gemini-4-other'
        router = litellm.Router(model_list=[second], num_retries=0)
        switched = await router.aresponses(model=second['model_name'], input=history +
            [{'role': 'user', 'content': 'Continue'}], tools=[tool], store=False)
        self.assertNotIn(SIGNATURE, json.dumps(self.requests[-1]))
        self.assertNotIn(TOOL_SIGNATURE, json.dumps(self.requests[-1]))
        self.assertIn('FIXTURE_OK', json.dumps(self.requests[-1]))


    async def test_stock_codex_exact_resume_and_forced_compaction(self):
        """No real auth/inference: stock CLI -> loopback -> pinned Vertex fixture."""
        binary = os.environ.get('GEMINI_TEST_CODEX') or shutil.which('codex')
        if not binary:
            self.skipTest('Set GEMINI_TEST_CODEX to a stock Codex binary for end-to-end acceptance')
        from compaction_fallback import CompactionResponses, is_compaction
        from refresh_models import configured_model
        import tomlkit
        # Stock Codex refuses CODEX_HOME beneath /tmp. Nothing in the real home
        # is read, mutated, copied, or authenticated by this fixture.
        with tempfile.TemporaryDirectory(prefix='.gemini-codex-', dir=Path.cwd()) as directory:
            home = Path(directory)
            catalog = home / 'models.json'
            _, row, _ = configured_model({'name': MODEL, 'litellm': {'model': 'vertex_ai/gemini-4-carbon'},
                'codex': {'display_name': 'Fixture Gemini', 'context_window': 1048576,
                          'default_reasoning_level': 'medium', 'supported_reasoning_levels': ['low', 'medium', 'high']}})
            catalog.write_text(json.dumps({'models': [row]}))
            bridge, _ = self.shared_bridge()
            compactor = CompactionResponses(bridge.route, catalog)
            client_requests, errors = [], []
            from responses_events import ResponsesEvents
            failure_renderer = ResponsesEvents.failed
            def failure(renderer, exc):
                errors.append(str(exc))
                return failure_renderer(renderer, exc)
            self.enterContext(patch.object(ResponsesEvents, 'failed', failure))
            compacted = []
            def fixture():
                if is_compaction(REQUEST.get()['request']):
                    compacted.append(len(self.requests))
                    return [{'text': 'CHECKPOINT: Remember FIXTURE_OK. No pending tools.', 'thoughtSignature': 'checkpoint-signature'}]
                if len(self.requests) == 1:
                    declarations = [d for tool in self.requests[-1]['tools'] for d in tool.get('function_declarations', [])]
                    name = next(d['name'] for d in declarations if d['name'].endswith('exec'))
                    return [{'thought': True, 'text': 'Visible provider summary.'},
                            {'functionCall': {'name': name, 'args': {'content': 'text("FIXTURE_TOOL_OK")'}},
                             'thoughtSignature': TOOL_SIGNATURE}]
                return [{'thought': True, 'text': 'Visible provider summary.'},
                        {'text': 'FIXTURE_OK', 'thoughtSignature': SIGNATURE}]
            self.fixture_factory = fixture; self.live_stream = True
            async def handle(reader, writer):
                token = None
                try:
                    header = (await reader.readuntil(b'\r\n\r\n')).decode()
                    headers = {k.lower(): v.strip() for k, v in (l.split(':', 1) for l in header.split('\r\n')[1:] if ':' in l)}
                    self.assertEqual(headers.get('authorization'), 'Bearer fake-offline-key')
                    data = json.loads(await reader.readexactly(int(headers['content-length'])))
                    client_requests.append(copy.deepcopy(data))
                    token = REQUEST.set({'caller': 'fixture-owner', 'headers': headers, 'request': data})
                    stream = await (await compactor.route(data, 'aresponses', llm_router=self.router))
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
                    async for event in stream:
                        if not isinstance(event, bytes) and event.type == 'response.failed':
                            errors.append(event.model_dump(exclude_none=True))
                        writer.write(event if isinstance(event, bytes) else ('data: ' + event.model_dump_json(exclude_none=True) + '\n\n').encode())
                        await writer.drain()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if token is not None: REQUEST.reset(token)
                    writer.close(); await writer.wait_closed()
            server = await asyncio.start_server(handle, '127.0.0.1', 0)
            self.addAsyncCleanup(server.wait_closed)
            self.addCleanup(server.close)
            port = server.sockets[0].getsockname()[1]
            (home / 'config.toml').write_text(tomlkit.dumps({
                'model': MODEL, 'model_provider': 'fixture', 'model_catalog_json': str(catalog),
                'model_reasoning_effort': 'medium', 'approval_policy': 'never', 'sandbox_mode': 'read-only',
                'web_search': 'live', 'check_for_update_on_startup': False,
                'features': {'shell_snapshot': False},
                'model_providers': {'fixture': {'name': 'Offline Gemini', 'base_url': f'http://127.0.0.1:{port}/v1',
                    'wire_api': 'responses', 'supports_websockets': False, 'requires_openai_auth': False,
                    'env_key': 'OFFLINE_FIXTURE_KEY', 'request_max_retries': 0}}}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_', 'AWS_', 'GOOGLE_'))}
            env.update(CODEX_HOME=str(home), HOME=str(home), OFFLINE_FIXTURE_KEY='fake-offline-key')
            async def run(args):
                process = await asyncio.create_subprocess_exec(binary, 'exec', *args, cwd=home, env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
                finally:
                    if process.returncode is None: process.kill(); await process.wait()
                self.assertFalse(errors, str(errors))
                self.assertEqual(process.returncode, 0, (stderr.decode()[-2500:], stdout.decode()[-2500:]))
                self.assertNotIn('OutputTextDelta without active item', stderr.decode())
                events = [json.loads(l) for l in stdout.splitlines()]
                self.assertTrue(any(e['type'] == 'turn.completed' for e in events), events)
                answers = [e['item']['text'] for e in events if e['type'] == 'item.completed' and e['item']['type'] == 'agent_message']
                self.assertEqual(answers, ['FIXTURE_OK'])
                return events
            first = await run(['--skip-git-repo-check', '--json', 'Remember FIXTURE_OK.'])
            session = next(e['thread_id'] for e in first if e['type'] == 'thread.started')
            self.assertTrue(any(i.get('type') == 'custom_tool_call_output' and 'FIXTURE_TOOL_OK' in json.dumps(i)
                                for r in client_requests for i in r['input']), 'Code Mode tool result missing')
            # Restart hooks/codec and flush live caches before the exact resume.
            self.integration.uninstall(); dispatch = self.integration.dispatch
            self.integration = install(self.router, StateCodec(self.key)); self.integration.dispatch = dispatch
            bridge.dispatch = self.integration.route
            TOOL_CALLS_CACHE.flush_cache()
            await run(['resume', session, '--skip-git-repo-check', '--json', 'Continue.'])
            self.assertTrue(any(str(i.get('encrypted_content', '')).startswith(PREFIX) for i in client_requests[-1]['input']))
            self.assertEqual([c for c in self.requests[-1]['contents'] if c['role'] == 'model'][0]['parts'], self.responses[0])
            await run(['-c', 'model_auto_compact_token_limit=1', 'resume', session, '--skip-git-repo-check', '--json', 'Continue after compaction.'])
            self.assertTrue(compacted, 'Stock Codex did not request forced compaction')
            continued = self.requests[-1]
            self.assertIn('CHECKPOINT', json.dumps(continued))
            self.assertNotIn(SIGNATURE, json.dumps(continued))
            self.assertNotIn(TOOL_SIGNATURE, json.dumps(continued))
            self.assertNotIn('functionResponse', json.dumps(continued))
            self.assertNotIn('checkpoint-signature', json.dumps(continued))
            self.assertFalse(any(str(i.get('encrypted_content', '')).startswith(PREFIX) for i in client_requests[-1]['input']))


if __name__ == '__main__': unittest.main()
