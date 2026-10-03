"""Live local-model acceptance. All tool calls use fixtures, never real devices."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct
import subprocess
import threading
import time
import zlib

import httpx

SPEC = json.loads(Path(__file__).with_name('local-model.json').read_text())
BACKEND = f"http://127.0.0.1:{SPEC['port']}"
NAME = SPEC['name']


def image_fixture():
    """1024px red-left/blue-right PNG, with no image dependency."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = (b'\x00' + b'\xff\x00\x00' * 512 + b'\x00\x00\xff' * 512) * 1024
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1024, 1024, 8, 2, 0, 0, 0))
    data += chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


def gpu_memory():
    result = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], text=True)
    return sum(int(line) for line in result.splitlines())


def backend(path, payload=None):
    with httpx.Client(timeout=300) as client:
        result = client.get(BACKEND + path) if payload is None else client.post(BACKEND + path, json=payload)
        result.raise_for_status()
        return result.json()


def state():
    rows = backend('/models')['data']
    return next(row['status']['value'] for row in rows if row['id'] == NAME)


class Probe:
    def __init__(self, proxy=False):
        self.proxy = proxy
        self.headers = {}
        self.metrics = []
        if proxy:
            key = subprocess.check_output(['systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
                                           str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
            self.headers['Authorization'] = 'Bearer ' + key

    def request(self, messages, tools=None, forced=False):
        started = time.monotonic()
        if not self.proxy:
            payload = {'model': NAME, 'messages': messages, 'max_tokens': 128, 'temperature': 0}
            if tools:
                payload.update(tools=tools, tool_choice={'type': 'function', 'function': {'name': 'set_light'}} if forced else 'auto')
            result = backend('/v1/chat/completions', payload)
            message = result['choices'][0]['message']
            record = {'seconds': round(time.monotonic() - started, 3), 'usage': result.get('usage'), 'timings': result.get('timings')}
        else:
            inputs = []
            for message in messages:
                if message.get('tool_calls'):
                    for call in message['tool_calls']:
                        inputs.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
                elif message['role'] == 'tool':
                    inputs.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
                else:
                    content = message['content']
                    if isinstance(content, list):
                        content = [({'type': 'input_text', 'text': p['text']} if p['type'] == 'text' else
                                    {'type': 'input_image', 'image_url': p['image_url']['url']}) for p in content]
                    inputs.append({'role': message['role'], 'content': content})
            payload = {'model': 'local/' + NAME, 'input': inputs, 'stream': True, 'max_output_tokens': 128}
            if tools:
                payload['tools'] = [{'type': 'function', **tool['function']} for tool in tools]
                if forced:
                    payload['tool_choice'] = {'type': 'function', 'name': 'set_light'}
            items, completed, first = [], False, None
            with httpx.stream('POST', 'http://127.0.0.1:4000/v1/responses', json=payload,
                              headers=self.headers, timeout=300) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data: ') or line == 'data: [DONE]':
                        continue
                    event = json.loads(line[6:])
                    if event.get('type') == 'response.output_text.delta' and first is None:
                        first = round(time.monotonic() - started, 3)
                    if event.get('type') == 'response.output_item.done':
                        items.append(event['item'])
                    if event.get('type') in ('response.failed', 'error'):
                        raise RuntimeError('Local Responses request failed: ' + json.dumps(event))
                    if event.get('type') == 'response.completed':
                        completed = True
            if not completed:
                raise RuntimeError('Missing response.completed')
            message = {'role': 'assistant', 'content': ''.join(p.get('text', '') for i in items
                       if i.get('type') == 'message' for p in i.get('content', []))}
            calls = [{'id': i['call_id'], 'type': 'function', 'function': {'name': i['name'], 'arguments': i['arguments']}}
                     for i in items if i.get('type') == 'function_call']
            if calls:
                message['tool_calls'] = calls
            record = {'seconds': round(time.monotonic() - started, 3), 'first_text_seconds': first}
        self.metrics.append(record)
        print(json.dumps({'request': record}), flush=True)
        return message

    def smoke(self):
        text = self.request([{'role': 'user', 'content': 'Reply with exactly LOCAL_QWEN_OK.'}])
        assert 'LOCAL_QWEN_OK' in (text.get('content') or ''), text
        image = self.request([{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Name the color on the left and then the color on the right. Be brief.'},
            {'type': 'image_url', 'image_url': {'url': image_fixture()}}]}])
        words = (image.get('content') or '').lower()
        assert 'red' in words and 'blue' in words and words.index('red') < words.index('blue'), image
        tools = [{'type': 'function', 'function': {'name': 'set_light', 'description': 'Set a room light.', 'parameters': {
            'type': 'object', 'properties': {'room': {'type': 'string', 'enum': ['kitchen', 'hallway']},
            'state': {'type': 'string', 'enum': ['on', 'off']}}, 'required': ['room', 'state'], 'additionalProperties': False}}}]
        messages = [{'role': 'user', 'content': 'Turn the kitchen light off. Use set_light and report its result.'}]
        call = self.request(messages, tools, forced=True)
        calls = call.get('tool_calls', [])
        assert len(calls) == 1 and calls[0]['function']['name'] == 'set_light', call
        assert json.loads(calls[0]['function']['arguments']) == {'room': 'kitchen', 'state': 'off'}, call
        continuation = messages + [call, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': 'LOCAL_LIGHT_OK: kitchen light is off.'}]
        followup = self.request(continuation)
        assert 'off' in (followup.get('content') or '').lower(), followup
        print('PASS text, image, light tool and continuation', flush=True)
        return continuation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--backend', action='store_true')
    target.add_argument('--proxy', action='store_true')
    parser.add_argument('--lifecycle', action='store_true', help='Unload this model, test concurrent cold requests and a full five-minute sleep cycle')
    args = parser.parse_args()
    probe = Probe(args.proxy)
    initial = state()
    print(json.dumps({'initial_state': initial}), flush=True)
    if args.lifecycle:
        if initial != 'unloaded':
            backend('/models/unload', {'model': NAME})
        deadline = time.monotonic() + 30
        while state() != 'unloaded' and time.monotonic() < deadline:
            time.sleep(0.2)
        assert state() == 'unloaded'
    baseline = gpu_memory()
    readings, stop = [baseline], threading.Event()
    def sample():
        while not stop.wait(1):
            readings.append(gpu_memory())
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        if args.lifecycle:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(probe.request, [{'role': 'user', 'content': 'Reply with exactly CONCURRENT_OK.'}]) for _ in range(2)]
                for f in futures:
                    assert 'CONCURRENT_OK' in (f.result().get('content') or '')
        continuation = probe.smoke()
        if args.lifecycle:
            print('Waiting for five-minute idle sleep; only non-waking monitoring is active.', flush=True)
            start = time.monotonic()
            while state() != 'sleeping':
                if time.monotonic() - start > SPEC['idle_seconds'] + 45:
                    raise RuntimeError('Model did not sleep after the idle timeout')
                backend('/health')
                time.sleep(5)
            asleep = gpu_memory()
            assert asleep <= baseline + 400, (baseline, asleep)
            print(json.dumps({'sleep_seconds': round(time.monotonic() - start, 1), 'sleep_gpu_mib': asleep}), flush=True)
            followup = probe.request(continuation)
            assert 'off' in (followup.get('content') or '').lower(), followup
            print('PASS sleep memory release and tool continuation after reload', flush=True)
    finally:
        stop.set()
        monitor.join(timeout=5)
    print(json.dumps({'baseline_gpu_mib': baseline, 'peak_gpu_mib': max(readings), 'requests': probe.metrics}), flush=True)


if __name__ == '__main__':
    main()
