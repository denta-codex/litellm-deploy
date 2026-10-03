"""Request identity shared with the provider, including LiteLLM's module loader."""
from contextvars import ContextVar
import hashlib
import json
import re
import time
import uuid

REQUEST = ContextVar('claude_request', default={})


class ResponsesErrorStream:
    """Translate pinned LiteLLM's bare SSE errors to terminal Responses events."""
    def __init__(self):
        self.buffer = b''
        self.response = None
        self.sequence = 0

    def feed(self, body, final=False):
        self.buffer += body
        output = bytearray()
        while match := re.search(rb'\r?\n\r?\n', self.buffer):
            frame, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
            output.extend(self.frame(frame))
        if final and self.buffer:
            output.extend(self.frame(self.buffer))
            self.buffer = b''
        return bytes(output)

    def frame(self, frame):
        data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b'data:'))
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return frame
        if not isinstance(event, dict):
            return frame
        sequence = event.get('sequence_number')
        if isinstance(sequence, int):
            self.sequence = max(self.sequence, sequence + 1)
        if isinstance(event.get('response'), dict):
            self.response = event['response']
        error = event.get('error')
        # Preserve native Responses errors and all successful events verbatim.
        if event.get('type') or not isinstance(error, dict):
            return frame
        code = error.get('code')
        if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
            code = {400: 'invalid_request_error', 401: 'authentication_error',
                    403: 'permission_denied', 429: 'rate_limit_exceeded'}.get(int(code), 'server_error')
        code = code or 'server_error'
        response = self.response or {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response',
                                     'created_at': int(time.time()), 'output': []}
        failed = {'type': 'response.failed', 'sequence_number': self.sequence,
                  'response': response | {'status': 'failed', 'error': {
                      'code': code, 'message': error.get('message') or 'Model provider request failed'}}}
        self.sequence += 1
        return ('event: response.failed\ndata: ' + json.dumps(failed) + '\n\n').encode()


class CaptureRequest:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get('headers', [])}
        # LiteLLM authenticates before dispatching the provider. Never persist the
        # credential itself or trust a caller-supplied account ID as authorization.
        credential = headers.get('authorization', '')
        context = {'caller': hashlib.sha256(credential.encode()).hexdigest() if credential else None,
                   'headers': {k: v for k, v in headers.items() if k in (
                       'thread-id', 'session-id', 'x-codex-turn-metadata', 'x-claude-chat-id')}}
        token = REQUEST.set(context)
        body = bytearray()
        errors = None
        async def responses_send(message):
            nonlocal errors
            if message['type'] == 'http.response.start':
                response_headers = dict(message.get('headers', []))
                if (scope.get('path') in ('/responses', '/v1/responses')
                        and message['status'] == 200
                        and response_headers.get(b'content-type', b'').startswith(b'text/event-stream')):
                    errors = ResponsesErrorStream()
                    message = message | {'headers': [(k, v) for k, v in message.get('headers', [])
                                                      if k.lower() != b'content-length']}
            elif message['type'] == 'http.response.body' and errors is not None:
                message = message | {'body': errors.feed(message.get('body', b''),
                                                         final=not message.get('more_body', False))}
            await send(message)

        async def capture_receive():
            message = await receive()
            if message['type'] == 'http.request':
                body.extend(message.get('body', b''))
                if not message.get('more_body', False):
                    try:
                        context['request'] = json.loads(body)
                    except ValueError:
                        pass  # The proxy supplies its ordinary JSON validation error.
                    body.clear()
            return message
        try:
            await self.app(scope, capture_receive, responses_send)
        finally:
            REQUEST.reset(token)


def identity(model, params):
    context = REQUEST.get()
    headers = context.get('headers', {})
    caller = context.get('caller')
    chat = headers.get('thread-id') or headers.get('session-id') or headers.get('x-claude-chat-id')
    if not caller or not chat:
        raise ValueError('Claude requires an authenticated request and thread-id or x-claude-chat-id')
    try:
        metadata = json.loads(headers.get('x-codex-turn-metadata', '{}'))
    except (TypeError, ValueError):
        raise ValueError('Invalid x-codex-turn-metadata') from None
    if not isinstance(metadata, dict):
        raise ValueError('Invalid x-codex-turn-metadata')
    return (caller, chat, str(metadata.get('context_window_id', 'default')), model,
            'compaction' if metadata.get('request_kind') == 'compaction' else 'chat')


def validate_request():
    """Reject unsupported controls before a lossy Responses→Chat conversion."""
    raw = REQUEST.get().get('request', {})
    allowed = {'model', 'input', 'messages', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls',
               'reasoning', 'reasoning_effort', 'text', 'response_format', 'stream', 'stream_options',
               'store', 'include', 'metadata', 'client_metadata', 'user', 'prompt_cache_key', 'service_tier'}
    unknown = {k for k, v in raw.items() if v is not None and k not in allowed}
    if unknown:
        raise ValueError('Unsupported Claude request fields: ' + ', '.join(sorted(unknown)))
    if raw.get('store') is True:
        raise ValueError('Claude does not provide server-side Responses retrieval')
    if raw.get('service_tier') not in (None, 'auto', 'default', 'priority'):
        raise ValueError('Claude service_tier must be auto, default, or priority')
    reasoning = raw.get('reasoning') or {}
    if set(reasoning) - {'effort', 'summary'} or reasoning.get('summary') not in (None, 'auto', 'concise', 'detailed'):
        raise ValueError('Unsupported Claude reasoning controls')
    if set(raw.get('text') or {}) - {'format'}:
        raise ValueError('Claude does not support text verbosity controls')
    return raw.get('service_tier') == 'priority'
