"""Launcher isolation and conflict-safe migration regression checks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import tomlkit
from fast_toggle import migrate, run


ORIGINAL = '''model_provider = "litellm"
model = "chatgpt/gpt-6-astra"
service_tier = "default"
model_catalog_json = "/unchanged/catalog.json"
[model_providers.litellm]
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
[model_providers.litellm.auth]
command = "/usr/bin/systemd-creds"
[plugins.example]
enabled = true
'''


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.home / '.codex/config.toml'
        self.config.parent.mkdir()
        self.config.write_text(ORIGINAL)
        self.launcher = self.home / '.local/bin/codex'
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to('/original/codex')
        target = self.home / '.local/share/litellm/scripts/codex-launcher'
        target.parent.mkdir(parents=True)
        target.write_text('fixture')
        self.state = self.home / '.local/state/litellm/fast-toggle.json'

    def test_preservation_idempotence_and_rollback(self):
        run('prepare', self.home)
        first = self.config.read_text()
        doc = tomlkit.parse(first)
        self.assertTrue(doc['model_providers']['litellm']['requires_openai_auth'])
        self.assertNotIn('auth', doc['model_providers']['litellm'])
        self.assertEqual(doc['model_providers']['litellm']['env_key'], 'LITELLM_PROXY_KEY')
        self.assertEqual(doc['service_tier'], 'default')
        self.assertFalse(doc['features']['shell_snapshot'])
        self.assertTrue(doc['plugins']['example']['enabled'])
        self.assertEqual(doc['model_catalog_json'], '/unchanged/catalog.json')
        self.assertIn('LITELLM_PROXY_KEY', doc['shell_environment_policy']['exclude'])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        run('prepare', self.home)
        self.assertEqual(self.config.read_text(), first)
        run('rollback', self.home)
        self.assertEqual(self.config.read_text(), ORIGINAL)
        self.assertEqual(os.readlink(self.launcher), '/original/codex')
        self.assertFalse(self.state.exists())

    def test_refuses_intervening_config_and_launcher_changes(self):
        run('prepare', self.home)
        prepared = self.config.read_text()
        self.config.write_text(prepared + '\n# new edit\n')
        with self.assertRaisesRegex(RuntimeError, 'intervening'):
            run('rollback', self.home)
        self.config.write_text(prepared)
        self.launcher.unlink()
        self.launcher.symlink_to('/new/codex')
        with self.assertRaisesRegex(RuntimeError, 'intervening'):
            run('rollback', self.home)
        self.assertEqual(os.readlink(self.launcher), '/new/codex')

    def test_recovers_interrupted_prepare_and_finish(self):
        run('prepare', self.home)
        self.config.write_text(ORIGINAL)
        run('prepare', self.home)
        run('finish', self.home)
        self.assertFalse(self.state.exists())
        run('prepare', self.home)
        self.assertFalse(self.state.exists())

    def test_preserves_existing_filter_forms(self):
        for policy in ('exclude = ["OTHER_SECRET"]', 'filters = { OTHER_SECRET = "exclude" }'):
            doc = tomlkit.parse(migrate(ORIGINAL + '\n[shell_environment_policy]\n' + policy))
            result = doc['shell_environment_policy']
            self.assertIn('OTHER_SECRET', result.get('exclude', result.get('filters')))
            self.assertFalse('exclude' in result and 'filters' in result)
        with self.assertRaisesRegex(RuntimeError, 'explicit'):
            migrate(ORIGINAL + '\n[shell_environment_policy.set]\nLITELLM_PROXY_KEY = "bad"')

    def test_refuses_intervening_launcher_content_changes(self):
        run('prepare', self.home)
        self.launcher.write_text('intervening edit')
        with self.assertRaisesRegex(RuntimeError, 'intervening'):
            run('rollback', self.home)
        self.assertTrue(self.state.exists())


class LauncherTests(unittest.TestCase):
    def test_arguments_and_fail_closed_without_secret_output(self):
        source = Path(__file__).with_name('codex-launcher').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creds, codex, launcher = [root / name for name in ('creds', 'codex', 'launcher')]
            codex.write_text('#!/bin/bash\n[[ "$LITELLM_PROXY_KEY" == "test-private-value" ]] || exit 9\nprintf "%s\\0" "$@"\n')
            codex.chmod(0o700)
            launcher.write_text(source.replace('/usr/bin/systemd-creds', str(creds)).replace(
                '/home/agent/.local/share/mise/installs/codex/latest/bin/codex', str(codex)))
            launcher.chmod(0o700)
            for body, success in [('printf test-private-value', True), ('exit 2', False), ('printf "bad value"', False), ('true', False)]:
                creds.write_text('#!/bin/bash\n' + body + '\n')
                creds.chmod(0o700)
                result = subprocess.run([str(launcher), 'app-server', 'with spaces', '', '$literal'], capture_output=True)
                self.assertEqual(result.returncode == 0, success)
                self.assertNotIn(b'test-private-value', result.stdout + result.stderr)
                if success:
                    self.assertEqual(result.stdout, b'app-server\0with spaces\0\0$literal\0')
                else:
                    self.assertIn(b'Codex:', result.stderr)
                    self.assertEqual(result.stdout, b'')


if __name__ == '__main__':
    unittest.main()
