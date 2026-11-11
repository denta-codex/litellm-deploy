"""Typed Responses presentation shared by native Gemini, search and compaction.

Reasoning content is never answer text. Only an explicit summary is displayed;
opaque carriers remain opaque. One renderer owns IDs, indexes and sequence.
"""
import copy
import time
import uuid

from litellm.types.utils import BaseLiteLLMOpenAIResponseObject


def normalize_item(item):
    item = copy.deepcopy(item)
    if not item.get('id'):
        item['id'] = 'item_' + uuid.uuid4().hex
    if item.get('type') == 'reasoning':
        item.pop('content', None)
        item.pop('role', None)
        item.setdefault('summary', [])
    return item


class ResponsesEvents:
    def __init__(self, model):
        self.initial = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response',
                        'created_at': int(time.time()), 'model': model,
                        'status': 'in_progress', 'output': []}
        self.sequence = 0
        self.indexes = {}
        self.started = set()
        self.deltas = {}
        self.parts = set()

    def event(self, kind, **fields):
        value = BaseLiteLLMOpenAIResponseObject(type=kind, sequence_number=self.sequence, **fields)
        self.sequence += 1
        return value

    def created(self):
        return self.event('response.created', response=self.initial)

    def reserve(self, ids):
        for item_id in ids:
            self.indexes.setdefault(item_id, len(self.indexes))

    def start(self, item):
        item_id = item['id']
        self.reserve([item_id])
        if item_id not in self.started:
            self.started.add(item_id)
            skeleton = copy.deepcopy(item)
            if item['type'] == 'message':
                skeleton['content'] = []; skeleton['status'] = 'in_progress'
            elif item['type'] == 'reasoning':
                skeleton['summary'] = []; skeleton.pop('encrypted_content', None)
            elif item['type'] in ('function_call', 'custom_tool_call'):
                skeleton['input' if item['type'] == 'custom_tool_call' else 'arguments'] = ''
                skeleton['status'] = 'in_progress'
            yield self.event('response.output_item.added', output_index=self.indexes[item_id], item=skeleton)

    def delta(self, item_id, kind, text):
        if kind not in ('message', 'reasoning'):
            raise ValueError('Invalid model feedback item')
        item = {'id': item_id, 'type': kind}
        if kind == 'message':
            item.update(role='assistant', content=[])
        else:
            item['summary'] = []
        yield from self.start(item)
        fields = {'item_id': item_id, 'output_index': self.indexes[item_id]}
        summary = kind == 'reasoning'
        fields['summary_index' if summary else 'content_index'] = 0
        if item_id not in self.parts:
            self.parts.add(item_id)
            yield self.event('response.reasoning_summary_part.added' if summary else 'response.content_part.added',
                             **fields, part={'type': 'summary_text', 'text': ''} if summary else
                             {'type': 'output_text', 'text': '', 'annotations': []})
        self.deltas[item_id] = self.deltas.get(item_id, '') + text
        yield self.event('response.reasoning_summary_text.delta' if summary else 'response.output_text.delta',
                         **fields, delta=text)

    def finish_item(self, item):
        yield from self.start(item)
        item_id, kind = item['id'], item['type']
        index = self.indexes[item_id]
        if kind == 'reasoning':
            for si, part in enumerate(item.get('summary', [])):
                fields = {'item_id': item_id, 'output_index': index, 'summary_index': si}
                if item_id not in self.deltas:
                    yield self.event('response.reasoning_summary_part.added', **fields, part={'type': 'summary_text', 'text': ''})
                    yield self.event('response.reasoning_summary_text.delta', **fields, delta=part.get('text', ''))
                elif si != 0 or self.deltas[item_id] != part.get('text', ''):
                    raise ValueError('Streamed reasoning summary differs from completed output')
                yield self.event('response.reasoning_summary_text.done', **fields, text=part.get('text', ''))
                yield self.event('response.reasoning_summary_part.done', **fields, part=part)
        elif kind == 'message':
            for ci, part in enumerate(item.get('content', [])):
                if part.get('type') != 'output_text':
                    continue
                fields = {'item_id': item_id, 'output_index': index, 'content_index': ci}
                if item_id not in self.deltas:
                    yield self.event('response.content_part.added', **fields, part=part | {'text': '', 'annotations': []})
                    yield self.event('response.output_text.delta', **fields, delta=part['text'])
                elif ci != 0 or self.deltas[item_id] != part['text']:
                    raise ValueError('Streamed answer differs from completed output')
                for ai, annotation in enumerate(part.get('annotations', [])):
                    yield self.event('response.output_text.annotation.added', **fields, annotation_index=ai, annotation=annotation)
                yield self.event('response.output_text.done', **fields, text=part['text'])
                yield self.event('response.content_part.done', **fields, part=part)
        elif kind in ('function_call', 'custom_tool_call'):
            custom = kind == 'custom_tool_call'
            field = 'input' if custom else 'arguments'
            prefix = 'response.custom_tool_call_input' if custom else 'response.function_call_arguments'
            yield self.event(prefix + '.delta', output_index=index, item_id=item_id, delta=item.get(field, ''))
            yield self.event(prefix + '.done', output_index=index, item_id=item_id, **{field: item.get(field, '')})
        yield self.event('response.output_item.done', output_index=index, item=item)

    def complete(self, response):
        response = copy.deepcopy(response)
        output = [normalize_item(i) for i in response.get('output', [])]
        ids = [i['id'] for i in output]
        if len(ids) != len(set(ids)) or set(self.indexes) - set(ids):
            raise ValueError('Streamed items are missing or duplicated in completed output')
        # First provider content determines public ordering (a summary may
        # arrive after text). Reserve the remaining batch before emitting done.
        self.reserve(ids)
        output.sort(key=lambda i: self.indexes[i['id']])
        for item in output:
            yield from self.finish_item(item)
        # Reservations are allocated for public items only; a hole is a broken
        # search/model batch, not something to repair by renumbering deltas.
        if [self.indexes[i['id']] for i in output] != list(range(len(output))):
            raise ValueError('Responses output indexes are not contiguous')
        response.update(id=self.initial['id'], output=output)
        status = response.get('status', 'completed')
        yield self.event('response.completed' if status == 'completed' else 'response.incomplete', response=response)

    def failed(self, exc):
        from gemini_native import NativeStateError
        original = getattr(exc, 'original_exception', None) or exc
        # LiteLLM may wrap request/codec failures. Only our deliberately safe
        # diagnostics may escape that wrapper, never a raw provider body.
        pending, seen = [exc], set()
        while pending:
            error = pending.pop()
            if id(error) in seen: continue
            seen.add(id(error))
            if isinstance(error, NativeStateError):
                original = error
                break
            pending.extend(e for e in (getattr(error, 'original_exception', None),
                                       error.__cause__, error.__context__) if isinstance(e, BaseException))
        status = getattr(original, 'status_code', 500)
        code = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_denied',
                408: 'request_timeout', 429: 'rate_limit_exceeded'}.get(status, 'server_error')
        # Adapter ValueErrors have deliberately non-secret diagnostics. Provider
        # exceptions can include raw request bodies, endpoints or credentials.
        message = str(original) if isinstance(original, NativeStateError) else str(exc) if isinstance(exc, ValueError) else 'Model provider request failed'
        return self.event('response.failed', response=self.initial | {'status': 'failed',
                          'error': {'code': code, 'message': message}})
