"""Recoverable desktop authentication configuration; never reads plaintext keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

import tomlkit


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
            temporary = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def link(path, target):
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / 'codex'
        temporary.symlink_to(target)
        os.replace(temporary, path)


def migrate(original):
    doc = tomlkit.parse(original)
    if doc.get('model_provider') != 'litellm':
        raise RuntimeError('Expected the existing LiteLLM provider')
    provider = doc['model_providers']['litellm']
    if provider.get('base_url') != 'http://127.0.0.1:4000/v1':
        raise RuntimeError('Unexpected LiteLLM endpoint')
    if provider.get('experimental_bearer_token') or provider.get('env_key') not in (None, 'LITELLM_PROXY_KEY'):
        raise RuntimeError('Unexpected provider credential configuration')
    provider.pop('auth', None)
    provider['requires_openai_auth'] = True
    provider['env_key'] = 'LITELLM_PROXY_KEY'
    # Shell snapshots can restore startup variables after environment filtering.
    # They must not capture the proxy credential or inject it into tools.
    doc.setdefault('features', tomlkit.table())['shell_snapshot'] = False
    # Keep the existing standard default. Desktop task choices are independent.
    if doc.get('service_tier') not in (None, 'default'):
        raise RuntimeError('Expected a standard-speed default before migration')
    policy = doc.setdefault('shell_environment_policy', tomlkit.table())
    # Codex supports the legacy arrays, but rejects mixing them with filters.
    if 'filters' in policy:
        filters = policy['filters']
        for key in list(filters):
            if key.upper() == 'LITELLM_PROXY_KEY':
                del filters[key]
        filters['LITELLM_PROXY_KEY'] = 'exclude'
    else:
        exclusions = policy.setdefault('exclude', [])
        if 'LITELLM_PROXY_KEY' not in exclusions:
            exclusions.append('LITELLM_PROXY_KEY')
    # Explicit environment assignments happen after filtering.
    if any(key.upper() == 'LITELLM_PROXY_KEY' for key in policy.get('set', {})):
        raise RuntimeError('Remove the explicit proxy-key shell assignment first')
    return tomlkit.dumps(doc)


def run(action, home):
    config = home / '.codex/config.toml'
    launcher = home / '.local/bin/codex'
    target = str(home / '.local/share/litellm/scripts/codex-launcher')
    state = home / '.local/state/litellm/fast-toggle.json'
    if action == 'finish' and not state.exists():
        print('unchanged: no pending recovery record')
        return
    if action == 'prepare' and not state.exists():
        original = config.read_text()
        prepared = migrate(original)
        if launcher.is_symlink() and os.readlink(launcher) == target and original == prepared:
            print('unchanged: Fast configuration already installed')
            return
        if not launcher.is_symlink():
            raise RuntimeError('Expected an existing Codex launcher symlink')
        if not Path(target).is_file():
            raise RuntimeError('Install the repository launcher before preparing')
        pending = {'original': original, 'prepared': prepared,
                   'launcher_before': os.readlink(launcher), 'launcher_after': target,
                   'launcher_sha256': hashlib.sha256(Path(target).read_bytes()).hexdigest()}
        write(state, json.dumps(pending))
    else:
        pending = json.loads(state.read_text())
    current = config.read_text()
    current_target = os.readlink(launcher) if launcher.is_symlink() else None
    if hashlib.sha256(Path(target).read_bytes()).hexdigest() != pending['launcher_sha256']:
        raise RuntimeError('Installed launcher changed since prepare; review intervening edits')
    if action == 'prepare' and current == pending['prepared'] and current_target == pending['launcher_after']:
        print('unchanged: already prepared; desktop acceptance pending')
        return
    if action in ('prepare', 'rollback'):
        # Permit recovery after interruption between the two atomic replacements.
        if current not in (pending['original'], pending['prepared']):
            raise RuntimeError('Config changed since prepare; refusing to overwrite intervening edits')
        if current_target not in (pending['launcher_before'], pending['launcher_after']):
            raise RuntimeError('Launcher changed since prepare; refusing to overwrite intervening edits')
        before = action == 'rollback'
        write(config, pending['original'] if before else pending['prepared'])
        link(launcher, pending['launcher_before'] if before else pending['launcher_after'])
        if before:
            state.unlink()
        print('changed: ' + ('restored prior launcher/configuration' if before else 'prepared; restart Grace through its desktop'))
    elif action == 'finish':
        # Task-level Fast choices may legitimately change the global saved tier.
        actual, expected = tomlkit.parse(current), tomlkit.parse(pending['prepared'])
        actual.pop('service_tier', None)
        expected.pop('service_tier', None)
        if actual != expected or current_target != pending['launcher_after']:
            raise RuntimeError('Configuration or launcher changed; review before removing recovery state')
        state.unlink()
        print('changed: removed accepted Fast-toggle recovery state')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'rollback', 'finish'])
    parser.add_argument('--home', type=Path, default=Path('/home/agent'))
    args = parser.parse_args()
    os.umask(0o077)
    run(args.action, args.home)
