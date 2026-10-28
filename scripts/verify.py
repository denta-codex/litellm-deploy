"""Verify local authentication, subscription streaming, and a complete tool loop."""
import argparse
import json
from deployment import proxy_key
import urllib.error
import urllib.request

p = argparse.ArgumentParser()
p.add_argument('--model', default='chatgpt/gpt-6-astra')
p.add_argument('--auto-tool-choice', action='store_true', help='Exercise providers that support automatic tool choice only')
p.add_argument('--base-url', default='http://127.0.0.1:4000', help='Proxy origin, including an isolated test port')
p.add_argument('--credential', default=None)
p.add_argument('--service-tier', choices=['priority', 'default'],
               help='Request this tier and report the returned tier separately')
args = p.parse_args()
key = proxy_key(args.credential)
base = args.base_url.rstrip('/')

def verify_tier(response):
    print(f'TIER requested={args.service_tier or "omitted"} returned={response.get("service_tier")!r}', flush=True)

def request(path, payload=None, authenticated=True):
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['Authorization'] = f'Bearer {key}'
    if payload is not None and args.service_tier:
        payload = dict(payload, service_tier=args.service_tier)
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
verify_tier(next(e['response'] for e in events if e.get('type') == 'response.completed'))
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
                    verify_tier(result)
                    result['output'] = [items[i] for i in sorted(items)]
                    return result
    raise RuntimeError('Response stream ended without completion')

probe_tool = {'type': 'function', 'name': 'deployment_probe', 'description': 'Check deployment.',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': []}}

first = completed_response({
    'model': args.model,
    'input': [{'role': 'user', 'content': 'Call deployment_probe to check this deployment.'}],
    'tools': [probe_tool],
    'tool_choice': 'auto' if args.auto_tool_choice else {'type': 'function', 'name': 'deployment_probe'},
})
calls = [item for item in first['output'] if item.get('type') == 'function_call']
assert len(calls) == 1 and calls[0]['name'] == 'deployment_probe', 'tool call missing'
second = completed_response({
    'model': args.model,
    'tools': [probe_tool],
    'input': [{'role': 'user', 'content': 'Call deployment_probe, then report its result.'}]
    + first['output']
    + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': 'DEPLOYMENT_TOOL_OK'}],
})
assert second['status'] == 'completed', 'follow-up did not complete'
reply = ''.join(part.get('text', '') for item in second['output'] if item.get('type') == 'message'
                for part in item.get('content', []) if part.get('type') == 'output_text')
assert 'DEPLOYMENT_TOOL_OK' in reply, 'Tool result was not used in the continuation'
print('PASS function call and tool-result continuation', flush=True)
