"""Export systemd credentials as configured, then start the LiteLLM server.

``credentials.json`` beside the LiteLLM config (rendered from the deployment
configuration; it holds no secrets) maps each credential name loaded by systemd
to one export:

- ``{"env": VAR}``: the stripped credential text; must be one non-blank token.
- ``{"path_env": [VAR, ...]}``: the credential file path, for SDKs that read files.
- ``{"json": {field: VAR, ...}}``: string fields of a JSON credential document.
- ``{"modal_token": VAR}``: a Modal combined ``wk-...ws-...`` inference token.

Ambient cloud credentials are removed first, so providers only ever see the
credentials this deployment loaded.
"""
import json
import os
from pathlib import Path
import re
import sys

AMBIENT = (
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'VERTEXAI_PROJECT',
    'VERTEXAI_LOCATION', 'VERTEXAI_CREDENTIALS', 'VERTEX_CREDENTIALS_PATH', 'GOOGLE_CLOUD_PROJECT',
    'GOOGLE_CLOUD_QUOTA_PROJECT', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_BEARER_TOKEN_BEDROCK', 'AWS_REGION_NAME', 'AWS_REGION',
    'AWS_DEFAULT_REGION', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN',
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'MODAL_API_KEY',
)
ISOLATED = {'AWS_SHARED_CREDENTIALS_FILE': '/dev/null', 'AWS_CONFIG_FILE': '/dev/null',
            'AWS_EC2_METADATA_DISABLED': 'true'}
VARIABLE = re.compile(r'[A-Z][A-Z0-9_]*')


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


def single_token(text):
    value = text.strip()
    if not value or any(c.isspace() for c in value):
        raise ValueError('Credential must be one non-blank token')
    return value


def variables(spec):
    """Validate one export spec and return the variables it sets."""
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ValueError('Each credential has exactly one export')
    (kind, target), = spec.items()
    names = {'env': lambda: [target], 'modal_token': lambda: [target],
             'path_env': lambda: target if isinstance(target, list) and target else None,
             'json': lambda: list(target.values()) if isinstance(target, dict) and target else None}.get(kind, lambda: None)()
    if not names or not all(isinstance(n, str) and VARIABLE.fullmatch(n) for n in names):
        raise ValueError('Invalid credential export')
    if kind == 'json' and not all(isinstance(field, str) and field for field in target):
        raise ValueError('Invalid credential export')
    return names


def exports(specs, directory):
    """Return the environment exported from loaded credentials."""
    if not isinstance(specs, dict):
        raise ValueError('Invalid credential map')
    seen, result = set(), {}
    for name, spec in specs.items():
        names = variables(spec)
        if seen & set(names):
            raise ValueError('Two credentials export the same variable')
        seen.update(names)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise ValueError('Invalid credential name')
        path = directory / name
        (kind, target), = spec.items()
        if kind == 'env':
            result[target] = single_token(path.read_text())
        elif kind == 'modal_token':
            result[target] = modal_token(path.read_text())
        elif kind == 'path_env':
            if not path.is_file():
                raise ValueError('Credential file missing')
            result.update(dict.fromkeys(target, str(path)))
        else:
            document = json.loads(path.read_text())
            for field, variable in target.items():
                if not isinstance(document, dict) or not isinstance(document.get(field), str) or not document[field]:
                    raise ValueError('Credential document is missing a field')
                result[variable] = document[field]
    if 'LITELLM_MASTER_KEY' not in result:
        raise ValueError('No credential exports LITELLM_MASTER_KEY')
    return result


def main():
    config = Path(sys.argv[1])
    try:
        specs = json.loads(config.with_name('credentials.json').read_text())
        loaded = exports(specs, Path(os.environ['CREDENTIALS_DIRECTORY']))
    except (KeyError, OSError, ValueError):
        sys.exit('Cannot load LiteLLM systemd credentials; check credentials.json and the encrypted credential files.')
    for name in AMBIENT:
        os.environ.pop(name, None)
    os.environ.update(ISOLATED)
    os.environ.update(loaded)
    server = Path(__file__).with_name('serve.py')
    port = sys.argv[2] if len(sys.argv) > 2 else '4000'
    os.execv(sys.executable, [sys.executable, str(server), '--config', str(config), '--port', port])


if __name__ == '__main__':
    main()
