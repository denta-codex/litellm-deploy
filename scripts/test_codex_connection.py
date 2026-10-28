"""Wire-level SSH proxy checks and an opt-in real pinned candidate restart."""
import asyncio
from compression import zstd
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from verify_codex_connection import Client, CheckFailure, session_hashes


class HistoryTests(unittest.TestCase):
    def test_native_compression_preserves_comparable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = [home / 'sessions/older.jsonl', home / 'sessions/recent.jsonl',
                     home / 'archived_sessions/archived.jsonl']
            for path in paths:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b'{"fixture":"' + path.stem.encode() + b'"}\n')
            before = session_hashes(home)
            for path in (paths[0], paths[2]):
                path.with_suffix('.jsonl.zst').write_bytes(zstd.compress(path.read_bytes()))
                path.unlink()
            self.assertEqual(session_hashes(home), before)
            paths[1].write_bytes(b'{"changed":true}\n')
            self.assertNotEqual(session_hashes(home), before)


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


class WireTests(unittest.IsolatedAsyncioTestCase):
    def client(self, data):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        process = type('Process', (), {'stdout': reader, 'stdin': Writer()})()
        return Client(process)

    async def test_fragmented_response_and_ping(self):
        client = self.client(b'\x89\x02hi\x01\x07{"resul\x80\x06t":{}}')
        self.assertEqual(json.loads(await client.receive()), {'result': {}})
        self.assertEqual(client.process.stdin.data[0], 0x8a)
        self.assertTrue(client.process.stdin.data[1] & 0x80)

    async def test_rpc_error_never_exposes_private_message(self):
        data = json.dumps({'id': 1, 'error': {'message': 'private-value'}}).encode()
        client = self.client(bytes([0x81, len(data)]) + data)
        with self.assertRaises(CheckFailure) as caught:
            await client.call('thread/list', {})
        self.assertEqual(str(caught.exception), 'thread/list')

    async def test_history_pagination_includes_every_provider(self):
        responses = [{'data': [{'id': 'b'}], 'nextCursor': 'next'}, {'data': [{'id': 'a'}]}]
        params = []
        client = self.client(b'')
        async def call(method, arguments):
            params.append(arguments)
            return responses.pop(0)
        client.call = call
        self.assertEqual(await client.inventory('thread/list'), ['a', 'b'])
        self.assertTrue(all(item['modelProviders'] == [] for item in params))
        self.assertEqual(params[1]['cursor'], 'next')


@unittest.skipUnless(os.environ.get('CODEX_CONNECTION_TEST_BINARY'), 'set CODEX_CONNECTION_TEST_BINARY for real candidate')
class CandidateTests(unittest.TestCase):
    def test_two_clients_reconnect_after_candidate_restart(self):
        result = subprocess.run([os.environ['CODEX_CONNECTION_TEST_UV'], 'run', '--no-project',
            str(Path(__file__).with_name('verify_codex_connection.py')), 'candidate',
            '--codex-binary', os.environ['CODEX_CONNECTION_TEST_BINARY'], '--home', str(Path.home())],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout),
            {'ok': True, 'clients': 2, 'candidate_restart': True, 'cleanup': True})


if __name__ == '__main__':
    unittest.main()
