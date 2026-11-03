"""Credential exports declared in credentials.json, without exposing their values."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import launch

SCRIPT = Path(__file__).with_name('launch.py')
KEY = {'litellm-proxy-key': {'env': 'LITELLM_MASTER_KEY'}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.write('litellm-proxy-key', 'sk-test\n')

    def write(self, name, text):
        (self.directory / name).write_text(text)

    def test_each_export_kind(self):
        self.write('modal', 'WK_SECRET=wk-a\nWS_SECRET=ws-b\n')
        self.write('aws', json.dumps({'access_key_id': 'id', 'secret_access_key': 'secret', 'region': 'r'}))
        self.write('vertex', '{}')
        specs = KEY | {'modal': {'modal_token': 'MODAL_API_KEY'},
            'aws': {'json': {'access_key_id': 'AWS_ACCESS_KEY_ID', 'secret_access_key': 'AWS_SECRET_ACCESS_KEY',
                             'region': 'AWS_REGION_NAME'}},
            'vertex': {'path_env': ['VERTEX_CREDENTIALS_PATH', 'GOOGLE_APPLICATION_CREDENTIALS']}}
        self.assertEqual(launch.exports(specs, self.directory), {
            'LITELLM_MASTER_KEY': 'sk-test', 'MODAL_API_KEY': 'wk-a.ws-b', 'AWS_ACCESS_KEY_ID': 'id',
            'AWS_SECRET_ACCESS_KEY': 'secret', 'AWS_REGION_NAME': 'r',
            'VERTEX_CREDENTIALS_PATH': str(self.directory / 'vertex'),
            'GOOGLE_APPLICATION_CREDENTIALS': str(self.directory / 'vertex')})

    def test_invalid_maps_are_rejected(self):
        self.write('other', 'token')
        self.write('doc', json.dumps({'field': ''}))
        for name, specs in [
            ('missing-master-key', {'other': {'env': 'OTHER'}}),
            ('duplicate-variable', KEY | {'other': {'env': 'LITELLM_MASTER_KEY'}}),
            ('unknown-kind', KEY | {'other': {'file': 'OTHER'}}),
            ('two-exports', KEY | {'other': {'env': 'A', 'path_env': ['B']}}),
            ('lowercase-variable', KEY | {'other': {'env': 'other'}}),
            ('empty-field', KEY | {'doc': {'json': {'field': 'FIELD'}}}),
            ('missing-file', KEY | {'absent': {'path_env': ['ABSENT']}}),
            ('bad-name', KEY | {'../other': {'env': 'OTHER'}}),
        ]:
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                launch.exports(specs, self.directory)

    def test_multi_token_text_is_rejected(self):
        self.write('litellm-proxy-key', 'two tokens')
        with self.assertRaises(ValueError):
            launch.exports(KEY, self.directory)

    def run_launcher(self, environment):
        config = self.directory / 'config.yaml'
        server = self.directory / 'serve.py'
        server.write_text('import json, os\nprint(json.dumps(dict(os.environ)))\n')
        script = self.directory / 'launch.py'
        script.write_text(SCRIPT.read_text())
        return subprocess.run([sys.executable, str(script), str(config)], capture_output=True, text=True,
            env={'PATH': os.environ['PATH'], 'CREDENTIALS_DIRECTORY': str(self.directory)} | environment)

    def test_ambient_cloud_credentials_are_replaced(self):
        self.write('credentials.json', json.dumps(KEY))
        result = self.run_launcher({'AWS_PROFILE': 'ambient', 'GOOGLE_APPLICATION_CREDENTIALS': '/ambient'})
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = json.loads(result.stdout)
        self.assertEqual(environment['LITELLM_MASTER_KEY'], 'sk-test')
        self.assertNotIn('AWS_PROFILE', environment)
        self.assertNotIn('GOOGLE_APPLICATION_CREDENTIALS', environment)
        self.assertEqual(environment['AWS_CONFIG_FILE'], '/dev/null')

    def test_failure_is_generic_and_never_prints_credentials(self):
        self.write('litellm-proxy-key', 'private value')
        self.write('credentials.json', json.dumps(KEY))
        result = self.run_launcher({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot load LiteLLM systemd credentials', result.stderr)
        self.assertNotIn('private', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
