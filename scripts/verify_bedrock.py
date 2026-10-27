"""Validate every advertised Bedrock effort and image input through the live proxy."""
import argparse
import base64
import struct
import zlib
import json
from pathlib import Path
import subprocess
import urllib.request


def verify(catalog, base_url):
    key = subprocess.check_output(['/usr/bin/systemd-creds', 'decrypt', '--user',
        '--name=litellm-proxy-key', str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
    rows = [r for r in json.loads(catalog.read_text())['models'] if r['slug'].startswith('bedrock/')]
    assert {r['slug'] for r in rows} == {'bedrock/opus-5.5', 'bedrock/gpt-6.1-sol'}
    for row in rows:
        for effort in row['supported_reasoning_levels']:
            payload = {'model': row['slug'], 'input': 'Reply with exactly BEDROCK_CAPABILITY_OK.',
                       'reasoning': {'effort': effort['effort']}, 'stream': False}
            check(base_url, key, payload)
            print('PASS ' + row['slug'] + ': effort=' + effort['effort'], flush=True)
        if 'image' in row['input_modalities']:
            # Fixed red PNG fixture; no external asset or network fetch.
            def chunk(kind, data):
                return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
            png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 2, 0, 0, 0))
                   + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\x00\x00' * 8) * 8)) + chunk(b'IEND', b''))
            image = base64.b64encode(png).decode()
            payload = {'model': row['slug'], 'stream': False,
                'input': [{'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'Acknowledge this image by replying BEDROCK_CAPABILITY_OK.'},
                    {'type': 'input_image', 'image_url': 'data:image/png;base64,' + image}]}]}
            check(base_url, key, payload)
            print('PASS ' + row['slug'] + ': image input', flush=True)


def check(base_url, key, payload):
    request = urllib.request.Request(base_url.rstrip('/') + '/v1/responses',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=180) as response:
        value = json.load(response)
    text = ''.join(p.get('text', '') for item in value.get('output', [])
                   if item.get('type') == 'message' for p in item.get('content', []))
    assert value.get('status') == 'completed' and 'BEDROCK_CAPABILITY_OK' in text, 'Capability probe did not complete'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    args = parser.parse_args()
    verify(args.catalog, args.base_url)
