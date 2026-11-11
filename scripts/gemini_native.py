"""Pinned Vertex/Responses fidelity shim. Codex still owns every client tool.

Native assistant parts travel in an authenticated, sealed Responses reasoning
item, not a process cache. Only this module understands the versioned carrier.
No provider credentials, conversation database, or independent agent is added.
"""
import argparse
import base64
import copy
from contextvars import ContextVar
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from claude_context import REQUEST

PREFIX = 'gemini-native-v1:'
NATIVE = 'gemini_native_v1'
MAX_STATE_BYTES = 16 * 1024 * 1024
TARGET = ContextVar('gemini_target', default=None)
CALL = ContextVar('gemini_call', default=None)
INSTALLED = None


def plain(value):
    return value.model_dump(exclude_none=True) if hasattr(value, 'model_dump') else copy.deepcopy(value)


def origin(model, provider='vertex_ai'):
    for prefix in ('vertex_ai/', 'vertex_ai_beta/'):
        model = model.removeprefix(prefix)
    return [provider, model]


def is_gemini(model, provider):
    return provider in ('vertex_ai', 'vertex_ai_beta') and model.rsplit('/', 1)[-1].startswith('gemini-')


def key_path():
    return Path(os.environ.get('GEMINI_STATE_KEY_FILE', Path.home() / '.local/state/litellm/gemini/state.key'))


