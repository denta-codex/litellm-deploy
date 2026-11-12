"""Pinned Vertex/Responses fidelity shim. Codex still owns every client tool.

Native assistant parts travel in an authenticated, sealed Responses reasoning
item, not a process cache. Only this module understands the versioned carrier.
No provider credentials, conversation database, or independent agent is added.
"""
import argparse
import asyncio
import base64
import copy
from contextvars import ContextVar
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import stat
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from claude_context import REQUEST

PREFIX = 'gemini-native-v1:'
NATIVE = 'gemini_native_v1'
MAX_STATE_BYTES = 16 * 1024 * 1024
TARGET = ContextVar('gemini_target', default=None)
CALL = ContextVar('gemini_call', default=None)
FEEDBACK = ContextVar('gemini_feedback', default=None)
# Hosted search mode for one managed Responses request: live, cached or none.
# None means the request did not come through GeminiResponses; leave it stock.
SEARCH = ContextVar('gemini_search', default=None)
HOSTED = ('web_search', 'web_search_preview')
SEARCH_SETTINGS = {'type', 'external_web_access', 'search_content_types', 'search_context_size', 'user_location'}
LOG = logging.getLogger('gemini_native')
INSTALLED = None


def configure_timing_logging():
    """Publish this adapter's metadata even when the server root logger is quiet."""
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    if not any(handler.name == 'gemini-native-metadata' for handler in LOG.handlers):
        handler = logging.StreamHandler()
        handler.set_name('gemini-native-metadata')
        handler.setFormatter(logging.Formatter('%(message)s'))
        LOG.addHandler(handler)


class NativeStateError(ValueError):
    """Deliberately non-secret diagnostics safe at the public Responses boundary."""
    status_code = 400


def feedback_event(renderer, entry):
    """Called by the presentation owner, never by the provider/parser."""
    kind, value = entry
    if kind == 'model_delta':
        call = value['call']
        call.setdefault('first_public_at', time.monotonic())
        return list(renderer.delta(value['id'], value['type'], value['delta']))
    if kind == 'model_timing':
        call = value
        started = call['started_at']
        LOG.info('gemini_timing %s', json.dumps({
            'provider': call['origin'][0], 'model': call['origin'][1],
            'dispatch_at': call['dispatch_at'], 'outcome': call['outcome'],
            'first_content_ms': round(1000 * (call['first_content_at'] - started)) if 'first_content_at' in call else None,
            'first_public_ms': round(1000 * (call['first_public_at'] - started)) if 'first_public_at' in call else None,
            'completion_ms': round(1000 * (call['completed_at'] - started)),
            # Counts only: queries and URLs are user content and never logged.
            'search_queries': call.get('search_queries', 0), 'url_retrievals': call.get('url_retrievals', 0),
            'url_failures': call.get('url_failures', 0)}))
        return []
    raise ValueError('Unknown Gemini feedback event')


def flush_timings(queue):
    """Cancellation can leave timing metadata queued without a live renderer."""
    while not queue.empty():
        entry = queue.get_nowait()
        if entry[0] == 'model_timing':
            feedback_event(None, entry)


async def render_execution(renderer, execute, queue):
    """Stream a single dispatch while retaining cancellation and terminal errors."""
    task = asyncio.create_task(execute())
    pending = None
    try:
        yield renderer.created()
        while not task.done() or not queue.empty():
            if not queue.empty():
                entry = queue.get_nowait()
            else:
                pending = asyncio.create_task(queue.get())
                ready, _ = await asyncio.wait([task, pending], return_when=asyncio.FIRST_COMPLETED)
                if pending not in ready:
                    pending.cancel(); await asyncio.gather(pending, return_exceptions=True)
                    continue
                entry = pending.result(); pending = None
            for event in feedback_event(renderer, entry):
                yield event
        for event in renderer.complete(plain(await task)):
            yield event
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        yield renderer.failed(exc)
    finally:
        task.cancel()
        if pending: pending.cancel()
        await asyncio.gather(task, *([pending] if pending else []), return_exceptions=True)
        flush_timings(queue)


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
        raise NativeStateError('Gemini state key unavailable; provision or restore the existing key before inference') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise NativeStateError('Gemini state key must be an owner-only regular file')
        value = os.read(fd, 33)
        if len(value) != 32:
            raise NativeStateError('Invalid Gemini state key')
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
        raise NativeStateError('Gemini state key was previously provisioned; restore it instead of replacing it')
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
        raise NativeStateError('Gemini native state requires authenticated caller identity')
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


