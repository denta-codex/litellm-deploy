"""Provision the dedicated project inference identity; never write a plaintext key."""
import base64
import json
import subprocess
import urllib.error
import urllib.request
from urllib.parse import quote

from vertex_google import ACCOUNT, PROJECT, CREDENTIAL, CREDENTIAL_NAME, PERMISSIONS, credential_values, encrypted_credentials

ROLE_ID = 'litellmVertexPredict'
ROLE = f'projects/{PROJECT}/roles/{ROLE_ID}'


def gcloud(*args):
    result = subprocess.run(['gcloud',*args,'--project',PROJECT,'--format=json','--quiet'],
        capture_output=True,text=True,timeout=120)
    if result.returncode:
        raise ValueError('GCP operation failed: '+ ' '.join(args[:3]) + '; credential output suppressed')
    return json.loads(result.stdout) if result.stdout.strip() else None


def provision():
    gcloud('services','enable','iam.googleapis.com','aiplatform.googleapis.com')
    accounts = gcloud('iam','service-accounts','list')
    if not any(a['email']==ACCOUNT for a in accounts):
        gcloud('iam','service-accounts','create','litellm-vertex','--display-name=Grace LiteLLM Vertex inference')
    roles = gcloud('iam','roles','list')
    existing = next((r for r in roles if r['name']==ROLE),None)
    if existing:
        role = gcloud('iam','roles','describe',ROLE_ID)
        if sorted(role.get('includedPermissions',[])) != sorted(PERMISSIONS) or role.get('deleted'):
            raise ValueError('Existing Vertex role differs from the exact inference permissions')
    else:
        gcloud('iam','roles','create',ROLE_ID,'--title=LiteLLM Vertex inference',
            '--stage=GA','--permissions='+','.join(PERMISSIONS))
    gcloud('projects','add-iam-policy-binding',PROJECT,'--member=serviceAccount:'+ACCOUNT,'--role='+ROLE,'--condition=None')
    exists = subprocess.run(['sudo','-n','test','-f',str(CREDENTIAL)],capture_output=True).returncode == 0
    if exists:
        encrypted_credentials()
        return {'changed':False,'account':ACCOUNT,'role':ROLE,'credential':str(CREDENTIAL)}
    token = subprocess.check_output(['gcloud','auth','print-access-token'],text=True,timeout=60).strip()
    url = 'https://iam.googleapis.com/v1/projects/-/serviceAccounts/'+quote(ACCOUNT,safe='')+'/keys'
    request = urllib.request.Request(url,data=json.dumps({'privateKeyType':'TYPE_GOOGLE_CREDENTIALS_FILE','keyAlgorithm':'KEY_ALG_RSA_2048'}).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=60) as response: created = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Vertex key creation HTTP {exc.code}; no key output printed') from None
    staged = str(CREDENTIAL)+'.new'
    try:
        plaintext = base64.b64decode(created['privateKeyData'],validate=True)
        credential_values(plaintext.decode())
        subprocess.run(['sudo','-n','systemd-creds','encrypt','--name='+CREDENTIAL_NAME,'-',staged],
            input=plaintext,capture_output=True,check=True,timeout=30)
        subprocess.run(['sudo','-n','chmod','0600',staged],check=True,capture_output=True)
        subprocess.run(['sudo','-n','mv','-T',staged,str(CREDENTIAL)],check=True,capture_output=True)
    except Exception:
        # Delete the newly issued cloud key if secure local storage failed.
        request=urllib.request.Request('https://iam.googleapis.com/v1/'+created['name'],method='DELETE',
            headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(request,timeout=60):pass
        subprocess.run(['sudo','-n','rm','-f',staged],check=True,capture_output=True)
        raise ValueError('Vertex credential storage failed; new key revoked') from None
    return {'changed':True,'account':ACCOUNT,'role':ROLE,'credential':str(CREDENTIAL)}


if __name__ == '__main__':print(json.dumps(provision()))
