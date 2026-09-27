"""Verify the live systemd-owned Codex app server and a real shell-tool turn."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess

from websockets.asyncio.client import unix_connect


async def verify(version, socket):
    pid = int(subprocess.check_output(
        ['systemctl', 'show', 'codex-app-server.service', '--property=MainPID', '--value'],
        text=True,
    ).strip())
    if pid <= 0:
        raise RuntimeError('codex-app-server.service has no running process')
    expected = Path(f'/home/agent/.local/share/mise/installs/codex/{version}/bin/codex').resolve()
    actual = Path(f'/proc/{pid}/exe').resolve()
    if actual != expected:
        raise RuntimeError(f'Running app server uses {actual}, expected pinned Codex {version}')
    environment = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    if not any(value.startswith(b'LITELLM_PROXY_KEY=') and value != b'LITELLM_PROXY_KEY=' for value in environment):
        raise RuntimeError('Running app server does not have the proxy credential')

    async with unix_connect(str(socket), uri='ws://localhost/', compression=None, max_size=16_000_000) as ws:
        counter = 0

        async def rpc(method, params):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({'id': counter, 'method': method, 'params': params}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 240))
                if message.get('id') == counter:
                    if 'error' in message:
                        raise RuntimeError(f'{method} failed with RPC code {message["error"].get("code")}')
                    return message['result']

        await rpc('initialize', {'clientInfo': {'name': 'codex_runtime_probe', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        await ws.send(json.dumps({'method': 'initialized'}))
        account = await rpc('account/read', {'refreshToken': False})
        if (account.get('account') or {}).get('type') != 'chatgpt' or not account.get('requiresOpenaiAuth'):
            raise RuntimeError('Live app server does not expose genuine ChatGPT identity')
        models = await rpc('model/list', {})
        astra = next((model for model in models.get('data', []) if model.get('model') == 'chatgpt/gpt-6-astra'), None)
        if not astra or not any(tier.get('id') == 'priority' for tier in astra.get('serviceTiers', [])):
            raise RuntimeError('Live Astra metadata does not advertise Fast')

        started = await rpc('thread/start', {
            'cwd': '/home/agent', 'ephemeral': True, 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'serviceTier': 'default',
        })
        thread = started['thread']['id']
        command = 'if test -z "${LITELLM_PROXY_KEY+x}"; then printf KEY_ABSENT; else printf KEY_PRESENT; fi'
        await rpc('turn/start', {'threadId': thread, 'serviceTier': 'default',
                                 'input': [{'type': 'text', 'text': 'Run exactly this shell command and report its output: ' + command}]})
        executed = False
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 240))
            if event.get('method') == 'item/completed':
                item = event['params']['item']
                if item.get('type') == 'commandExecution':
                    output = item.get('aggregatedOutput', '')
                    if item.get('exitCode') != 0 or 'KEY_ABSENT' not in output or 'KEY_PRESENT' in output:
                        raise RuntimeError('Shell tool inherited the proxy credential or failed')
                    executed = True
            if event.get('method') == 'turn/completed':
                if event['params']['turn']['status'] != 'completed':
                    raise RuntimeError('Live app-server verification turn did not complete')
                break
        if not executed:
            raise RuntimeError('Live app server did not execute the shell isolation probe')
    print(json.dumps({'version': version, 'service': 'active', 'chatgpt_identity': True,
                      'fast_capability': True, 'proxy_auth': True, 'tool_key_absent': True}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--socket', type=Path,
                        default=Path('/home/agent/.codex/app-server-control/app-server-control.sock'))
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.version, args.socket))
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
