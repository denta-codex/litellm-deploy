"""Bounded Fable acceptance against a disposable candidate, never the live service.

Provider credentials stay in the proxy environment. Existing launch.exports
loads systemd credentials without copying or logging them. Successful staging
is removed; failed evidence is retained until acceptance succeeds or is retired.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile

import httpx
import yaml

import deploy_config
from launch import AMBIENT, ISOLATED, exports
from refresh_models import atomic_write, configured_model
from verify_claude import stop

MODEL = 'bedrock/fable-5.2'
SCRIPTS = Path(__file__).resolve().parent
CHECKS = {
    'api': ['verify.py', '--auto-tool-choice'],
    'capabilities': ['verify_capabilities.py'],
    'codex': ['verify_codex.py', '--effort', 'high'],
    'search': ['verify_shared_search.py'],
    'image': ['verify_codex_image.py'],
}


async def verify(args):
    config = deploy_config.load(args.config)
    specs = [s for s in config['models'] if s['name'] == MODEL]
    if len(specs) != 1:
        raise ValueError('Deployment must configure exactly one ' + MODEL)
    route, row, _ = configured_model(specs[0])
    live = args.live_config
    proxy_config = yaml.safe_load(live.read_text())
    helpers = [r for r in proxy_config['model_list'] if r['model_name'] == 'chatgpt/gpt-6-luna']
    if len(helpers) != 1:
        raise ValueError('The configured shared-search helper is missing')
    proxy_config['model_list'] = [route, *helpers]
    proxy_config['general_settings'] = {'master_key': 'os.environ/LITELLM_MASTER_KEY', 'disable_spend_logs': True}
    proxy_config['litellm_settings'] = {'turn_off_message_logging': True, 'num_retries': 0}
    proxy_env = {k: v for k, v in os.environ.items() if k not in AMBIENT}
    proxy_env.update(ISOLATED)
    proxy_env.update(exports(json.loads(live.with_name('credentials.json').read_text()), args.credentials_directory))
    args.state_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='fable-acceptance-', dir=args.state_root))
    proxy = None
    report = {'model': MODEL, 'full_acceptance_passed': False, 'checks': [],
              'route_sha256': hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest(),
              'source_sha256': {name: hashlib.sha256((SCRIPTS / name).read_bytes()).hexdigest()
                  for name in ('chat_native.py', 'shared_search.py', 'refresh_models.py', 'verify_fable.py',
                               'verify_claude.py', *(check[0] for check in CHECKS.values()))},
              'lock_sha256': hashlib.sha256((SCRIPTS.parent / 'uv.lock').read_bytes()).hexdigest()}
    try:
        catalog = directory / 'catalog.json'
        catalog.write_text(json.dumps({'models': [row]}))
        candidate = directory / 'config.yaml'
        candidate.write_text(yaml.safe_dump(proxy_config))
        proxy_env.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False',
            SHARED_SEARCH_STATE=str(directory / 'search'), CLAUDE_ADAPTER_STATE=str(directory / 'claude'),
            GEMINI_NATIVE_STATE=str(directory / 'gemini'),
            CHATGPT_TOKEN_DIR=str(Path.home() / '.local/state/litellm/chatgpt'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        print('Candidate evidence: ' + str(directory), flush=True)
        with (directory / 'proxy.log').open('w') as log:
            proxy = await asyncio.create_subprocess_exec(sys.executable, str(SCRIPTS / 'serve.py'),
                '--config', str(candidate), '--port', str(port), env=proxy_env,
                stdout=log, stderr=log, start_new_session=True)
        async with httpx.AsyncClient() as client:
            for _ in range(180):
                if proxy.returncode is not None:
                    raise RuntimeError('Candidate proxy exited; inspect its private log')
                try:
                    if (await client.get(base + '/health/liveliness')).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.25)
            else:
                raise RuntimeError('Candidate startup timed out')
        # Verifiers do not inherit the loaded provider credentials.
        for check in args.checks:
            command = [sys.executable, str(SCRIPTS / CHECKS[check][0]), *CHECKS[check][1:],
                       '--model', MODEL, '--base-url', base]
            if check != 'api':
                command += ['--catalog', str(catalog)]
            with (directory / (check + '.log')).open('w') as log:
                process = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=log,
                    env=os.environ | {'SHARED_SEARCH_TEST_ROOT': str(directory)}, start_new_session=True)
                try:
                    await asyncio.wait_for(process.wait(), args.timeout)
                except TimeoutError:
                    await stop(process)
                    raise RuntimeError('Acceptance check timed out: ' + check) from None
                except BaseException:
                    await stop(process)
                    raise
            passed = process.returncode == 0
            report['checks'].append({'check': check, 'passed': passed, 'returncode': process.returncode})
            print(('PASS ' if passed else 'FAIL ') + MODEL + ': ' + check, flush=True)
            if not passed:
                raise RuntimeError('Acceptance failed: ' + check + '; inspect private evidence')
        records = [json.loads(p.read_text()) for p in (directory / 'search').glob('*/ws_shared*.json')]
        if any('Web search failed' in r['result']['output'] for r in records):
            raise RuntimeError('Shared-search records contain a failed search')
        report['full_acceptance_passed'] = set(args.checks) == set(CHECKS)
        report['selected_checks_passed'] = True
        if report['full_acceptance_passed']:
            atomic_write(args.state_root / 'fable-acceptance.json', json.dumps(report, indent=2) + '\n')
    finally:
        if proxy is not None:
            await stop(proxy)
        (directory / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        if report.get('selected_checks_passed'):
            shutil.rmtree(directory)
        else:
            print('Retained failed acceptance evidence: ' + str(directory), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/litellm-deploy/config.yml')
    parser.add_argument('--live-config', type=Path, default=Path.home() / '.config/litellm/config.yaml')
    parser.add_argument('--credentials-directory', type=Path, default=Path('/run/credentials/litellm.service'))
    parser.add_argument('--state-root', type=Path, default=Path.home() / '.local/state/litellm')
    parser.add_argument('--checks', nargs='+', choices=list(CHECKS), default=list(CHECKS))
    parser.add_argument('--timeout', type=int, default=900, help='Ceiling per check, with no acceptance retries')
    try:
        print(json.dumps(asyncio.run(verify(parser.parse_args()))))
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(type(exc).__name__ + ': acceptance could not finish; inspect retained evidence')
