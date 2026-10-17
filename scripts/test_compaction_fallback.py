"""Compaction recovery: routing, atomic summaries, limits, and cancellation."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'

from claude_context import REQUEST
from compaction_fallback import (AttemptError, CompactionResponses, assess_size, candidates,
                                 declare_historical_search, fallback_request, fits, portable_input, plain)


def row(name, priority=1, **extra):
    return {'slug': name, 'visibility': 'list', 'priority': priority, 'context_window': 1000000,
            'input_modalities': ['text', 'image'], **extra}


def route(name, provider=None, endpoint=None):
    return {'model_name': name, 'litellm_params': {'model': provider or name, 'api_base': endpoint}}


def result(text='COMPLETE SUMMARY'):
    return {'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'fixture', 'output': [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}]}


async def complete(text='COMPLETE SUMMARY'):
    response = result(text)
    yield {'type': 'response.created', 'response': response | {'status': 'in_progress', 'output': []}}
    yield {'type': 'response.output_item.done', 'output_index': 0, 'item': response['output'][0]}
    yield {'type': 'response.completed', 'response': response}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.catalog = Path(self.directory.name) / 'catalog.json'
        self.rows = [row('first/model', comp_hash='1'), row('first/second', 2, comp_hash='1'),
                     row('other/model', 3), row('last/model', 4)]
        self.catalog.write_text(json.dumps({'models': self.rows}))
        self.router = SimpleNamespace(model_list=[route(r['slug']) for r in self.rows])
        self.calls = []
        self.token = REQUEST.set({'caller': 'authenticated-fixture', 'headers': {
            'x-codex-turn-metadata': json.dumps({'request_kind': 'compaction'})}})
        self.addCleanup(REQUEST.reset, self.token)
        self.request = {'model': 'first/model', 'input': [{'role': 'user', 'content': 'Remember MARKER.'}],
                        'instructions': 'Summarize the task.', 'stream': True}

    def integration(self, backends, **kwargs):
        async def dispatch(data, route_type, **unused):
            self.calls.append(copy.deepcopy(data))
            async def execute():
                action = backends[data['model']]
                if isinstance(action, Exception):
                    raise action
                return action()
            return execute()
        return CompactionResponses(dispatch, self.catalog, **kwargs)

    async def events(self, integration, request=None):
        call = await integration.route(request or self.request, 'aresponses', llm_router=self.router)
        return [plain(e) async for e in await call]

    async def test_success_never_tries_fallbacks(self):
        events = await self.events(self.integration({'first/model': complete}))
        self.assertEqual([c['model'] for c in self.calls], ['first/model'])
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(events[-1]['response']['metadata']['compaction_model'], 'first/model')

    async def test_image_heavy_search_history_reaches_unknown_tokenizer_provider(self):
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 300000}
        request = self.request | {'input': [
            {'role': 'user', 'content': 'Remember MARKER. ' + 'History. ' * 50000},
            {'type': 'web_search_call', 'id': 'ws_native', 'status': 'completed',
             'action': {'type': 'search', 'query': 'readable transparent cards'}},
            {'type': 'function_call_output', 'call_id': 'images', 'output': [image] * 35}]}
        self.rows[2]['context_window'] = 1048576
        self.catalog.write_text(json.dumps({'models': self.rows}))
        with self.assertLogs('compaction_fallback', level='WARNING') as logs:
            events = await self.events(self.integration({'first/model': AttemptError('UPSTREAM'),
                                                        'other/model': complete}), request)
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'other/model'])
        portable = self.calls[-1]['input'][0]['content']
        self.assertEqual(sum(part.get('type') == 'input_image' for part in portable), 35)
        self.assertTrue(any('ws_native' in p.get('text', '') for p in portable))
        sizing = json.loads(events[-1]['response']['metadata']['compaction_sizing'])
        self.assertEqual(sizing['size_decision'], 'unknown')
        self.assertEqual(sizing['image_count'], 35)
        self.assertLess(sizing['estimated_tokens'], 600000)
        self.assertNotIn('data:image', '\n'.join(logs.output))
        self.assertNotIn('readable transparent cards', '\n'.join(logs.output))

    async def test_provider_context_rejection_continues_to_next_candidate(self):
        events = await self.events(self.integration({'first/model': AttemptError('FIRST'),
            'other/model': AttemptError('Too large', code='context_length_exceeded', status=400),
            'last/model': complete}))
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'other/model', 'last/model'])
        self.assertEqual(events[-1]['type'], 'response.completed')

    async def test_exhaustion_records_provider_context_limit(self):
        self.rows = self.rows[:3]
        self.catalog.write_text(json.dumps({'models': self.rows}))
        events = await self.events(self.integration({'first/model': AttemptError('ORIGINAL'),
            'other/model': AttemptError('Too large', code='context_length_exceeded', status=400),
            'first/second': AttemptError('Unavailable')}))
        response = events[-1]['response']
        self.assertEqual(response['error']['message'], 'ORIGINAL')
        attempts = json.loads(response['metadata']['compaction_attempts'])
        self.assertEqual(attempts[1]['outcome'], 'provider_context_limit')
        self.assertEqual(attempts[1]['size_decision'], 'unknown')

    async def test_partial_failure_is_discarded_before_other_provider_recovers(self):
        async def interrupted():
            yield {'type': 'response.output_text.delta', 'delta': 'SECRET PARTIAL SUMMARY'}
            raise AttemptError('response protection is unavailable')
        events = await self.events(self.integration({'first/model': interrupted, 'other/model': complete}))
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'other/model'])
        self.assertNotIn('SECRET PARTIAL', json.dumps(events))
        self.assertEqual(sum(e['type'] == 'response.completed' for e in events), 1)
        portable = self.calls[-1]
        self.assertIn('MARKER', json.dumps(portable['input']))
        self.assertEqual(portable['tools'], [])
        self.assertEqual(portable['tool_choice'], 'none')
        for call in self.calls:
            self.assertEqual(call['num_retries'], 0)
            self.assertEqual(call['max_fallbacks'], 0)
            self.assertTrue(call['disable_fallbacks'])
            self.assertEqual(call['fallbacks'], [])

    async def test_exhaustion_keeps_original_error_and_attempt_outcomes(self):
        errors = {r['slug']: AttemptError('ORIGINAL' if i == 0 else 'LATER') for i, r in enumerate(self.rows)}
        events = await self.events(self.integration(errors))
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'other/model', 'last/model', 'first/second'])
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertEqual(events[-1]['response']['error']['message'], 'ORIGINAL')
        self.assertEqual(len(json.loads(events[-1]['response']['metadata']['compaction_attempts'])), 4)
        self.assertFalse(any(e['type'] == 'response.completed' for e in events))

    async def test_ordinary_reviewer_disabled_and_invalid_metadata_bypass(self):
        for header, model, enabled in (('compaction', 'codex-auto-review', True), ('chat', 'first/model', True),
                                      ('broken', 'first/model', True), ('compaction', 'first/model', False)):
            if header == 'broken':
                metadata = '['
            else:
                metadata = json.dumps({'request_kind': header})
            token = REQUEST.set({'headers': {'x-codex-turn-metadata': metadata}})
            try:
                integration = self.integration({model: complete}, enabled=enabled)
                request = self.request | {'model': model}
                await self.events(integration, request)
                self.assertEqual(self.calls[-1], request)
            finally:
                REQUEST.reset(token)

    async def test_canonical_client_metadata_overrides_compatibility_header(self):
        token = REQUEST.set({'caller': 'fixture', 'headers': {
            'x-codex-turn-metadata': '{"request_kind":"turn"}'}, 'request': {
            'client_metadata': {'x-codex-turn-metadata': '{"request_kind":"compaction"}'}}})
        try:
            events = await self.events(self.integration({'first/model': AttemptError('FIRST'), 'other/model': complete}))
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(events[-1]['type'], 'response.completed')
        finally:
            REQUEST.reset(token)

    async def test_proxy_fields_do_not_enter_provider_validation(self):
        from claude_context import validate_request, codex_turn_metadata
        raw = self.request | {'client_metadata': {'x-codex-turn-metadata': '{"request_kind":"compaction"}'}}
        token = REQUEST.set(REQUEST.get() | {'request': raw})
        checked = []
        async def dispatch(data, route_type, **kwargs):
            async def execute():
                validate_request()
                checked.append(codex_turn_metadata()['request_kind'])
                return complete()
            return execute()
        try:
            integration = CompactionResponses(dispatch, self.catalog)
            request = self.request | {'litellm_metadata': {'internal': True}, 'proxy_server_request': {'internal': True}}
            await self.events(integration, request)
            self.assertEqual(checked, ['compaction'])
        finally:
            REQUEST.reset(token)

    async def test_authentication_and_primary_invalid_request_stop(self):
        for status in (401, 403, 400):
            self.calls.clear()
            events = await self.events(self.integration({'first/model': AttemptError('DENIED', status=status)}))
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(events[-1]['response']['error']['message'], 'DENIED')

    async def test_unknown_size_reaches_provider_and_candidate_rejection_continues(self):
        self.rows[1]['context_window'] = 100
        self.catalog.write_text(json.dumps({'models': self.rows}))
        events = await self.events(self.integration({'first/model': AttemptError('FIRST'),
            'other/model': AttemptError('UNSUPPORTED', status=400), 'last/model': complete}))
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'other/model', 'last/model'])
        self.assertEqual(events[-1]['type'], 'response.completed')

    async def test_opaque_history_skips_cross_provider_but_compatible_provider_can_recover(self):
        request = self.request | {'input': [{'type': 'compaction', 'encrypted_content': 'OPAQUE'}]}
        events = await self.events(self.integration({'first/model': AttemptError('FIRST'),
            'first/second': complete}), request)
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'first/second'])
        self.assertEqual(self.calls[-1]['input'], request['input'])
        self.assertEqual(events[-1]['type'], 'response.completed')

    async def test_candidate_access_denial_skips_inference(self):
        self.rows[1]['context_window'] = 100
        self.catalog.write_text(json.dumps({'models': self.rows}))
        from compaction_fallback import UnusableHistory
        async def authorize(model, kwargs):
            if model == 'other/model':
                raise UnusableHistory('model_access_denied')
        integration = self.integration({'first/model': AttemptError('FIRST'), 'last/model': complete}, authorize=authorize)
        await self.events(integration)
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'last/model'])

    async def test_known_text_overflow_is_recorded_without_provider_attempt(self):
        self.rows[2]['context_window'] = 100
        self.catalog.write_text(json.dumps({'models': self.rows}))
        with patch.dict('litellm.model_cost', {'other/model': {}}), \
                patch('litellm.token_counter', return_value=1000), \
                self.assertLogs('compaction_fallback', level='WARNING') as logs:
            events = await self.events(self.integration({'first/model': AttemptError('FIRST'),
                                                        'last/model': complete}))
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'last/model'])
        self.assertEqual(events[-1]['type'], 'response.completed')
        rejected = json.loads(logs.output[1].partition('compaction_recovery ')[2])
        self.assertEqual(rejected['outcome'], 'context_limit')
        self.assertEqual(rejected['size_decision'], 'over_limit')

    async def test_timeout_keepalive_and_stream_cleanup(self):
        closed = asyncio.Event()
        async def stalled():
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.set()
        integration = self.integration({'first/model': stalled, 'other/model': complete}, timeout=.025, keepalive=.005)
        call = await integration.route(self.request, 'aresponses', llm_router=self.router)
        events = [e async for e in await call]
        self.assertTrue(any(isinstance(e, bytes) and e.startswith(b':') for e in events))
        self.assertTrue(closed.is_set())
        self.assertEqual(plain(events[-1])['type'], 'response.completed')

    async def test_disconnect_cancels_active_attempt_without_fallback(self):
        closed = asyncio.Event()
        async def stalled():
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.set()
        integration = self.integration({'first/model': stalled}, keepalive=.005)
        call = await integration.route(self.request, 'aresponses', llm_router=self.router)
        stream = await call
        await stream.__anext__()  # Keepalive while the first attempt runs.
        await stream.aclose()
        self.assertTrue(closed.is_set())
        self.assertEqual(len(self.calls), 1)

    async def test_empty_and_incomplete_streams_recover(self):
        async def empty():
            yield {'type': 'response.output_text.delta', 'delta': 'INCOMPLETE'}
        async def incomplete():
            yield {'type': 'response.incomplete', 'response': {'status': 'incomplete'}}
        for backend in (empty, incomplete):
            self.calls.clear()
            events = await self.events(self.integration({'first/model': backend, 'other/model': complete}))
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(events[-1]['type'], 'response.completed')

    async def test_catalog_is_reread_and_missing_catalog_still_attempts_original(self):
        integration = self.integration({'first/model': AttemptError('FIRST'), 'last/model': complete})
        self.catalog.write_text(json.dumps({'models': [self.rows[0], self.rows[-1]]}))
        await self.events(integration)
        self.assertEqual([c['model'] for c in self.calls], ['first/model', 'last/model'])
        self.calls.clear()
        self.catalog.unlink()
        events = await self.events(integration)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(events[-1]['response']['error']['message'], 'FIRST')

    async def test_nonstreaming_summary_and_metadata_only_logs(self):
        integration = self.integration({'first/model': AttemptError('PRIVATE transcript'), 'other/model': complete})
        with self.assertLogs('compaction_fallback', level='WARNING') as logs:
            call = await integration.route(self.request | {'stream': False}, 'aresponses', llm_router=self.router)
            response = plain(await call)
        self.assertEqual(response['status'], 'completed')
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertNotIn('MARKER', '\n'.join(logs.output))


class CatalogTests(unittest.TestCase):
    def test_historical_search_declaration_is_specific_to_chatgpt_native_history(self):
        original, available = candidates({'models': [row('chatgpt/a', comp_hash='1'),
                                                    row('chatgpt/b', comp_hash='1'), row('other/c')]},
                                         [route('chatgpt/a'), route('chatgpt/b'), route('other/c')], 'chatgpt/a')
        request = {'model': 'chatgpt/a', 'tools': [], 'input': [
            {'type': 'web_search_call', 'id': 'ws_historical', 'status': 'completed',
             'action': {'type': 'search', 'query': 'Historical query'}}]}
        routed = declare_historical_search(request, original)
        self.assertEqual(routed['tools'], [{'type': 'web_search'}])
        self.assertEqual(routed['tool_choice'], 'none')
        self.assertFalse(routed['parallel_tool_calls'])
        self.assertEqual(request['tools'], [])
        self.assertEqual(declare_historical_search(routed, original), routed)
        for target in available:
            fallback = declare_historical_search(fallback_request(request, original, target), target)
            self.assertEqual(bool(fallback['tools']), target.model == 'chatgpt/b')
        ordinary = request | {'input': [{'role': 'user', 'content': 'No search history'}]}
        self.assertIs(declare_historical_search(ordinary, original), ordinary)
        self.assertIs(declare_historical_search(request, None), request)

    def test_priority_stability_hidden_inactive_duplicates_and_distinct_backends(self):
        rows = [row('first/original', 1), row('first/alternate', 2), row('other/b', 9), row('other/a', 9),
                row('bedrock/same-maker', 5), row('alias/duplicate', 3), row('hidden', 0, visibility='hide'),
                row('inactive', 0), row('codex-auto-review', 0)]
        routes = [route(r['slug']) for r in rows if r['slug'] != 'inactive']
        routes[5] = route('alias/duplicate', 'other/b')
        routes[4] = route('bedrock/same-maker', 'bedrock/first-model')
        _, order = candidates({'models': rows}, routes, 'first/original')
        # A duplicate route adopts its earliest picker position, but is attempted only once.
        self.assertEqual([c.model for c in order], ['alias/duplicate', 'bedrock/same-maker', 'other/a', 'first/alternate'])

    def test_common_openai_transport_with_distinct_endpoints_has_distinct_groups(self):
        rows = [row('one'), row('two')]
        routes = [route('one', 'openai/model', 'http://local/v1'), route('two', 'openai/model', 'https://remote/v1')]
        original, available = candidates({'models': rows}, routes, 'one')
        self.assertNotEqual(original.group, available[0].group)

    def test_url_credentials_are_excluded_and_default_ports_are_normalized(self):
        rows = [row('one'), row('two')]
        routes = [route('one', 'openai/model', 'https://user:secret@example.com/v1?api_key=secret'),
                  route('two', 'openai/model', 'https://example.com:443/v1')]
        original, available = candidates({'models': rows}, routes, 'one')
        self.assertEqual(available, [])
        self.assertNotIn('secret', repr(original.group))

    def test_readable_tools_images_and_opaque_state(self):
        source = {'input': [{'role': 'user', 'content': 'Goal'},
            {'type': 'additional_tools', 'tools': [{'name': 'shell'}]},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'shell', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'Result'},
            {'type': 'reasoning', 'encrypted_content': 'OPAQUE', 'summary': [{'text': 'Finding'}]}]}
        portable = portable_input(source)
        encoded = json.dumps(portable)
        for retained in ('Goal', 'call_1', 'Result', 'Finding'):
            self.assertIn(retained, encoded)
        self.assertNotIn('OPAQUE', encoded)
        image = {'type': 'input_image', 'image_url': 'https://example.com/image'}
        multimodal = portable_input({'input': [{'type': 'function_call_output', 'call_id': 'call_1', 'output': [image]}]})
        self.assertEqual(multimodal[0]['content'][-1], image)
        with self.assertRaisesRegex(ValueError, 'opaque_compaction'):
            portable_input({'input': [{'type': 'compaction', 'encrypted_content': 'OPAQUE'}]})
        with self.assertRaisesRegex(ValueError, 'unsupported_history'):
            portable_input({'input': [{'type': 'unknown'}]})

    def test_limits_images_and_compatible_history_are_preserved(self):
        original, available = candidates({'models': [row('first/a', comp_hash='1'), row('first/b', comp_hash='1')]},
                                         [route('first/a'), route('first/b')], 'first/a')
        request = {'input': [{'type': 'reasoning', 'encrypted_content': 'PRESERVED'}]}
        routed = fallback_request(request, original, available[0])
        self.assertEqual(routed['input'], request['input'])
        target = available[0]
        target.metadata['input_modalities'] = ['text']
        image = {'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.com/image'}]}]}
        self.assertEqual(fits(image, target), (False, 'unsupported_modality'))
        target.metadata['context_window'] = 9000
        self.assertEqual(fits({'input': 'X' * 10000}, target), (True, None))

    def test_encoded_image_length_does_not_change_readable_size(self):
        target = candidates({'models': [row('other/model')]}, [route('other/model')], 'other/model')[0]
        small = {'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Describe the image'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'}]}]}
        large = copy.deepcopy(small)
        large['input'][0]['content'][1]['image_url'] += 'A' * 1200000
        before, after = assess_size(small, target), assess_size(large, target)
        self.assertEqual(before, after)
        self.assertEqual(after.size_decision, 'unknown')
        self.assertEqual(after.image_count, 1)

    def test_native_tool_images_and_encrypted_state_are_not_counted_as_text(self):
        target = candidates({'models': [row('other/model')]}, [route('other/model')], 'other/model')[0]
        request = {'input': [
            {'type': 'reasoning', 'encrypted_content': 'PRIVATE' * 200000,
             'summary': [{'text': 'Readable finding'}]},
            {'type': 'function_call_output', 'call_id': 'img', 'output': [
                {'type': 'input_image', 'image_url': 'https://example.invalid/private-image'}]}]}
        size = assess_size(request, target)
        self.assertLess(size.estimated_tokens, 1000)
        self.assertEqual(size.image_count, 1)
        self.assertEqual(size.size_decision, 'unknown')
        target.metadata['input_modalities'] = ['text']
        self.assertEqual(fits(request, target), (False, 'unsupported_modality'))

    def test_known_route_tokenizer_rejects_real_text_overflow(self):
        target = candidates({'models': [row('picker/alias', context_window=9000)]},
                            [route('picker/alias', 'openai/fixture-native')], 'picker/alias')[0]
        with patch.dict('litellm.model_cost', {'fixture-native': {}}), \
                patch('litellm.token_counter', return_value=10000) as counter:
            size = assess_size({'input': 'Oversized text'}, target)
        counter.assert_called_once()
        self.assertEqual(counter.call_args.kwargs['model'], 'fixture-native')
        self.assertEqual(size.size_decision, 'over_limit')
        self.assertEqual(size.rejection, 'context_limit')

    def test_missing_window_and_tokenizer_failures_defer_to_provider(self):
        target = candidates({'models': [row('other/model')]}, [route('other/model')], 'other/model')[0]
        target.metadata.pop('context_window')
        self.assertEqual(assess_size({'input': 'History'}, target).size_decision, 'unknown')
        target.metadata['context_window'] = 100
        with patch.dict('litellm.model_cost', {'other/model': {}}), \
                patch('litellm.token_counter', side_effect=ValueError('Tokenizer unavailable')):
            size = assess_size({'input': 'History' * 10000}, target)
        self.assertEqual(size.size_decision, 'unknown')
        self.assertIsNone(size.rejection)


if __name__ == '__main__':
    unittest.main()
