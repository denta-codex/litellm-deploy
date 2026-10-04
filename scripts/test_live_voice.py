"""Lifecycle and wire-contract checks with synthetic credentials and no network."""
import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from live_voice import Call, MODEL, MAX_BYTES, SubscriptionProvider, VoiceError, VoiceGateway, install

KEY = 'test-private-gateway-key'
SESSION = {'model': MODEL, 'audio': {'output': {'voice': 'cove'}}, 'delegation': {'type': 'client'}}


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
    def __aiter__(self):
        return self
    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item
    async def send(self, message):
        self.sent.append(message)
    async def close(self):
        self.closed = True
        await self.queue.put(None)


class Client:
    def __init__(self, key=KEY):
        self.headers = {'authorization': 'Bearer ' + key}
        self.queue = asyncio.Queue()
        self.sent = []
        self.accepted = asyncio.Event()
        self.closed = None
    async def accept(self):
        self.accepted.set()
    async def close(self, code=1000):
        self.closed = code
    async def receive_text(self):
        value = await self.queue.get()
        if value is None:
            raise WebSocketDisconnect()
        return value
    async def send_text(self, value):
        self.sent.append(value)


class Provider:
    def __init__(self):
        self.created, self.ended, self.sockets = [], [], []
        self.fail_attach = False
        self.fail_hangup = False
        self.gate = None
    async def create(self, sdp, session, headers):
        self.created.append((sdp, session, headers))
        if self.gate:
            await self.gate.wait()
        return 'rtc_upstream_' + str(len(self.created)), b'v=0\r\nanswer', {'Authorization': 'Bearer subscription-secret'}
    async def attach(self, call):
        if self.fail_attach:
            raise VoiceError(503, 'temporary', 'temporary')
        socket = Socket()
        self.sockets.append(socket)
        return socket
    async def hangup(self, call):
        if self.fail_hangup:
            raise OSError('offline')
        self.ended.append(call.upstream_id)
    async def close(self):
        pass


