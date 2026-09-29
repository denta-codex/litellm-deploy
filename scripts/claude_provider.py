"""Subscription-backed Claude CustomLLM provider for the pinned LiteLLM router."""
import asyncio
from contextlib import asynccontextmanager
import contextvars
import json
import os
from pathlib import Path
import time
import uuid

from litellm import CustomLLM, ModelResponse
from litellm.types.utils import ModelResponseStream
from litellm.llms.custom_llm import CustomLLMError

from claude_context import CaptureRequest, identity, validate_request
from claude_session import MODEL, Session, configuration, content_blocks, digest, history_prompt, unresolved_calls

# Each idle worker is a Claude CLI process (~110 MiB). Evict the least recently
# used idle worker beyond this; busy workers are never evicted.
MAX_WORKERS = 20


def chunk(text='', *, tools=None, reasoning=None, finish=None, usage=None):
    value = {'text': text, 'tool_use': tools, 'is_finished': finish is not None,
             'finish_reason': finish, 'usage': usage, 'index': 0}
    if reasoning is not None:
        value['provider_specific_fields'] = {'reasoning_content': reasoning}
    return value


def usage_counts(usage):
    if not usage:
        return None
    # Claude's input_tokens excludes cache reads/writes. All three are input.
    if 'input_tokens' not in usage or 'output_tokens' not in usage:
        return None
    prompt = usage['input_tokens'] + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
    return {'prompt_tokens': prompt, 'completion_tokens': usage['output_tokens'],
            'total_tokens': prompt + usage['output_tokens'],
            'prompt_tokens_details': {'cached_tokens': usage.get('cache_read_input_tokens', 0)}}


