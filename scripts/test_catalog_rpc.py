"""Offline contract with the pinned stock binary; no inference or account reads."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from catalog_enrichment import enrich_catalog
from test_refresh_models import REVIEW_MODEL, encode, generate, model

PINNED_CODEX = Path('/home/agent/.local/share/mise/installs/codex/0.159.2/bin/codex')


@unittest.skipUnless(PINNED_CODEX.is_file(), 'Pinned stock Codex 0.159.2 unavailable')
class StockCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def stock_reads(self, home, catalog):
        (home / 'config.toml').write_text(
            'model_provider="litellm"\nmodel="chatgpt/fixture"\nmodel_catalog_json=' + json.dumps(str(catalog)) + '\n'
            '[model_providers.litellm]\nname="Offline fixture"\nbase_url="http://127.0.0.1:1/v1"\nwire_api="responses"\n')
        process = await asyncio.create_subprocess_exec(
            str(PINNED_CODEX), 'app-server', cwd=home,
            env=os.environ | {'CODEX_HOME': str(home)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.addAsyncCleanup(self.stop, process)
        counter = 0

        async def rpc(method, params):
            nonlocal counter
            counter += 1
            process.stdin.write((json.dumps({'id': counter, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 20)
                self.assertTrue(line, 'Stock app-server exited before replying')
                result = json.loads(line)
                if result.get('id') == counter:
                    self.assertNotIn('error', result, f'{method}: {result.get("error")}')
                    return result['result']

        await rpc('initialize', {'clientInfo': {'name': 'catalog_fixture', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        process.stdin.write(b'{"method":"initialized"}\n')
        await process.stdin.drain()
        config = await rpc('config/read', {'includeLayers': False})
        self.assertEqual(config['config']['model_provider'], 'litellm')
        path = config['config']['model_catalog_json']
        self.assertEqual(path, str(catalog))
        file = await rpc('fs/readFile', {'path': path})
        self.assertEqual(base64.b64decode(file['dataBase64']), catalog.read_bytes())
        rows, cursor = [], None
        while True:
            page = await rpc('model/list', {'limit': 2, 'cursor': cursor})
            rows.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
        await self.stop(process)
        return rows

    async def stop(self, process):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def test_enrichment_is_accepted_and_stock_rpc_fields_are_unchanged(self):
        source = {'models': [model('fixture'), model('fixture-two'), model('fixture-three'), model(REVIEW_MODEL, 'hide')]}
        config, catalog, _ = generate(source, {'model_list': []}, {'models': []}, 'chatgpt/fixture')
        for row in catalog['models']:
            row.update(description='Offline fixture', default_reasoning_level='high',
                       supported_reasoning_levels=[{'effort': 'high', 'description': 'High'}],
                       shell_type='shell_command', supported_in_api=True, priority=1,
                       supports_reasoning_summaries=True, support_verbosity=True,
                       default_verbosity='low', experimental_supported_tools=[],
                       truncation_policy={'mode': 'tokens', 'limit': 10000})
        enriched, _ = enrich_catalog(catalog, config, {'models': []}, {'openai': {'fixture': [{
            'tier': 'standard', 'context': 'short', 'input_per_million': 2, 'output_per_million': 10}]},
            'models_dev': {}}, '2026-10-05T00:00:00Z')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parsed, listed = [], []
            for name, document in (('stock', catalog), ('enriched', enriched)):
                home = root / name
                home.mkdir()
                path = home / 'catalog.json'
                path.write_text(encode(document))
                result = subprocess.run([str(PINNED_CODEX), 'debug', 'models', '-c',
                    'model_catalog_json=' + json.dumps(str(path))], env=os.environ | {'CODEX_HOME': str(home)},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed.append(json.loads(result.stdout))
                listed.append(await self.stock_reads(home, path))
            self.assertEqual(parsed[0], parsed[1])
            self.assertEqual(listed[0], listed[1])
            self.assertEqual([r['model'] for r in listed[1]], ['chatgpt/fixture', 'chatgpt/fixture-two', 'chatgpt/fixture-three'])
            self.assertNotIn('remote_codex', parsed[1])


if __name__ == '__main__':
    unittest.main()
