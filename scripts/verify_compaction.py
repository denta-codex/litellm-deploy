"""Metadata-only live acceptance of a synthetic compaction through Grace's proxy."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib
import urllib.request
import uuid


def verify(model=None, base_url='http://127.0.0.1:4000'):
    selected = model or tomllib.loads((Path.home() / '.codex/config.toml').read_text())['model']
    # Existing repository-owned encrypted credential; keep its plaintext in memory.
    key = subprocess.check_output(['systemd-creds', 'decrypt', '--user',
        str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
    task_id = str(uuid.uuid4())
    marker = 'LITELLM_COMPACTION_OK'
    payload = {'model': selected, 'stream': True, 'store': False, 'tools': [], 'tool_choice': 'none',
               'instructions': 'Create only a continuation summary. Preserve the validation marker. Do not execute tools.',
               'input': [{'role': 'user', 'content': 'Remember the validation marker ' + marker},
                         {'role': 'assistant', 'content': 'The marker will be needed for the next turn.'},
                         {'role': 'user', 'content': 'You are performing a CONTEXT CHECKPOINT COMPACTION. '
                          'Summarize this conversation for continuation.'}]}
    metadata = json.dumps({'request_kind': 'compaction', 'context_window_id': task_id})
    request = urllib.request.Request(base_url.rstrip('/') + '/v1/responses', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
                 'thread-id': task_id, 'x-codex-turn-metadata': metadata})
    final, completions, text = None, 0, ''
    with urllib.request.urlopen(request, timeout=150) as response:
        for line in response:
            if not line.startswith(b'data: ') or line.strip() == b'data: [DONE]':
                continue
            event = json.loads(line[6:])
            if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                raise RuntimeError('Live compaction did not complete; inspect compaction_recovery service logs')
            if event.get('type') == 'response.output_text.delta':
                text += event['delta']
            if event.get('type') == 'response.completed':
                completions += 1
                final = event['response']
    if completions != 1 or marker not in text:
        raise RuntimeError('Live compaction did not return one summary retaining the marker')
    actual = (final.get('metadata') or {}).get('compaction_model')
    if not actual:
        raise RuntimeError('Live proxy did not identify the compactor; recovery may be disabled')
    return {'requested_model': selected, 'compactor_model': actual, 'marker_retained': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model')
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    args = parser.parse_args()
    print(json.dumps(verify(args.model, args.base_url)))
