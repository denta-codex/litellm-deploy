"""Non-secret host settings and in-memory access to the local gateway key."""
import json
import os
from pathlib import Path
import subprocess


def settings(home=None):
    home = Path(home) if home is not None else Path.home()
    path = home / '.config/litellm/deployment.json'
    value = json.loads(path.read_text())
    if Path(value['home']) != home or value['credential_mode'] not in ('user', 'system'):
        raise ValueError('Gateway deployment settings do not match the selected home')
    return value


def proxy_key(credential=None, home=None):
    """Return a key to the caller; never report subprocess output on failure."""
    home = Path(home) if home is not None else Path.home()
    config = settings(home)
    if config['credential_mode'] == 'user':
        command = ['/usr/bin/systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
                   str(credential or home / '.config/litellm/proxy-key.cred'), '-']
    else:
        command = ['/usr/bin/systemd-creds', 'decrypt', '--name=litellm-proxy-key',
                   config['system_credential'], '-']
        if os.geteuid() != 0:
            command = ['sudo', '-n', *command]
    try:
        key = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Cannot load the local gateway credential') from None
    if not key or any(character.isspace() for character in key):
        raise RuntimeError('The local gateway credential is empty or invalid')
    return key


def codex_binary(home=None):
    return settings(home)['codex']
