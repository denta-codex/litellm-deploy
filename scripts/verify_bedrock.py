"""Validate every advertised Bedrock effort and image input through the live proxy."""
import argparse
import base64
import struct
import zlib
import json
from pathlib import Path
from deployment import proxy_key
import urllib.request


def image_fixture():
    """A recognizable image whose answer is never disclosed in the prompt."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 128, 128, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x00\x00\xff' * 128) * 128)) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def verify(catalog, base_url):
    key = proxy_key()
    rows = [r for r in json.loads(catalog.read_text())['models'] if r['slug'].startswith('bedrock/')]
    assert {r['slug'] for r in rows} == {'bedrock/opus-5.5', 'bedrock/gpt-6.1-sol'}
    for row in rows:
        for effort in row['supported_reasoning_levels']:
            payload = {'model': row['slug'], 'input': 'Reply with exactly BEDROCK_CAPABILITY_OK.',
                       'reasoning': {'effort': effort['effort']}, 'stream': False}
            check(base_url, key, payload)
            print('PASS ' + row['slug'] + ': effort=' + effort['effort'], flush=True)
        if 'image' in row['input_modalities']:
            question = 'What is the dominant color of the supplied image? Reply with only the color name.'
            image = image_fixture()
            payload = {'model': row['slug'], 'stream': True, 'store': False,
                'input': [{'role': 'user', 'content': [
                    {'type': 'input_text', 'text': question},
                    {'type': 'input_image', 'image_url': image}]}]}
            check(base_url, key, payload, expected='blue', exact=True)
            print('PASS ' + row['slug'] + ': streamed user-image recognition', flush=True)
            payload['tools'] = [{'type': 'function', 'name': 'view_image',
                'description': 'Load an image and return its visual content.',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': []}}]
            payload['tool_choice'] = 'auto'
            payload['input'] = [
                {'role': 'user', 'content': question},
                {'type': 'function_call', 'call_id': 'call_image_probe', 'name': 'view_image', 'arguments': '{}'},
                {'type': 'function_call_output', 'call_id': 'call_image_probe', 'output': [
                    {'type': 'input_text', 'text': 'Image loaded by view_image.'},
                    {'type': 'input_image', 'image_url': image}]}]
            check(base_url, key, payload, expected='blue', exact=True)
            print('PASS ' + row['slug'] + ': streamed tool-image recognition', flush=True)


def check(base_url, key, payload, expected='BEDROCK_CAPABILITY_OK', exact=False):
    request = urllib.request.Request(base_url.rstrip('/') + '/v1/responses',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=180) as response:
        if payload.get('stream'):
            value, texts = None, []
            for line in response:
                if not line.startswith(b'data: ') or line.strip() == b'data: [DONE]':
                    continue
                event = json.loads(line[6:])
                assert event.get('type') not in ('error', 'response.failed', 'response.incomplete'), 'Capability stream failed'
                if event.get('type') == 'response.output_text.delta':
                    texts.append(event.get('delta', ''))
                if event.get('type') == 'response.completed':
                    value = event['response']
            assert value is not None, 'Capability stream ended without completion'
            streamed_text = ''.join(texts)
            assert streamed_text, 'Capability stream returned no text deltas'
        else:
            value = json.load(response)
            streamed_text = ''
    text = ''.join(p.get('text', '') for item in value.get('output', [])
                   if item.get('type') == 'message' for p in item.get('content', []))
    text = text or streamed_text
    matches = text.strip().lower().rstrip('.') == expected.lower() if exact else expected in text
    assert value.get('status') == 'completed' and matches, 'Capability probe did not recognize the expected content'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    args = parser.parse_args()
    verify(args.catalog, args.base_url)
