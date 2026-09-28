"""Regression checks for the transactional Grace Codex runtime deployment."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_runtime import CONFIG_PATH, MANAGED_PATHS, Runtime, render_launcher


CONFIG = '''model_provider = "litellm"
model = "chatgpt/gpt-6-astra"
service_tier = "default"
model_catalog_json = "/home/agent/.config/litellm/codex-models.json"
[model_providers.litellm]
name = "LiteLLM"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
requires_openai_auth = true
env_key = "LITELLM_PROXY_KEY"
[features]
shell_snapshot = false
[shell_environment_policy]
exclude = ["LITELLM_PROXY_KEY"]
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

    def config_state(self):
        path = self.path(CONFIG_PATH)
        info = path.stat()
        return path.read_bytes(), info.st_mode, info.st_uid, info.st_gid, info.st_mtime_ns

    def stage_schema_one_transaction(self):
        paths = (CONFIG_PATH,) + MANAGED_PATHS
        before = {name: self.runtime.snapshot(name) for name in paths}
        desired = self.runtime.desired()
        for name in MANAGED_PATHS:
            self.runtime.install_entry(name, desired[name])
        after = {name: self.runtime.fingerprint(self.runtime.snapshot(name, include_content=False))
                 for name in paths}
        transaction = {
            'schema': 1, 'target_version': self.version, 'created_at': 1,
            'socket_before': self.runtime.socket_identity(), 'before': before,
            'before_fingerprints': {name: self.runtime.fingerprint(entry)
                                    for name, entry in before.items()},
            'after': after,
        }
        self.runtime.write_json(self.runtime.transaction, transaction)
        return before

    def test_initial_adoption_never_writes_config_and_removes_emergency_override(self):
        config_before = self.config_state()
        before = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        result = self.runtime.apply()
        self.assertTrue(result['changed'])
        self.assertFalse(self.path('/etc/systemd/system/codex-app-server.service.d/20-litellm-launcher.conf').exists())
        self.assertEqual(self.config_state(), config_before)
        self.assertIn(f'/codex/{self.version}/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())
        transaction = json.loads(self.runtime.transaction.read_text())
        self.assertEqual(transaction['before'], before)
        self.assertEqual(transaction['schema'], 2)
        self.assertNotIn(CONFIG_PATH, transaction['before'])
        self.assertNotIn(CONFIG_PATH, transaction['before_fingerprints'])
        self.assertNotIn(CONFIG_PATH, transaction['after'])
        self.assertEqual(self.runtime.transaction.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.runtime.summary()['restart_required'])
        self.assertFalse(self.runtime.apply()['changed'])
        self.assertEqual(self.config_state(), config_before)

    def test_rollback_restores_runtime_without_touching_config(self):
        originals = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        self.runtime.apply()
        edited = self.path(CONFIG_PATH).read_text() + '\n# Grace changed an unrelated preference.\n'
        self.path(CONFIG_PATH).write_text(edited)
        result = self.runtime.rollback()
        self.assertTrue(result['changed'])
        self.assertEqual({name: self.runtime.snapshot(name) for name in MANAGED_PATHS}, originals)
        self.assertEqual(self.path(CONFIG_PATH).read_text(), edited)
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
        config_before = self.config_state()
        self.runtime.apply()
        self.runtime.finish()
        accepted = json.loads(self.runtime.installed.read_text())
        self.assertEqual(accepted['version'], self.version)
        self.assertEqual(accepted['schema'], 2)
        self.assertNotIn(CONFIG_PATH, accepted['managed'])
        self.assertFalse(self.runtime.apply()['changed'])
        self.assertEqual(self.config_state(), config_before)
        self.install_binary('0.156.0')
        upgraded = Runtime('0.156.0', self.root)
        self.assertTrue(upgraded.apply()['changed'])
        self.assertIn('/codex/0.156.0/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())
        upgraded.rollback()
        self.assertIn(f'/codex/{self.version}/bin/codex',
                      self.path('/home/agent/.local/share/litellm/scripts/codex-launcher').read_text())
        self.assertEqual(self.config_state(), config_before)

    def test_accepted_revision_refuses_drift_before_upgrade(self):
        self.runtime.apply()
        self.runtime.finish()
        self.path('/etc/profile.d/codex-app-server.sh').write_text('# drift\n')
        self.install_binary('0.156.0')
        with self.assertRaisesRegex(RuntimeError, 'Previously accepted'):
            Runtime('0.156.0', self.root).apply()

    def test_compliant_user_edits_do_not_create_runtime_drift(self):
        self.runtime.apply()
        self.runtime.finish()
        edited = self.path(CONFIG_PATH).read_text() + '\n# Grace owns this file.\n[plugins.second]\nenabled = true\n'
        self.path(CONFIG_PATH).write_text(edited)
        self.assertFalse(self.runtime.apply()['changed'])
        self.assertEqual(self.path(CONFIG_PATH).read_text(), edited)

    def test_invalid_config_stops_before_runtime_changes_without_exposing_values(self):
        invalid = CONFIG.replace('requires_openai_auth = true', 'requires_openai_auth = false')
        invalid = invalid.replace('exclude = ["LITELLM_PROXY_KEY"]', 'exclude = []')
        invalid = invalid.replace('env_key = "LITELLM_PROXY_KEY"',
                                  'env_key = "WRONG"\nexperimental_bearer_token = "private-value"')
        self.path(CONFIG_PATH).write_text(invalid)
        before = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        with self.assertRaises(RuntimeError) as caught:
            self.runtime.apply()
        message = str(caught.exception)
        self.assertIn('requires_openai_auth = true', message)
        self.assertIn('env_key = "LITELLM_PROXY_KEY"', message)
        self.assertIn('experimental_bearer_token', message)
        self.assertIn('Exclude LITELLM_PROXY_KEY', message)
        self.assertNotIn('private-value', message)
        self.assertFalse(self.runtime.transaction.exists())
        self.assertEqual({name: self.runtime.snapshot(name) for name in MANAGED_PATHS}, before)

    def test_each_required_config_invariant_has_an_actionable_error(self):
        cases = [
            (CONFIG.replace('model_provider = "litellm"', 'model_provider = "openai"'),
             'Set model_provider = "litellm"'),
            (CONFIG.replace('base_url = "http://127.0.0.1:4000/v1"', 'base_url = "https://example.invalid"'),
             'Set model_providers.litellm.base_url'),
            (CONFIG.replace('wire_api = "responses"', 'wire_api = "chat"'),
             'Set model_providers.litellm.wire_api'),
            (CONFIG.replace('requires_openai_auth = true', 'requires_openai_auth = false'),
             'Set model_providers.litellm.requires_openai_auth = true'),
            (CONFIG.replace('env_key = "LITELLM_PROXY_KEY"', 'env_key = "OTHER_KEY"'),
             'Set model_providers.litellm.env_key = "LITELLM_PROXY_KEY"'),
            (CONFIG.replace('[features]',
                            '[model_providers.litellm.auth]\ncommand = "/bin/false"\n[features]'),
             'Remove the model_providers.litellm.auth table'),
            (CONFIG.replace('env_key = "LITELLM_PROXY_KEY"',
                            'env_key = "LITELLM_PROXY_KEY"\nexperimental_bearer_token = "hidden"'),
             'Remove model_providers.litellm.experimental_bearer_token'),
            (CONFIG.replace('service_tier = "default"', 'service_tier = "priority"'),
             'Set service_tier = "default" or remove it'),
            (CONFIG.replace('shell_snapshot = false', 'shell_snapshot = true'),
             'Set features.shell_snapshot = false'),
            (CONFIG.replace('exclude = ["LITELLM_PROXY_KEY"]', 'exclude = []'),
             'Exclude LITELLM_PROXY_KEY'),
            (CONFIG + '\n[shell_environment_policy.set]\nLITELLM_PROXY_KEY = "hidden"\n',
             'Remove LITELLM_PROXY_KEY from shell_environment_policy.set'),
        ]
        before = {name: self.runtime.snapshot(name) for name in MANAGED_PATHS}
        for content, expected in cases:
            with self.subTest(expected=expected):
                self.path(CONFIG_PATH).write_text(content)
                with self.assertRaisesRegex(RuntimeError, expected.replace('.', r'\.').replace('[', r'\[')):
                    self.runtime.apply()
                self.assertFalse(self.runtime.transaction.exists())
                self.assertEqual({name: self.runtime.snapshot(name) for name in MANAGED_PATHS}, before)

    def test_preview_verify_and_finish_leave_config_metadata_unchanged(self):
        config_before = self.config_state()
        self.runtime.summary()
        self.runtime.apply()
        socket = self.path('/home/agent/.codex/app-server-control/app-server-control.sock')
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.touch()
        self.runtime.verify()
        self.runtime.finish()
        self.assertEqual(self.config_state(), config_before)

    def test_config_permissions_and_owner_are_validated_without_repair(self):
        self.path(CONFIG_PATH).chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'chmod 600'):
            self.runtime.apply()
        self.assertEqual(self.path(CONFIG_PATH).stat().st_mode & 0o777, 0o644)
        self.path(CONFIG_PATH).chmod(0o600)
        self.runtime.uid += 1
        with self.assertRaisesRegex(RuntimeError, 'chown agent:agent'):
            self.runtime.validate_config()

    def test_exact_schema_one_transaction_is_cut_over_without_config_snapshot(self):
        config_before = self.config_state()
        self.stage_schema_one_transaction()
        preview = self.runtime.summary()
        self.assertTrue(preview['pending_config_release'])
        self.assertEqual(json.loads(self.runtime.transaction.read_text())['schema'], 1)
        result = self.runtime.apply()
        self.assertTrue(result['changed'])
        transaction = json.loads(self.runtime.transaction.read_text())
        self.assertEqual(transaction['schema'], 2)
        for section in ('before', 'before_fingerprints', 'after'):
            self.assertNotIn(CONFIG_PATH, transaction[section])
        self.assertEqual(self.config_state(), config_before)

    def test_schema_one_cutover_requires_fully_staged_non_config_files(self):
        self.stage_schema_one_transaction()
        self.path('/etc/systemd/system/codex-app-server.service').write_text('# drift\n')
        with self.assertRaisesRegex(RuntimeError, 'not fully staged'):
            self.runtime.apply()
        self.assertEqual(json.loads(self.runtime.transaction.read_text())['schema'], 1)

    def test_schema_one_rollback_preserves_user_config(self):
        originals = self.stage_schema_one_transaction()
        edited = self.path(CONFIG_PATH).read_text() + '\n# user edit after staging\n'
        self.path(CONFIG_PATH).write_text(edited)
        self.runtime.rollback()
        self.assertEqual(self.path(CONFIG_PATH).read_text(), edited)
        self.assertFalse(self.runtime.transaction.exists())
        for name in MANAGED_PATHS:
            self.assertEqual(self.runtime.snapshot(name), originals[name])

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
