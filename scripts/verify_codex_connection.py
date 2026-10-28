"""Check stock SSH proxy clients without exposing RPC data or changing live history."""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from compression import zstd
import hashlib
import json
import os
import secrets
import struct
from pathlib import Path
import tempfile


class CheckFailure(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def session_hashes(codex_home):
    files = []
    for directory in ('sessions', 'archived_sessions'):
        group = []
        for path in sorted((codex_home / directory).rglob('*')):
            if path.name.endswith('.jsonl.zst'):
                name = str(path.relative_to(codex_home))[:-4]
                data = zstd.decompress(path.read_bytes())
            elif path.suffix == '.jsonl':
                name, data = str(path.relative_to(codex_home)), path.read_bytes()
            else:
                continue
            group.append([name, hashlib.sha256(data).hexdigest()])
        files.extend(sorted(group))
    return files


class Client:
    def __init__(self, process):
        self.process = process
        self.counter = 0

    async def call(self, method, params):
        self.counter += 1
        await self.send({'id': self.counter, 'method': method, 'params': params})
        while True:
            message = json.loads(await asyncio.wait_for(self.receive(), 30))
            if message.get('id') == self.counter:
                if 'error' in message or not isinstance(message.get('result'), dict):
                    raise CheckFailure(method)
                return message['result']

    async def send(self, message):
        await self.frame(json.dumps(message).encode(), 1)

    async def frame(self, payload, opcode):
        length = len(payload)
        header = bytes([0x80 | opcode])
        header += (bytes([0x80 | length]) if length < 126 else
                   b'\xfe' + struct.pack('!H', length) if length < 65536 else
                   b'\xff' + struct.pack('!Q', length))
        mask = secrets.token_bytes(4)
        self.process.stdin.write(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
        await self.process.stdin.drain()

    async def handshake(self):
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.process.stdin.write((f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        await self.process.stdin.drain()
        headers = await asyncio.wait_for(self.process.stdout.readuntil(b'\r\n\r\n'), 10)
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if not headers.startswith(b'HTTP/1.1 101 ') or accept.lower() not in headers.lower():
            raise CheckFailure('websocket_upgrade')

    async def receive(self):
        chunks = []
        while True:
            header = await self.process.stdout.readexactly(2)
            final, opcode = bool(header[0] & 0x80), header[0] & 0x0f
            length = header[1] & 0x7f
            if length == 126:
                length = struct.unpack('!H', await self.process.stdout.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', await self.process.stdout.readexactly(8))[0]
            if length > 16_000_000 or header[1] & 0x80:
                raise CheckFailure('websocket_frame')
            payload = await self.process.stdout.readexactly(length)
            if opcode == 8:
                raise CheckFailure('proxy_closed')
            if opcode == 9:
                await self.frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise CheckFailure('websocket_opcode')
            chunks.append(payload)
            if sum(map(len, chunks)) > 16_000_000:
                raise CheckFailure('websocket_message')
            if final:
                return b''.join(chunks)

    async def inventory(self, method):
        items, cursor = [], None
        while True:
            params = {'limit': 100}
            if method == 'thread/list':
                # Include every provider so a provider change cannot hide old chats.
                params['modelProviders'] = []
            if cursor:
                params['cursor'] = cursor
            page = await self.call(method, params)
            if not isinstance(page.get('data'), list):
                raise CheckFailure(method)
            items.extend(str(item['id']) for item in page['data'])
            next_cursor = page.get('nextCursor')
            if not next_cursor:
                return sorted(items)
            if next_cursor == cursor:
                raise CheckFailure('pagination')
            cursor = next_cursor


async def stop(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()


@asynccontextmanager
async def proxy(binary, home, socket, environment):
    process = await asyncio.create_subprocess_exec(
        str(binary), 'app-server', 'proxy', '--sock', str(socket),
        env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16_000_000)
    try:
        client = Client(process)
        await client.handshake()
        initialized = await client.call('initialize', {
            'clientInfo': {'name': 'codex_ssh_connection_check', 'version': '1'},
            'capabilities': {'experimentalApi': True}})
        if initialized.get('codexHome') != str(home):
            raise CheckFailure('shared_home')
        await client.send({'method': 'initialized'})
        yield client
    finally:
        await stop(process)


async def shared_clients(binary, home, environment):
    socket = home / 'app-server-control/app-server-control.sock'
    async with proxy(binary, home, socket, environment) as first, \
            proxy(binary, home, socket, environment) as second:
        inventories = []
        for method in ('project/list', 'thread/list'):
            left, right = await asyncio.gather(first.inventory(method), second.inventory(method))
            if left != right:
                raise CheckFailure('shared_inventory_changed')
            inventories.append(left)
        return {'projects': inventories[0], 'threads': inventories[1]}


def clean_environment(home):
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home))
    for name in ('LITELLM_PROXY_KEY', 'OPENAI_API_KEY', 'OPENAI_AUTH_TOKEN'):
        env.pop(name, None)
    return env


async def candidate(binary):
    targets = set()
    with tempfile.TemporaryDirectory(prefix='codex-ssh-check-') as directory:
        home = Path(directory)
        home.chmod(0o700)
        (home / 'config.toml').write_text('''model = "fixture"
model_provider = "fixture"
cli_auth_credentials_store = "file"
[model_providers.fixture]
name = "Fixture"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false
''')
        env = clean_environment(home)
        socket = home / 'app-server-control/app-server-control.sock'
        try:
            for _ in range(2):
                server = await asyncio.create_subprocess_exec(
                    str(binary), '-c', 'features.code_mode_host=true', 'app-server', '--listen', 'unix://',
                    env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    for _ in range(200):
                        if socket.exists():
                            break
                        if server.returncode is not None:
                            raise CheckFailure('candidate_server')
                        await asyncio.sleep(0.1)
                    else:
                        raise CheckFailure('candidate_socket')
                    targets.add(socket.resolve())
                    await shared_clients(binary, home, env)
                finally:
                    await stop(server)
                socket.unlink(missing_ok=True)
        finally:
            for target in targets:
                if target.parent == Path(f'/tmp/codex-daemon-{os.getuid()}'):
                    for path in (target, target.with_suffix('.lock')):
                        try:
                            if path.lstat().st_uid == os.getuid():
                                path.unlink()
                        except FileNotFoundError:
                            pass
    return {'ok': True, 'clients': 2, 'candidate_restart': True, 'cleanup': True}


async def verify(action, binary, home):
    if action == 'candidate':
        return await candidate(binary)
    codex_home = home / '.codex'
    state = home / '.local/state/litellm/codex-connection-baseline.json'
    inventory = await shared_clients(binary, codex_home, clean_environment(home))
    auth = codex_home / 'auth.json'
    report = {name: digest(ids) for name, ids in inventory.items()}
    report['auth'] = hashlib.sha256(auth.read_bytes()).hexdigest()
    files = session_hashes(codex_home)
    report['session_files'] = digest(files)
    history = codex_home / 'history.jsonl'
    report['history_file'] = hashlib.sha256(history.read_bytes()).hexdigest() if history.exists() else None
    if action == 'baseline':
        if state.exists():
            raise CheckFailure('baseline_already_exists')
        with state.open('x') as output:
            json.dump(report, output)
    elif state.exists() and json.loads(state.read_text()) != report:
        raise CheckFailure('history_or_native_auth_changed')
    return {'ok': True, 'clients': 2, 'shared_home': True,
            'projects': len(inventory['projects']), 'threads': len(inventory['threads']), 'session_files': len(files),
            'history_and_native_auth_preserved': action == 'live' and state.exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['candidate', 'baseline', 'live'])
    parser.add_argument('--codex-binary', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = asyncio.run(verify(args.action, args.codex_binary, args.home))
    except (CheckFailure, OSError, ValueError, KeyError, TimeoutError) as error:
        result = {'ok': False, 'stage': str(error) if isinstance(error, CheckFailure) else 'ssh_proxy_acceptance'}
    print(json.dumps(result, sort_keys=True))
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
