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


SCHEMA = 2
LEGACY_SCHEMA = 1
CONFIG_PATH = '/home/agent/.codex/config.toml'
MANAGED_PATHS = (
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


def config_errors(doc):
    errors = []
    if doc.get('model_provider') != 'litellm':
        errors.append('Set model_provider = "litellm".')
    providers = doc.get('model_providers', {})
    provider = providers.get('litellm', {}) if hasattr(providers, 'get') else {}
    if provider.get('base_url') != 'http://127.0.0.1:4000/v1':
        errors.append('Set model_providers.litellm.base_url = "http://127.0.0.1:4000/v1".')
    if provider.get('wire_api') != 'responses':
        errors.append('Set model_providers.litellm.wire_api = "responses".')
    if provider.get('requires_openai_auth') is not True:
        errors.append('Set model_providers.litellm.requires_openai_auth = true.')
    if provider.get('env_key') != 'LITELLM_PROXY_KEY':
        errors.append('Set model_providers.litellm.env_key = "LITELLM_PROXY_KEY".')
    if 'auth' in provider:
        errors.append('Remove the model_providers.litellm.auth table.')
    if 'experimental_bearer_token' in provider:
        errors.append('Remove model_providers.litellm.experimental_bearer_token.')
    if doc.get('service_tier') not in (None, 'default'):
        errors.append('Set service_tier = "default" or remove it.')
    features = doc.get('features', {})
    if not hasattr(features, 'get') or features.get('shell_snapshot') is not False:
        errors.append('Set features.shell_snapshot = false.')
    policy = doc.get('shell_environment_policy', {})
    policy = policy if hasattr(policy, 'get') else {}
    assigned = policy.get('set', {})
    assigned = assigned if hasattr(assigned, 'keys') else {}
    if any(str(key).upper() == 'LITELLM_PROXY_KEY' for key in assigned):
        errors.append('Remove LITELLM_PROXY_KEY from shell_environment_policy.set.')
    filters = policy.get('filters', {})
    filters = filters if hasattr(filters, 'items') else {}
    filtered = any(str(key).upper() == 'LITELLM_PROXY_KEY' and value == 'exclude'
                   for key, value in filters.items())
    exclusions = policy.get('exclude', [])
    excluded = (not isinstance(exclusions, (str, bytes, dict)) and
                any(str(value) == 'LITELLM_PROXY_KEY' for value in exclusions))
    if not (filtered or excluded):
        errors.append('Exclude LITELLM_PROXY_KEY through shell_environment_policy.exclude or filters.')
    return errors


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
        return {
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

    def validate_config(self):
        config = self.path(CONFIG_PATH)
        if config.is_symlink() or not config.exists():
            raise RuntimeError(f'Grace\'s user-managed Codex config must be a regular file at {CONFIG_PATH}.')
        info = config.stat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f'Grace\'s user-managed Codex config must be a regular file at {CONFIG_PATH}.')
        errors = []
        if info.st_uid != self.uid or info.st_gid != self.gid:
            errors.append(f'Run chown agent:agent {CONFIG_PATH}.')
        if stat.S_IMODE(info.st_mode) != 0o600:
            errors.append(f'Run chmod 600 {CONFIG_PATH}.')
        doc = tomlkit.parse(config.read_text())
        errors.extend(config_errors(doc))
        if errors:
            raise RuntimeError('Grace\'s user-managed Codex config is incompatible:\n- ' + '\n- '.join(errors))
        return doc

    def validate_catalog(self, config):
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

    def load_installed(self):
        installed = self.load(self.installed)
        if installed.get('schema') != SCHEMA:
            raise RuntimeError('Unsupported accepted Codex runtime state; expected schema 2')
        return installed

    def cutover_pending(self, write, require_version=False):
        transaction = self.load(self.transaction)
        if require_version and transaction.get('target_version') != self.version:
            raise RuntimeError('A different Codex runtime transaction is pending')
        if transaction.get('schema') == SCHEMA:
            return transaction, False
        legacy_paths = set(MANAGED_PATHS) | {CONFIG_PATH}
        required = {'before', 'before_fingerprints', 'after'}
        if transaction.get('schema') != LEGACY_SCHEMA or not required.issubset(transaction):
            raise RuntimeError('Unsupported pending Codex runtime state; expected the known schema-1 transaction')
        if any(set(transaction[name]) != legacy_paths for name in required):
            raise RuntimeError('Pending schema-1 transaction does not match Grace\'s known runtime state')
        converted = dict(transaction)
        converted['schema'] = SCHEMA
        for name in required:
            converted[name] = {path: entry for path, entry in transaction[name].items()
                               if path != CONFIG_PATH}
        actual = self.current_fingerprints()
        mismatches = [name for name in MANAGED_PATHS if actual[name] != converted['after'][name]]
        if mismatches:
            raise RuntimeError('Pending schema-1 runtime is not fully staged at: ' + ', '.join(mismatches))
        if write:
            self.write_json(self.transaction, converted)
        return converted, True

    def load_pending(self):
        transaction = self.load(self.transaction)
        if transaction.get('schema') != SCHEMA:
            raise RuntimeError('Run the runtime apply action once to release config.toml from the pending transaction')
        return transaction

    def summary(self):
        self.validate_config()
        desired = {name: self.fingerprint(entry) for name, entry in self.desired().items()}
        current = self.current_fingerprints()
        installed = self.load_installed() if self.installed.exists() else None
        pending, pending_cutover = self.cutover_pending(write=False) if self.transaction.exists() else (None, False)
        changes = [name for name in MANAGED_PATHS if current[name] != desired[name]]
        return {
            'action': 'preview', 'target_version': self.version,
            'binary_installed': self.binary().is_file(),
            'pending': pending is not None,
            'pending_config_release': pending_cutover,
            'config': 'valid and user-managed',
            'accepted_version': installed.get('version') if installed else None,
            'changes': changes,
            'restart_required': (self.socket_identity() == pending['socket_before']) if pending else bool(changes),
        }

    def apply(self):
        config = self.validate_config()
        self.validate_binary()
        self.validate_catalog(config)
        self.check_remote_candidate()
        desired = self.desired()
        desired_fingerprints = {name: self.fingerprint(entry) for name, entry in desired.items()}
        if self.transaction.exists():
            transaction, cutover = self.cutover_pending(write=True, require_version=True)
            actual = self.current_fingerprints()
            for name in MANAGED_PATHS:
                if actual[name] not in (transaction['before_fingerprints'][name], transaction['after'][name]):
                    raise RuntimeError(f'Intervening edit at {name}; refusing to continue')
            if actual == transaction['after']:
                return {'action': 'apply', 'changed': cutover, 'target_version': self.version,
                        'config': 'valid and user-managed',
                        'restart_required': self.socket_identity() == transaction['socket_before']}
        else:
            if self.installed.exists():
                accepted = self.load_installed()
                self.assert_matches(accepted['managed'], 'Previously accepted Codex runtime')
                if accepted['version'] == self.version and self.current_fingerprints() == desired_fingerprints:
                    return {'action': 'apply', 'changed': False, 'target_version': self.version,
                            'config': 'valid and user-managed',
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
                'config': 'valid and user-managed',
                'restart_required': self.socket_identity() == transaction['socket_before']}

    def run_remote_check(self, name, arguments=()):
        checker = self.path(f'/home/agent/.local/libexec/remote-codex-checks/{name}')
        if not checker.is_file():
            raise RuntimeError('Deploy Remote Codex connection checks before changing or accepting the runtime')
        argv = ['/home/agent/.local/share/mise/shims/uv', 'run', '--no-project', str(checker), *arguments]
        if os.geteuid() != self.uid:
            argv = ['/usr/sbin/runuser', '-u', 'agent', '--', *argv]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
            report = json.loads(result.stdout)
            accepted = result.returncode == 0 and isinstance(report, dict) and report.get('ok') is True
        except (OSError, ValueError, subprocess.SubprocessError):
            accepted = False
        if not accepted:
            # Never echo raw helper stdout/stderr, credentials or RPC responses.
            raise RuntimeError('Remote Codex acceptance failed; keep recovery state and inspect the connection checks')

    def check_remote_candidate(self):
        self.run_remote_check('candidate-check.py', ['--codex-binary', str(self.binary())])

    def check_remote_connection(self):
        self.run_remote_check('connection-check.py')

    def check_running_version(self):
        result = subprocess.run(['systemctl', 'show', 'codex-app-server.service', '-p', 'MainPID', '--value'],
                                capture_output=True, text=True, timeout=10)
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            raise RuntimeError('Cannot identify the running Codex app server') from None
        if result.returncode or pid <= 0 or Path(f'/proc/{pid}/exe').resolve() != self.binary().resolve():
            raise RuntimeError('Running Codex app server does not use the requested pinned version')

    def verify(self):
        config = self.validate_config()
        source = self.load_pending() if self.transaction.exists() else self.load_installed()
        managed_version = source['target_version'] if 'target_version' in source else source['version']
        if managed_version != self.version:
            raise RuntimeError('Requested version differs from managed runtime state')
        expected = source['after'] if 'after' in source else source['managed']
        self.assert_matches(expected, 'Managed Codex runtime')
        self.validate_binary()
        self.validate_catalog(config)
        if self.transaction.exists() and self.socket_identity() == source['socket_before']:
            raise RuntimeError('Desktop has not restarted the systemd-owned app server')
        self.check_running_version()
        self.check_remote_connection()
        return {'action': 'verify', 'version': self.version, 'files': 'accepted',
                'config': 'valid and user-managed', 'socket_replaced': True, 'remote_connection': True}

    def rollback(self):
        if not self.transaction.exists():
            return {'action': 'rollback', 'changed': False, 'message': 'no pending transaction'}
        transaction, _ = self.cutover_pending(write=True)
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
        self.verify()
        transaction = self.load_pending()
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
