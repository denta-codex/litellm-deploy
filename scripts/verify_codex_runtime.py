"""Verify the live systemd-owned Codex app server and a real shell-tool turn."""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import tomllib

from websockets.asyncio.client import unix_connect


def configured_model(home, model=None):
    """Use the user's selected model and published catalog, not a subscription ID."""
    home = Path(home)
    config = tomllib.loads((home / '.codex/config.toml').read_text())
    provider = config.get('model_provider')
    settings = config.get('model_providers', {}).get(provider, {})
    if (provider != 'litellm' or settings.get('wire_api') != 'responses'
            or settings.get('requires_openai_auth') is not True
            or settings.get('env_key') != 'LITELLM_PROXY_KEY'):
        raise RuntimeError('Configured provider must use LiteLLM Responses with genuine ChatGPT identity and proxy auth')
    selected = model or config.get('model')
    catalog = Path(config['model_catalog_json'])
    if not catalog.is_absolute():
        raise RuntimeError('Configured model catalog must use an absolute path')
    rows = [row for row in json.loads(catalog.read_text())['models'] if row.get('slug') == selected]
    if len(rows) != 1 or rows[0].get('visibility') != 'list':
        raise RuntimeError('Configured model must have exactly one selectable catalog row')
    return config, rows[0]


def service_tiers(model):
    return ('priority', 'default') if any(
        tier.get('id') == 'priority' for tier in model.get('service_tiers', [])
    ) else ('default',)


def probe_tiers(config, model, *, all_tiers=False):
    tier = config.get('service_tier') or 'default'
    if tier not in ('priority', 'default'):
        raise RuntimeError('Configured service tier must be priority or default')
    if tier not in service_tiers(model):
        raise RuntimeError('Configured service tier is not advertised by the selected model')
    # Model capability is not account entitlement to optional paid Fast usage.
    return service_tiers(model) if all_tiers else (tier,)


def isolation_prompt(model):
    tool = ('the `exec` tool by calling `tools.exec_command`' if model.get('tool_mode') == 'code_mode_only'
            else 'your shell command tool')
    command = 'if test -z "${LITELLM_PROXY_KEY+x}"; then printf KEY_ABSENT; else printf KEY_PRESENT; fi'
    return f'Run exactly this shell command using {tool} and report its output: {command}'


def turn_failure(turn):
    info = (turn.get('error') or {}).get('codexErrorInfo')
    known = ('contextWindowExceeded', 'usageLimitExceeded', 'serverOverloaded', 'sandboxError',
             'internalServerError', 'httpConnectionFailed', 'responseStreamConnectionFailed',
             'responseStreamDisconnected', 'other')
    kind = info if isinstance(info, str) and info in known else next(
        (name for name in known if isinstance(info, dict) and name in info), 'unknown')
    message = str((turn.get('error') or {}).get('message', '')).lower()
    categories = [phrase for phrase in ('code mode', 'sandbox', 'permission denied', 'read-only',
                                        'failed to spawn', 'timed out', 'usage limit', 'rate limit')
                  if phrase in message]
    return RuntimeError(f'App-server probe turn failed ({kind}; categories={categories}); '
                        'no response content reported')


async def verify(version, socket, home=None):
    home = Path(home) if home is not None else Path.home()
    config, metadata = configured_model(home)
    model = metadata['slug']
    fast = 'priority' in service_tiers(metadata)
    tier, = probe_tiers(config, metadata)
    pid = int(subprocess.check_output(
        ['systemctl', 'show', 'codex-app-server.service', '--property=MainPID', '--value'],
        text=True,
    ).strip())
    if pid <= 0:
        raise RuntimeError('codex-app-server.service has no running process')
    expected = (home / f'.local/share/mise/installs/codex/{version}/bin/codex').resolve()
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
        cursor, listed = None, None
        while True:
            models = await rpc('model/list', {'cursor': cursor})
            listed = next((row for row in models.get('data', []) if row.get('model') == model), None)
            cursor = models.get('nextCursor')
            if listed is not None or not cursor:
                break
        if listed is None:
            raise RuntimeError('Live model list does not contain the configured model')
        if any(tier.get('id') == 'priority' for tier in listed.get('serviceTiers', [])) != fast:
            raise RuntimeError('Live configured-model Fast capability differs from the catalog')

        started = await rpc('thread/start', {
            'cwd': str(home), 'ephemeral': True, 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'serviceTier': tier,
        })
        if started.get('modelProvider') != config['model_provider'] or started.get('model') != model:
            raise RuntimeError('Live app server does not use the configured default model and provider')
        thread = started['thread']['id']
        await rpc('turn/start', {'threadId': thread, 'serviceTier': tier,
                                 'input': [{'type': 'text', 'text': isolation_prompt(metadata)}]})
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
                    raise turn_failure(event['params']['turn'])
                break
        if not executed:
            raise RuntimeError('Live app server did not execute the shell isolation probe')
    print(json.dumps({'version': version, 'service': 'active', 'chatgpt_identity': True,
                      'fast_capability': fast, 'provider': config['model_provider'], 'model': model,
                      'requested_tier': tier, 'proxy_auth': True, 'tool_key_absent': True}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--socket', type=Path,
                        default=None)
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.version, args.socket or args.home / '.codex/app-server-control/app-server-control.sock', args.home))
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