def search_mode(data):
    """Validate a hosted search declaration Gemini can honor with Google Search."""
    hosted = [t for t in data.get('tools') or [] if t.get('type') in HOSTED]
    choice = data.get('tool_choice')
    if len(hosted) > 1:
        raise NativeStateError('Only one hosted search declaration is supported')
    if isinstance(choice, dict) and choice.get('type') in HOSTED:
        raise NativeStateError('Gemini cannot force a hosted search call; use tool_choice auto')
    if not hosted:
        return 'none'
    if 'filters' in hosted[0]:
        raise NativeStateError('Gemini Google Search cannot enforce hosted search domain filters')
    unknown = set(hosted[0]) - SEARCH_SETTINGS
    if unknown:
        raise NativeStateError('Unsupported hosted search settings: ' + ', '.join(sorted(unknown)))
    if choice == 'none' or (isinstance(choice, dict) and choice.get('type') == 'none'):
        return 'none'
    # Cached search must not fetch arbitrary live pages, so it omits URL Context.
    return 'cached' if hosted[0].get('external_web_access') is False else 'live'


def merge_grounding(target, value):
    """Streamed candidates may split or repeat grounding; keep one consistent copy."""
    for value in value if isinstance(value, list) else [value]:
        chunks = target.setdefault('groundingChunks', [])
        keys = [json.dumps(c, sort_keys=True) for c in chunks]
        local = []
        for chunk in value.get('groundingChunks') or []:
            key = json.dumps(chunk, sort_keys=True)
            if key not in keys:
                keys.append(key); chunks.append(copy.deepcopy(chunk))
            local.append(keys.index(key))
        supports = target.setdefault('groundingSupports', [])
        for support in value.get('groundingSupports') or []:
            support = copy.deepcopy(support)
            if local:  # Indices refer to this message's own chunk list.
                support['groundingChunkIndices'] = [local[i] for i in support.get('groundingChunkIndices') or []
                                                    if isinstance(i, int) and 0 <= i < len(local)]
            if support not in supports:
                supports.append(support)
        queries = target.setdefault('webSearchQueries', [])
        queries.extend(q for q in value.get('webSearchQueries') or [] if q not in queries)
        for key, item in value.items():
            if key not in ('groundingChunks', 'groundingSupports', 'webSearchQueries'):
                target[key] = copy.deepcopy(item)
    return target


def merge_url_context(target, value):
    entries = target.setdefault('urlMetadata', [])
    for value in value if isinstance(value, list) else [value]:
        for entry in value.get('urlMetadata') or []:
            known = next((e for e in entries if e.get('retrievedUrl') == entry.get('retrievedUrl')), None)
            if known is None:
                entries.append(copy.deepcopy(entry))
            else:
                known.update(copy.deepcopy(entry))
        for key, item in value.items():
            if key != 'urlMetadata':
                target[key] = copy.deepcopy(item)
    return target


