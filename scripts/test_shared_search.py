"""Exercise upstream's actual model loop and Responses bridge with fake I/O."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import httpx
import litellm
from claude_context import REQUEST
from claude_provider import ClaudeProvider
from claude_session import MODEL
from shared_search import SEARCH_NAME, ChatGPTSearch, SearchJournal, SearchResponses, SearchResult, SharedSearchInterceptor, plain


class Backend:
    def __init__(self):
        self.queries = []

    async def search(self, query, settings):
        self.queries.append(query)
        return SearchResult('The fixture fact is BLUE.', [{'title': 'Fixture', 'url': 'https://example.com/fact'}])


class FakeSession:
    instances = []

    def __init__(self, key, config, state):
        self.key, self.config = key, config
        self.closed = False
        self.queue = asyncio.Queue()
        self.idle = asyncio.Event()
        self.history, self.pending, self.delivered, self.response_cache = [], {}, {}, {}
        self.boundary_usage = {'input_tokens': 10, 'output_tokens': 5}
        self.__class__.instances.append(self)

    async def start(self, prompt):
        self.pending['call_sdk'] = {'spec': {'id': 'call_sdk', 'name': SEARCH_NAME,
                                           'arguments': '{"query":"fixture fact"}'}}
        await self.queue.put(('tools', [self.pending['call_sdk']['spec']]))

    def save(self):
        pass

    def deliver(self, call_id, result):
        if call_id not in self.delivered:
            self.delivered[call_id] = result
            self.queue.put_nowait(('text', 'BLUE: https://example.com/fact'))
            self.queue.put_nowait(('done', {'input_tokens': 10, 'output_tokens': 5}))
            self.idle.set()

    async def submit(self, prompt):
        await self.queue.put(('text', 'BLUE: https://example.com/fact'))
        await self.queue.put(('done', {'input_tokens': 10, 'output_tokens': 5}))
        self.idle.set()

    async def close(self):
        self.closed = True


class SharedSearchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.requests = []
        self.mixed = False
        self.repeat = False
        self.parallel_searches = 1
        self.backend = Backend()
        self.interceptor = SharedSearchInterceptor()
        self.callbacks = litellm.callbacks
        litellm.callbacks = [self.interceptor]
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        self.patch = patch('litellm.aclient_session', self.client)
        self.patch.start()
        self.router = litellm.Router(model_list=[{'model_name': 'modal/fixture', 'litellm_params': {
            'model': 'openai/fixture', 'api_key': 'modal-only', 'api_base': 'https://modal.invalid/v1',
            'use_chat_completions_api': True}}], num_retries=0)
        async def dispatch(data, route_type, **kwargs):
            return self.router.aresponses(**data)
        self.bridge = SearchResponses(dispatch, self.interceptor, self.backend, SearchJournal(self.directory.name))
        self.token = REQUEST.set({'caller': 'test-caller', 'headers': {'thread-id': 'test'}, 'request': {}})

    async def asyncTearDown(self):
        REQUEST.reset(self.token)
        self.patch.stop()
        await self.client.aclose()
        litellm.callbacks = self.callbacks
        litellm.in_memory_llm_clients_cache.flush_cache()

    def upstream(self, request):
        self.assertEqual(request.headers['authorization'], 'Bearer modal-only')
        body = json.loads(request.content)
        self.requests.append(body)
        available = any(t.get('function', {}).get('name') == SEARCH_NAME for t in body.get('tools', []))
        if not available or (any(m.get('role') == 'tool' for m in body['messages']) and not self.repeat):
            message = {'role': 'assistant', 'content': 'BLUE: https://example.com/fact'}
            reason = 'stop'
        else:
            calls = [{'id': 'call_search' + (str(len(self.requests)) if self.repeat else '') + (str(i) if i else ''),
                'type': 'function', 'function': {'name': SEARCH_NAME,
                'arguments': json.dumps({'query': 'fixture fact' + (str(i) if i else '')})}} for i in range(self.parallel_searches)]
            if self.mixed:
                calls.append({'id': 'call_client', 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}})
            message = {'role': 'assistant', 'content': 'Checking.', 'tool_calls': calls}
            reason = 'tool_calls'
        return httpx.Response(200, json={'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})

    async def call(self, **extra):
        request = {'model': 'modal/fixture', 'input': 'Find the fact', 'stream': False,
                   'tools': [{'type': 'web_search'}, {'type': 'function', 'name': 'probe', 'parameters': {'type': 'object', 'properties': {}}}]} | extra
        async with asyncio.timeout(20):
            coroutine = await self.bridge.route(data=request, route_type='aresponses')
            result = await coroutine
            if request['stream']:
                return [plain(e) async for e in result]
            return plain(result)

    async def test_upstream_loop_and_sources(self):
        response = await self.call()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.backend.queries, ['fixture fact'])
        searches = [i for i in response['output'] if i['type'] == 'web_search_call']
        self.assertEqual(len(searches), 1)
        self.assertTrue(any('BLUE' in m.get('content', '') for m in self.requests[-1]['messages'] if m['role'] == 'tool'))
        self.assertFalse(any(i.get('name') == SEARCH_NAME for i in response['output']))
        self.assertTrue(response['output'][-1]['content'][0]['annotations'])

    async def test_stream_matches_json_and_real_progress(self):
        events = await self.call(stream=True)
        self.assertIn('response.web_search_call.searching', [e['type'] for e in events])
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(len(self.requests), 2)

    async def test_mixed_tools_and_followup(self):
        self.mixed = True
        first = await self.call()
        self.assertEqual(len(self.requests), 1, 'Client tools must run before model continuation')
        self.assertEqual([i['name'] for i in first['output'] if i['type'] == 'function_call'], ['probe'])
        history = [{'role': 'user', 'content': 'Find the fact'}, *first['output'],
                   {'type': 'function_call_output', 'call_id': 'call_client', 'output': 'CLIENT_OK'}]
        second = await self.call(input=history)
        self.assertEqual(len(self.backend.queries), 1)
        self.assertIn('BLUE', second['output'][-1]['content'][0]['text'])
        tools = [m for m in self.requests[-1]['messages'] if m['role'] == 'tool']
        self.assertEqual({m['tool_call_id'] for m in tools}, {'call_search', 'call_client'})

    async def test_claude_custom_provider_uses_same_upstream_loop(self):
        FakeSession.instances = []
        provider = ClaudeProvider(Path(self.directory.name) / 'claude', FakeSession)
        previous = litellm.custom_provider_map
        litellm.custom_provider_map = [{'provider': 'claudesdk', 'custom_handler': provider}]
        from litellm.utils import custom_llm_setup
        custom_llm_setup()
        self.router = litellm.Router(model_list=[{'model_name': 'claude/opus-5.5', 'litellm_params': {
            'model': 'claudesdk/' + MODEL, 'use_chat_completions_api': True,
            'allowed_openai_params': ['reasoning_effort', 'parallel_tool_calls'], 'num_retries': 0}}], num_retries=0)
        try:
            response = await self.call(model='claude/opus-5.5')
            self.assertEqual(self.backend.queries, ['fixture fact'])
            self.assertEqual(len(FakeSession.instances), 1)
            history = [{'role': 'user', 'content': 'Find the fact'}, *response['output'],
                       {'role': 'user', 'content': 'Recall the source'}]
            await self.call(model='claude/opus-5.5', input=history)
            self.assertEqual(len(FakeSession.instances), 1, 'Search replay must not replace the SDK conversation')
        finally:
            await provider.close()
            litellm.custom_provider_map = previous
            custom_llm_setup()

    async def test_retries_replay_without_another_search_even_after_restart(self):
        first = await self.call()
        self.bridge.journal = SearchJournal(self.directory.name)
        second = await self.call(stream=True)
        self.assertEqual(second[-1]['response']['output'], first['output'])
        self.assertEqual(self.backend.queries, ['fixture fact'])

    async def test_concurrent_duplicate_requests_share_one_search(self):
        first, second = await asyncio.gather(self.call(), self.call())
        self.assertEqual(first['output'], second['output'])
        self.assertEqual(self.backend.queries, ['fixture fact'])
        self.assertEqual(self.bridge.active, {})

    async def test_failure_is_truthful_and_does_not_leak_exception_details(self):
        async def fail(query, settings):
            raise RuntimeError('credential=DO_NOT_EXPOSE')
        self.backend.search = fail
        response = await self.call()
        search = next(i for i in response['output'] if i['type'] == 'web_search_call')
        self.assertEqual(search['status'], 'failed')
        sent = json.dumps(self.requests)
        self.assertNotIn('DO_NOT_EXPOSE', sent)
        self.assertIn('Search backend unavailable', sent)

    async def test_journal_isolated_and_missing_history_fails(self):
        response = await self.call()
        search = next(i for i in response['output'] if i['type'] == 'web_search_call')
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.bridge.journal.expand('another-caller', [search])
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.bridge.journal.read('test-caller', '../escape')

    async def test_parallel_searches_and_limit_keep_every_call(self):
        self.parallel_searches = 4
        response = await self.call(stream=True)
        searches = [i for i in response[-1]['response']['output'] if i['type'] == 'web_search_call']
        self.assertEqual(len(searches), 4)
        self.assertEqual(len(self.backend.queries), 3)
        self.assertEqual(sum(i['status'] == 'failed' for i in searches), 1)
        tools = [m for m in self.requests[-1]['messages'] if m['role'] == 'tool']
        self.assertEqual(len(tools), 4)
        self.assertEqual(len({m['tool_call_id'] for m in tools}), 4)

    async def test_search_loop_ceiling_allows_final_answer(self):
        self.repeat = True
        response = await self.call()
        self.assertEqual(len(self.backend.queries), 3)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(response['output'][-1]['type'], 'message')
        self.assertFalse(any(t.get('function', {}).get('name') == SEARCH_NAME for t in self.requests[-1]['tools']))

    async def test_active_stream_close_cancels_backend(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def slow(query, settings):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.backend.search = slow
        coroutine = await self.bridge.route(data={'model': 'modal/fixture', 'input': 'cancel fixture',
            'tools': [{'type': 'web_search'}], 'stream': True}, route_type='aresponses')
        stream = await coroutine
        await anext(stream)
        await asyncio.wait_for(entered.wait(), 5)
        await stream.aclose()
        self.assertTrue(cancelled.is_set())

    async def test_native_chatgpt_history_is_not_treated_as_our_journal(self):
        item = {'type': 'web_search_call', 'id': 'ws_native', 'status': 'completed',
                'action': {'type': 'search', 'query': 'native'}}
        self.assertEqual(self.bridge.journal.expand('test-caller', [item]), [item])

    async def test_helper_aggregates_subscription_items_and_requires_real_search(self):
        class Router:
            def __init__(self):
                self.request = None
                self.search = True
            async def aresponses(self, **kwargs):
                self.request = kwargs
                async def events():
                    if self.search:
                        yield {'type': 'response.output_item.done', 'item': {'id': 'ws_helper',
                            'type': 'web_search_call', 'status': 'completed'}}
                    yield {'type': 'response.output_item.done', 'item': {'id': 'msg_helper', 'type': 'message',
                        'content': [{'type': 'output_text', 'text': 'Fact', 'annotations': [
                            {'type': 'url_citation', 'title': 'Source', 'url': 'https://example.com/fact'}]}]}}
                    yield {'type': 'response.completed', 'response': {'output': [], 'usage': {'output_tokens': 5}}}
                return events()
        router = Router()
        backend = ChatGPTSearch(router=router)
        result = await backend.search('query', {'type': 'web_search'})
        self.assertEqual(len(result.sources), 1)
        self.assertIsInstance(router.request['input'], list)
        self.assertEqual(set(router.request), {'model', 'input', 'instructions', 'tools', 'tool_choice', 'reasoning', 'stream', 'store'})
        router.search = False
        with self.assertRaisesRegex(RuntimeError, 'no completed hosted search'):
            await backend.search('query', {'type': 'web_search'})


if __name__ == '__main__':
    unittest.main()
