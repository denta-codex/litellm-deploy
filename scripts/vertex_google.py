"""Exact Vertex project/model and encrypted service-account authentication."""
import json
import re
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

PROJECT = 'fourth-gantry-511518-b0'
LOCATION = 'global'
MODEL = 'gemini-4-argon'
SLUG = 'vertex/' + MODEL
ACCOUNT = 'litellm-vertex@' + PROJECT + '.iam.gserviceaccount.com'
CREDENTIAL_NAME = 'vertex-ai-credentials'
CREDENTIAL = Path('/etc/credstore.encrypted/litellm-vertex-ai')
PERMISSIONS = ['aiplatform.endpoints.predict', 'serviceusage.services.use']
ENDPOINT = f'https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent'


def credential_values(text):
    value = json.loads(text)
    if (not isinstance(value, dict) or value.get('type') != 'service_account'
            or value.get('project_id') != PROJECT or value.get('client_email') != ACCOUNT
            or value.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or not isinstance(value.get('private_key'), str)
            or not value['private_key'].startswith('-----BEGIN PRIVATE KEY-----\n')
            or not isinstance(value.get('private_key_id'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', value['private_key_id'])):
        raise ValueError('Invalid dedicated Vertex service-account credential')
    return value


def encrypted_credentials(path=CREDENTIAL):
    result = subprocess.run(['sudo', '-n', 'systemd-creds', 'decrypt',
        '--name=' + CREDENTIAL_NAME, str(path), '-'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Cannot decrypt Vertex credential; secret output suppressed')
    return credential_values(result.stdout)


def discover(value):
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request
    credential_values(json.dumps(value))
    credentials = Credentials.from_service_account_info(value, scopes=['https://www.googleapis.com/auth/cloud-platform'])
    credentials.refresh(Request())
    payload = {'contents':[{'role':'user','parts':[{'text':'Reply exactly VERTEX_DISCOVERY_OK.'}]}],
        'generationConfig':{'maxOutputTokens':128, 'thinkingConfig':{'thinkingLevel':'LOW'}}}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json', 'Authorization':'Bearer '+credentials.token,
                 'x-goog-user-project':PROJECT})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Vertex discovery HTTP {exc.code}; no routing changes made') from None
    text = ''.join(p.get('text','') for c in data.get('candidates',[]) for p in c.get('content',{}).get('parts',[]))
    if 'VERTEX_DISCOVERY_OK' not in text:
        raise ValueError('Argon inference probe failed; no routing changes made')
    return {'project':PROJECT, 'location':LOCATION, 'model':MODEL, 'inference_verified':True}