class Routes(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.gateway = VoiceGateway(lambda: KEY, self.provider)
        app = FastAPI()
        install(app, self.gateway)
        self.client = self.enterContext(TestClient(app))
        self.headers = {'Authorization': 'Bearer ' + KEY}

    def test_authentication_precedes_provider_access(self):
        self.assertEqual(self.client.post('/v1/live', json={}).status_code, 401)
        self.assertFalse(self.provider.created)

    def test_multipart_preserves_session_and_hides_credentials(self):
        response = self.client.post('/v1/live', headers=self.headers, files={
            'sdp': (None, 'v=0\r\noffer'), 'session': (None, json.dumps(SESSION))})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.provider.created[0][1], SESSION)
        self.assertTrue(response.headers['location'].startswith('/v1/live/rtc_'))
        self.assertNotIn('upstream', response.headers['location'])
        self.assertNotIn('subscription-secret', str(response.headers) + response.text)

    def test_bad_model_and_invalid_offers(self):
        for payload in ({}, [], {'sdp':'v=0', 'session':{}}, {'sdp': [], 'session':SESSION}):
            self.assertEqual(self.client.post('/v1/live', headers=self.headers, json=payload).status_code, 400)
        self.assertFalse(self.provider.created)

    def test_offer_bound(self):
        response = self.client.post('/v1/live', headers=self.headers, content=b'x' * (MAX_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.provider.created)

    def test_missing_handle_and_wrong_key_cannot_attach(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/v1/live/rtc_unknown', headers=self.headers):
                pass
        self.assertFalse(self.provider.sockets)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = Provider()
        self.gateway = VoiceGateway(lambda: KEY, self.provider, ttl=0.01, capacity=2)
    async def asyncTearDown(self):
        await self.gateway.close()
    async def create(self):
        return (await self.gateway.create('v=0', SESSION, {}))[0]

    async def test_retry_does_not_consume_handle_and_duplicate_is_rejected(self):
        call = await self.create()
        self.provider.fail_attach = True
        await self.gateway.sideband(Client(), call.handle)
        self.assertEqual(call.state, 'pending')
        self.provider.fail_attach = False
        client = Client()
        task = asyncio.create_task(self.gateway.sideband(client, call.handle))
        await client.accepted.wait()
        duplicate = Client()
        await self.gateway.sideband(duplicate, call.handle)
        self.assertEqual(duplicate.closed, 1008)
        await client.queue.put(None)
        await task
        self.assertEqual(self.provider.ended, [call.upstream_id])
        self.assertFalse(self.gateway.calls)

    async def test_two_calls_relay_independently_and_explicit_close_terminates(self):
        clients, calls, tasks = [], [], []
        for _ in range(2):
            call = await self.create()
            client = Client()
            tasks.append(asyncio.create_task(self.gateway.sideband(client, call.handle)))
            await client.accepted.wait()
            clients.append(client); calls.append(call)
        for i, call in enumerate(calls):
            await call.upstream.queue.put('event-' + str(i))
        await asyncio.sleep(0)
        self.assertEqual(clients[0].sent, ['event-0'])
        self.assertEqual(clients[1].sent, ['event-1'])
        for client in clients:
            await client.queue.put('{"type":"session.close"}')
        await asyncio.gather(*tasks)
        self.assertEqual(len(self.provider.ended), 2)

    async def test_capacity_includes_inflight_creation_and_cancellation_cleans_up(self):
        self.provider.gate = asyncio.Event()
        tasks = [asyncio.create_task(self.create()) for _ in range(2)]
        await asyncio.sleep(0)
        with self.assertRaises(VoiceError) as result:
            await self.create()
        self.assertEqual(result.exception.status, 429)
        tasks[0].cancel()
        self.provider.gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        await self.gateway.close()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(len(self.provider.ended), 2)

    async def test_failed_cleanup_stays_tracked_for_retry(self):
        call = await self.create()
        self.provider.fail_hangup = True
        self.assertFalse(await self.gateway.retire(call))
        self.assertIn(call.handle, self.gateway.calls)
        self.provider.fail_hangup = False
        self.assertTrue(await self.gateway.retire(call))
        self.assertFalse(self.gateway.calls)

    async def test_abandoned_offer_expires_and_reaper_retries_hangup(self):
        call = await self.create()
        call.created -= 10
        self.provider.fail_hangup = True
        await self.gateway.reap_once()
        self.assertEqual(call.state, 'closing')
        self.provider.fail_hangup = False
        await self.gateway.reap_once()
        self.assertFalse(self.gateway.calls)
        self.assertEqual(self.provider.ended, [call.upstream_id])

    async def test_shutdown_during_attachment_cannot_resurrect_call(self):
        call = await self.create()
        entered, release = asyncio.Event(), asyncio.Event()
        socket = Socket()
        async def delayed_attach(call):
            entered.set()
            await release.wait()
            return socket
        self.provider.attach = delayed_attach
        client = Client()
        task = asyncio.create_task(self.gateway.sideband(client, call.handle))
        await entered.wait()
        await self.gateway.close()
        release.set()
        await task
        self.assertTrue(socket.closed)
        self.assertFalse(client.accepted.is_set())
        self.assertFalse(self.gateway.calls)

    async def test_shutdown_terminates_pending_and_active_calls(self):
        pending = await self.create()
        active = await self.create()
        client = Client()
        task = asyncio.create_task(self.gateway.sideband(client, active.handle))
        await client.accepted.wait()
        await self.gateway.close()
        await task
        self.assertCountEqual(self.provider.ended, [pending.upstream_id, active.upstream_id])
        self.assertFalse(self.gateway.calls)


class Auth(unittest.TestCase):
    def test_failed_refresh_never_starts_device_login(self):
        with patch('live_voice.Authenticator') as cls:
            auth = cls.return_value
            auth._read_auth_file.return_value = {'access_token':'expired','refresh_token':'refresh'}
            auth._is_token_expired.return_value = True
            auth._refresh_tokens.side_effect = RuntimeError('sensitive provider body')
            with self.assertRaises(VoiceError) as result:
                SubscriptionProvider.load_headers()
            self.assertEqual(result.exception.code, 'subscription_refresh_failed')
            self.assertNotIn('sensitive', str(result.exception))
            auth._login_device_code.assert_not_called()
            auth.get_access_token.assert_not_called()


if __name__ == '__main__':
    unittest.main()
