"""Claude contract, reconciliation and pinned Responses conversion tests."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import subprocess
import shutil
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

import litellm
import yaml
from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, ToolUseBlock
from claude_context import REQUEST, validate_request
from claude_provider import ClaudeProvider, usage_counts
from claude_session import CORRELATION, EFFORTS, MODEL, Session, configuration, content_blocks, digest
from configure_claude import MANIFEST, ClaudeSetup, generate
from refresh_models import Refresh

FUNCTION = {'type': 'function', 'function': {'name': 'probe', 'description': 'Read a marker',
    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}}}


class FakeClient:
    instances = []
    scripted = []

    def __init__(self, options):
        self.options = options
        self.prompts = []
        self.interrupted = asyncio.Event()
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def query(self, prompt):
        self.prompts.append([item async for item in prompt])

    async def interrupt(self):
        self.interrupted.set()

    async def receive_response(self):
        yield SystemMessage('init', {'model': MODEL, 'session_id': 'fake-session'})
        script = self.__class__.scripted.pop(0) if self.__class__.scripted else ['ANSWER']
        for item in script:
            if isinstance(item, str):
                yield StreamEvent('event', 'fake-session', {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': item}})
            elif item == 'wait':
                await self.interrupted.wait()
            else:
                yield item
        yield ResultMessage('success', 1, 1, False, 1, 'fake-session', usage={'input_tokens': 7, 'output_tokens': 3})


class ContractTests(unittest.TestCase):
    def test_catalog_preserves_defaults_and_other_routes(self):
        original = {'model_list': [{'model_name': 'chatgpt/gpt-6-astra'}], 'general_settings': {'keep': True}}
        catalog = {'models': [{'slug': 'modal/test'}]}
        config, new, summary = generate(json.loads(MANIFEST.read_text()), original, catalog, 'chatgpt/gpt-6-astra')
        self.assertEqual(config['model_list'][0], original['model_list'][0])
        self.assertEqual(new['models'][0], catalog['models'][0])
        row = new['models'][1]
        self.assertEqual(row['display_name'], 'Claude Opus 5.5')
        self.assertEqual([e['effort'] for e in row['supported_reasoning_levels']], list(EFFORTS))
        self.assertTrue(row['supports_search_tool'])
        self.assertNotIn('experimental', row['description'].lower())
        self.assertEqual(generate(json.loads(MANIFEST.read_text()), config, new, '')[2]['test_models'], [])

    def test_configuration_rejects_unimplemented_controls(self):
        for params in ({'temperature': 0.2}, {'reasoning_effort': 'none'}, {'max_tokens': 100},
                       {'tools': [{'type': 'web_search'}]}, {'tool_choice': 'required'},
                       {'response_format': {'type': 'unknown'}}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                configuration([], params)

    def test_raw_responses_controls_cannot_be_silently_dropped(self):
        for raw in ({'temperature': 0.5}, {'max_output_tokens': 1}, {'store': True},
                    {'service_tier': 'priority'}, {'text': {'verbosity': 'low'}}):
            token = REQUEST.set({'request': raw})
            try:
                with self.assertRaises(ValueError):
                    validate_request()
            finally:
                REQUEST.reset(token)

    def test_all_efforts_reach_pinned_cli_flags(self):
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        with tempfile.TemporaryDirectory() as state:
            for effort in EFFORTS:
                session = Session((effort,), configuration([], {'reasoning_effort': effort}), state)
                options = session.options()
                command = SubprocessCLITransport(prompt='probe', options=options)._build_command()
                self.assertEqual(command[command.index('--effort') + 1], effort)
                self.assertEqual(command[command.index('--model') + 1], MODEL)
                self.assertEqual(command[command.index('--thinking-display') + 1], 'summarized')
                self.assertEqual(options.tools, [])

    def test_cli_environment_drops_unrelated_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'scripts'
            scripts.mkdir()
            wrapper = scripts / 'claude-cli'
            shutil.copy2(Path(__file__).with_name('claude-cli'), wrapper)
            binary = root / '.venv/lib/python3.14/site-packages/claude_agent_sdk/_bundled/claude'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\n/usr/bin/env | /usr/bin/cut -d= -f1\n')
            binary.chmod(0o700)
            env = os.environ | {name: 'synthetic-secret' for name in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'MODAL_API_KEY', 'LITELLM_MASTER_KEY')}
            names = subprocess.check_output([str(wrapper)], env=env, text=True).splitlines()
            self.assertIn('HOME', names)
            self.assertIn('CLAUDE_CONFIG_DIR', names)
            self.assertFalse(set(names) & {'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'MODAL_API_KEY', 'LITELLM_MASTER_KEY'})

    def test_image_and_usage_mapping(self):
        self.assertEqual(content_blocks([{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YQ=='}}])[0]['source']['data'], 'YQ==')
        with self.assertRaisesRegex(ValueError, 'inline'):
            content_blocks([{'type': 'image_url', 'image_url': {'url': 'http://localhost/private'}}])
        self.assertIsNone(usage_counts(None))
        self.assertEqual(usage_counts({'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 3})['total_tokens'], 6)

    def test_wrong_transaction_owner_names_claude_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            refresh = Refresh(Path(directory), Path(directory), 'uv')
            refresh.transaction.mkdir(parents=True)
            (refresh.transaction / 'journal.json').write_text(json.dumps({'prefix': 'claude/'}))
            with self.assertRaisesRegex(ValueError, 'claude.yml'):
                refresh.journal()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def loop_factory():
        loop = asyncio.new_event_loop()
        def tick():
            loop.call_later(0.01, tick)
        tick()
        return loop

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FakeClient.instances, FakeClient.scripted = [], []
        self.provider = ClaudeProvider(self.temp.name, lambda key, config, state: Session(key, config, state, FakeClient))
        self.token = REQUEST.set({'caller': 'authenticated-test', 'headers': {'thread-id': 'chat'}})

    async def asyncTearDown(self):
        await self.provider.close()
        REQUEST.reset(self.token)

    async def response(self, messages=None, **params):
        async with asyncio.timeout(5):
            return [item async for item in self.provider.astreaming(MODEL, messages or [{'role': 'user', 'content': 'hello'}], optional_params=params)]

    async def test_stream_nonstream_retry_and_restart_replay(self):
        first = await self.response()
        self.assertEqual(first, await self.response())
        self.assertEqual(len(FakeClient.instances), 1)
        other = ClaudeProvider(self.temp.name)
        replay = [item async for item in other.astreaming(MODEL, [{'role': 'user', 'content': 'hello'}], optional_params={})]
        self.assertEqual(replay, first)
        response = await self.provider.acompletion(MODEL, [{'role': 'user', 'content': 'hello'}], optional_params={})
        self.assertEqual(response.choices[0].message.content, 'ANSWER')
        self.assertEqual(response.usage.total_tokens, 10)

    async def test_followup_effort_and_instruction_changes(self):
        messages = [{'role': 'user', 'content': 'hello'}]
        await self.response(messages)
        await asyncio.sleep(0)
        followup = messages + [{'role': 'assistant', 'content': 'ANSWER'}, {'role': 'user', 'content': 'next'}]
        await self.response(followup)
        self.assertEqual(len(FakeClient.instances), 1)
        changed = followup + [{'role': 'assistant', 'content': 'ANSWER'}, {'role': 'user', 'content': 'harder'}]
        await self.response(changed, reasoning_effort='max')
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertEqual(FakeClient.instances[-1].options.effort, 'max')
        await self.response([{'role': 'system', 'content': 'new instructions'}, *changed])
        self.assertEqual(len(FakeClient.instances), 3)

    async def test_missing_identity_and_unresolved_crash_fail_closed(self):
        token = REQUEST.set({})
        with self.assertRaisesRegex(Exception, 'authenticated'):
            await self.response()
        REQUEST.reset(token)
        with self.assertRaisesRegex(Exception, 'unresolved'):
            await self.response([{'role': 'assistant', 'tool_calls': [{'id': 'lost'}]}])

    async def test_independent_chats_and_windows(self):
        await self.response()
        for headers in ({'thread-id': 'other'}, {'thread-id': 'chat', 'x-codex-turn-metadata': '{"context_window_id":"new"}'}):
            token = REQUEST.set({'caller': 'authenticated-test', 'headers': headers})
            await self.response()
            REQUEST.reset(token)
        self.assertEqual(len(self.provider.sessions), 3)

    async def test_hook_preserves_sdk_identity_and_arguments(self):
        session = Session(('hook',), configuration([], {'tools': [FUNCTION]}), self.temp.name, FakeClient)
        options = session.options()
        hook = options.hooks['PreToolUse'][0].hooks[0]
        result = await hook({'tool_name': 'mcp__codex__tool_0', 'tool_input': {'value': 'a'}}, 'sdk-id', {})
        self.assertEqual(result['hookSpecificOutput']['updatedInput'], {'value': 'a', CORRELATION: 'sdk-id'})
        await session.consume(AssistantMessage([ToolUseBlock('sdk-id', 'mcp__codex__tool_0', {'value': 'a'})], MODEL))
        await session.consume(StreamEvent('e', 'sid', {'type': 'message_stop'}))
        kind, calls = await session.queue.get()
        self.assertEqual(kind, 'tools')
        self.assertEqual(calls[0]['arguments'], '{"value": "a"}')
        self.assertEqual(calls[0]['id'], session.register('sdk-id', 'probe', {'value': 'a'}))
        session.deliver(calls[0]['id'], 'result')
        session.deliver(calls[0]['id'], 'result')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            session.deliver(calls[0]['id'], 'different')

    async def test_partial_out_of_order_results_and_new_prompt_after_abort(self):
        key = ('authenticated-test', 'chat', 'default', MODEL, 'chat')
        config = configuration([], {'tools': [FUNCTION]})
        session = Session(key, config, self.temp.name, FakeClient)
        self.provider.sessions[key] = session
        session.history = [{'role': 'user', 'content': 'two tools'}]
        session.idle.clear()
        ids = [session.register('sdk-' + str(i), 'probe', {'value': str(i)}) for i in range(2)]
        calls = [{'id': k, 'type': 'function', 'function': {'name': 'probe', 'arguments': '{}'}} for k in ids]
        messages = session.history + [{'role': 'assistant', 'tool_calls': calls}, {'role': 'tool', 'tool_call_id': ids[1], 'content': 'second'}]
        partial = await self.response(messages, tools=[FUNCTION])
        self.assertEqual([c['tool_use']['id'] for c in partial if c.get('tool_use')], [ids[0]])
        continuation = messages + [{'role': 'tool', 'tool_call_id': ids[0], 'content': 'aborted by user'}, {'role': 'user', 'content': 'NEW_REQUEST'}]
        answer = await self.response(continuation, tools=[FUNCTION])
        self.assertEqual(''.join(c['text'] for c in answer), 'ANSWER')
        self.assertTrue(session.closed)
        self.assertIn('NEW_REQUEST', json.dumps(FakeClient.instances[-1].prompts))

    async def test_sdk_error_statuses_survive(self):
        for status in (401, 403, 429, 529):
            session = Session(('error', status), configuration([], {}), self.temp.name, FakeClient)
            await session.consume(ResultMessage('success', 1, 1, True, 1, 'sid', api_error_status=status))
            kind, error = await session.queue.get()
            self.assertEqual((kind, error.status_code), ('error', status))
        for label, status in (('authentication_failed', 401), ('billing_error', 403), ('rate_limit', 429)):
            session = Session(('error', label), configuration([], {}), self.temp.name, FakeClient)
            await session.consume(AssistantMessage([], '<synthetic>', error=label))
            _, error = await session.queue.get()
            self.assertEqual(error.status_code, status)

    async def test_pinned_router_responses_conversion(self):
        from litellm.utils import custom_llm_setup
        mapping = [{'provider': 'claudesdk', 'custom_handler': self.provider}]
        with patch.object(litellm, 'custom_provider_map', mapping):
            custom_llm_setup()
            routes, _, _ = generate(json.loads(MANIFEST.read_text()), {}, {}, '')
            router = litellm.Router(model_list=routes['model_list'], num_retries=0)
            events = [e.model_dump() async for e in await router.aresponses(
                model='claude/opus-5.5', input='hello', reasoning={'effort': 'high'}, stream=True)]
            self.assertTrue(any(e['type'] == 'response.completed' for e in events))
            self.assertEqual(''.join(e.get('delta', '') for e in events if e['type'] == 'response.output_text.delta'), 'ANSWER')

    async def test_pinned_router_emits_reasoning_summaries(self):
        from litellm.utils import custom_llm_setup
        FakeClient.scripted = [[StreamEvent('event', 'sid', {'type': 'content_block_delta',
            'delta': {'type': 'thinking_delta', 'thinking': 'A short summary.'}}), 'ANSWER']]
        with patch.object(litellm, 'custom_provider_map', [{'provider': 'claudesdk', 'custom_handler': self.provider}]):
            custom_llm_setup()
            routes, _, _ = generate(json.loads(MANIFEST.read_text()), {}, {}, '')
            router = litellm.Router(model_list=routes['model_list'], num_retries=0)
            events = [e.model_dump() async for e in await router.aresponses(model='claude/opus-5.5', input='hello', stream=True)]
            self.assertTrue(any('reasoning_summary' in e['type'] for e in events))

    async def test_usage_is_not_counted_twice_across_tool_boundary(self):
        session = Session(('usage',), configuration([], {'tools': [FUNCTION]}), self.temp.name, FakeClient)
        session.options()
        await session.consume(AssistantMessage([ToolUseBlock('id', 'mcp__codex__tool_0', {'value': 'x'})], MODEL,
            usage={'input_tokens': 10, 'output_tokens': 5}))
        await session.consume(StreamEvent('e', 'sid', {'type': 'message_delta', 'usage': {'input_tokens': 10, 'output_tokens': 5}}))
        await session.consume(StreamEvent('e', 'sid', {'type': 'message_stop'}))
        self.assertEqual(session.boundary_usage['input_tokens'], 10)
        await session.queue.get()
        await session.consume(ResultMessage('success', 1, 1, False, 2, 'sid', usage={'input_tokens': 30, 'output_tokens': 12}))
        kind, usage = await session.queue.get()
        self.assertEqual((kind, usage['input_tokens'], usage['output_tokens']), ('done', 20, 7))

    async def test_split_sdk_messages_form_one_parallel_tool_batch(self):
        session = Session(('batch',), configuration([], {'tools': [FUNCTION]}), self.temp.name, FakeClient)
        session.options()
        for index in range(2):
            await session.consume(AssistantMessage([ToolUseBlock(str(index), 'mcp__codex__tool_0', {'value': str(index)})], MODEL))
            self.assertTrue(session.queue.empty())
        await session.consume(StreamEvent('e', 'sid', {'type': 'message_delta', 'usage': {'input_tokens': 10, 'output_tokens': 15}}))
        await session.consume(StreamEvent('e', 'sid', {'type': 'message_stop'}))
        kind, calls = await session.queue.get()
        self.assertEqual((kind, len(calls)), ('tools', 2))
        self.assertEqual(session.reported_usage['output_tokens'], 15)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.refresh = ClaudeSetup(home, home / 'runtime', 'uv')
        (home / '.codex').mkdir()
        (home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/gpt-6-astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump({'model_list': [{'model_name': 'chatgpt/gpt-6-astra'}]}))
        self.refresh.paths['catalog'].write_text('{"models": []}')
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': json.loads(MANIFEST.read_text())}
        self.originals = self.refresh.originals()

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            self.refresh.prepare()

    def test_failure_cannot_publish_and_rolls_back_exactly(self):
        self.prepare()
        self.refresh.activate()
        with patch('configure_claude.subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(ValueError, 'validation failed'):
                self.refresh.validate()
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_publish_requires_full_acceptance_and_converges(self):
        self.prepare()
        self.refresh.activate()
        self.assertEqual(self.refresh.paths['catalog'].read_text(), self.originals['catalog'])
        with patch('configure_claude.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.refresh.validate()
            commands = [c.args[0] for c in run.call_args_list]
            self.assertIn('verify_claude.py', ' '.join(commands[0]))
            self.assertNotIn('--api-only', commands[0])
            self.assertIn('verify_shared_search.py', ' '.join(commands[1]))
        self.refresh.finish()
        self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
        with patch.object(self.refresh, 'discover', return_value=self.snapshot):
            self.assertFalse(self.refresh.prepare()['any_changes'])

    def test_concurrent_edits_are_not_overwritten_by_recovery(self):
        self.prepare()
        self.refresh.activate()
        self.refresh.paths['catalog'].write_text('concurrent edit')
        with self.assertRaisesRegex(ValueError, 'Concurrent edit'):
            self.refresh.rollback()


if __name__ == '__main__':
    unittest.main()
