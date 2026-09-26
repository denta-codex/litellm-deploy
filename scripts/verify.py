"""Verify local authentication, subscription streaming, and a complete tool loop."""
import argparse
import json
import subprocess
import urllib.error
import urllib.request

p = argparse.ArgumentParser()
p.add_argument('--model', default='chatgpt/gpt-6-astra')
p.add_argument('--credential', default='/home/agent/.config/litellm/proxy-key.cred')
args = p.parse_args()
key = subprocess.check_output([
    '/usr/bin/systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
    args.credential, '-',
], text=True).strip()
base = 'http://127.0.0.1:4000'

def request(path, payload=None, authenticated=True):
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['Authorization'] = f'Bearer {key}'
    data = None if payload is None else json.dumps(payload).encode()
    return urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=180)

try:
    request('/v1/models', authenticated=False)
except urllib.error.HTTPError as exc:
    assert exc.code in (401, 403), exc.code
else:
    raise RuntimeError('Unauthenticated model request was accepted')
with request('/v1/models') as response:
    models = json.load(response)
assert args.model in [model['id'] for model in models['data']], models
payload = {'model': args.model, 'input': [{'role': 'user', 'content': 'Reply with exactly LITELLM_STREAM_OK.'}], 'stream': True}
events = []
with request('/v1/responses', payload) as response:
    for line in response:
        if line.startswith(b'data: ') and line.strip() != b'data: [DONE]':
            event = json.loads(line[6:])
            events.append(event)
assert any(e.get('type') == 'response.completed' for e in events), 'stream did not complete'
text = ''.join(e.get('delta', '') for e in events if e.get('type') == 'response.output_text.delta')
assert 'LITELLM_STREAM_OK' in text, 'unexpected streaming reply'
print('PASS authentication, catalog, subscription streaming', flush=True)

def completed_response(payload):
    payload['stream'] = True
    items = {}
    with request('/v1/responses', payload) as response:
        for line in response:
            if line.startswith(b'data: ') and line.strip() != b'data: [DONE]':
                event = json.loads(line[6:])
                if event.get('type') == 'response.output_item.done':
                    items[event['output_index']] = event['item']
                if event.get('type') == 'response.completed':
                    result = event['response']
                    result['output'] = [items[i] for i in sorted(items)]
                    return result
    raise RuntimeError('Response stream ended without completion')

first = completed_response({
    'model': args.model,
    'input': [{'role': 'user', 'content': 'Call deployment_probe to check this deployment.'}],
    'tools': [{'type': 'function', 'name': 'deployment_probe', 'description': 'Check deployment.',
               'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': []}}],
    'tool_choice': {'type': 'function', 'name': 'deployment_probe'},
})
calls = [item for item in first['output'] if item.get('type') == 'function_call']
assert len(calls) == 1 and calls[0]['name'] == 'deployment_probe', 'tool call missing'
second = completed_response({
    'model': args.model,
    'input': [{'role': 'user', 'content': 'Call deployment_probe, then report its result.'}]
    + first['output']
    + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': 'DEPLOYMENT_TOOL_OK'}],
})
assert second['status'] == 'completed', 'follow-up did not complete'
print('PASS function call and tool-result continuation', flush=True)
