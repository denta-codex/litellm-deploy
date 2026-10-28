"""Exercise host isolation, Love's credential path, and portable runtime recovery."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deployment
from codex_runtime import Runtime, render_launcher, render_service
from runtime_rollout import inventory


SOURCE = Path(__file__).resolve().parent.parent
LOVE_HOME = Path('/mnt/HC_Volume_103914271/agent/home')


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.home / '.config/litellm/deployment.json'
        self.config.parent.mkdir(parents=True)

    def configure(self, mode):
        self.config.write_text(json.dumps({'home': str(self.home), 'credential_mode': mode,
            'system_credential': '/etc/credstore.encrypted/litellm-proxy-key', 'codex': '/managed/codex'}))

    def test_user_credentials_preserve_graces_unprivileged_decryption(self):
        self.configure('user')
        with patch('deployment.subprocess.check_output', return_value='test-key\n') as run:
            self.assertEqual(deployment.proxy_key(home=self.home), 'test-key')
        self.assertEqual(run.call_args.args[0][:3], ['/usr/bin/systemd-creds', 'decrypt', '--user'])
        self.assertIn(str(self.home / '.config/litellm/proxy-key.cred'), run.call_args.args[0])

    def test_love_uses_system_encryption_without_user_option(self):
        self.configure('system')
        for uid, prefix in [(2002, ['sudo', '-n']), (0, [])]:
            with self.subTest(uid=uid), patch('deployment.os.geteuid', return_value=uid), \
                    patch('deployment.subprocess.check_output', return_value='test-key\n') as run:
                self.assertEqual(deployment.proxy_key(home=self.home), 'test-key')
                self.assertEqual(run.call_args.args[0], prefix + ['/usr/bin/systemd-creds', 'decrypt',
                    '--name=litellm-proxy-key', '/etc/credstore.encrypted/litellm-proxy-key', '-'])

    def test_invalid_keys_and_failures_never_expose_output(self):
        self.configure('system')
        for value in ('', 'bad key', 'bad\nkey'):
            with self.subTest(value=value), patch('deployment.subprocess.check_output', return_value=value):
                with self.assertRaises(RuntimeError):
                    deployment.proxy_key(home=self.home)
        failure = subprocess.CalledProcessError(1, ['decrypt'], output='private-value')
        with patch('deployment.subprocess.check_output', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                deployment.proxy_key(home=self.home)
            self.assertNotIn('private-value', str(caught.exception))

    def test_context_must_match_the_callers_home(self):
        self.configure('system')
        value = json.loads(self.config.read_text())
        value['home'] = '/another/account'
        self.config.write_text(json.dumps(value))
        with patch('deployment.subprocess.check_output') as run, self.assertRaises(ValueError):
            deployment.proxy_key(home=self.home)
        run.assert_not_called()


class LoveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = Runtime('0.159.2', self.root, LOVE_HOME, 'system', False)
        for method in ('check_remote_candidate', 'check_remote_connection', 'check_running_version', 'validate_catalog'):
            self.enterContext(patch.object(self.runtime, method))
        # Reuse the existing contract; only the host paths differ.
        from test_codex_runtime import CONFIG
        self.write(LOVE_HOME / '.codex/config.toml', CONFIG.replace('/home/agent', str(LOVE_HOME)))
        self.write('/etc/systemd/system/codex-app-server.service', '# existing love service\n', 0o644)
        self.write(LOVE_HOME / '.local/share/mise/installs/codex/0.159.2/bin/codex',
                   '#!/bin/sh\nprintf "codex-cli 0.159.2\\n"\n', 0o700)

    def write(self, name, value, mode=0o600):
        path = self.runtime.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(mode)

    def test_love_adoption_and_rollback_preserve_config_and_prior_service(self):
        config = self.runtime.path(LOVE_HOME / '.codex/config.toml')
        original = config.read_bytes()
        before = self.runtime.current_fingerprints()
        self.runtime.apply()
        desired = self.runtime.desired()
        service = base64.b64decode(desired['/etc/systemd/system/codex-app-server.service']['content']).decode()
        self.assertIn(f'RequiresMountsFor={LOVE_HOME}', service)
        self.assertIn('LoadCredentialEncrypted=litellm-proxy-key:', service)
        self.assertNotIn('/home/agent', service)
        self.assertNotIn('SSH_AUTH_SOCK', service)
        self.assertEqual(config.read_bytes(), original)
        self.runtime.rollback()
        self.assertEqual(self.runtime.current_fingerprints(), before)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse(self.runtime.transaction.exists())

    def test_service_credential_launcher_needs_no_privileged_child(self):
        credentials = self.root / 'credentials'
        credentials.mkdir()
        (credentials / 'litellm-proxy-key').write_text('private-value\n')
        binary = self.root / 'codex with spaces'
        binary.write_text('#!/bin/bash\n[[ "$LITELLM_PROXY_KEY" == private-value ]] || exit 9\nprintf "%s\\0" "$@"\n')
        binary.chmod(0o700)
        launcher = self.root / 'launcher'
        launcher.write_text(render_launcher('0.159.2', binary=str(binary), home=LOVE_HOME, credential_mode='system'))
        launcher.chmod(0o700)
        env = os.environ | {'CREDENTIALS_DIRECTORY': str(credentials)}
        result = subprocess.run([launcher, 'app-server', 'with spaces', '$literal'], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'app-server\0with spaces\0$literal\0')
        self.assertNotIn(b'private-value', result.stdout + result.stderr)
        (credentials / 'litellm-proxy-key').write_text('bad key')
        failed = subprocess.run([launcher], env=env, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, b'')
        self.assertNotIn(b'bad key', failed.stderr)

    def test_grace_retains_its_current_service_auth_and_forwarding(self):
        service = render_service()
        self.assertIn('Environment=SSH_AUTH_SOCK=/home/agent/', service)
        self.assertNotIn('LoadCredentialEncrypted=', service)


class HostInventoryTests(unittest.TestCase):
    def test_love_enables_basic_gateway_and_cloud_providers(self):
        love, grace = inventory(SOURCE, 'love'), inventory(SOURCE, 'grace')
        self.assertEqual(love['litellm_home'], str(LOVE_HOME))
        for feature in ('modal', 'voice', 'claude'):
            self.assertFalse(love['litellm_' + feature + '_enabled'])
            self.assertTrue(grace['litellm_' + feature + '_enabled'])
        for feature in ('bedrock', 'vertex'):
            self.assertTrue(love['litellm_' + feature + '_enabled'])
            self.assertEqual(love['litellm_' + feature + '_credential'],
                             grace['litellm_' + feature + '_credential'])
        self.assertEqual(love['litellm_credential_mode'], 'system')
        self.assertEqual(grace['litellm_credential_mode'], 'user')


if __name__ == '__main__':
    unittest.main()