class ClaudeProvider(CustomLLM):
    def __init__(self, state=None, session_factory=Session):
        super().__init__()
        self.state = Path(state or os.environ.get('CLAUDE_ADAPTER_STATE', Path.home() / '.local/state/litellm/claude'))
        self.session_factory = session_factory
        self.sessions = {}  # Least recently used first.
        self.locks = {}
        self.cleanups = {}

    async def close(self):
        for key, session in list(self.sessions.items()):
            self.retire(key, session)
        await asyncio.gather(*list(self.cleanups.values()), return_exceptions=True)
        self.sessions.clear()
        self.locks.clear()

    def retire(self, key, session):
        """Unregister now; close in a task the request's cancellation cannot interrupt."""
        if self.sessions.get(key) is session:
            del self.sessions[key]
            lock = self.locks.get(key)
            if lock and not lock.locked():
                del self.locks[key]
        task = self.cleanups.get(session)
        if task is None:
            task = asyncio.create_task(session.close(), context=contextvars.Context())
            self.cleanups[session] = task

            def forget(done):
                self.cleanups.pop(session, None)
                if not done.cancelled():
                    done.exception()  # Mark retrieved; nobody awaits evictions.
            task.add_done_callback(forget)
        return task

    def make_room(self, key):
        for other in list(self.sessions):
            if len(self.sessions) - (key in self.sessions) < MAX_WORKERS:
                break
            lock = self.locks.get(other)
            if other != key and not (lock and lock.locked()):
                self.retire(other, self.sessions[other])

    async def reconcile(self, key, messages, config):
        session = self.sessions.get(key)
        if session and session.closed:
            session = None
        if session:
            suffix = messages[len(session.history):] if messages[:len(session.history)] == session.history else None
            new_user = suffix is not None and any(m['role'] == 'user' for m in suffix)
            replace = config != session.config or suffix is None or (new_user and not session.idle.is_set())
            if replace:
                if unresolved_calls(messages):
                    raise CustomLLMError(409, 'Claude cannot replace a session with unresolved tool execution; submit results or explicit aborts')
                await asyncio.shield(self.retire(key, session))
                session = None
        if session is None:
            if unresolved_calls(messages):
                raise CustomLLMError(409, 'Claude session unavailable with unresolved tool execution; refusing to replay tools')
            self.make_room(key)
            session = self.session_factory(key, config, self.state)
            self.sessions[key] = session
            session.history = messages
            await session.start(history_prompt(messages))
            return session, None

        outputs = [m for m in messages if m['role'] == 'tool']
        fresh = [m for m in outputs if m.get('tool_call_id') in session.pending
                 and m['tool_call_id'] not in session.delivered]
        for result in outputs:
            call_id = result.get('tool_call_id')
            if call_id in session.pending or call_id in session.delivered:
                session.deliver(call_id, result.get('content', ''))
        waiting = [v['spec'] for k, v in session.pending.items() if k not in session.delivered]
        if waiting:
            session.history = messages
            session.save()
            return session, waiting
        if not fresh:
            users = [m for m in (suffix or []) if m['role'] == 'user']
            if not users:
                raise CustomLLMError(409, 'Claude request has no new user input or pending tool result')
            await session.submit([b for m in users for b in content_blocks(m.get('content'))])
        session.history = messages
        session.save()
        return session, None

    async def _chunks(self, model, messages, **kwargs):
        if model != MODEL:
            raise CustomLLMError(400, 'Only ' + MODEL + ' is configured')
        params = kwargs.get('optional_params', {})
        try:
            validate_request()
            config = configuration(messages, params)
            key = identity(model, params)
        except (ValueError, KeyError, TypeError) as exc:
            raise CustomLLMError(400, str(exc)) from None
        request_id = digest([messages, config])
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            session = self.sessions.get(key)
            if session:
                self.sessions[key] = self.sessions.pop(key)  # Mark most recently used.
            if session and request_id in session.response_cache:
                for item in session.response_cache[request_id]:
                    yield item
                return
            # Completed HTTP responses can be replayed after a proxy restart.
            if session is None:
                journal = self.state / 'journals' / (digest(key) + '.json')
                if journal.exists():
                    saved = json.loads(journal.read_text())
                    cached = saved.get('responses', {}).get(request_id)
                    if cached is not None:
                        for item in cached:
                            yield item
                        return
            normal_boundary = False
            emitted = []
            try:
                session, waiting = await self.reconcile(key, messages, config)
                while True:
                    replaying_pending = waiting is not None
                    kind, data = ('tools', waiting) if waiting is not None else await session.queue.get()
                    waiting = None
                    items = []
                    if kind == 'text':
                        items = [chunk(data)]
                    elif kind == 'reasoning':
                        items = [chunk(reasoning=data)]
                    elif kind == 'tools':
                        for index, call in enumerate(data):
                            items.append(chunk(tools={'index': index, 'id': call['id'], 'type': 'function',
                                                      'function': {'name': call['name'], 'arguments': call['arguments']}}))
                        boundary_usage = {'input_tokens': 0, 'output_tokens': 0} if replaying_pending else session.boundary_usage
                        items.append(chunk(finish='tool_calls', usage=usage_counts(boundary_usage)))
                        normal_boundary = True
                    elif kind == 'done':
                        items = [chunk(finish='stop', usage=usage_counts(data))]
                        normal_boundary = True
                    elif kind == 'error':
                        raise data
                    for item in items:
                        emitted.append(item)
                    if normal_boundary:
                        # Persist the terminal boundary before exposing it to the client.
                        session.response_cache[request_id] = emitted
                        while len(session.response_cache) > 16:
                            del session.response_cache[next(iter(session.response_cache))]
                        if kind == 'done':
                            session.pending.clear()
                        session.save()
                    for item in items:
                        yield item
                    if normal_boundary:
                        return
            except ValueError as exc:
                raise CustomLLMError(400, str(exc)) from None
            finally:
                if session is not None and not normal_boundary:
                    # Stop or failure: the next request must get a fresh worker.
                    await asyncio.shield(self.retire(key, session))

    async def astreaming(self, model, messages, **kwargs):
        async for item in self._chunks(model, messages, **kwargs):
            reasoning = item.get('provider_specific_fields', {}).get('reasoning_content')
            if reasoning is not None:
                # Pinned LiteLLM's generic dict handler nests this field, whereas
                # its Responses translator reads delta.reasoning_content.
                yield ModelResponseStream(model=MODEL, choices=[{'index': 0,
                    'delta': {'reasoning_content': reasoning}, 'finish_reason': None}])
            else:
                yield item

    async def acompletion(self, model, messages, **kwargs):
        chunks = [item async for item in self._chunks(model, messages, **kwargs)]
        tools = [item['tool_use'] for item in chunks if item.get('tool_use')]
        message = {'role': 'assistant', 'content': ''.join(item['text'] for item in chunks) or None}
        if tools:
            message['tool_calls'] = [{k: v for k, v in item.items() if k != 'index'} for item in tools]
        reasoning = ''.join(item.get('provider_specific_fields', {}).get('reasoning_content', '') for item in chunks)
        if reasoning:
            message['reasoning_content'] = reasoning
        usage = next((item['usage'] for item in reversed(chunks) if item.get('usage') is not None), None)
        response = ModelResponse(id='chatcmpl-' + uuid.uuid4().hex, created=int(time.time()), model=MODEL,
            choices=[{'index': 0, 'message': message, 'finish_reason': chunks[-1]['finish_reason']}], usage=usage)
        if usage is None:
            response.usage = None
        return response


provider = ClaudeProvider()


def install(app):
    """Register one shared provider instance; wrap the actual ASGI lifespan."""
    import litellm
    litellm.custom_provider_map = [entry for entry in litellm.custom_provider_map if entry['provider'] != 'claudesdk'] + [
        {'provider': 'claudesdk', 'custom_handler': provider}]
    from litellm.utils import custom_llm_setup
    custom_llm_setup()
    app.add_middleware(CaptureRequest)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app) as state:
            try:
                yield state
            finally:
                await provider.close()
    app.router.lifespan_context = lifespan
