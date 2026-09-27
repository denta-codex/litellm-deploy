"""Load systemd credentials into the stock LiteLLM process without a disk copy."""
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
        token = modal_token((credentials / 'modal-inference-token').read_text())
    except (KeyError, OSError, ValueError):
        sys.exit('Cannot load LiteLLM systemd credentials; check the encrypted credential files.')
    os.environ['LITELLM_MASTER_KEY'] = key
    os.environ['MODAL_API_KEY'] = token
    executable = Path(sys.executable).with_name('litellm')
    os.execv(str(executable), [str(executable), '--config', sys.argv[1], '--host', '127.0.0.1', '--port', '4000'])


if __name__ == '__main__':
    main()