def cite_grounding(text, parts, grounding):
    """Map Vertex segments to Responses character offsets in the answer text.

    Vertex reports UTF-8 byte offsets within one part and omits zero-valued
    startIndex/partIndex. Streaming re-chunks parts, so the quoted segment text
    is authoritative whenever the offsets disagree with it.
    """
    chunks = grounding.get('groundingChunks') or []
    starts, position = {}, 0
    for index, part in enumerate(parts):
        if isinstance(part.get('text'), str) and part.get('thought') is not True:
            starts[index] = position; position += len(part['text'])
    annotations = []
    for support in grounding.get('groundingSupports') or []:
        segment = support.get('segment') or {}
        quote, span = segment.get('text'), None
        index = segment.get('partIndex', 0)
        if index in starts:
            raw = parts[index]['text'].encode()
            first, last = segment.get('startIndex', 0), segment.get('endIndex')
            if isinstance(first, int) and isinstance(last, int) and 0 <= first <= last <= len(raw):
                try:
                    span = (starts[index] + len(raw[:first].decode()), starts[index] + len(raw[:last].decode()))
                except UnicodeDecodeError:
                    span = None
        if span is None or (quote and text[span[0]:span[1]] != quote):
            found = text.find(quote) if quote else -1
            span = (found, found + len(quote)) if found >= 0 else None
        if span is None:
            continue
        for chunk in support.get('groundingChunkIndices') or []:
            web = (chunks[chunk].get('web') or {}) if isinstance(chunk, int) and 0 <= chunk < len(chunks) else {}
            if web.get('uri'):
                annotations.append({'type': 'url_citation', 'url': web['uri'],
                                    'title': web.get('title') or web.get('domain') or web['uri'],
                                    'start_index': span[0], 'end_index': span[1]})
    return annotations


def search_items(native):
    """Public web_search_call items, only for searches Vertex confirmed."""
    grounding, context = native.get('grounding') or {}, native.get('url_context') or {}
    sources = []
    for chunk in grounding.get('groundingChunks') or []:
        uri = (chunk.get('web') or {}).get('uri')
        if uri and all(s['url'] != uri for s in sources):
            sources.append({'type': 'url', 'url': uri})
    items = []
    for query in dict.fromkeys(q for q in grounding.get('webSearchQueries') or [] if isinstance(q, str) and q):
        action = {'type': 'search', 'query': query, 'queries': [query]}
        if not items and sources:
            # Vertex does not attribute chunks to queries; list them once.
            action['sources'] = sources
        items.append({'type': 'web_search_call', 'id': 'ws_' + uuid.uuid4().hex, 'status': 'completed', 'action': action})
    for entry in context.get('urlMetadata') or []:
        if entry.get('retrievedUrl'):
            ok = entry.get('urlRetrievalStatus') == 'URL_RETRIEVAL_STATUS_SUCCESS'
            items.append({'type': 'web_search_call', 'id': 'ws_' + uuid.uuid4().hex,
                          'status': 'completed' if ok else 'failed',
                          'action': {'type': 'open_page', 'url': entry['retrievedUrl']}})
    return items


class StateCodec:
    def __init__(self, path=None):
        self.path = Path(path) if path else key_path()

    def seal(self, value, owner):
        payload = json.dumps(value, separators=(',', ':')).encode()
        if len(payload) > MAX_STATE_BYTES:
            raise NativeStateError('Gemini native state exceeds its response limit')
        nonce = os.urandom(12)
        sealed = AESGCM(read_key(self.path)).encrypt(nonce, payload, (PREFIX + owner).encode())
        return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()

    def open(self, token, owner):
        if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 2 * MAX_STATE_BYTES:
            raise NativeStateError('Invalid Gemini native state')
        key = read_key(self.path)
        try:
            raw = base64.b64decode(token[len(PREFIX):], altchars=b'-_', validate=True)
            payload = AESGCM(key).decrypt(raw[:12], raw[12:], (PREFIX + owner).encode())
            value = json.loads(payload)
            if not isinstance(value, dict) or value.get('version') != 1:
                raise ValueError()
            return value
        except (InvalidTag, ValueError, TypeError):
            raise NativeStateError('Gemini native state is invalid or belongs to another caller') from None


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
            raise NativeStateError('Gemini native state carrier identity changed')
        skip.add(index)
        target = TARGET.get()
        if target is not None and target != value.get('origin'):
            continue  # A model switch keeps public history, never foreign state.
        positions = []
        for item_id in value.get('batch_ids', []):
            matches = ids.get(item_id, [])
            if len(matches) != 1:
                raise NativeStateError('Gemini native state is missing its original assistant batch')
            positions.append(matches[0])
        if positions != sorted(positions) or not positions:
            raise NativeStateError('Invalid Gemini native assistant batch')
        public = [source[p] for p in positions]
        if digest(public_batch(public)) != value.get('batch_hash'):
            raise NativeStateError('Gemini native state does not match its assistant batch')
        reasoning = value.get('reasoning_ids', [])
        for rid in reasoning:
            skip.update(ids.get(rid, []))
        covered = set(positions) | skip
        if any(p not in covered for p in range(positions[0], positions[-1] + 1)):
            raise NativeStateError('Gemini native assistant batch was reordered')
        if positions[0] in batches or any(p in skip for p in positions):
            raise NativeStateError('Gemini native assistant batch was duplicated')
        batches[positions[0]] = (positions, public, value)
        skip.update(positions)
        # Native search items are display history; the sealed parts already
        # hold the signed model turn. Stock conversion would add assistant text.
        for sid in value.get('search_ids', []):
            skip.update(ids.get(sid, []))
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
                raise NativeStateError('Invalid Gemini native assistant batch')
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


