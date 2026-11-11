"""Signed Claude replay and strict stream lifecycle through the production hook.

AWS Invoke JSON and binary eventstream fixtures exercise the actual Bedrock
transport, Responses conversion, router, and production hook. No credentials
or inference are needed.
"""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import httpx
import litellm
from chat_native import install, plain
from litellm.responses.litellm_completion_transformation.reasoning_items import decode_thinking_blocks
from litellm.responses.litellm_completion_transformation.transformation import TOOL_CALLS_CACHE
from refresh_models import configured_model


BLOCKS = [{'type': 'thinking', 'thinking': '', 'signature': 'fixture-signature'},
          {'type': 'redacted_thinking', 'data': 'Zml4dHVyZS1yZWRhY3RlZA=='}]
MODEL = 'fixture/fable'


def aws_frame(kind, value):
    headers = b''
    for name, text in ((':message-type', 'event'), (':event-type', kind),
                       (':content-type', 'application/json')):
        key, data = name.encode(), text.encode()
        headers += bytes([len(key)]) + key + b'\x07' + struct.pack('>H', len(data)) + data
    payload = json.dumps(value).encode()
    prelude = struct.pack('>II', 16 + len(headers) + len(payload), len(headers))
    message = prelude + struct.pack('>I', zlib.crc32(prelude)) + headers + payload
    return message + struct.pack('>I', zlib.crc32(message))


class AWSStream(httpx.AsyncByteStream):
    def __init__(self, events, owner):
        self.events, self.owner = events, owner

    async def __aiter__(self):
        for event in self.events:
            frame = aws_frame('chunk', {'bytes': base64.b64encode(json.dumps(event).encode()).decode()})
            # Both prelude and payload can span arbitrary transport packets.
            for offset in range(0, len(frame), 13):
                yield frame[offset:offset + 13]
            if (self.owner.gate is not None and event.get('delta', {}).get('text') == 'FABLE_'):
                await self.owner.gate.wait()

    async def aclose(self):
        self.owner.closed = True


def check_events(test, events):
    """Deltas require a live item; terminal output must exactly match done items."""
    added, done, deltas = {}, {}, {}
    previous = -1
    terminal = None
    for event in events:
        kind = event['type']
        test.assertIsNone(terminal, 'Event after terminal response')
        sequence = event['sequence_number']
        test.assertGreater(sequence, previous)
        previous = sequence
        if kind == 'response.output_item.added':
            item = event['item']
            test.assertNotIn(item['id'], added)
            added[item['id']] = event['output_index']
        elif kind == 'response.output_item.done':
            item = event['item']
            test.assertIn(item['id'], added)
            test.assertNotIn(item['id'], done)
            test.assertEqual(event['output_index'], added[item['id']])
            done[item['id']] = item
        elif 'item_id' in event:
            item_id = event['item_id']
            test.assertIn(item_id, added, 'Delta/part before output_item.added')
            test.assertNotIn(item_id, done, 'Delta/part after output_item.done')
            test.assertEqual(event['output_index'], added[item_id])
            if kind == 'response.output_text.delta':
                deltas[item_id] = deltas.get(item_id, '') + event['delta']
        if kind in ('response.completed', 'response.incomplete', 'response.failed'):
            terminal = event['response']
    test.assertIsNotNone(terminal)
    test.assertEqual({i['id']: i for i in terminal['output']}, done)
    test.assertEqual(set(added), set(done))
    test.assertEqual(sorted(added.values()), list(range(len(added))))
    for item_id, text in deltas.items():
        test.assertEqual(text, ''.join(p['text'] for p in done[item_id]['content'] if p['type'] == 'output_text'))
    return terminal


class SignedBridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick(): loop.call_later(.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.requests = []
        self.finish = 'stop'
        self.tool = False
        self.blocks = copy.deepcopy(BLOCKS)
        self.thinking = ''
        self.gate = None
        self.closed = False
        self.codex_mode = False
        self.answer = 'FABLE_OK'
        self.parallel = 1
        self.installation = install()
        self.addCleanup(self.installation.uninstall)
        litellm.in_memory_llm_clients_cache.flush_cache()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream))
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
        with patch.object(AsyncHTTPHandler, 'create_client', return_value=self.client):
            handler = AsyncHTTPHandler()
        handler.client = self.client
        self.http_patch = patch('litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client',
                                return_value=handler)
        self.http_patch.start()
        self.invoke_patch = patch('litellm.llms.bedrock.chat.invoke_handler.get_async_httpx_client', return_value=handler)
        self.invoke_patch.start()
        self.routes = [{'model_name': MODEL, 'litellm_params': {
            'model': 'bedrock/us.anthropic.claude-fable-5-2', 'aws_region_name': 'us-east-1',
            'aws_access_key_id': 'AKIA' + 'A' * 16, 'aws_secret_access_key': 'B' * 40,
            'max_tokens': 65536, 'use_chat_completions_api': True,
            'additional_drop_params': ['prompt_cache_key'],
            'allowed_openai_params': ['tools', 'tool_choice']}}]
        route, _, _ = configured_model(FableConfigurationTests().spec())
        self.routes[0]['model_info'] = route['model_info']
        self.router = litellm.Router(model_list=self.routes, num_retries=0, timeout=10)

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.invoke_patch.stop()
        await self.client.aclose()
        litellm.in_memory_llm_clients_cache.flush_cache()
        TOOL_CALLS_CACHE.flush_cache()

    def upstream(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertIn('/model/us.anthropic.claude-fable-5-2/invoke', str(request.url))
        return self.invoke_response(request, body)

    def invoke_response(self, request, body):
        call = self.tool and not any(p.get('type') == 'tool_result' for m in body['messages'] for p in m['content'])
        if self.codex_mode:
            call = len(self.requests) == 1
        finish = 'tool_use' if call else {'stop': 'end_turn', 'length': 'max_tokens',
                                       'content_filter': 'refusal'}[self.finish]
        content = copy.deepcopy(self.blocks)
        if self.thinking:
            content.insert(0, {'type': 'thinking', 'thinking': self.thinking, 'signature': 'raw-signature'})
        if call:
            name = body['tools'][0]['name']
            arguments = {'content': 'patch fixture'} if name == 'apply_patch' else {'value': 'fixture'}
            if self.codex_mode:
                name = next(t['name'] for t in body['tools'] if t['name'].endswith('exec'))
                arguments = {'content': 'text("FABLE_TOOL_OK")'}
            content.append({'type': 'tool_use', 'id': 'call_fixture', 'name': name, 'input': arguments})
            for index in range(1, self.parallel):
                content.append({'type': 'tool_use', 'id': 'call_fixture' + str(index),
                                'name': name, 'input': arguments})
        elif finish == 'end_turn':
            content.append({'type': 'text', 'text': self.answer})
        usage = {'input_tokens': 10, 'output_tokens': 4096 if finish == 'max_tokens' else 5}
        message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
            'model': 'claude-fable-5-2', 'content': content, 'stop_reason': finish,
            'stop_sequence': None, 'usage': usage}
        if request.url.path.endswith('/invoke'):
            return httpx.Response(200, json=message)
        self.assertTrue(request.url.path.endswith('/invoke-with-response-stream'))
        events = [{'type': 'message_start', 'message': message | {'content': [], 'stop_reason': None}}]
        for index, part in enumerate(content):
            kind = part['type']
            start = part.copy()
            if kind == 'thinking': start.update(thinking='', signature='')
            elif kind == 'text': start['text'] = ''
            elif kind == 'tool_use': start['input'] = {}
            events.append({'type': 'content_block_start', 'index': index, 'content_block': start})
            if kind == 'thinking':
                if part['thinking']:
                    events.append({'type': 'content_block_delta', 'index': index,
                                   'delta': {'type': 'thinking_delta', 'thinking': part['thinking']}})
                events.append({'type': 'content_block_delta', 'index': index,
                               'delta': {'type': 'signature_delta', 'signature': part['signature']}})
            elif kind == 'text':
                events.extend({'type': 'content_block_delta', 'index': index,
                               'delta': {'type': 'text_delta', 'text': t}} for t in (part['text'][:-2], part['text'][-2:]))
            elif kind == 'tool_use':
                events.append({'type': 'content_block_delta', 'index': index,
                               'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(part['input'])}})
            events.append({'type': 'content_block_stop', 'index': index})
        events += [{'type': 'message_delta', 'delta': {'stop_reason': finish, 'stop_sequence': None}, 'usage': usage},
                   {'type': 'message_stop'}]
        return httpx.Response(200, headers={'Content-Type': 'application/vnd.amazon.eventstream'},
                              stream=AWSStream(events, self))

    def replayed_blocks(self, message):
        parts = message['content']
        return [p for p in parts if p.get('type') in ('thinking', 'redacted_thinking')]

    async def response(self, history='Hello', stream=True, **kwargs):
        async with asyncio.timeout(15):
            value = await self.router.aresponses(model=MODEL, input=history, stream=stream, store=False, **kwargs)
            if not stream: return plain(value)
            events = [plain(e) async for e in value]
        self.assertFalse(any(e['type'].startswith('response.reasoning_summary') for e in events),
                         'Unmarked reasoning must not be presented as a summary')
        return check_events(self, events)

    async def test_empty_signed_and_redacted_blocks_survive_stream_and_restart(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                first = await self.response(stream=stream)
                carrier = next(i for i in first['output'] if i['type'] == 'reasoning')
                self.assertEqual(list(decode_thinking_blocks(carrier.get('encrypted_content')) or ()), self.blocks)
                TOOL_CALLS_CACHE.flush_cache()
                litellm.in_memory_llm_clients_cache.flush_cache()
                self.router = litellm.Router(model_list=self.routes, num_retries=0, timeout=10)
                await self.response([{'role': 'user', 'content': 'Hello'}, *first['output'],
                                     {'role': 'user', 'content': 'Continue'}], stream=stream)
                assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
                self.assertEqual(self.replayed_blocks(assistant), self.blocks)

    async def test_reasoning_and_tool_result_stay_attached_after_cache_flush(self):
        self.tool = True
        tools = [{'type': 'function', 'name': 'probe', 'description': 'Probe',
                  'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}]
        first = await self.response('Use probe', tools=tools)
        calls = [i for i in first['output'] if i['type'] == 'function_call']
        self.assertEqual(len(calls), 1)
        TOOL_CALLS_CACHE.flush_cache()
        second = await self.response([{'role': 'user', 'content': 'Use probe'}, *first['output'],
            {'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': 'RESULT'}], tools=tools)
        self.assertEqual(second['status'], 'completed')
        assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
        self.assertEqual(self.replayed_blocks(assistant), self.blocks)

    async def test_reasoning_only_limits_remain_incomplete(self):
        for finish, reason in (('length', 'max_output_tokens'), ('content_filter', 'content_filter')):
            with self.subTest(finish=finish):
                self.finish = finish
                result = await self.response(max_output_tokens=4096)
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['incomplete_details']['reason'], reason)
                self.assertFalse(any(i['type'] == 'message' for i in result['output']))
                carrier = next(i for i in result['output'] if i['type'] == 'reasoning')
                self.assertEqual(list(decode_thinking_blocks(carrier.get('encrypted_content')) or ()), self.blocks)

    async def test_parallel_namespaced_and_custom_tools_replay_signed_batch(self):
        function = {'type': 'function', 'name': 'probe', 'description': 'Probe',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}
        for tool in (function, {'type': 'namespace', 'name': 'functions', 'tools': [function]},
                     {'type': 'custom', 'name': 'apply_patch', 'description': 'Apply patch', 'format': {'type': 'text'}}):
            with self.subTest(tool=tool['type']):
                self.tool = True; self.parallel = 2
                prefix = [{'role': 'user', 'content': 'Use the tools'}]
                first = await self.response(prefix, tools=[tool])
                calls = [i for i in first['output'] if i['type'] in ('function_call', 'custom_tool_call')]
                self.assertEqual(len(calls), 2)
                results = [{'type': 'custom_tool_call_output' if c['type'] == 'custom_tool_call' else 'function_call_output',
                            'call_id': c['call_id'], 'output': 'RESULT' + str(index)} for index, c in enumerate(calls)]
                if tool['type'] == 'custom':
                    self.assertEqual(calls[0]['input'], 'patch fixture')
                if tool['type'] == 'namespace':
                    self.assertEqual(calls[0]['namespace'], 'functions')
                TOOL_CALLS_CACHE.flush_cache()
                result = await self.response(prefix + first['output'] + results, tools=[tool])
                self.assertEqual(result['status'], 'completed')
                messages = self.requests[-1]['messages']
                self.assertEqual(self.replayed_blocks(next(m for m in messages if m['role'] == 'assistant')), self.blocks)
                self.assertEqual(sum(p['type'] == 'tool_result' for m in messages for p in m['content']), 2)

    async def test_raw_reasoning_never_leaks_as_answer_or_summary(self):
        self.blocks = []
        self.thinking = 'PRIVATE_FIXTURE_REASONING'
        result = await self.response()
        self.assertEqual(result['status'], 'completed')
        carrier = next(i for i in result['output'] if i['type'] == 'reasoning')
        self.assertIn(self.thinking, json.dumps(carrier))  # replay state, not presentation

    async def test_budget_default_and_explicit_client_cap_reach_bedrock(self):
        await self.response(reasoning={'effort': 'high'})
        self.assertEqual(self.requests[-1]['max_tokens'], 65536)
        self.assertEqual(self.requests[-1]['output_config']['effort'], 'high')
        self.assertEqual(self.requests[-1]['thinking']['type'], 'adaptive')
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            await self.response(reasoning={'effort': effort}, max_output_tokens=8192)
            self.assertEqual(self.requests[-1]['max_tokens'], 8192)
            self.assertEqual(self.requests[-1]['output_config']['effort'], effort)

    async def test_answer_is_incremental_not_buffered_to_completion(self):
        self.gate = asyncio.Event()
        stream = await self.router.aresponses(model=MODEL, input='Hello', stream=True, store=False)
        events = []
        async with asyncio.timeout(10):
            async for event in stream:
                events.append(plain(event))
                if event.type == 'response.output_text.delta':
                    break
        self.assertEqual(events[-1]['delta'], 'FABLE_')
        self.assertFalse(self.closed)
        self.assertFalse(any(e['type'] == 'response.completed' for e in events))
        self.gate.set()
        events.extend([plain(e) async for e in stream])
        check_events(self, events)
        self.assertTrue(self.closed)

    async def test_shared_search_preserves_signed_prefix_and_tools(self):
        from claude_context import REQUEST
        from shared_search import SEARCH_NAME, SearchJournal, SearchResponses, SharedSearchInterceptor
        from test_shared_search import Backend
        self.tool = True
        # Render a hosted search as the same ordinary Bedrock function call.
        original = self.invoke_response
        def search(request, body):
            response = original(request, body)
            if request.url.path.endswith('/invoke'):
                value = json.loads(response.content)
                for block in value['content']:
                    if block['type'] == 'tool_use':
                        block.update(name=SEARCH_NAME, input={'query': 'fixture fact'})
                return httpx.Response(200, json=value)
            return response
        self.invoke_response = search
        interceptor = SharedSearchInterceptor()
        callbacks = litellm.callbacks
        litellm.callbacks = [interceptor]
        self.addCleanup(setattr, litellm, 'callbacks', callbacks)
        async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
        with tempfile.TemporaryDirectory(prefix='fable-search-') as directory:
            bridge = SearchResponses(dispatch, interceptor, Backend(), SearchJournal(directory), self.router)
            data = dict(model=MODEL, input=[{'role': 'user', 'content': 'Find the fact'}],
                        stream=True, store=False, reasoning={'effort': 'high'}, tools=[{'type': 'web_search'}])
            token = REQUEST.set({'caller': 'fixture-owner', 'headers': {}, 'request': data})
            try:
                result = await (await bridge.route(data, 'aresponses'))
                final = check_events(self, [plain(e) async for e in result])
                self.assertEqual(len(self.requests), 2)
                first, second = self.requests
                self.assertEqual(second['messages'][:len(first['messages'])], first['messages'])
                self.assertEqual(second['tools'], first['tools'])
                assistant = next(m for m in second['messages'] if m['role'] == 'assistant')
                self.assertEqual(self.replayed_blocks(assistant), self.blocks)
                self.assertTrue(any(i['type'] == 'web_search_call' for i in final['output']))
                self.assertEqual(list(decode_thinking_blocks(final['output'][0]['encrypted_content'])), self.blocks)
                data['max_output_tokens'] = 8192
                result = await (await bridge.route(data, 'aresponses'))
                check_events(self, [plain(e) async for e in result])
                self.assertEqual(len(self.requests), 4, 'A changed cap must not hit the completed-response cache')
                self.assertEqual(self.requests[-1]['max_tokens'], 8192)
            finally:
                REQUEST.reset(token)

    async def test_stock_codex_signed_tool_resume_and_compaction(self):
        """Real stock CLI -> loopback -> Responses -> Bedrock binary fixture."""
        binary = shutil.which('codex')
        if not binary:
            self.skipTest('Stock Codex binary is unavailable')
        from claude_context import REQUEST
        from compaction_fallback import CompactionResponses, is_compaction
        import tomlkit
        self.codex_mode = True
        requests, errors, compacted = [], [], []
        with tempfile.TemporaryDirectory(prefix='.fable-codex-', dir=Path.cwd()) as directory:
            home = Path(directory)
            catalog = home / 'catalog.json'
            spec = FableConfigurationTests().spec(); spec['name'] = MODEL
            _, row, _ = configured_model(spec)
            catalog.write_text(json.dumps({'models': [row]}))
            async def dispatch(data, route_type, **kwargs): return self.router.aresponses(**data)
            bridge = CompactionResponses(dispatch, catalog)
            async def handle(reader, writer):
                token = None
                try:
                    header = (await reader.readuntil(b'\r\n\r\n')).decode()
                    headers = {k.lower(): v.strip() for k, v in
                               (l.split(':', 1) for l in header.split('\r\n')[1:] if ':' in l)}
                    self.assertEqual(headers.get('authorization'), 'Bearer fake-offline-key')
                    data = json.loads(await reader.readexactly(int(headers['content-length'])))
                    requests.append(copy.deepcopy(data))
                    token = REQUEST.set({'caller': 'fixture-owner', 'headers': headers, 'request': data})
                    compact = is_compaction(data)
                    self.answer = 'CHECKPOINT FABLE_OK' if compact else 'FABLE_OK'
                    if compact: compacted.append(len(self.requests))
                    stream = await (await bridge.route(data, 'aresponses', llm_router=self.router))
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
                    async for event in stream:
                        writer.write(event if isinstance(event, bytes) else
                                     ('data: ' + event.model_dump_json(exclude_none=True) + '\n\n').encode())
                        await writer.drain()
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    if token is not None: REQUEST.reset(token)
                    writer.close(); await writer.wait_closed()
            server = await asyncio.start_server(handle, '127.0.0.1', 0)
            self.addAsyncCleanup(server.wait_closed)
            self.addCleanup(server.close)
            port = server.sockets[0].getsockname()[1]
            (home / 'config.toml').write_text(tomlkit.dumps({
                'model': MODEL, 'model_provider': 'fixture', 'model_catalog_json': str(catalog),
                'model_reasoning_effort': 'high', 'approval_policy': 'never', 'sandbox_mode': 'read-only',
                'web_search': 'disabled', 'check_for_update_on_startup': False,
                'features': {'shell_snapshot': False},
                'model_providers': {'fixture': {'name': 'Offline Fable', 'base_url': f'http://127.0.0.1:{port}/v1',
                    'wire_api': 'responses', 'supports_websockets': False, 'requires_openai_auth': False,
                    'env_key': 'OFFLINE_FIXTURE_KEY', 'request_max_retries': 0, 'stream_max_retries': 0}}}))
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
                self.assertEqual(process.returncode, 0, stderr.decode()[-2000:])
                self.assertNotIn('OutputTextDelta without active item', stderr.decode())
                events = [json.loads(l) for l in stdout.splitlines()]
                self.assertTrue(any(e['type'] == 'turn.completed' for e in events))
                answers = [e['item']['text'] for e in events if e['type'] == 'item.completed'
                           and e['item']['type'] == 'agent_message']
                self.assertEqual(answers, ['FABLE_OK'])
                return events
            first = await run(['--skip-git-repo-check', '--json', 'Remember FABLE_OK.'])
            session = next(e['thread_id'] for e in first if e['type'] == 'thread.started')
            self.assertTrue(any('FABLE_TOOL_OK' in json.dumps(i) for r in requests for i in r['input']))
            self.installation.uninstall(); self.installation = install()
            self.addCleanup(self.installation.uninstall)
            TOOL_CALLS_CACHE.flush_cache()
            litellm.in_memory_llm_clients_cache.flush_cache()
            self.router = litellm.Router(model_list=self.routes, num_retries=0, timeout=10)
            await run(['resume', session, '--skip-git-repo-check', '--json', 'Continue.'])
            assistant = next(m for m in self.requests[-1]['messages'] if m['role'] == 'assistant')
            self.assertEqual(self.replayed_blocks(assistant), self.blocks)
            await run(['-c', 'model_auto_compact_token_limit=1', 'resume', session,
                       '--skip-git-repo-check', '--json', 'Continue after compaction.'])
            self.assertTrue(compacted)
            continued = json.dumps(self.requests[-1]['messages'])
            self.assertIn('CHECKPOINT', continued)
            self.assertNotIn('fixture-signature', continued)
            self.assertNotIn(BLOCKS[1]['data'], continued)
            self.assertNotIn('tool_result', continued)


class FableConfigurationTests(unittest.TestCase):
    def spec(self):
        return {'name': 'bedrock/fable-5.2', 'litellm': {
            'model': 'bedrock/us.anthropic.claude-fable-5-2', 'use_chat_completions_api': True},
            'codex': {'display_name': 'Fable', 'context_window': 1000000,
                      'supported_reasoning_levels': ['low', 'medium', 'high', 'xhigh', 'max'],
                      'default_reasoning_level': 'high'}}

    def test_fable_route_has_explicit_reasoning_and_answer_headroom(self):
        original = self.spec()
        route, row, verification = configured_model(original)
        self.assertEqual(route['litellm_params']['max_tokens'], 65536)
        self.assertNotIn('max_tokens', original['litellm'])
        self.assertIn('--auto-tool-choice', verification['args'])

    def test_explicit_budget_is_respected_and_invalid_values_rejected(self):
        for budget in (8192, 32768, 128000):
            spec = self.spec(); spec['litellm']['max_tokens'] = budget
            self.assertEqual(configured_model(spec)[0]['litellm_params']['max_tokens'], budget)
        for budget in (0, -1, True, 128001, '65536'):
            spec = self.spec(); spec['litellm']['max_tokens'] = budget
            with self.assertRaisesRegex(ValueError, 'max_tokens'):
                configured_model(spec)


if __name__ == '__main__':
    unittest.main()
