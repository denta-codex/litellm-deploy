"""Live Argon reasoning levels and image input through the Responses boundary."""
import argparse
import base64
import json
from pathlib import Path
import struct
import subprocess
import zlib

from verify_bedrock import check
from vertex_google import SLUG


def verify(catalog, base_url):
    rows = [r for r in json.loads(catalog.read_text())['models'] if r['slug'].startswith('vertex/')]
    assert [r['slug'] for r in rows] == [SLUG]
    key = subprocess.check_output(['/usr/bin/systemd-creds','decrypt','--user',
        '--name=litellm-proxy-key',str(Path.home()/'.config/litellm/proxy-key.cred'),'-'],text=True).strip()
    for effort in rows[0]['supported_reasoning_levels']:
        check(base_url,key,{'model':SLUG, 'input':'Reply exactly VERTEX_CAPABILITY_OK.',
            'reasoning':{'effort':effort['effort']}, 'stream':False},'VERTEX_CAPABILITY_OK')
        print('PASS Argon effort='+effort['effort'],flush=True)
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,8,8,2,0,0,0))
        +chunk(b'IDAT',zlib.compress((b'\x00'+b'\xff\x00\x00'*8)*8))+chunk(b'IEND',b''))
    check(base_url,key,{'model':SLUG,'stream':False,'input':[{'role':'user','content':[
        {'type':'input_text','text':'Acknowledge this image by replying VERTEX_CAPABILITY_OK.'},
        {'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(png).decode()}]}]},'VERTEX_CAPABILITY_OK')
    print('PASS Argon image input',flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:4000')
    args=parser.parse_args()
    verify(args.catalog,args.base_url)
