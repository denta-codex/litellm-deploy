"""Offline staged probes retain identity/isolation and only request advertised tiers."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import tomlkit

from test_verify_codex_runtime import ConfigFixture, FakeRPC
import verify_fast_app_server as staged


class FakeUpstream:
    status = 200
    headers = {'Content-Type': 'text/event-stream'}

    def __init__(self):
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_any(self):
        yield b'fixture'


class FakeSession:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return FakeUpstream()


class StagedTests(ConfigFixture):
    def run_probe(self, *, identity='chatgpt', wrong_tier=False, output='KEY_ABSENT', override=None,
                  all_tiers=False):
        isolated = self.home / 'isolated'
        isolated.mkdir()
        auth = {'tokens': {'access_token': 'fixture-access', 'refresh_token': 'never-copy-me'},
                'OPENAI_API_KEY': 'never-copy-me'}
        (self.home / '.codex/auth.json').write_text(json.dumps(auth))
        before = (self.home / '.codex/auth.json').read_bytes()
        args = SimpleNamespace(home=self.home, model=override, base_url=None, launcher=None,
                               all_tiers=all_tiers)
        session = FakeSession()
        handlers, launched = [], []

        def runner(app, **kwargs):
            handlers.append(next(iter(app.router.routes())).handler)
            return SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())

        async def launch(*argv, **kwargs):
            config = tomlkit.loads((isolated / 'config.toml').read_text())
            selected = config['model']
            rpc = FakeRPC(selected, identity=identity, output=output)
            to_relay = []

            def write(data):
                message = json.loads(data)
                rpc.request(message)
                if message['method'] == 'turn/start':
                    tier = message['params']['serviceTier']
                    to_relay.append(None if wrong_tier or tier == 'default' else tier)

            async def readline():
                while to_relay:
                    payload = {'model': selected, 'service_tier': to_relay.pop(0)}
                    request = SimpleNamespace(headers={'Authorization': 'Bearer fixture-proxy-key'},
                        method='POST', path='/v1/responses', path_qs='/v1/responses',
                        read=AsyncMock(return_value=json.dumps(payload).encode()))
                    await handlers[0](request)
                return (await rpc.recv() + '\n').encode()

            process = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()),
                                      stdout=SimpleNamespace(readline=readline), returncode=None,
                                      terminate=lambda: None, wait=AsyncMock())
            launched.append((argv, kwargs, config, rpc, process))
            return process

        response = SimpleNamespace(prepare=AsyncMock(), write=AsyncMock(), write_eof=AsyncMock())
        site = SimpleNamespace(start=AsyncMock(), _server=SimpleNamespace(
            sockets=[SimpleNamespace(getsockname=lambda: ('127.0.0.1', 12345))]))
        with patch.object(staged, 'ClientSession', return_value=session), \
                patch.object(staged.web, 'AppRunner', side_effect=runner), \
                patch.object(staged.web, 'TCPSite', return_value=site), \
                patch.object(staged.web, 'StreamResponse', return_value=response), \
                patch.object(staged, 'proxy_key', return_value='fixture-proxy-key') as key, \
                patch.object(staged, 'codex_binary', return_value='configured-codex') as binary, \
                patch.object(staged.asyncio, 'create_subprocess_exec', side_effect=launch), \
                redirect_stdout(io.StringIO()):
            asyncio.run(staged.verify(args, isolated))
        self.assertEqual((self.home / '.codex/auth.json').read_bytes(), before)
        copy = json.loads((isolated / 'auth.json').read_text())
        self.assertEqual((copy['tokens']['refresh_token'], copy['OPENAI_API_KEY']), ('', None))
        key.assert_called_once_with(home=self.home)
        binary.assert_called_once_with(home=self.home)
        return launched[0], session

    def test_vertex_without_astra_probes_default_tier_and_supported_effort(self):
        (argv, kwargs, config, rpc, _), session = self.run_probe()
        self.assertEqual((config['model'], config['model_reasoning_effort']), (self.model, 'high'))
        self.assertEqual(config['model_catalog_json'], str(self.catalog))
        self.assertEqual(config['model_providers']['litellm']['requires_openai_auth'], True)
        self.assertEqual(argv[0], 'configured-codex')
        self.assertEqual(kwargs['env']['LITELLM_PROXY_KEY'], 'fixture-proxy-key')
        turns = [m for m in rpc.messages if m['method'] == 'turn/start']
        self.assertEqual([m['params']['serviceTier'] for m in turns], ['default'])
        self.assertIn('tools.exec_command', turns[0]['params']['input'][0]['text'])
        self.assertEqual([url for _, url, _ in session.requests], ['http://127.0.0.1:4567/v1/responses'])

    def test_optional_all_tiers_requests_priority_and_default_and_low_if_supported(self):
        self.row['service_tiers'] = [{'id': 'priority'}]
        self.row['supported_reasoning_levels'].append({'effort': 'low'})
        self.save()
        (_, _, config, rpc, _), session = self.run_probe(all_tiers=True)
        self.assertEqual(config['model_reasoning_effort'], 'low')
        self.assertEqual([m['params']['serviceTier'] for m in rpc.messages if m['method'] == 'turn/start'],
                         ['priority', 'default'])
        self.assertEqual([json.loads(request['data'])['service_tier'] for _, _, request in session.requests],
                         ['priority', None])

    def test_explicit_model_override_uses_its_own_reasoning_and_tier_metadata(self):
        self.save([self.row, dict(self.row, slug='claude/fixture', service_tiers=[{'id': 'priority'}])])
        (_, _, config, rpc, _), _ = self.run_probe(override='claude/fixture', all_tiers=True)
        self.assertEqual(config['model'], 'claude/fixture')
        self.assertEqual([m['params']['serviceTier'] for m in rpc.messages if m['method'] == 'turn/start'],
                         ['priority', 'default'])

    def test_api_key_identity_is_not_a_substitute_for_chatgpt(self):
        with self.assertRaises(AssertionError):
            self.run_probe(identity='apiKey')

    def test_priority_that_does_not_reach_proxy_is_rejected(self):
        self.row['service_tiers'] = [{'id': 'priority'}]
        self.save()
        with self.assertRaises(AssertionError):
            self.run_probe(wrong_tier=True, all_tiers=True)

    def test_advertised_fast_does_not_require_unconfigured_paid_tier(self):
        self.row['service_tiers'] = [{'id': 'priority'}]
        self.save()
        (_, _, _, rpc, _), _ = self.run_probe()
        self.assertEqual([m['params']['serviceTier'] for m in rpc.messages if m['method'] == 'turn/start'],
                         ['default'])

    def test_configured_priority_must_be_forwarded_and_verified(self):
        self.row['service_tiers'] = [{'id': 'priority'}]
        self.config['service_tier'] = 'priority'
        self.save()
        (_, _, config, rpc, _), _ = self.run_probe()
        self.assertEqual(config['service_tier'], 'priority')
        self.assertEqual([m['params']['serviceTier'] for m in rpc.messages if m['method'] == 'turn/start'],
                         ['priority'])

    def test_shell_key_leak_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.run_probe(output='KEY_PRESENT')


if __name__ == '__main__':
    unittest.main()
