"""Manage Grace's stock Codex runtime as one recoverable transaction."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
import time

import tomlkit


SCHEMA = 1
MANAGED_PATHS = (
    '/home/agent/.codex/config.toml',
    '/home/agent/.local/share/litellm/scripts/codex-launcher',
    '/home/agent/.local/bin/codex',
    '/etc/systemd/system/codex-app-server.service',
    '/etc/profile.d/codex-app-server.sh',
    '/etc/systemd/system/codex-app-server.service.d/20-litellm-launcher.conf',
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def render_launcher(version, credential='/home/agent/.config/litellm/proxy-key.cred',
                    binary=None, decrypt='/usr/bin/systemd-creds'):
    binary = binary or f'/home/agent/.local/share/mise/installs/codex/{version}/bin/codex'
    return f'''#!/bin/bash
# Repository-managed launcher for Grace's Codex runtime.
set +x
set -euo pipefail
unset LITELLM_PROXY_KEY
if ! LITELLM_PROXY_KEY=$({decrypt} decrypt --user --name=litellm-proxy-key {credential} - 2>/dev/null); then
    echo 'Codex: cannot decrypt the LiteLLM proxy credential.' >&2
    exit 1
fi
if [[ -z "$LITELLM_PROXY_KEY" || "$LITELLM_PROXY_KEY" =~ [[:space:]] ]]; then
    echo 'Codex: the LiteLLM proxy credential is empty or invalid.' >&2
    exit 1
fi
export LITELLM_PROXY_KEY
exec {binary} "$@"
'''


def render_service():
    return '''[Unit]
Description=Codex app server for agent
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/home/agent
StartLimitIntervalSec=0

[Service]
Type=simple
User=agent
Group=agent
WorkingDirectory=/home/agent
Environment=HOME=/home/agent
Environment=CODEX_HOME=/home/agent/.codex
Environment=PATH=/home/agent/.local/share/mise/shims:/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=SSH_AUTH_SOCK=/home/agent/.codex/app-server-control/forwarded-ssh-agent.sock
ExecStart=/home/agent/.local/share/litellm/scripts/codex-launcher -c features.code_mode_host=true app-server --listen unix://
Restart=always
RestartSec=5
TimeoutStopSec=120
KillMode=control-group
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def render_profile():
    return '''# systemd owns the agent app server; Codex Desktop connects through its proxy.
if [ "$(id -un)" = agent ]; then
    export CODEX_SSH_SKIP_APP_SERVER_BOOT=true
fi
'''


def migrate_config(original):
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
    doc.setdefault('features', tomlkit.table())['shell_snapshot'] = False
    if doc.get('service_tier') not in (None, 'default'):
        raise RuntimeError('Expected a standard-speed default before runtime migration')
    policy = doc.setdefault('shell_environment_policy', tomlkit.table())
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
    if any(key.upper() == 'LITELLM_PROXY_KEY' for key in policy.get('set', {})):
        raise RuntimeError('Remove the explicit proxy-key shell assignment first')
    return tomlkit.dumps(doc)


class Runtime:
    def __init__(self, version, root=Path('/'), home=Path('/home/agent')):
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', version):
            raise RuntimeError('Codex version must be an exact release identifier')
        self.version = version
        self.root = root
        self.home = home
        self.agent = pwd.getpwnam('agent') if root == Path('/') else None
        self.uid = self.agent.pw_uid if self.agent else os.getuid()
        self.gid = self.agent.pw_gid if self.agent else os.getgid()
        self.state_dir = self.path('/home/agent/.local/state/litellm')
        self.transaction = self.state_dir / 'codex-runtime.json'
        self.installed = self.state_dir / 'codex-runtime-installed.json'
        self.observation = self.state_dir / 'fast-observation.jsonl'

    def path(self, absolute):
        absolute = Path(absolute)
        return absolute if self.root == Path('/') else self.root / absolute.relative_to('/')

    def binary(self):
        return self.path(f'/home/agent/.local/share/mise/installs/codex/{self.version}/bin/codex')

    def desired(self):
        config = self.path('/home/agent/.codex/config.toml')
        return {
            '/home/agent/.codex/config.toml': self.file(migrate_config(config.read_text()), 0o600, self.uid, self.gid),
            '/home/agent/.local/share/litellm/scripts/codex-launcher': self.file(render_launcher(self.version), 0o700, self.uid, self.gid),
            '/home/agent/.local/bin/codex': {'kind': 'symlink', 'target': '/home/agent/.local/share/litellm/scripts/codex-launcher'},
            '/etc/systemd/system/codex-app-server.service': self.file(render_service(), 0o644, 0 if self.root == Path('/') else self.uid, 0 if self.root == Path('/') else self.gid),
            '/etc/profile.d/codex-app-server.sh': self.file(render_profile(), 0o644, 0 if self.root == Path('/') else self.uid, 0 if self.root == Path('/') else self.gid),
            '/etc/systemd/system/codex-app-server.service.d/20-litellm-launcher.conf': {'kind': 'absent'},
        }

    @staticmethod
    def file(content, mode, uid, gid):
        data = content.encode()
        return {'kind': 'file', 'content': base64.b64encode(data).decode(), 'sha256': sha256(data),
                'mode': mode, 'uid': uid, 'gid': gid}

    def snapshot(self, name, include_content=True):
        path = self.path(name)
        if path.is_symlink():
            return {'kind': 'symlink', 'target': os.readlink(path)}
        if not path.exists():
            return {'kind': 'absent'}
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f'Refusing unsupported managed path type: {name}')
        data = path.read_bytes()
        result = {'kind': 'file', 'sha256': sha256(data), 'mode': stat.S_IMODE(info.st_mode),
                  'uid': info.st_uid, 'gid': info.st_gid}
        if include_content:
            result['content'] = base64.b64encode(data).decode()
        return result

    def fingerprint(self, entry):
        return {key: value for key, value in entry.items() if key != 'content'}

    def write_json(self, path, value, mode=0o600):
        self.write_file(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode(), mode, self.uid, self.gid)

    @staticmethod
    def ensure_dir(path, mode, uid, gid):
        if path.exists():
            return
        path.mkdir(parents=True, mode=mode)
        os.chmod(path, mode)
        os.chown(path, uid, gid)

    def write_file(self, path, data, mode, uid, gid):
        parent_mode = 0o700 if uid == self.uid else 0o755
        self.ensure_dir(path.parent, parent_mode, uid, gid)
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.chown(temporary, uid, gid)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def install_entry(self, name, entry):
        path = self.path(name)
        if entry['kind'] == 'absent':
            if path.is_symlink() or path.exists():
                path.unlink()
            return
        parent_uid = self.uid if name.startswith('/home/agent/') else 0
        parent_gid = self.gid if name.startswith('/home/agent/') else 0
        self.ensure_dir(path.parent, 0o700 if parent_uid == self.uid else 0o755, parent_uid, parent_gid)
        if entry['kind'] == 'symlink':
            temporary = path.parent / ('.' + path.name + '.new')
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink()
            temporary.symlink_to(entry['target'])
            os.replace(temporary, path)
            return
        self.write_file(path, base64.b64decode(entry['content']), entry['mode'], entry['uid'], entry['gid'])

    def validate_binary(self):
        binary = self.binary()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f'Codex {self.version} is not installed at the managed path')
        result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != f'codex-cli {self.version}':
            raise RuntimeError(f'Managed binary did not report codex-cli {self.version}')

    def validate_catalog(self):
        catalog = self.path('/home/agent/.config/litellm/codex-models.json')
        if not catalog.is_file():
            raise RuntimeError('Existing Codex model catalog is missing')
        self.ensure_dir(self.state_dir, 0o700, self.uid, self.gid)
        with tempfile.TemporaryDirectory(prefix='codex-runtime-catalog-', dir=self.state_dir) as temporary:
            result = subprocess.run([self.binary(), 'debug', 'models', '-c',
                                     'model_catalog_json=' + json.dumps(str(catalog))],
                                    env=os.environ | {'CODEX_HOME': temporary}, capture_output=True,
                                    text=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Pinned Codex rejected the existing catalog: ' + result.stderr[-1200:])
        try:
            models = json.loads(result.stdout)['models']
            config = tomlkit.parse(self.path('/home/agent/.codex/config.toml').read_text())
            selected = config['model']
            if not any(model.get('slug') == selected for model in models):
                raise RuntimeError(f'Existing catalog does not contain selected model {selected}')
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError('Pinned Codex returned an invalid catalog probe') from error

    def current_fingerprints(self):
        return {name: self.fingerprint(self.snapshot(name, include_content=False)) for name in MANAGED_PATHS}

    def assert_matches(self, expected, label):
        actual = self.current_fingerprints()
        mismatches = [name for name in MANAGED_PATHS if actual[name] != expected[name]]
        if mismatches:
            raise RuntimeError(f'{label} differs at: ' + ', '.join(mismatches))

    def socket_identity(self):
        socket = self.path('/home/agent/.codex/app-server-control/app-server-control.sock')
        if not socket.exists():
            return None
        info = socket.stat()
        return [info.st_ino, info.st_mtime_ns, info.st_ctime_ns]

    def load(self, path):
        return json.loads(path.read_text())

    def summary(self):
        desired = {name: self.fingerprint(entry) for name, entry in self.desired().items()}
        current = self.current_fingerprints()
        installed = self.load(self.installed) if self.installed.exists() else None
        pending = self.load(self.transaction) if self.transaction.exists() else None
        changes = [name for name in MANAGED_PATHS if current[name] != desired[name]]
        return {
            'action': 'preview', 'target_version': self.version,
            'binary_installed': self.binary().is_file(),
            'pending': pending is not None,
            'accepted_version': installed.get('version') if installed else None,
            'changes': changes,
            'restart_required': (self.socket_identity() == pending['socket_before']) if pending else bool(changes),
        }

    def apply(self):
        self.validate_binary()
        self.validate_catalog()
        desired = self.desired()
        desired_fingerprints = {name: self.fingerprint(entry) for name, entry in desired.items()}
        if self.transaction.exists():
            transaction = self.load(self.transaction)
            if transaction['target_version'] != self.version:
                raise RuntimeError('A different Codex runtime transaction is pending')
            actual = self.current_fingerprints()
            for name in MANAGED_PATHS:
                if actual[name] not in (transaction['before_fingerprints'][name], transaction['after'][name]):
                    raise RuntimeError(f'Intervening edit at {name}; refusing to continue')
            if actual == transaction['after']:
                return {'action': 'apply', 'changed': False, 'target_version': self.version,
                        'restart_required': self.socket_identity() == transaction['socket_before']}
        else:
            if self.installed.exists():
                accepted = self.load(self.installed)
                self.assert_matches(accepted['managed'], 'Previously accepted Codex runtime')
                if accepted['version'] == self.version and self.current_fingerprints() == desired_fingerprints:
                    return {'action': 'apply', 'changed': False, 'target_version': self.version,
                            'restart_required': False}
            before = {name: self.snapshot(name) for name in MANAGED_PATHS}
            transaction = {
                'schema': SCHEMA, 'target_version': self.version, 'created_at': time.time(),
                'socket_before': self.socket_identity(), 'before': before,
                'before_fingerprints': {name: self.fingerprint(entry) for name, entry in before.items()},
                'after': desired_fingerprints,
            }
            self.ensure_dir(self.state_dir, 0o700, self.uid, self.gid)
            self.write_json(self.transaction, transaction)
        for name in MANAGED_PATHS:
            self.install_entry(name, desired[name])
        self.assert_matches(desired_fingerprints, 'Prepared Codex runtime')
        return {'action': 'apply', 'changed': True, 'target_version': self.version,
                'restart_required': self.socket_identity() == transaction['socket_before']}

    def verify(self):
        source = self.load(self.transaction) if self.transaction.exists() else self.load(self.installed)
        managed_version = source['target_version'] if 'target_version' in source else source['version']
        if managed_version != self.version:
            raise RuntimeError('Requested version differs from managed runtime state')
        expected = source['after'] if 'after' in source else source['managed']
        self.assert_matches(expected, 'Managed Codex runtime')
        self.validate_binary()
        self.validate_catalog()
        if self.transaction.exists() and self.socket_identity() == source['socket_before']:
            raise RuntimeError('Desktop has not restarted the systemd-owned app server')
        return {'action': 'verify', 'version': self.version, 'files': 'accepted', 'socket_replaced': True}

    def rollback(self):
        if not self.transaction.exists():
            return {'action': 'rollback', 'changed': False, 'message': 'no pending transaction'}
        transaction = self.load(self.transaction)
        self.assert_matches(transaction['after'], 'Prepared Codex runtime')
        for name in MANAGED_PATHS:
            self.install_entry(name, transaction['before'][name])
        self.assert_matches(transaction['before_fingerprints'], 'Restored Codex runtime')
        self.transaction.unlink()
        self.observation.unlink(missing_ok=True)
        return {'action': 'rollback', 'changed': True, 'restart_required': True}

    def finish(self):
        if not self.transaction.exists():
            return {'action': 'finish', 'changed': False, 'message': 'no pending transaction'}
        transaction = self.load(self.transaction)
        self.assert_matches(transaction['after'], 'Prepared Codex runtime')
        manifest = {'schema': SCHEMA, 'version': transaction['target_version'],
                    'accepted_at': time.time(), 'managed': transaction['after']}
        self.write_json(self.installed, manifest)
        self.transaction.unlink()
        self.observation.unlink(missing_ok=True)
        return {'action': 'finish', 'changed': True, 'version': transaction['target_version']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preview', 'apply', 'verify', 'rollback', 'finish'])
    parser.add_argument('--version', required=True)
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    os.umask(0o077)
    runtime = Runtime(args.version, args.root)
    try:
        result = runtime.summary() if args.action == 'preview' else getattr(runtime, args.action)()
    except (OSError, RuntimeError, subprocess.SubprocessError, tomlkit.exceptions.ParseError) as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
