"""Bounded suggestion probe; report only latency and token metadata."""
import json
import subprocess
import time
import urllib.request

from configure_cerebras import ROUTE


def main():
    key = subprocess.check_output(['/usr/bin/systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
        '/home/agent/.config/litellm/proxy-key.cred', '-'], stderr=subprocess.DEVNULL, text=True).strip()
    payload = {'model': ROUTE, 'reasoning_effort': 'none', 'max_tokens': 128, 'stream': False,
        'num_retries': 0, 'messages': [
            {'role': 'system', 'content': 'Return JSON with exactly three short follow-up chat messages in a suggestions array.'},
            {'role': 'user', 'content': 'User: Scope suggested mobile replies. Assistant: Tap a suggestion to send its exact text.'}],
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'followups', 'strict': True,
            'schema': {'type': 'object', 'properties': {'suggestions': {'type': 'array',
                'items': {'type': 'string'}, 'minItems': 3, 'maxItems': 3}},
                'required': ['suggestions'], 'additionalProperties': False}}}}
    start = time.perf_counter()
    request = urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions',
        data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        choice = result['choices'][0]
        messages = json.loads(choice['message']['content'])['suggestions']
        assert choice['finish_reason'] == 'stop' and len(messages) == 3 and len(set(messages)) == 3
        assert all(isinstance(m, str) and m.strip() and len(m) <= 160 for m in messages)
        usage = result.get('usage', {})
        assert usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0) == 0
    except Exception:
        raise SystemExit('Cerebras suggestion probe failed') from None
    print(json.dumps({'model': ROUTE, 'seconds': round(time.perf_counter() - start, 3),
        'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
        'reasoning_tokens': 0, 'valid_suggestions': 3}))


if __name__ == '__main__':
    main()
