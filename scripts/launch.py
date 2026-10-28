"""Load systemd credentials before starting the configured LiteLLM server."""
import os
from pathlib import Path
import re
import sys


def modal_token(text):
    text = text.strip()
    if '=' not in text and '\n' not in text and '\r' not in text:
        token = text
    else:
        values = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.removeprefix('export ').partition('=')
            key, value = key.strip(), value.strip()
            if not separator or key not in ('MODAL_PROXY_TOKEN', 'WK_SECRET', 'WS_SECRET') or key in values:
                raise ValueError('Invalid Modal credential format')
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
        token = values.get('MODAL_PROXY_TOKEN')
        if not token:
            wk, ws = values.get('WK_SECRET', ''), values.get('WS_SECRET', '')
            token = wk + '.' + ws if wk and ws else ''
    if not re.fullmatch(r'wk-[A-Za-z0-9_-]+\.ws-[A-Za-z0-9_-]+', token):
        raise ValueError('Modal credential must contain a combined wk/ws inference token')
    return token


def main():
    try:
        credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
        key = (credentials / 'litellm-proxy-key').read_text().strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError('Invalid LiteLLM proxy credential')
        modal_path = credentials / 'modal-inference-token'
        token = modal_token(modal_path.read_text()) if modal_path.exists() else None
        bedrock_path = credentials / 'bedrock-aws-credentials'
        bedrock = None
        if bedrock_path.exists():
            from bedrock_aws import credential_values
            bedrock = credential_values(bedrock_path.read_text())
        vertex_path = credentials / 'vertex-ai-credentials'
        if vertex_path.exists():
            from vertex_google import credential_values as vertex_values
            vertex_values(vertex_path.read_text())
    except (KeyError, OSError, ValueError):
        sys.exit('Cannot load LiteLLM systemd credentials; check the encrypted credential files.')
    os.environ['LITELLM_MASTER_KEY'] = key
    os.environ.pop('MODAL_API_KEY', None)
    if token is not None:
        os.environ['MODAL_API_KEY'] = token
    for name in ('GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
                 'VERTEXAI_PROJECT', 'VERTEXAI_LOCATION', 'VERTEXAI_CREDENTIALS',
                 'VERTEX_CREDENTIALS_PATH', 'GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_QUOTA_PROJECT'):
        os.environ.pop(name, None)
    if vertex_path.exists():
        os.environ['VERTEX_CREDENTIALS_PATH'] = str(vertex_path)
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(vertex_path)
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE',
                 'AWS_DEFAULT_PROFILE', 'AWS_BEARER_TOKEN_BEDROCK', 'AWS_REGION_NAME', 'AWS_REGION', 'AWS_DEFAULT_REGION',
                 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
                 'AWS_CONTAINER_CREDENTIALS_FULL_URI'):
        os.environ.pop(name, None)
    os.environ['AWS_SHARED_CREDENTIALS_FILE'] = '/dev/null'
    os.environ['AWS_CONFIG_FILE'] = '/dev/null'
    os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
    if bedrock is not None:
        os.environ['AWS_ACCESS_KEY_ID'] = bedrock['access_key_id']
        os.environ['AWS_SECRET_ACCESS_KEY'] = bedrock['secret_access_key']
        os.environ['AWS_REGION_NAME'] = bedrock['region']
    server = Path(__file__).with_name('serve.py')
    port = sys.argv[2] if len(sys.argv) > 2 else '4000'
    os.execv(sys.executable, [sys.executable, str(server), '--config', sys.argv[1], '--port', port])


if __name__ == '__main__':
    main()
