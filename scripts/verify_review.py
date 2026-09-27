"""Probe the reserved approval-review route with streaming JSON output.

This checks the inference transport, not Codex's approval policy or decisions.
"""
import argparse
import json
from pathlib import Path
import subprocess
import urllib.request

from refresh_models import REVIEW_MODEL


def verify(credential):
    key = subprocess.check_output([
        '/usr/bin/systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
        str(credential), '-',
    ], text=True).strip()
    payload = {
        'model': REVIEW_MODEL,
        'instructions': 'Return only the requested JSON object for this deployment transport probe.',
        'input': [{'role': 'user', 'content': 'Return exactly {"status":"REVIEW_ROUTE_OK"} with no other text.'}],
        'stream': True,
        'store': False,
        'reasoning': {'effort': 'medium'},
        # The repository adapter forwards this schema. This prompt also asks
        # for JSON, so this transport check alone cannot prove schema enforcement.
        'text': {'format': {
            'type': 'json_schema', 'name': 'review_route_probe', 'strict': True,
            'schema': {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['REVIEW_ROUTE_OK']}},
                       'required': ['status'], 'additionalProperties': False},
        }},
    }
    request = urllib.request.Request(
        'http://127.0.0.1:4000/v1/responses', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    del key
    items = {}
    completed = None
    with urllib.request.urlopen(request, timeout=180) as response:
        for line in response:
            if not line.startswith(b'data: ') or line.strip() == b'data: [DONE]':
                continue
            event = json.loads(line[6:])
            if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                raise ValueError('Reviewer returned an error or incomplete response')
            if event.get('type') == 'response.output_item.done':
                items[event['output_index']] = event['item']
            if event.get('type') == 'response.completed':
                completed = event['response']
                break
    if completed is None or completed.get('status') != 'completed':
        raise ValueError('Reviewer stream did not complete')
    output = [items[i] for i in sorted(items)] if items else completed.get('output', [])
    text = ''.join(part.get('text', '') for item in output if item.get('type') == 'message'
                   for part in item.get('content', []) if part.get('type') == 'output_text')
    try:
        result = json.loads(text)
    except ValueError:
        raise ValueError('Reviewer did not return structured JSON') from None
    if result != {'status': 'REVIEW_ROUTE_OK'}:
        raise ValueError('Reviewer returned unexpected structured output')
    print('PASS codex-auto-review route, streaming completion, and JSON output', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--credential', type=Path, default=Path.home() / '.config/litellm/proxy-key.cred')
    args = p.parse_args()
    verify(args.credential)


if __name__ == '__main__':
    main()
