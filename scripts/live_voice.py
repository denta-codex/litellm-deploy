"""Narrow Codex 0.159.2 WebRTC adapter for LiteLLM 1.102.1.

Protocol: openai/codex rust-v0.159.2 realtime_call.rs and realtime_websocket/.
Upstream replacement: https://github.com/BerriAI/litellm/pull/40366
Reference revision: 966b047dd5ff7e67b6afe4156172c1e3d53ba0fa (not vendored).
Removal contract and live acceptance: docs/VOICE.md. No audio bridge, standalone
audio WebSocket, API-key backend, virtual-key support, or custom Codex protocol.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
from importlib.metadata import version
import json
import logging
import os
import re
import secrets
import time

import httpx
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from litellm.llms.chatgpt.authenticator import Authenticator
from litellm.llms.chatgpt.common_utils import get_chatgpt_default_headers
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

MODEL = 'gpt-live-1-codex'
MAX_BYTES = 1024 * 1024
MAX_CALLS = 16
OFFER_TTL = 120
LOG = logging.getLogger('litellm.subscription_voice')
FORWARD_HEADERS = ('originator', 'x-session-id', 'session-id', 'thread-id', 'x-oai-attestation')


class VoiceError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def provider_error(response):
    """Keep status and machine-readable code, never echo arbitrary provider text."""
    try:
        error = response.json().get('error', {})
        code = error.get('code') or error.get('type')
        if not isinstance(code, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', code):
            code = 'upstream_rejected'
    except (ValueError, AttributeError):
        code = 'upstream_rejected'
    return VoiceError(response.status_code, code, 'Subscription voice request rejected; check subscription access and voice configuration.')


class SubscriptionProvider:
    def __init__(self):
        self.auth_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=40, follow_redirects=False)

    @staticmethod
    def load_headers(force=False):
        auth = Authenticator()
        record = auth._read_auth_file()
        if not record:
            raise VoiceError(401, 'subscription_login_required', 'Restore the existing ChatGPT subscription login on Grace.')
        token = record.get('access_token')
        if force or not token or auth._is_token_expired(record, token):
            if not record.get('refresh_token'):
                raise VoiceError(401, 'subscription_login_required', 'Refresh the ChatGPT subscription login on Grace.')
            try:
                # Reuse upstream refresh/storage without its interactive-login fallback.
                token = auth._refresh_tokens(record['refresh_token'])['access_token']
            except Exception:
                raise VoiceError(401, 'subscription_refresh_failed', 'ChatGPT subscription refresh failed; sign in again on Grace.') from None
        headers = get_chatgpt_default_headers(token, auth.get_account_id())
        headers.pop('accept', None)
        headers.pop('content-type', None)
        headers['openai-alpha'] = 'quicksilver=v2'
        return headers

    async def headers(self, forwarded, force=False):
        async with self.auth_lock:
            headers = await asyncio.to_thread(self.load_headers, force)
        headers.update({k:forwarded[k] for k in FORWARD_HEADERS if k in forwarded})
        return headers

    async def create(self, sdp, session, forwarded):
        for attempt in range(2):
            headers = await self.headers(forwarded, force=bool(attempt))
            response = await self.http.post(
                'https://chatgpt.com/backend-api/codex/realtime/calls',
                params={'intent': 'quicksilver', 'architecture': 'avas'},
                headers=headers, json={'sdp': sdp, 'session': session})
            if response.status_code != 401 or attempt:
                break
        if response.status_code != 201:
            raise provider_error(response)
        call_id = response.headers.get('location', '').split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', call_id):
            raise VoiceError(502, 'invalid_call_handle', 'Subscription backend returned an invalid call handle.')
        return call_id, response.content, headers

    async def attach(self, call):
        try:
            return await connect('wss://api.openai.com/v1/live/' + call.upstream_id,
                                 additional_headers=call.headers, open_timeout=15,
                                 close_timeout=3, max_size=4 * MAX_BYTES, max_queue=8)
        except InvalidStatus as error:
            raise VoiceError(error.response.status_code, 'sideband_rejected', 'Subscription voice control connection rejected.') from None
        except WebSocketException:
            raise VoiceError(502, 'sideband_unavailable', 'Subscription voice control connection unavailable.') from None

    async def hangup(self, call):
        for attempt in range(2):
            headers = call.headers if not attempt else await self.headers(call.headers, force=True)
            response = await self.http.post(
                'https://api.openai.com/v1/realtime/calls/' + call.upstream_id + '/hangup',
                headers=headers, timeout=10)
            if response.status_code != 401 or attempt:
                break
        # Live validation proved 200 terminates the call and subsequent attach is 404.
        if response.status_code not in (200, 204, 404, 410):
            raise provider_error(response)

    async def close(self):
        await self.http.aclose()


@dataclass
class Call:
    handle: str
    upstream_id: str | None = None
    headers: dict = field(default_factory=dict, repr=False)
    state: str = 'creating'
    created: float = field(default_factory=time.monotonic)
    upstream: object = field(default=None, repr=False)
    client: object = field(default=None, repr=False)
    cleanup: object = field(default=None, repr=False)


class VoiceGateway:
    def __init__(self, key, provider=None, ttl=OFFER_TTL, capacity=MAX_CALLS):
        self.key = key
        self.provider = provider or SubscriptionProvider()
        self.ttl, self.capacity = ttl, capacity
        self.calls = {}
        self.tasks = set()
        self.stopping = False
        self.reaper = None

    def authorized(self, headers):
        key = self.key()
        return bool(key) and hmac.compare_digest(headers.get('authorization', '').encode(), ('Bearer ' + key).encode())

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def done(task):
            self.tasks.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(done)
        return task

    async def provision(self, call, sdp, session, forwarded):
        try:
            call.upstream_id, answer, call.headers = await self.provider.create(sdp, session, forwarded)
            call.state = 'pending'
            return answer
        except BaseException:
            self.calls.pop(call.handle, None)
            raise

    async def create(self, sdp, session, forwarded):
        # Reservation occurs before the first await, including in-flight HTTP requests.
        if self.stopping:
            raise VoiceError(503, 'voice_stopping', 'Voice service is shutting down.')
        if len(self.calls) >= self.capacity:
            raise VoiceError(429, 'voice_capacity', 'Too many concurrent voice calls.')
        call = Call('rtc_' + secrets.token_urlsafe(32))
        self.calls[call.handle] = call
        task = self.spawn(self.provision(call, sdp, session, forwarded))
        try:
            answer = await asyncio.shield(task)
            return call, answer
        except asyncio.CancelledError:
            async def abandoned():
                try:
                    await task
                except Exception:
                    return
                await self.retire(call)
            self.spawn(abandoned())
            raise

    async def retire(self, call):
        if call.cleanup and not call.cleanup.done():
            return await asyncio.shield(call.cleanup)
        if self.calls.get(call.handle) is not call or not call.upstream_id:
            return
        call.state = 'closing'
        call.cleanup = self.spawn(self.terminate(call))
        return await asyncio.shield(call.cleanup)

    async def terminate(self, call):
        try:
            # HTTP hangup is independent of the possibly disconnected sideband.
            await self.provider.hangup(call)
        except Exception:
            LOG.warning('voice_cleanup_failed; retrying while service is running')
            return False
        finally:
            if call.upstream:
                try:
                    await call.upstream.close()
                except Exception:
                    pass
            if call.client:
                try:
                    await call.client.close(code=1000)
                except (RuntimeError, WebSocketDisconnect):
                    pass
        self.calls.pop(call.handle, None)
        return True

    async def reap_once(self):
        expired = [c for c in self.calls.values() if c.state == 'closing' or
                   c.state == 'pending' and time.monotonic() - c.created > self.ttl]
        await asyncio.gather(*(self.retire(c) for c in expired))

    async def reap(self):
        while True:
            await asyncio.sleep(5)
            await self.reap_once()

    async def start(self):
        self.reaper = asyncio.create_task(self.reap())

    async def close(self):
        self.stopping = True
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        # Creation is bounded by the HTTP timeout; wait for handles before cleanup.
        pending = list(self.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for _ in range(2):
            await asyncio.gather(*(self.retire(c) for c in list(self.calls.values())))
            if not self.calls:
                break
        if self.calls:
            LOG.error('voice_shutdown_cleanup_unconfirmed count=%d', len(self.calls))
        await self.provider.close()

    async def sideband(self, ws, handle):
        if not self.authorized(ws.headers):
            await ws.close(code=1008)
            return
        call = self.calls.get(handle)
        if self.stopping or not call or call.state != 'pending' or time.monotonic() - call.created > self.ttl:
            await ws.close(code=1008)
            return
        call.state = 'attaching'
        upstream = None
        tasks = []
        try:
            upstream = await self.provider.attach(call)
        except (VoiceError, OSError, TimeoutError):
            if call.state == 'attaching':
                call.state = 'pending'  # Stock Codex may retry its initial attachment.
            await ws.close(code=1013)
            return
        except asyncio.CancelledError:
            if call.state == 'attaching':
                call.state = 'pending'
            raise
        if self.stopping or call.state != 'attaching' or self.calls.get(handle) is not call:
            await upstream.close()
            await ws.close(code=1013)
            return
        call.upstream, call.client, call.state = upstream, ws, 'active'
        try:
            await ws.accept()
            async def upload():
                while True:
                    message = await ws.receive_text()
                    if len(message.encode()) > MAX_BYTES:
                        await ws.close(code=1009)
                        return
                    await upstream.send(message)
                    try:
                        if json.loads(message).get('type') == 'session.close':
                            return
                    except (ValueError, AttributeError):
                        pass
            async def download():
                async for message in upstream:
                    if isinstance(message, str):
                        await ws.send_text(message)
            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionClosed, WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.retire(call)


async def parse_offer(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise VoiceError(413, 'offer_too_large', 'Voice offer exceeds 1 MiB.')
        chunks.append(chunk)
    request._body = b''.join(chunks)
    try:
        content_type = request.headers.get('content-type', '').lower()
        if content_type.startswith('multipart/form-data'):
            async with request.form(max_files=0, max_fields=2, max_part_size=MAX_BYTES) as form:
                sdp, session = form.get('sdp'), form.get('session')
                if not isinstance(sdp, str) or not isinstance(session, str):
                    raise ValueError()
                session = json.loads(session)
        elif content_type.startswith('application/json'):
            body = json.loads(request._body)
            sdp, session = body['sdp'], body['session']
        else:
            raise ValueError()
        if not isinstance(sdp, str) or not sdp.startswith('v=0') or not isinstance(session, dict):
            raise ValueError()
        if session.get('model') != MODEL or session.get('delegation', {}).get('type') != 'client':
            raise VoiceError(400, 'unsupported_voice_session', 'Use gpt-live-1-codex with client delegation.')
        return sdp, session
    except (ValueError, KeyError, TypeError, AttributeError, HTTPException):
        raise VoiceError(400, 'invalid_voice_offer', 'Expected a Codex multipart or JSON SDP/session offer.') from None


def install(app, gateway=None):
    if version('litellm') != '1.102.1':
        raise RuntimeError('Revalidate or remove live_voice before upgrading LiteLLM')
    if getattr(app.state, 'subscription_voice', None):
        raise RuntimeError('Subscription voice already installed')
    gateway = gateway or VoiceGateway(lambda: os.environ.get('LITELLM_MASTER_KEY'))
    app.state.subscription_voice = gateway

    @app.post('/v1/live', include_in_schema=False)
    async def create(request: Request):
        if not gateway.authorized(request.headers):
            return JSONResponse({'error': {'code': 'invalid_gateway_credential', 'message': 'Invalid gateway credential.'}}, status_code=401)
        try:
            sdp, session = await parse_offer(request)
            call, answer = await gateway.create(sdp, session, request.headers)
            return Response(answer, status_code=201, media_type='application/sdp',
                            headers={'Location': '/v1/live/' + call.handle})
        except VoiceError as error:
            return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)
        except (httpx.HTTPError, OSError, TimeoutError):
            return JSONResponse({'error': {'code': 'voice_upstream_unavailable', 'message': 'Subscription voice backend unavailable.'}}, status_code=502)

    @app.websocket('/v1/live/{handle}')
    async def sideband(ws: WebSocket, handle: str):
        await gateway.sideband(ws, handle)

    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app) as state:
            await gateway.start()
            try:
                yield state
            finally:
                await gateway.close()
    app.router.lifespan_context = lifespan
