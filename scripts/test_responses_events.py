"""Responses lifecycle invariants independent of the inference provider."""
import unittest

from responses_events import ResponsesEvents, normalize_item


def plain(events):
    return [e.model_dump(exclude_none=True) for e in events]


def message(text='ANSWER', identity='msg_fixture'):
    return {'id': identity, 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


class EventsTests(unittest.TestCase):
    def test_raw_reasoning_is_preserved_without_becoming_display_feedback(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_kimi', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'required replay state', 'annotations': []}]}
        normalized = normalize_item(reasoning)
        self.assertEqual(normalized['content'], reasoning['content'])
        self.assertNotIn('role', normalized)
        events = plain(ResponsesEvents('modal/kimi').complete(
            {'status': 'completed', 'output': [reasoning]}))
        self.assertEqual(events[-1]['response']['output'][0]['content'], reasoning['content'])
        self.assertFalse(any(e['type'] in ('response.output_text.delta', 'response.reasoning_summary_text.delta')
                             for e in events))

    def test_reasoning_content_is_not_answer_text_and_opaque_state_is_not_displayed(self):
        renderer = ResponsesEvents('fixture')
        reasoning = {'id': 'rs_fixture', 'type': 'reasoning', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'NOT_FOR_DISPLAY'}],
                     'summary': [{'type': 'summary_text', 'text': 'Public summary'}], 'encrypted_content': 'OPAQUE'}
        events = plain([renderer.created(), *renderer.complete({'status': 'completed', 'output': [reasoning, message()]})])
        self.assertFalse(any(e.get('delta') in ('NOT_FOR_DISPLAY', 'OPAQUE') for e in events))
        self.assertEqual([e['delta'] for e in events if e['type'] == 'response.output_text.delta'], ['ANSWER'])
        self.assertEqual([e['delta'] for e in events if e['type'] == 'response.reasoning_summary_text.delta'], ['Public summary'])
        done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        self.assertEqual(done, events[-1]['response']['output'])
        self.assertEqual(done[0]['encrypted_content'], 'OPAQUE')
        self.assertNotIn('content', done[0])
        self.assertNotIn('encrypted_content', next(e['item'] for e in events if e['type'] == 'response.output_item.added'))

    def test_late_summary_keeps_existing_indexes_and_completed_item_order(self):
        renderer = ResponsesEvents('fixture')
        events = [renderer.created(), *renderer.delta('msg_fixture', 'message', 'ANSWER'),
                  *renderer.delta('rs_fixture', 'reasoning', 'Summary')]
        events += list(renderer.complete({'status': 'completed', 'output': [
            {'id': 'rs_fixture', 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Summary'}]}, message()]}))
        events = plain(events)
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
        self.assertEqual([e['output_index'] for e in events if e['type'] == 'response.output_item.done'], [0, 1])
        self.assertEqual(events[-1]['response']['output'], [e['item'] for e in events if e['type'] == 'response.output_item.done'])
        active = set()
        for event in events:
            if event['type'] == 'response.output_item.added': active.add(event['item']['id'])
            if event['type'].endswith('.delta'): self.assertIn(event['item_id'], active)

    def test_parallel_function_and_custom_calls_are_complete_before_publication(self):
        renderer = ResponsesEvents('fixture')
        calls = [{'id': 'fc_fixture', 'type': 'function_call', 'name': 'probe', 'call_id': 'call_function', 'arguments': '{"x":1}'},
                 {'id': 'ct_fixture', 'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'call_custom', 'input': 'text("OK")'}]
        events = plain(renderer.complete({'status': 'completed', 'output': calls}))
        starts = [e['item'] for e in events if e['type'] == 'response.output_item.added']
        self.assertEqual(starts[0]['arguments'], '')
        self.assertEqual(starts[1]['input'], '')
        self.assertEqual([e['delta'] for e in events if e['type'].endswith('.delta')], ['{"x":1}', 'text("OK")'])
        self.assertEqual(events[-1]['response']['output'], calls)

    def test_broken_batches_are_rejected_instead_of_rewriting_published_text(self):
        for mode in ('missing', 'duplicate', 'text', 'hole'):
            renderer = ResponsesEvents('fixture')
            list(renderer.delta('msg_fixture', 'message', 'ANSWER'))
            output = [] if mode == 'missing' else [message()]
            if mode == 'duplicate': output *= 2
            if mode == 'text': output = [message('CHANGED')]
            if mode == 'hole': renderer.reserve(['missing_reservation'])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                list(renderer.complete({'status': 'completed', 'output': output}))

    def test_none_ids_are_assigned_once_and_content_parts_keep_their_indexes(self):
        renderer = ResponsesEvents('fixture')
        item = message(identity=None)
        item['content'].append({'type': 'output_text', 'text': 'SECOND', 'annotations': []})
        events = plain(renderer.complete({'status': 'completed', 'output': [item]}))
        final_id = events[-1]['response']['output'][0]['id']
        self.assertTrue(final_id)
        self.assertTrue(all(e.get('item_id', final_id) == final_id for e in events))
        self.assertEqual([e['content_index'] for e in events if e['type'] == 'response.output_text.delta'], [0, 1])

    def test_completed_search_items_get_typed_lifecycle_once(self):
        searches = [{'id': 'ws_done', 'type': 'web_search_call', 'status': 'completed',
                     'action': {'type': 'search', 'query': 'fixture', 'queries': ['fixture']}},
                    {'id': 'ws_failed', 'type': 'web_search_call', 'status': 'failed',
                     'action': {'type': 'open_page', 'url': 'https://example.com/missing'}}]
        renderer = ResponsesEvents('fixture')
        events = plain(renderer.complete({'status': 'completed', 'output': [message(), *searches]}))
        lifecycle = [(e['item_id'], e['type'].rpartition('.')[2]) for e in events if e['type'].startswith('response.web_search_call.')]
        self.assertEqual(lifecycle, [('ws_done', 'in_progress'), ('ws_done', 'searching'), ('ws_done', 'completed'),
                                     ('ws_failed', 'in_progress'), ('ws_failed', 'searching')])
        added = [e['item'] for e in events if e['type'] == 'response.output_item.added' and e['item']['type'] == 'web_search_call']
        self.assertEqual([i['status'] for i in added], ['in_progress', 'in_progress'])
        self.assertEqual(events[-1]['response']['output'][1:], searches)
        # Shared search starts and finishes its calls mid-stream; never repeat them.
        renderer = ResponsesEvents('fixture')
        list(renderer.start({'id': 'ws_done', 'type': 'web_search_call', 'status': 'in_progress'}))
        events = plain(renderer.complete({'status': 'completed', 'output': searches[:1]}))
        self.assertFalse(any(e['type'].startswith('response.web_search_call.') for e in events))


if __name__ == '__main__': unittest.main()