def repair_tool_results(messages):
    """Keep each tool result where Gemini's converter can pair it with its call.

    LiteLLM pairs a result only with the latest assistant tool-call message and
    raises otherwise. Codex can append a background command's late result after
    a newer call; that error then repeats on every retry and kills the chat.
    Move a first result next to its call; drop repeats and results with no call.
    """
    from litellm.litellm_core_utils.prompt_templates.factory import THOUGHT_SIGNATURE_SEPARATOR
    def field(message, key):
        return message.get(key) if isinstance(message, dict) else getattr(message, key, None)
    def ident(value):
        return str(value or '').split(THOUGHT_SIGNATURE_SEPARATOR, 1)[0]
    owners = {}
    for index, message in enumerate(messages):
        if field(message, 'role') == 'assistant':
            for call in field(message, 'tool_calls') or []:
                owners.setdefault(ident(field(call, 'id')), index)
    latest, answered, late, kept = None, set(), {}, []
    dropped = moved = 0
    for index, message in enumerate(messages):
        if field(message, 'role') == 'assistant' and field(message, 'tool_calls'):
            latest = index
        if field(message, 'role') != 'tool':
            kept.append((index, message)); continue
        call = ident(field(message, 'tool_call_id'))
        owner = owners.get(call)
        if call in answered or owner is None or owner > index:
            dropped += 1; continue
        answered.add(call)
        if owner == latest:
            kept.append((index, message))
        else:
            late.setdefault(owner, []).append(message); moved += 1
    if not dropped and not moved:
        return messages
    LOG.info('gemini_history_repair %s', json.dumps({'dropped': dropped, 'moved': moved}))
    result, pending = [], []
    for index, message in kept:
        if pending and field(message, 'role') != 'tool':
            result.extend(pending); pending = []
        result.append(message)
        pending = late.get(index, pending)
    return result + pending


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
                    active = (CALL.get() or {}).get('origin', origin(model))
                    if value.get('origin') != active:
                        raise NativeStateError('Gemini native state belongs to a different model')
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
            for part in item.get('content') if isinstance(item.get('content'), list) else []:
                if part.get('type') == 'output_text':
                    # Replaces LiteLLM's byte-offset annotations.
                    part['annotations'] = cite_grounding(part.get('text', ''), native['parts'], native.get('grounding') or {})
    batch = public_batch(output)
    searches = search_items(native)
    # After the published batch: grounding metadata arrives with the last chunk.
    output.extend(searches)
    if batch:
        carrier_id = native['carrier_id']
        payload = {'version': 1, 'origin': native['origin'], 'parts': native['parts'],
                   'carrier_id': carrier_id, 'batch_ids': [i['id'] for i in batch],
                   'batch_hash': digest(batch), 'reasoning_ids': [i['id'] for i in output if i['type'] == 'reasoning'],
                   'search_ids': [i['id'] for i in searches]}
        output.append({'type': 'reasoning', 'id': carrier_id, 'summary': [],
                       'encrypted_content': codec.seal(payload, caller())})
    from litellm.types.llms.openai import ResponsesAPIResponse
    value['output'] = output
    return ResponsesAPIResponse(**value)


