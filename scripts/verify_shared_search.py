"""Real Codex shared-search acceptance, optionally against a disposable proxy."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

import httpx
import tomlkit
import yaml

from launch import modal_token
from verify_claude import RPC, stop

ROOT = Path(__file__).resolve().parent.parent


async def main(args):
    parent = Path(os.environ.get('SHARED_SEARCH_TEST_ROOT', Path.home() / '.local/state/litellm'))
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='search-acceptance-', dir=parent))
    proxy = None
    logs = []
    success = False
    try:
        catalog = json.loads(args.catalog.read_text())
        models = args.model or [m['slug'] for m in catalog['models'] if m['slug'].startswith(('claude/', 'modal/'))]
        for row in catalog['models']:
            if row['slug'] in models:
                row.update(supports_search_tool=True, web_search_tool_type='text_and_image')
        catalog_path = directory / 'catalog.json'
        catalog_path.write_text(json.dumps(catalog))
        env = os.environ | {'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'LITELLM_TELEMETRY': 'False'}
        if args.isolated:
            key = secrets.token_hex(24)
            config = yaml.safe_load((Path.home() / '.config/litellm/config.yaml').read_text())
            config['general_settings'] = {'master_key': 'os.environ/SEARCH_TEST_KEY', 'disable_spend_logs': True}
            config['litellm_settings'] = {'turn_off_message_logging': True, 'num_retries': 0}
            if not any(r['model_name'] == 'chatgpt/gpt-6-luna' for r in config['model_list']):
                raise RuntimeError('The configured GPT-6-Luna search route is missing')
            config_path = directory / 'config.yaml'
            config_path.write_text(yaml.safe_dump(config))
            auth = directory / 'claude-config'
            auth.mkdir(mode=0o700)
            (auth / '.credentials.json').symlink_to(Path.home() / '.claude/.credentials.json')
            env.update(SEARCH_TEST_KEY=key, CLAUDE_CONFIG_DIR=str(auth),
                CLAUDE_ADAPTER_STATE=str(directory / 'claude'), SHARED_SEARCH_STATE=str(directory / 'search'),
                CHATGPT_TOKEN_DIR=str(Path.home() / '.local/state/litellm/chatgpt'))
            if any(m.startswith('modal/') for m in models):
                credential = subprocess.check_output(['sudo', 'cat', '/run/credentials/litellm.service/modal-inference-token'], text=True)
                env['MODAL_API_KEY'] = modal_token(credential)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            log = (directory / 'proxy.log').open('w')
            logs.append(log)
            proxy = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'scripts/serve.py'),
                '--config', str(config_path), '--port', str(port), env=env, stdout=log, stderr=log, start_new_session=True)
            async with httpx.AsyncClient() as client:
                for _ in range(120):
                    if proxy.returncode is not None:
                        raise RuntimeError('Disposable proxy exited')
                    try:
                        if (await client.get(base + '/health/liveliness')).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.25)
                else:
                    raise RuntimeError('Disposable proxy startup timed out')
        else:
            base = args.base_url
            key = subprocess.check_output(['/usr/bin/systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
                str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
        for model in models:
            home = directory / ('codex-' + str(models.index(model)))
            home.mkdir(mode=0o700)
            config = {'model': model, 'model_provider': 'search_acceptance', 'model_catalog_json': str(catalog_path),
                'model_reasoning_effort': 'low', 'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'live',
                'features': {'shell_snapshot': False}, 'shell_environment_policy': {'exclude': ['SEARCH_TEST_KEY', 'MODAL_API_KEY']},
                'model_providers': {'search_acceptance': {'name': 'Search acceptance', 'base_url': base + '/v1',
                    'wire_api': 'responses', 'env_key': 'SEARCH_TEST_KEY', 'supports_websockets': False,
                    'requires_openai_auth': False, 'request_max_retries': 0, 'stream_max_retries': 0}}}
            (home / 'config.toml').write_text(tomlkit.dumps(config))
            log = (home / 'codex.log').open('w')
            logs.append(log)
            process = await asyncio.create_subprocess_exec('codex', 'app-server', cwd=home,
                env=env | {'CODEX_HOME': str(home), 'SEARCH_TEST_KEY': key}, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=log, start_new_session=True)
            rpc = RPC(process)
            try:
                await rpc.call('initialize', {'clientInfo': {'name': 'search_acceptance', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
                await rpc.send({'method': 'initialized', 'params': {}})
                thread = (await rpc.call('thread/start', {'model': model, 'modelProvider': 'search_acceptance', 'cwd': str(home),
                    'approvalPolicy': 'never', 'sandbox': 'read-only', 'baseInstructions': 'Follow the user. Be concise. Cite sources with their full URLs.'}))['thread']['id']
                reply, items = await rpc.turn(thread, 'Use native web search to find the official uv documentation for installing Python. '
                    'Do not use the shell or another tool for research. Give the official page URL and one fact from it.')
                (home / 'search-items.json').write_text(json.dumps(items, indent=2))
                assert any(i['type'] == 'webSearch' for i in items), 'Codex received no native search item'
                assert 'https://docs.astral.sh/uv/' in reply, 'No official source URL in answer'
                print('PASS ' + model + ': native Codex search and sourced answer', flush=True)
                reply, items = await rpc.turn(thread, 'Use the shell tool to run exactly: printf SEARCH_TOOL_OK . '
                    'Report its output and the official documentation URL from your previous answer without searching again.')
                (home / 'followup-items.json').write_text(json.dumps(items, indent=2))
                assert any(i['type'] == 'commandExecution' for i in items), 'No external tool execution'
                assert 'SEARCH_TOOL_OK' in reply and 'https://docs.astral.sh/uv/' in reply, 'Lost search or tool context'
                print('PASS ' + model + ': client tool and follow-up search context', flush=True)
                if args.isolated:
                    records = [json.loads(p.read_text()) for p in (directory / 'search').glob('*/ws_shared*.json')]
                    assert records and any(r.get('sources') for r in records), 'Helper returned no structured sources'
                    assert any(r.get('helper_usage') for r in records), 'No completed helper response was recorded'
            finally:
                await stop(process)
                await rpc.reader
        if args.all_efforts and 'claude/opus-5.5' in models:
            async with httpx.AsyncClient(timeout=240) as client:
                for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
                    response = await client.post(base + '/v1/responses', headers={'Authorization': 'Bearer ' + key,
                        'x-claude-chat-id': secrets.token_hex(12)}, json={'model': 'claude/opus-5.5', 'stream': False,
                        'input': 'Search the official uv documentation for its command to install Python. Answer with the command and source URL.',
                        'tools': [{'type': 'web_search'}], 'reasoning': {'effort': effort}})
                    response.raise_for_status()
                    value = response.json()
                    assert any(i['type'] == 'web_search_call' and i['status'] == 'completed' for i in value['output']), 'Missing completed search'
                    print('PASS Opus search effort=' + effort, flush=True)
        success = True
    finally:
        if proxy:
            await stop(proxy)
        for log in logs:
            log.close()
        if success:
            shutil.rmtree(directory)
        else:
            print('Retained failed search acceptance evidence: ' + str(directory), file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated', action='store_true')
    parser.add_argument('--all-efforts', action='store_true')
    parser.add_argument('--model', action='append')
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    parser.add_argument('--catalog', type=Path, default=Path.home() / '.config/litellm/codex-models.json')
    asyncio.run(main(parser.parse_args()))
