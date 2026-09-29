"""Request identity shared with the provider, including LiteLLM's module loader."""
from contextvars import ContextVar
import hashlib
import json

REQUEST = ContextVar('claude_request', default={})


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
            await self.app(scope, capture_receive, send)
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
    if raw.get('service_tier') not in (None, 'auto', 'default'):
        raise ValueError('Claude does not support service_tier selection')
    reasoning = raw.get('reasoning') or {}
    if set(reasoning) - {'effort', 'summary'} or reasoning.get('summary') not in (None, 'auto', 'concise', 'detailed'):
        raise ValueError('Unsupported Claude reasoning controls')
    if set(raw.get('text') or {}) - {'format'}:
        raise ValueError('Claude does not support text verbosity controls')
