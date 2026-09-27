"""Regression checks for the transactional Grace Codex runtime deployment."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import tomlkit

from codex_runtime import MANAGED_PATHS, Runtime, migrate_config, render_launcher


CONFIG = '''model_provider = "litellm"
model = "chatgpt/gpt-6-astra"
service_tier = "default"
model_catalog_json = "/home/agent/.config/litellm/codex-models.json"
[model_providers.litellm]
name = "LiteLLM"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
[model_providers.litellm.auth]
command = "/usr/bin/systemd-creds"
[plugins.example]
enabled = true
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(prefix='codex-runtime-test-'))
        self.root = Path(self.directory)
        self.version = '0.155.1'
        self.write('/home/agent/.codex/config.toml', CONFIG, 0o600)
        self.write('/home/agent/.config/litellm/codex-models.json', json.dumps({'models': [
            {'slug': 'chatgpt/gpt-6-astra', 'visibility': 'list'}]}), 0o600)
        self.write('/home/agent/.local/share/litellm/scripts/codex-launcher', '# old launcher\n', 0o700)
        link = self.path('/home/agent/.local/bin/codex')
        link.parent.mkdir(parents=True)
        link.symlink_to('/old/codex')
        self.write('/etc/systemd/system/codex-app-server.service', '# old service\n', 0o644)
        self.write('/etc/profile.d/codex-app-server.sh', '# old profile\n', 0o644)
        self.write('/etc/systemd/system/codex-app-server.service.d/20-litellm-launcher.conf',
                   '# emergency override\n', 0o644)
        self.install_binary(self.version)
        self.runtime = Runtime(self.version, self.root)

    def path(self, path):
        return self.root / path.removeprefix('/')

    def write(self, path, content, mode):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(mode)

    def install_binary(self, version):
        script = f'''#!/bin/bash
if [[ "$1" == "--version" ]]; then printf 'codex-cli {version}\\n'; exit 0; fi
if [[ "$1" == "debug" && "$2" == "models" ]]; then
  printf '%s\\n' '{{"models":[{{"slug":"chatgpt/gpt-6-astra","visibility":"list"}}]}}'
  exit 0
fi
exit 2
'''
        self.write(f'/home/agent/.local/share/mise/installs/codex/{version}/bin/codex', script, 0o700)

    def test_initial_adoption_preserves_config_and_removes_emergency_override(self):
        before = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        result = self.runtime.apply()
        self.assertTrue(result['changed'])
        self.assertFalse(self.path('/etc/systemd/system/codex-app-server.service.d/20-litellm-launcher.conf').exists())
        config = tomlkit.parse(self.path('/home/agent/.codex/config.toml').read_text())
        self.assertTrue(config['model_providers']['litellm']['requires_openai_auth'])
        self.assertEqual(config['model_providers']['litellm']['env_key'], 'LITELLM_PROXY_KEY')
        self.assertNotIn('auth', config['model_providers']['litellm'])
        self.assertEqual(config['service_tier'], 'default')
        self.assertFalse(config['features']['shell_snapshot'])
        self.assertTrue(config['plugins']['example']['enabled'])
        self.assertIn('LITELLM_PROXY_KEY', config['shell_environment_policy']['exclude'])
        self.assertIn(f'/codex/{self.version}/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())
        transaction = json.loads(self.runtime.transaction.read_text())
        self.assertEqual(transaction['before'], before)
        self.assertEqual(self.runtime.transaction.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.runtime.summary()['restart_required'])
        self.assertFalse(self.runtime.apply()['changed'])

    def test_rollback_restores_binary_selection_config_unit_and_override_together(self):
        originals = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        self.runtime.apply()
        result = self.runtime.rollback()
        self.assertTrue(result['changed'])
        self.assertEqual({name: self.runtime.snapshot(name) for name in MANAGED_PATHS}, originals)
        self.assertFalse(self.runtime.transaction.exists())

    def test_refuses_intervening_edits_and_recovers_interrupted_apply(self):
        self.runtime.apply()
        prepared = self.path('/etc/systemd/system/codex-app-server.service').read_text()
        self.path('/etc/systemd/system/codex-app-server.service').write_text(prepared + '# edit\n')
        with self.assertRaisesRegex(RuntimeError, 'Intervening edit'):
            self.runtime.apply()
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            self.runtime.rollback()
        self.path('/etc/systemd/system/codex-app-server.service').write_text('# old service\n')
        self.assertTrue(self.runtime.apply()['changed'])
        self.assertEqual(self.path('/etc/systemd/system/codex-app-server.service').read_text(), prepared)

    def test_finish_tracks_revision_and_allows_explicit_version_upgrade(self):
        self.runtime.apply()
        self.runtime.finish()
        accepted = json.loads(self.runtime.installed.read_text())
        self.assertEqual(accepted['version'], self.version)
        self.assertFalse(self.runtime.apply()['changed'])
        self.install_binary('0.156.0')
        upgraded = Runtime('0.156.0', self.root)
        self.assertTrue(upgraded.apply()['changed'])
        self.assertIn('/codex/0.156.0/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())
        upgraded.rollback()
        self.assertIn(f'/codex/{self.version}/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())

    def test_accepted_revision_refuses_drift_before_upgrade(self):
        self.runtime.apply()
        self.runtime.finish()
        self.path('/etc/profile.d/codex-app-server.sh').write_text('# drift\n')
        self.install_binary('0.156.0')
        with self.assertRaisesRegex(RuntimeError, 'Previously accepted'):
            Runtime('0.156.0', self.root).apply()

    def test_config_migration_supports_filters_and_rejects_explicit_secret_set(self):
        filtered = CONFIG + '\n[shell_environment_policy.filters]\nOTHER_SECRET = "exclude"\n'
        result = tomlkit.parse(migrate_config(filtered))
        self.assertEqual(result['shell_environment_policy']['filters']['LITELLM_PROXY_KEY'], 'exclude')
        self.assertIn('OTHER_SECRET', result['shell_environment_policy']['filters'])
        with self.assertRaisesRegex(RuntimeError, 'explicit'):
            migrate_config(CONFIG + '\n[shell_environment_policy.set]\nLITELLM_PROXY_KEY = "bad"\n')

    def test_requires_an_exact_release_version_and_installed_binary(self):
        with self.assertRaisesRegex(RuntimeError, 'exact release'):
            Runtime('latest', self.root)
        missing = Runtime('0.999.0', self.root)
        with self.assertRaisesRegex(RuntimeError, 'not installed'):
            missing.apply()


class LauncherTests(unittest.TestCase):
    def test_arguments_and_credential_failures_without_secret_output(self):
        with tempfile.TemporaryDirectory(prefix='codex-launcher-test-') as directory:
            root = Path(directory)
            decrypt, binary, launcher = [root / name for name in ('decrypt', 'codex', 'launcher')]
            binary.write_text('#!/bin/bash\n[[ "$LITELLM_PROXY_KEY" == "private-value" ]] || exit 9\nprintf "%s\\0" "$@"\n')
            binary.chmod(0o700)
            launcher.write_text(render_launcher('test', credential='/unused', binary=str(binary), decrypt=str(decrypt)))
            launcher.chmod(0o700)
            for body, success in [('printf private-value', True), ('exit 2', False),
                                  ('printf "bad value"', False), ('true', False)]:
                decrypt.write_text('#!/bin/bash\n' + body + '\n')
                decrypt.chmod(0o700)
                result = subprocess.run([launcher, 'app-server', 'with spaces', '$literal'], capture_output=True)
                self.assertEqual(result.returncode == 0, success)
                self.assertNotIn(b'private-value', result.stdout + result.stderr)
                if success:
                    self.assertEqual(result.stdout, b'app-server\0with spaces\0$literal\0')
                else:
                    self.assertEqual(result.stdout, b'')
                    self.assertIn(b'Codex:', result.stderr)


if __name__ == '__main__':
    unittest.main()
