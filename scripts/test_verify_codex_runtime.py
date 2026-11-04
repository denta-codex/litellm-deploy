"""Configured-default runtime probes must not require a subscription model."""
from collections import deque
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

import verify_codex_runtime as runtime


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.home / '.codex').mkdir()
        self.model = 'vertex/gemini-4-argon'
        self.catalog = self.home / 'catalog.json'
        self.row = {'slug': self.model, 'visibility': 'list', 'service_tiers': [],
                    'supported_reasoning_levels': [{'effort': 'high'}],
                    'default_reasoning_level': 'high', 'tool_mode': 'code_mode_only'}
        self.config = {'model_provider': 'litellm', 'model': self.model,
                       'model_catalog_json': str(self.catalog), 'model_providers': {'litellm': {
                           'name': 'LiteLLM', 'base_url': 'http://127.0.0.1:4567/v1',
                           'wire_api': 'responses', 'requires_openai_auth': True,
                           'env_key': 'LITELLM_PROXY_KEY'}}}
        self.save()

    def save(self, rows=None):
        (self.home / '.codex/config.toml').write_text(tomlkit.dumps(self.config))
        self.catalog.write_text(json.dumps({'models': rows if rows is not None else [self.row]}))


class ConfigTests(ConfigFixture):
    def test_selected_vertex_needs_neither_astra_nor_priority_nor_low(self):
        config, row = runtime.configured_model(self.home)
        self.assertEqual(config['model'], self.model)
        self.assertEqual(runtime.service_tiers(row), ('default',))
        self.assertIn('tools.exec_command', runtime.isolation_prompt(row))

    def test_override_uses_that_catalog_rows_capabilities(self):
        subscription = dict(self.row, slug='chatgpt/fixture', service_tiers=[{'id': 'priority'}])
        self.save([self.row, subscription])
        _, row = runtime.configured_model(self.home, 'chatgpt/fixture')
        self.assertEqual(runtime.service_tiers(row), ('priority', 'default'))

    def test_missing_hidden_or_ambiguous_default_is_not_accepted(self):
        for rows in ([], [dict(self.row, visibility='hide')], [self.row, self.row]):
            with self.subTest(rows=rows):
                self.save(rows)
                with self.assertRaisesRegex(RuntimeError, 'selectable catalog row'):
                    runtime.configured_model(self.home)

    def test_provider_must_preserve_identity_and_proxy_auth(self):
        provider = self.config['model_providers']['litellm']
        for key, value in (('wire_api', 'chat'), ('requires_openai_auth', False),
                           ('env_key', 'OPENAI_API_KEY')):
            with self.subTest(key=key):
                old = provider[key]
                provider[key] = value
                self.save()
                with self.assertRaisesRegex(RuntimeError, 'genuine ChatGPT identity'):
                    runtime.configured_model(self.home)
                provider[key] = old
        self.config['model_provider'] = 'openai'
        self.save()
        with self.assertRaises(RuntimeError):
            runtime.configured_model(self.home)


class FakeRPC:
    def __init__(self, model, *, identity='chatgpt', provider='litellm', fast=False, output='KEY_ABSENT'):
        self.model, self.identity, self.provider = model, identity, provider
        self.fast, self.output = fast, output
        self.messages, self.pending = [], deque()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def request(self, message):
        self.messages.append(message)
        if 'id' not in message:
            return
        method = message['method']
        result = {}
        if method == 'account/read':
            result = {'account': {'type': self.identity}, 'requiresOpenaiAuth': True}
        elif method == 'model/list':
            # The configured model need not be on the first page.
            result = ({'data': [], 'nextCursor': 'second-page'} if not message['params']['cursor'] else
                      {'data': [{'model': self.model, 'serviceTiers': [{'id': 'priority'}] if self.fast else []}]})
        elif method == 'thread/start':
            result = {'thread': {'id': 'fixture'}, 'model': self.model, 'modelProvider': self.provider}
        self.pending.append({'id': message['id'], 'result': result})
        if method == 'turn/start':
            self.pending.extend([
                {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution',
                    'exitCode': 0, 'aggregatedOutput': self.output}}},
                {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])

    async def send(self, data):
        self.request(json.loads(data))

    async def recv(self):
        return json.dumps(self.pending.popleft())


class LiveTests(ConfigFixture):
    def run_probe(self, rpc):
        import asyncio

        original_resolve = Path.resolve
        expected = self.home / '.local/share/mise/installs/codex/fixture/bin/codex'

        def resolve(path, *args, **kwargs):
            return expected if str(path) == '/proc/123/exe' else original_resolve(path, *args, **kwargs)

        with patch.object(runtime.subprocess, 'check_output', return_value='123\n'), \
                patch.object(Path, 'resolve', autospec=True, side_effect=resolve), \
                patch.object(Path, 'read_bytes', return_value=b'LITELLM_PROXY_KEY=fixture\0'), \
                patch.object(runtime, 'unix_connect', return_value=rpc), redirect_stdout(io.StringIO()) as output:
            asyncio.run(runtime.verify('fixture', self.home / 'socket', self.home))
        return json.loads(output.getvalue())

    def test_vertex_default_passes_with_real_identity_and_no_fast(self):
        rpc = FakeRPC(self.model)
        result = self.run_probe(rpc)
        self.assertEqual((result['model'], result['fast_capability'], result['chatgpt_identity']),
                         (self.model, False, True))
        starts = [m for m in rpc.messages if m['method'] in ('thread/start', 'turn/start')]
        self.assertTrue(all(m['params']['serviceTier'] == 'default' for m in starts))
        self.assertNotIn('model', starts[0]['params'])  # Observe, never override, the live default.

    def test_advertised_subscription_fast_is_still_checked(self):
        self.row.update(slug='chatgpt/fixture', service_tiers=[{'id': 'priority'}])
        self.config['model'] = self.row['slug']
        self.save()
        self.assertTrue(self.run_probe(FakeRPC(self.row['slug'], fast=True))['fast_capability'])
        with self.assertRaisesRegex(RuntimeError, 'Fast capability differs'):
            self.run_probe(FakeRPC(self.row['slug']))

    def test_wrong_identity_default_provider_or_leaked_key_fails(self):
        for rpc, error in ((FakeRPC(self.model, identity='apiKey'), 'genuine ChatGPT identity'),
                           (FakeRPC('chatgpt/unconfigured'), 'configured model'),
                           (FakeRPC(self.model, provider='openai'), 'configured default'),
                           (FakeRPC(self.model, output='KEY_PRESENT'), 'inherited the proxy credential')):
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                self.run_probe(rpc)


if __name__ == '__main__':
    unittest.main()