def read_key(path):
    """Refuse symlinks, permissive files, and silently recreated runtime keys."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError('Gemini state key unavailable; provision or restore the existing key before inference') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Gemini state key must be an owner-only regular file')
        value = os.read(fd, 33)
        if len(value) != 32:
            raise ValueError('Invalid Gemini state key')
        return value
    finally:
        os.close(fd)


def provision_key(path):
    """Deployment-only initialization; an existing key is validated, never replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = path.with_suffix(path.suffix + '.provisioned')
    if path.exists() or path.is_symlink():
        read_key(path)
        if not marker.exists():
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        return False
    if marker.exists():
        raise ValueError('Gemini state key was previously provisioned; restore it instead of replacing it')
    # Link a completely written private file atomically; concurrent deployment
    # must never observe a partially written key or replace the winning key.
    temporary = path.parent / ('.state-key-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(os.urandom(32))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            return True
        except FileExistsError:
            read_key(path)
            return False
    finally:
        temporary.unlink()


def caller():
    value = REQUEST.get().get('caller')
    if not value:
        raise ValueError('Gemini native state requires authenticated caller identity')
    return value


def public_batch(items):
    """Bind model-visible content, not annotations or transport lifecycle fields."""
    result = []
    for item in items:
        kind = item.get('type', 'message')
        if kind == 'message' and item.get('role') == 'assistant':
            content = item.get('content', [])
            if isinstance(content, str):
                content = [{'type': 'output_text', 'text': content}]
            result.append({'type': kind, 'id': item.get('id'), 'role': 'assistant',
                           'content': [{k: v for k, v in p.items() if k != 'annotations'} for p in content]})
        elif kind in ('function_call', 'custom_tool_call'):
            keys = ('type', 'id', 'call_id', 'name', 'arguments', 'input', 'namespace')
            result.append({k: item[k] for k in keys if k in item})
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class StateCodec:
    def __init__(self, path=None):
        self.path = Path(path) if path else key_path()

    def seal(self, value, owner):
        payload = json.dumps(value, separators=(',', ':')).encode()
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError('Gemini native state exceeds its response limit')
        nonce = os.urandom(12)
        sealed = AESGCM(read_key(self.path)).encrypt(nonce, payload, (PREFIX + owner).encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()

    def open(self, token, owner):
        if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 2 * MAX_STATE_BYTES:
            raise ValueError('Invalid Gemini native state')
        key = read_key(self.path)
        try:
            raw = base64.b64decode(token[len(PREFIX):], altchars=b'-_', validate=True)
            payload = AESGCM(key).decrypt(raw[:12], raw[12:], (PREFIX + owner).encode())
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get('version') != 1:
                raise ValueError()
            return value
        except (InvalidTag, ValueError, TypeError):
            raise ValueError('Gemini native state is invalid or belongs to another caller') from None


def hydrate_input(source, request, replay_reasoning, original, codec):
    """Use the stock conversion per segment, then retain exact native batches.

    A carrier may follow its batch, allowing signatures received at stream end
    to be sealed without retracting an already delivered summary or text item.
    """
    if not isinstance(source, list):
        return original(source, request, replay_reasoning)
    carriers = [(i, item) for i, item in enumerate(source) if item.get('type') == 'reasoning'
                and str(item.get('encrypted_content', '')).startswith(PREFIX)]
    if not carriers:
        return original(source, request, replay_reasoning)
    owner = caller()
    batches, skip = {}, set()
    ids = {}
    for index, item in enumerate(source):
        if item.get('id'):
            ids.setdefault(item['id'], []).append(index)
    for index, item in carriers:
        value = codec.open(item['encrypted_content'], owner)
        if item.get('id') != value.get('carrier_id'):
            raise ValueError('Gemini native state carrier identity changed')
        skip.add(index)
        target = TARGET.get()
        if target is not None and target != value.get('origin'):
            continue  # A model switch keeps public history, never foreign state.
        positions = []
        for item_id in value.get('batch_ids', []):
            matches = ids.get(item_id, [])
            if len(matches) != 1:
                raise ValueError('Gemini native state is missing its original assistant batch')
            positions.append(matches[0])
        if positions != sorted(positions) or not positions:
            raise ValueError('Invalid Gemini native assistant batch')
        public = [source[p] for p in positions]
        if digest(public_batch(public)) != value.get('batch_hash'):
            raise ValueError('Gemini native state does not match its assistant batch')
        reasoning = value.get('reasoning_ids', [])
        for rid in reasoning:
            skip.update(ids.get(rid, []))
        covered = set(positions) | skip
        if any(p not in covered for p in range(positions[0], positions[-1] + 1)):
            raise ValueError('Gemini native assistant batch was reordered')
        if positions[0] in batches or any(p in skip for p in positions):
            raise ValueError('Gemini native assistant batch was duplicated')
        batches[positions[0]] = (positions, public, value)
        skip.update(positions)
    result = original([], request, replay_reasoning)
    seen_calls = set()
    def append(messages):
        for message in messages:
            message = plain(message)
            calls = message.get('tool_calls') or []
            if calls:
                fresh = [c for c in calls if c.get('id') not in seen_calls]
                if not fresh and not message.get('content'):
                    continue
                message['tool_calls'] = fresh
                seen_calls.update(c['id'] for c in fresh)
            result.append(message)
    pending = []
    for index, item in enumerate(source):
        if index in batches:
            append(original(pending, {}, replay_reasoning)); pending = []
            positions, public, value = batches[index]
            converted = [plain(m) for m in original(public, {}, True)]
            if any(m.get('role') != 'assistant' for m in converted):
                raise ValueError('Invalid Gemini native assistant batch')
            content, calls = [], []
            for message in converted:
                text = message.get('content')
                content.extend([{'type': 'text', 'text': text}] if isinstance(text, str) else text or [])
                calls.extend(message.get('tool_calls') or [])
            append([{'role': 'assistant', 'content': content or None, 'tool_calls': calls,
                     'provider_specific_fields': {NATIVE: value}}])
        elif index not in skip:
            pending.append(item)
    append(original(pending, {}, replay_reasoning))
    return result


def native_messages(messages, model, params, original):
    """Let LiteLLM resolve user media and tool results, replacing only model parts."""
    if not any((m.get('provider_specific_fields') or {}).get(NATIVE) for m in messages):
        return original(messages, model, params)
    shadow = copy.deepcopy(messages)
    replacements = {}
    index = 0
    while index < len(shadow):
        if shadow[index].get('role') != 'assistant':
            index += 1; continue
        end = index
        while end < len(shadow) and shadow[end].get('role') == 'assistant':
            end += 1
        group = messages[index:end]
        if any((m.get('provider_specific_fields') or {}).get(NATIVE) for m in group):
            parts = []
            for message in group:
                value = (message.get('provider_specific_fields') or {}).get(NATIVE)
                if value:
                    if value.get('origin') != origin(model):
                        raise ValueError('Gemini native state belongs to a different model')
                    parts.extend(copy.deepcopy(value['parts']))
                else:
                    for content in original([message], model, params):
                        if content.get('role') == 'model':
                            parts.extend(content['parts'])
            marker = 'gemini_replay_' + uuid.uuid4().hex
            replacements[marker] = parts
            for offset in range(index, end):
                shadow[offset]['content'] = marker if offset == index else None
                shadow[offset].pop('reasoning_content', None)
                shadow[offset].pop('thinking_blocks', None)
                shadow[offset].pop('images', None)
                shadow[offset]['provider_specific_fields'] = {}
        index = end
    contents = original(shadow, model, params)
    for content in contents:
        markers = [p.get('text') for p in content.get('parts', []) if p.get('text') in replacements]
        if markers:
            if len(markers) != 1:
                raise ValueError('Gemini native model turns were unexpectedly merged')
            content['parts'] = replacements.pop(markers[0])
    if replacements:
        raise ValueError('Gemini native model turn was lost during conversion')
    return contents


def seal_response(response, native, codec, request):
    value = plain(response)
    output = value['output']
    # Only raw Vertex thought=true parts establish summary provenance.
    summaries = ''.join(p.get('text', '') for p in native['parts'] if p.get('thought') is True)
    show = (request.get('reasoning') or {}).get('summary') != 'none'
    output = [i for i in output if i.get('type') != 'reasoning']
    if summaries and show:
        output.insert(0, {'type': 'reasoning', 'id': native['reasoning_id'],
                          'summary': [{'type': 'summary_text', 'text': summaries}]})
    for item in output:
        item.pop('provider_specific_fields', None)
        if item.get('type') == 'message':
            item['id'] = native['message_id']
    batch = public_batch(output)
    if batch:
        carrier_id = native['carrier_id']
        payload = {'version': 1, 'origin': native['origin'], 'parts': native['parts'],
                   'carrier_id': carrier_id, 'batch_ids': [i['id'] for i in batch],
                   'batch_hash': digest(batch), 'reasoning_ids': [i['id'] for i in output if i['type'] == 'reasoning']}
        output.append({'type': 'reasoning', 'id': carrier_id, 'summary': [],
                       'encrypted_content': codec.seal(payload, caller())})
    from litellm.types.llms.openai import ResponsesAPIResponse
    value['output'] = output
    return ResponsesAPIResponse(**value)


class GeminiResponses:
    """Authenticated dispatch facade, using the same boundary as shared search."""
    def __init__(self, dispatch, router=None):
        self.dispatch, self.router = dispatch, router

    def managed(self, model):
        router = self.router
        if router is None:
            from litellm.proxy.proxy_server import llm_router as router
        deployments = (router.get_model_list(model_name=model) if router else None) or []
        return any(is_gemini((d.get('litellm_params') or {}).get('model', ''),
                             (d.get('litellm_params') or {}).get('custom_llm_provider') or
                             (d.get('litellm_params') or {}).get('model', '').partition('/')[0]) for d in deployments)

    async def route(self, data, route_type, **kwargs):
        if route_type != 'aresponses':
            return await self.dispatch(data=data, route_type=route_type, **kwargs)
        managed = self.managed(data.get('model', ''))
        if not managed:
            # Native OpenAI routes bypass the Chat bridge, so clean our carrier
            # at dispatch as well. Other providers keep their own opaque data.
            source = data.get('input')
            if isinstance(source, list) and any(str(i.get('encrypted_content', '')).startswith(PREFIX) for i in source):
                data = copy.deepcopy(data)
                data['input'] = [i for i in data['input'] if not str(i.get('encrypted_content', '')).startswith(PREFIX)]
            return await self.dispatch(data=data, route_type=route_type, **kwargs)
        if data.get('previous_response_id'):
            from litellm.exceptions import BadRequestError
            raise BadRequestError(message='Previous response not found; send self-contained history',
                                  model=data['model'], llm_provider='vertex_ai',
                                  code='previous_response_not_found')
        if not data.get('stream'):
            return await self.dispatch(data=data, route_type=route_type, **kwargs)
        routed = copy.deepcopy(data); routed['stream'] = False
        async def run():
            from responses_events import ResponsesEvents
            events = ResponsesEvents(data['model'])
            async def stream():
                yield events.created()
                call = await self.dispatch(data=routed, route_type=route_type, **kwargs)
                response = await call
                for event in events.complete(plain(response)):
                    yield event
            return stream()
        return run()


def install(router=None, codec=None):
    """Version checked, reversible hooks; tests exercise actual pinned LiteLLM."""
    global INSTALLED
    if INSTALLED is not None:
        return INSTALLED
    if version('litellm') != '1.106.5':
        raise RuntimeError('Gemini native bridge requires LiteLLM 1.106.5; revalidate before upgrading')
    from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig as Transform
    from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexGeminiConfig, VertexLLM
    from litellm.proxy import common_request_processing
    codec = codec or StateCodec()
    hooks = []
    def patch(cls, name, value, static=False):
        hooks.append((cls, name, cls.__dict__[name]))
        setattr(cls, name, staticmethod(value) if static else value)
    original_request = Transform.transform_responses_api_request_to_chat_completion_request
    def request_transform(*args, **kwargs):
        # The pinned handler always supplies these as keyword arguments.
        model, provider = kwargs.get('model', ''), kwargs.get('custom_llm_provider')
        token = TARGET.set(origin(model, provider) if is_gemini(model, provider) else ['foreign', model])
        try:
            return original_request(*args, **kwargs)
        finally:
            TARGET.reset(token)
    patch(Transform, 'transform_responses_api_request_to_chat_completion_request', request_transform, True)
    original_input = Transform.transform_responses_api_input_to_messages
    def input_transform(input, responses_api_request, replay_reasoning=False):
        return hydrate_input(input, responses_api_request, replay_reasoning, original_input, codec)
    patch(Transform, 'transform_responses_api_input_to_messages', input_transform, True)
    original_messages = VertexGeminiConfig._transform_messages
    def message_transform(self, messages, model=None, litellm_params=None):
        return native_messages(messages, model, litellm_params,
                               lambda m, n, p: original_messages(self, m, n, p))
    patch(VertexGeminiConfig, '_transform_messages', message_transform)
    original_vertex = VertexGeminiConfig.transform_response
    def vertex_response(self, *args, **kwargs):
        response = original_vertex(self, *args, **kwargs)
        call = CALL.get()
        if call is not None:
            candidates = kwargs['raw_response'].json().get('candidates', [])
            if candidates:
                call['parts'] = copy.deepcopy(candidates[0].get('content', {}).get('parts', []))
                response.choices[0].message.provider_specific_fields = (
                    plain(response.choices[0].message).get('provider_specific_fields', {}) | {NATIVE: call})
        return response
    patch(VertexGeminiConfig, 'transform_response', vertex_response)
    original_completion = VertexLLM.async_completion
    async def completion(self, *args, **kwargs):
        model, provider = kwargs.get('model', ''), kwargs.get('custom_llm_provider')
        if not is_gemini(model, provider) or not REQUEST.get().get('caller'):
            return await original_completion(self, *args, **kwargs)
        call = {'origin': origin(model, provider), 'parts': [],
                'message_id': 'msg_' + uuid.uuid4().hex, 'reasoning_id': 'rs_' + str(uuid.uuid4()),
                'carrier_id': 'rs_' + str(uuid.uuid4())}
        token = CALL.set(call)
        try:
            return await original_completion(self, *args, **kwargs)
        finally:
            CALL.reset(token)
    patch(VertexLLM, 'async_completion', completion)
    original_response = Transform.transform_chat_completion_response_to_responses_api_response
    def response_transform(*args, **kwargs):
        response = original_response(*args, **kwargs)
        chat = kwargs.get('chat_completion_response')
        chat = plain(chat) if chat is not None else {}
        choices = chat.get('choices') or []
        native = ((choices[0].get('message', {}).get('provider_specific_fields') or {}).get(NATIVE) if choices else None)
        if native:
            return seal_response(response, native, codec, kwargs.get('responses_api_request') or {})
        return response
    patch(Transform, 'transform_chat_completion_response_to_responses_api_response', response_transform, True)
    dispatch = common_request_processing.route_request
    integration = GeminiResponses(dispatch, router)
    common_request_processing.route_request = integration.route
    def uninstall():
        global INSTALLED
        common_request_processing.route_request = dispatch
        for cls, name, original in reversed(hooks):
            setattr(cls, name, original)
        INSTALLED = None
    integration.uninstall = uninstall
    INSTALLED = integration
    return integration


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['provision-key'])
    parser.add_argument('--key-file', type=Path, required=True)
    args = parser.parse_args()
    print('created' if provision_key(args.key_file) else 'unchanged')
