"""Exercise real Codex native search and tool continuation through LiteLLM."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from deployment import codex_binary, proxy_key
import tempfile

import tomlkit

TOOL_MARKER = 'LITELLM_CODEX_TOOL_OK'
TOOL_COMMAND = f'printf {TOOL_MARKER}'


def shell_prompt(follow_up, tool_mode=None):
    # Code-mode models have no top-level shell tool: the shell is the nested
    # tools.exec_command inside the JavaScript `exec` tool. Combined with the
    # read-only sandbox text, a bare "use the shell tool" led gpt-6-luna to
    # refuse in roughly 1 of 8 runs, so name the real path and say it is allowed.
    tool = ('the `exec` tool by calling `tools.exec_command`' if tool_mode == 'code_mode_only'
            else 'your shell command tool')
    return (f'Run this command using {tool}: `{TOOL_COMMAND}`. '
            'It only prints text and writes nothing, so the read-only sandbox allows it without approval. '
            f'Then report its output and {follow_up}')


def agent_replies(events):
    return '\n'.join(e['item']['text'] for e in events if e['type'] == 'item.completed'
                     and e['item']['type'] == 'agent_message')


def require_shell_result(events):
    commands = [e['item'] for e in events if e['type'] == 'item.completed'
                and e['item']['type'] == 'command_execution']
    if not commands:
        raise AssertionError('Shell execution/result event missing: the model attempted no command; '
                             f'it replied {agent_replies(events)[-500:]!r}')
    if not any(c.get('exit_code') == 0 and TOOL_MARKER in c.get('aggregated_output', '') for c in commands):
        attempts = [(c.get('command'), c.get('exit_code'), c.get('aggregated_output', '')[-200:]) for c in commands]
        raise AssertionError(f'Shell execution/result event missing: no successful {TOOL_MARKER} output in {attempts!r}')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='chatgpt/gpt-6-astra')
    p.add_argument('--base-url', default='http://127.0.0.1:4000', help='Proxy origin, including an isolated test port')
    p.add_argument('--catalog', type=Path, default=Path.home() / '.config/litellm/codex-models.json')
    p.add_argument('--skip-search', action='store_true', help='Validate tools/context without advertising unsupported search')
    p.add_argument('--service-tier', choices=['priority', 'default'], help='Exercise Codex with an explicit service tier')
    p.add_argument('--launcher', type=Path, default=None)
    args = p.parse_args()
    args.launcher = args.launcher or codex_binary()
    metadata = next(m for m in json.loads(args.catalog.read_text())['models'] if m['slug'] == args.model)
    efforts = [r['effort'] for r in metadata.get('supported_reasoning_levels', [])]
    effort = 'low' if 'low' in efforts else metadata.get('default_reasoning_level', 'medium')
    state = Path.home() / '.local/state/litellm'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Codex rejects homes under /tmp. This private disposable home keeps test tasks
    # out of the desktop's history and is removed even on failed verification.
    with tempfile.TemporaryDirectory(prefix='codex-probe-', dir=state) as temporary:
        probe(args, metadata, effort, Path(temporary))


def probe(args, metadata, effort, home):
    native = json.loads((Path.home() / '.codex/auth.json').read_text())
    native['tokens']['refresh_token'] = ''
    native['OPENAI_API_KEY'] = None
    auth = home / 'auth.json'
    auth.touch(mode=0o600)
    auth.write_text(json.dumps(native))
    doc = {
        'model_provider': 'litellm', 'model': args.model,
        'service_tier': 'default',
        'model_catalog_json': str(args.catalog.resolve()),
        'model_reasoning_effort': effort, 'approval_policy': 'never',
        'sandbox_mode': 'read-only', 'web_search': 'disabled' if args.skip_search else 'live',
        'features': {'shell_snapshot': False},
        'shell_environment_policy': {'exclude': ['LITELLM_PROXY_KEY']},
        'model_providers': {'litellm': {
            'name': 'LiteLLM', 'base_url': 'http://127.0.0.1:4000/v1',
            'wire_api': 'responses', 'supports_websockets': False,
            'requires_openai_auth': True, 'env_key': 'LITELLM_PROXY_KEY',
        }},
    }
    doc['model_providers']['litellm']['base_url'] = args.base_url.rstrip('/') + '/v1'
    if args.service_tier:
        doc['service_tier'] = args.service_tier
        doc['features']['fast_mode'] = True
    (home / 'config.toml').write_text(tomlkit.dumps(doc))
    env = os.environ | {'CODEX_HOME': str(home), 'LITELLM_PROXY_KEY': proxy_key()}

    def run(arguments):
        result = subprocess.run([str(args.launcher), 'exec', *arguments], env=env,
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
    replies = agent_replies(events)
    retained = 'LITELLM_CONTEXT_OK' if args.skip_search else 'https://docs.astral.sh/uv/'
    assert retained in replies, 'Initial search/context probe failed'
    thread = next(e['thread_id'] for e in events if e['type'] == 'thread.started')
    print('PASS stock Codex ' + ('context marker' if args.skip_search else 'native web search through LiteLLM'), flush=True)

    events = run(['resume', thread, '--skip-git-repo-check', '--json', shell_prompt(
        'the marker I asked you to remember.' if args.skip_search else
        'the official documentation URL from your previous answer.', metadata.get('tool_mode'))])
    require_shell_result(events)
    replies = agent_replies(events)
    assert TOOL_MARKER in replies and retained in replies, 'Tool/context continuation failed'
    print('PASS explicit resume, shell execution, tool result, and prior search context', flush=True)


if __name__ == '__main__':
    main()
