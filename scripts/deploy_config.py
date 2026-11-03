"""Load the per-machine deployment configuration.

The repository is the same on every machine. Everything that differs between
machines lives in one file outside the repository, by default
``~/.config/litellm-deploy/config.yml`` (see ``deploy/config.example.yml``).
``deploy/tasks/target.yml`` maps the same keys onto Ansible variables; keep the
two mappings identical.
"""
from pathlib import Path

import yaml

DEFAULT = Path.home() / '.config/litellm-deploy/config.yml'
SYSTEM_CREDENTIAL = '/etc/credstore.encrypted/litellm-proxy-key'
PROXY_CREDENTIAL = {'litellm-proxy-key': {'env': 'LITELLM_MASTER_KEY'}}
REQUIRED = {'user': str, 'home': str, 'mise': str, 'codex_version': str, 'default_model': str,
            'credential_mode': str}
OPTIONAL = {'forward_agent': (bool, False), 'compaction_fallback': (bool, True), 'voice': (bool, False),
            'local_inference': (bool, False), 'browser': ((bool, dict), False), 'credentials': (list, []),
            'models': (list, [])}


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('Deployment configuration must be a mapping')
    unknown = set(config) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        raise ValueError('Unknown deployment configuration keys: ' + ', '.join(sorted(unknown)))
    for key, kind in REQUIRED.items():
        if not isinstance(config.get(key), kind) or not config[key]:
            raise ValueError('Deployment configuration requires ' + key)
    result = dict(config)
    for key, (kind, default) in OPTIONAL.items():
        result.setdefault(key, default)
        if not isinstance(result[key], kind):
            raise ValueError('Invalid deployment configuration value: ' + key)
    if result['credential_mode'] not in ('user', 'system'):
        raise ValueError('credential_mode must be user or system')
    if not Path(result['home']).is_absolute() or not Path(result['mise']).is_absolute():
        raise ValueError('home and mise must be absolute paths')
    if result['browser'] is True or (isinstance(result['browser'], dict) and not result['browser'].get('node')):
        raise ValueError('browser must be false or a mapping with node')
    names = set()
    for credential in result['credentials']:
        if (not isinstance(credential, dict) or set(credential) != {'name', 'path', 'export'}
                or credential['name'] in names or credential['name'] in PROXY_CREDENTIAL
                or not Path(str(credential['path'])).is_absolute()):
            raise ValueError('Each credential needs a unique name, an absolute path, and one export')
        names.add(credential['name'])
    return result


def load(path=None):
    path = Path(path) if path is not None else DEFAULT
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ValueError(f'Missing deployment configuration {path}; start from deploy/config.example.yml') from None
    return validate(yaml.safe_load(text))


def credential_exports(config):
    """The non-secret ``credentials.json`` consumed by ``launch.py``."""
    return PROXY_CREDENTIAL | {c['name']: c['export'] for c in config['credentials']}


def variables(config):
    """Template variables, named as in the Ansible playbooks."""
    home = config['home']
    return {
        'litellm_user': config['user'],
        'litellm_home': home,
        'litellm_root': home + '/.local/share/litellm',
        'litellm_config': home + '/.config/litellm',
        'litellm_state': home + '/.local/state/litellm',
        'codex_mise': config['mise'],
        'codex_version': config['codex_version'],
        'litellm_codex': f"{home}/.local/share/mise/installs/codex/{config['codex_version']}/bin/codex",
        'litellm_model': config['default_model'],
        'litellm_credential_mode': config['credential_mode'],
        'litellm_system_credential': SYSTEM_CREDENTIAL,
        'codex_forward_agent': config['forward_agent'],
        'compaction_fallback_enabled': config['compaction_fallback'],
        'litellm_voice_enabled': config['voice'],
        'litellm_local_inference': config['local_inference'],
        'litellm_browser': config['browser'],
        'litellm_credentials': config['credentials'],
        'litellm_credential_exports': credential_exports(config),
        'litellm_models': config['models'],
    }
