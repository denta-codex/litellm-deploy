"""Real isolated stock app-server turns with ChatGPT identity and proxy auth.

The loopback relay checks credentials in memory and reports only tier metadata.
The temporary native login has no refresh token and is removed on every exit.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile

from aiohttp import ClientSession, ClientTimeout, web
import tomlkit


async def verify(args, home):
    native = json.loads((Path.home() / '.codex/auth.json').read_text())
    native['tokens']['refresh_token'] = ''
    native['OPENAI_API_KEY'] = None
    (home / 'auth.json').write_text(json.dumps(native))
    key = subprocess.check_output(['/usr/bin/systemd-creds', 'decrypt', '--user',
        '--name=litellm-proxy-key', str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
    observed = []
    async with ClientSession(timeout=ClientTimeout(total=240)) as session:
        async def relay(request):
            if request.headers.get('Authorization') != 'Bearer ' + key:
                raise web.HTTPUnauthorized(reason='Proxy credential mismatch')
            body = await request.read()
            if request.path.endswith('/responses'):
                payload = json.loads(body)
                tier = payload.get('service_tier')
                observed.append(tier)
                print(json.dumps({'proxy_auth': True, 'requested_tier': tier}), flush=True)
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ('host', 'content-length', 'connection', 'accept-encoding')}
            async with session.request(request.method, args.base_url.rstrip('/') + request.path_qs,
                                       data=body, headers=headers) as upstream:
                response = web.StreamResponse(status=upstream.status, headers={
                    'Content-Type': upstream.headers.get('Content-Type', 'application/json')})
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                    await response.write_eof()
                except ConnectionResetError:
                    # Codex may close SSE immediately after response.completed.
                    pass
                return response
        app = web.Application(client_max_size=32 * 1024**2)
        app.router.add_route('*', '/{path:.*}', relay)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        doc = {
            'model_provider': 'litellm', 'model': args.model, 'model_reasoning_effort': 'low',
            'service_tier': 'default',
            'model_catalog_json': str(Path.home() / '.config/litellm/codex-models.json'),
            'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'disabled',
            'features': {'fast_mode': True, 'shell_snapshot': False},
            'shell_environment_policy': {'exclude': ['LITELLM_PROXY_KEY']},
            'model_providers': {'litellm': {
                'name': 'LiteLLM', 'wire_api': 'responses', 'supports_websockets': False,
                'base_url': 'http://127.0.0.1:4000/v1',
                'requires_openai_auth': True, 'env_key': 'LITELLM_PROXY_KEY',
            }},
        }
        doc['model_providers']['litellm']['base_url'] = f'http://127.0.0.1:{port}/v1'
        (home / 'config.toml').write_text(tomlkit.dumps(doc))
        process = await asyncio.create_subprocess_exec(str(args.launcher), '-c', 'features.code_mode_host=true', 'app-server',
            env=os.environ | {'CODEX_HOME': str(home)}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        counter = 0
        async def receive():
            line = await asyncio.wait_for(process.stdout.readline(), 240)
            if not line:
                raise RuntimeError('Isolated app server exited before completion')
            return json.loads(line)
        async def rpc(method, params):
            nonlocal counter
            counter += 1
            process.stdin.write((json.dumps({'id': counter, 'method': method, 'params': params}) + '\n').encode())
            await process.stdin.drain()
            while True:
                result = await receive()
                if result.get('id') == counter:
                    if 'error' in result:
                        raise RuntimeError(f'{method} failed with RPC code {result["error"].get("code")}')
                    return result['result']
        try:
            await rpc('initialize', {'clientInfo': {'name': 'codex_runtime_probe', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            account = await rpc('account/read', {'refreshToken': False})
            assert account['account']['type'] == 'chatgpt' and account['requiresOpenaiAuth']
            print('PASS native ChatGPT identity with custom provider', flush=True)
            started = await rpc('thread/start', {'cwd': str(home), 'ephemeral': True})
            thread = started['thread']['id']
            for tier in ('priority', 'default'):
                offset = len(observed)
                command = 'if test -z "${LITELLM_PROXY_KEY+x}"; then printf KEY_ABSENT; else printf KEY_PRESENT; fi'
                await rpc('turn/start', {'threadId': thread, 'serviceTier': tier,
                    'input': [{'type': 'text', 'text': 'Run exactly this shell command and report its output: ' + command}]})
                executed = False
                while True:
                    event = await receive()
                    if event.get('method') == 'item/completed':
                        item = event['params']['item']
                        if item.get('type') == 'commandExecution':
                            output = item.get('aggregatedOutput', '')
                            print(json.dumps({'shell_exit': item.get('exitCode'), 'key_absent': 'KEY_ABSENT' in output,
                                              'key_present': 'KEY_PRESENT' in output}), flush=True)
                            assert item.get('exitCode') == 0 and 'KEY_ABSENT' in output and 'KEY_PRESENT' not in output
                            executed = True
                    if event.get('method') == 'turn/completed':
                        assert event['params']['turn']['status'] == 'completed'
                        break
                assert executed, 'No successful real shell-environment probe'
                sent = observed[offset:]
                assert sent and all(t == 'priority' if tier == 'priority' else t in (None, 'default') for t in sent)
                print(f'PASS {tier} real turn, tool continuation, proxy authentication, key absent from shell', flush=True)
            assert not any(key.encode() in path.read_bytes() for path in home.rglob('*') if path.is_file()), 'Proxy key persisted in isolated Codex home'
            print('PASS proxy key absent from disposable Codex files', flush=True)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='chatgpt/gpt-6-astra')
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    parser.add_argument('--launcher', type=Path, default=Path.home() / '.local/bin/codex')
    args = parser.parse_args()
    os.umask(0o077)
    state = Path.home() / '.local/state/litellm'
    with tempfile.TemporaryDirectory(prefix='fast-app-probe-', dir=state) as directory:
        asyncio.run(verify(args, Path(directory)))


if __name__ == '__main__':
    main()
