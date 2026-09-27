"""Exercise real Codex native search and tool continuation through LiteLLM."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

import tomlkit

p = argparse.ArgumentParser()
p.add_argument('--model', default='chatgpt/gpt-6-astra')
p.add_argument('--base-url', default='http://127.0.0.1:4000', help='Proxy origin, including an isolated test port')
p.add_argument('--catalog', type=Path, default=Path.home() / '.config/litellm/codex-models.json')
p.add_argument('--skip-search', action='store_true', help='Validate tools/context without advertising unsupported search')
p.add_argument('--service-tier', choices=['priority', 'default'], help='Exercise Codex with an explicit service tier')
args = p.parse_args()
metadata = next(m for m in json.loads(args.catalog.read_text())['models'] if m['slug'] == args.model)
efforts = [r['effort'] for r in metadata.get('supported_reasoning_levels', [])]
effort = 'low' if 'low' in efforts else metadata.get('default_reasoning_level', 'medium')
state = Path.home() / '.local/state/litellm'
state.mkdir(parents=True, exist_ok=True, mode=0o700)
# Codex rejects homes under /tmp. This private disposable home keeps test tasks
# out of the desktop's history and is removed even on failed verification.
with tempfile.TemporaryDirectory(prefix='codex-probe-', dir=state) as temporary:
    home = Path(temporary)
    doc = {
        'model_provider': 'litellm', 'model': args.model,
        'model_catalog_json': str(args.catalog.resolve()),
        'model_reasoning_effort': effort, 'approval_policy': 'never',
        'sandbox_mode': 'read-only', 'web_search': 'disabled' if args.skip_search else 'live',
        'model_providers': {'litellm': {
            'name': 'LiteLLM', 'base_url': args.base_url.rstrip('/') + '/v1',
            'wire_api': 'responses', 'supports_websockets': False,
            'auth': {'command': '/usr/bin/systemd-creds',
                     'args': ['decrypt', '--user', '--name=litellm-proxy-key',
                              str(Path.home() / '.config/litellm/proxy-key.cred'), '-'],
                     'refresh_interval_ms': 0},
        }},
    }
    if args.service_tier:
        doc['service_tier'] = args.service_tier
        doc['features'] = {'fast_mode': True}
    (home / 'config.toml').write_text(tomlkit.dumps(doc))
    env = os.environ | {'CODEX_HOME': str(home)}

    def run(arguments):
        result = subprocess.run(['codex', 'exec', *arguments], env=env,
                                capture_output=True, text=True, timeout=240)
        if result.returncode:
            raise RuntimeError(f'Codex probe failed ({result.returncode}): {result.stderr[-2000:]}')
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assert any(e['type'] == 'turn.completed' for e in events), 'Codex turn did not complete'
        assert not any(e['type'] in ('error', 'turn.failed') for e in events), 'Codex reported failure'
        return events

    prompt = ('Remember the marker LITELLM_CONTEXT_OK and acknowledge it.' if args.skip_search else
                  'Use native web search to find the official uv documentation about installing Python. '
                  'Do not substitute shell, curl, or another tool for web search. '
                  'Report the official page URL and a fact from the result.')
    events = run(['--skip-git-repo-check', '-C', str(home), '--json', prompt])
    searches = [e['item'] for e in events if e['type'] == 'item.completed'
                and e['item']['type'] == 'web_search']
    if not args.skip_search:
        assert any(s.get('action', {}).get('type') == 'search' for s in searches), 'No real native web-search event'
    replies = '\n'.join(e['item']['text'] for e in events if e['type'] == 'item.completed'
                        and e['item']['type'] == 'agent_message')
    retained = 'LITELLM_CONTEXT_OK' if args.skip_search else 'https://docs.astral.sh/uv/'
    assert retained in replies, 'Initial search/context probe failed'
    thread = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
    print('PASS stock Codex ' + ('context marker' if args.skip_search else 'native web search through LiteLLM'), flush=True)

    events = run(['resume', thread, '--skip-git-repo-check', '--json',
                  'Use the shell tool to run exactly: printf LITELLM_CODEX_TOOL_OK . '
                  'Then report that output and ' + ('the marker I asked you to remember.' if args.skip_search else
                  'the official documentation URL from your previous answer.')])
    commands = [e['item'] for e in events if e['type'] == 'item.completed'
                and e['item']['type'] == 'command_execution']
    assert any(c.get('exit_code') == 0 and 'LITELLM_CODEX_TOOL_OK' in c.get('aggregated_output', '')
               for c in commands), 'Shell execution/result event missing'
    replies = '\n'.join(e['item']['text'] for e in events if e['type'] == 'item.completed'
                        and e['item']['type'] == 'agent_message')
    assert 'LITELLM_CODEX_TOOL_OK' in replies and retained in replies, 'Tool/context continuation failed'
    print('PASS explicit resume, shell execution, tool result, and prior search context', flush=True)