class GeminiResponses:
    """Authenticated dispatch facade, installed outside shared search.

    Managed Gemini routes claim hosted search here and run Google Search and
    URL Context natively, so the shared search helper never sees them.
    """
    def __init__(self, dispatch, router=None, journal=None):
        self.dispatch, self.router, self.journal = dispatch, router, journal

    def managed(self, model):
        router = self.router
        if router is None:
            from litellm.proxy.proxy_server import llm_router as router
        deployments = (router.get_model_list(model_name=model) if router else None) or []
        return any(is_gemini((d.get('litellm_params') or {}).get('model', ''),
                             (d.get('litellm_params') or {}).get('custom_llm_provider') or
                             (d.get('litellm_params') or {}).get('model', '').partition('/')[0]) for d in deployments)

    def claim_search(self, data):
        mode = search_mode(data)
        routed = copy.deepcopy(data)
        if routed.get('tools'):
            routed['tools'] = [t for t in routed['tools'] if t.get('type') not in HOSTED]
        source = routed.get('input')
        if isinstance(source, list) and any(i.get('type') == 'web_search_call' and
                                            str(i.get('id', '')).startswith('ws_shared') for i in source):
            # Searches made through the shared helper before Gemini owned search.
            journal = self.journal
            if journal is None:
                from shared_search import SearchJournal
                journal = SearchJournal()
            routed['input'] = journal.expand(caller(), source)
        return routed, mode

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
        routed, mode = self.claim_search(data)
        if routed.get('stream') or FEEDBACK.get() is not None:
            # A provider retry could replace an already-published batch. Fail
            # the stream terminally; an explicit client retry is a new request.
            routed |= {'num_retries': 0, 'max_fallbacks': 0, 'disable_fallbacks': True}
        async def searched():
            token = SEARCH.set(mode)
            try:
                call = await self.dispatch(data=routed, route_type=route_type, **kwargs)
                response = await call
            finally:
                SEARCH.reset(token)
            if 'tools' in data and hasattr(response, 'tools'):
                response.tools = copy.deepcopy(data['tools'])  # Echo the caller's declarations.
            return response
        if not routed.get('stream'):
            return searched()
        routed['stream'] = False
        async def run():
            from responses_events import ResponsesEvents
            events = ResponsesEvents(data['model'])
            queue = asyncio.Queue()
            async def execute():
                token = FEEDBACK.set(queue)
                try:
                    return await searched()
                finally:
                    FEEDBACK.reset(token)
            return render_execution(events, execute, queue)
        return run()


def install(router=None, codec=None):
    """Version checked, reversible hooks; tests exercise actual pinned LiteLLM."""
    global INSTALLED
    configure_timing_logging()
    if INSTALLED is not None:
        return INSTALLED
    if version('litellm') != '1.106.5':
        raise RuntimeError('Gemini native bridge requires LiteLLM 1.106.5; revalidate before upgrading')
    from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig as Transform
    from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexGeminiConfig, VertexLLM, ModelResponseIterator
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
        messages = hydrate_input(input, responses_api_request, replay_reasoning, original_input, codec)
        target = TARGET.get()
        if target is None or target[0] == 'foreign':
            return messages
        return repair_tool_results(messages)
    patch(Transform, 'transform_responses_api_input_to_messages', input_transform, True)
    original_messages = VertexGeminiConfig._transform_messages
    def message_transform(self, messages, model=None, litellm_params=None):
        return native_messages(messages, model, litellm_params,
                               lambda m, n, p: original_messages(self, m, n, p))
    patch(VertexGeminiConfig, '_transform_messages', message_transform)
    original_map = VertexGeminiConfig.map_openai_params
    def map_params(self, non_default_params, optional_params, model, drop_params):
        result = original_map(self, non_default_params, optional_params, model, drop_params)
        mode = SEARCH.get()
        if mode in ('live', 'cached') and type(self) is VertexGeminiConfig and is_gemini(model, 'vertex_ai'):
            tools = list(result.get('tools') or [])
            present = {k for t in tools if isinstance(t, dict) for k in t}
            if not present & {'googleSearch', 'google_search'}:
                tools.append({'googleSearch': {}})
            if mode == 'live' and not present & {'url_context', 'urlContext'}:
                tools.append({'url_context': {}})
            result['tools'] = tools
            # Gemini combines built-in and client function tools only with
            # this flag (verified on Vertex); LiteLLM would otherwise drop them.
            result['include_server_side_tool_invocations'] = True
        return result
    patch(VertexGeminiConfig, 'map_openai_params', map_params)
    original_vertex = VertexGeminiConfig.transform_response
    def vertex_response(self, *args, **kwargs):
        response = original_vertex(self, *args, **kwargs)
        call = CALL.get()
        if call is not None:
            candidates = kwargs['raw_response'].json().get('candidates', [])
            if candidates:
                parts = copy.deepcopy(candidates[0].get('content', {}).get('parts', []))
                grounding = merge_grounding({}, candidates[0].get('groundingMetadata') or {})
                context = merge_url_context({}, candidates[0].get('urlContextMetadata') or {})
                if grounding['webSearchQueries'] or context['urlMetadata']:
                    # Vertex returns each hidden server-side search/URL step as a
                    # signature-only part, then rejects that part on replay
                    # ("Invalid thought signature", verified live). Signed text
                    # and function-call parts replay correctly without them.
                    # Turns without server-side tool use keep stream-end text
                    # signatures, which arrive in the same shape.
                    parts = [p for p in parts if set(p) != {'thoughtSignature'}]
                call['parts'] = parts
                call.update(grounding=grounding, url_context=context,
                            search_queries=len(set(grounding['webSearchQueries'])),
                            url_retrievals=len(context['urlMetadata']),
                            url_failures=sum(e.get('urlRetrievalStatus') != 'URL_RETRIEVAL_STATUS_SUCCESS'
                                             for e in context['urlMetadata']))
                # The pinned adapter also embeds signatures in call IDs and
                # tool-cache entries. New Responses history must have only one
                # state transport: the caller/model-bound sealed carrier.
                from litellm.litellm_core_utils.prompt_templates.factory import THOUGHT_SIGNATURE_SEPARATOR
                message = response.choices[0].message
                for tool in message.tool_calls or []:
                    tool.id = tool.id.split(THOUGHT_SIGNATURE_SEPARATOR, 1)[0]
                    tool.provider_specific_fields = {k: v for k, v in
                        (getattr(tool, 'provider_specific_fields', None) or {}).items() if k != 'thought_signature'}
                    fields = getattr(tool.function, 'provider_specific_fields', None)
                    if fields:
                        tool.function.provider_specific_fields = {k: v for k, v in fields.items() if k != 'thought_signature'}
                native = {k: call[k] for k in ('origin', 'parts', 'message_id', 'reasoning_id', 'carrier_id',
                                               'grounding', 'url_context')}
                response.choices[0].message.provider_specific_fields = (
                    plain(response.choices[0].message).get('provider_specific_fields', {}) | {NATIVE: native})
        return response
    patch(VertexGeminiConfig, 'transform_response', vertex_response)
    original_parser = ModelResponseIterator.chunk_parser
    def chunk_parser(self, chunk):
        parsed = original_parser(self, chunk)
        call = CALL.get()
        if call is not None and 'aggregate' in call:
            aggregate = call['aggregate']
            for key, value in chunk.items():
                if key != 'candidates': aggregate[key] = copy.deepcopy(value)
            for candidate in chunk.get('candidates', []):
                if candidate.get('index', 0) != 0: continue
                target = aggregate['candidates'][0]
                for key, value in candidate.items():
                    # A trailing finish chunk must not erase earlier grounding.
                    if key == 'groundingMetadata': merge_grounding(target.setdefault(key, {}), value)
                    elif key == 'urlContextMetadata': merge_url_context(target.setdefault(key, {}), value)
                    elif key != 'content': target[key] = copy.deepcopy(value)
                for part in candidate.get('content', {}).get('parts', []):
                    target['content']['parts'].append(copy.deepcopy(part))
                    call['received_bytes'] += len(json.dumps(part).encode())
                    if call['received_bytes'] > MAX_STATE_BYTES:
                        raise ValueError('Gemini provider stream exceeds its response limit')
                    text = part.get('text')
                    if not isinstance(text, str) or not text: continue
                    call.setdefault('first_content_at', time.monotonic())
                    thought = part.get('thought') is True
                    if thought and not call['show_summary']: continue
                    FEEDBACK.get().put_nowait(('model_delta', {'type': 'reasoning' if thought else 'message',
                        'id': call['reasoning_id'] if thought else call['message_id'], 'delta': text, 'call': call}))
        return parsed
    patch(ModelResponseIterator, 'chunk_parser', chunk_parser)
    original_completion = VertexLLM.async_completion
    async def completion(self, *args, **kwargs):
        model, provider = kwargs.get('model', ''), kwargs.get('custom_llm_provider')
        if not is_gemini(model, provider) or not REQUEST.get().get('caller'):
            return await original_completion(self, *args, **kwargs)
        read_key(codec.path)  # Fail before inference if durable state is missing.
        call = {'origin': origin(model, provider), 'parts': [],
                'message_id': 'msg_' + uuid.uuid4().hex, 'reasoning_id': 'rs_' + str(uuid.uuid4()),
                'carrier_id': 'rs_' + str(uuid.uuid4()), 'started_at': time.monotonic(), 'dispatch_at': time.time()}
        token = CALL.set(call)
        try:
            queue = FEEDBACK.get()
            if queue is None:
                try:
                    response = await original_completion(self, *args, **kwargs)
                    if any(p.get('text') for p in call['parts']): call['first_content_at'] = time.monotonic()
                    call['outcome'] = 'completed'
                    return response
                except asyncio.CancelledError:
                    call['outcome'] = 'cancelled'; raise
                except Exception:
                    call['outcome'] = 'failed'; raise
                finally:
                    call['completed_at'] = time.monotonic()
                    feedback_event(None, ('model_timing', call))
            # Only transport streams. The existing LiteLLM agentic engine still
            # receives one complete ModelResponse per iteration and owns search.
            call.update(aggregate={'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': []}}]},
                        received_bytes=0,
                        show_summary=(REQUEST.get().get('request', {}).get('reasoning') or {}).get('summary') != 'none')
            stream_kwargs = kwargs | {'stream': True}
            stream_kwargs['optional_params'] = copy.deepcopy(kwargs['optional_params'])
            stream_kwargs['data'] = kwargs['data'] | {'optional_params': stream_kwargs['optional_params']}
            wrapper, raw = None, None
            try:
                wrapper = await self.async_streaming(*args, **stream_kwargs)
                # Consume the provider iterator, not CustomStreamWrapper's
                # success callbacks: the outer JSON completion accounts once.
                raw = await wrapper.make_call(client=kwargs.get('client'))
                async for _ in raw: pass
                aggregate = call['aggregate']
                if not aggregate['candidates'][0].get('finishReason') and not aggregate.get('promptFeedback', {}).get('blockReason'):
                    raise ValueError('Gemini provider stream ended without a finish reason')
                import httpx
                response = VertexGeminiConfig().transform_response(
                    model=model, raw_response=httpx.Response(200, json=aggregate,
                        headers=getattr(raw, 'response_headers', {})), model_response=kwargs['model_response'],
                    logging_obj=kwargs['logging_obj'], request_data={}, messages=kwargs['messages'],
                    optional_params=kwargs['optional_params'], litellm_params=kwargs['litellm_params'], encoding=kwargs['encoding'])
                call['outcome'] = 'completed'
                return response
            except asyncio.CancelledError:
                call['outcome'] = 'cancelled'; raise
            except Exception:
                call['outcome'] = 'failed'; raise
            finally:
                if raw is not None: await raw.aclose()
                if wrapper is not None: await wrapper.aclose()
                call['completed_at'] = time.monotonic()
                queue.put_nowait(('model_timing', call))
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
