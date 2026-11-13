"""The per-machine configuration contract shared by the scripts and Ansible."""
import ast
from pathlib import Path
import re
import tempfile
import unittest

import yaml

import deploy_config

SOURCE = Path(__file__).resolve().parent.parent
EXAMPLE = SOURCE / 'deploy/config.example.yml'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.example = yaml.safe_load(EXAMPLE.read_text())

    def test_example_loads_with_documented_defaults(self):
        config = deploy_config.load(EXAMPLE)
        self.assertEqual(config['credential_mode'], 'user')
        self.assertEqual(deploy_config.credential_exports(config)['litellm-proxy-key'],
                         {'env': 'LITELLM_MASTER_KEY'})
        minimal = {key: self.example[key] for key in deploy_config.REQUIRED}
        self.assertEqual({key: deploy_config.validate(minimal)[key] for key in deploy_config.OPTIONAL},
                         {key: default for key, (_, default) in deploy_config.OPTIONAL.items()})

    def test_missing_file_points_to_the_example(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError) as caught:
            deploy_config.load(Path(directory) / 'config.yml')
        self.assertIn('config.example.yml', str(caught.exception))

    def test_invalid_configurations_are_rejected(self):
        credential = self.example['credentials'][0]
        for name, changes in [
            ('unknown-key', {'modal': True}),
            ('missing-required', {'user': ''}),
            ('bad-mode', {'credential_mode': 'root'}),
            ('relative-home', {'home': 'home/example'}),
            ('browser-without-node', {'browser': True}),
            ('browser-relative-node', {'browser': {'node': 'bin/node'}}),
            ('browser-relative-executable', {'browser': {'node': '/usr/bin/node', 'executable': 'chrome'}}),
            ('browser-unknown-key', {'browser': {'node': '/usr/bin/node', 'channel': 'chrome'}}),
            ('wrong-type', {'voice': 'yes'}),
            ('duplicate-credential', {'credentials': [credential, credential]}),
            ('proxy-key-listed', {'credentials': [credential | {'name': 'litellm-proxy-key'}]}),
            ('relative-credential', {'credentials': [credential | {'path': 'credstore/x'}]}),
            ('extra-credential-field', {'credentials': [credential | {'mode': 'system'}]}),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                deploy_config.validate(self.example | changes)

    def test_browser_executable_defaults_to_chromium(self):
        default = deploy_config.validate(self.example | {'browser': {'node': '/usr/bin/node'}})
        self.assertEqual(default['browser'], {'node': '/usr/bin/node', 'executable': '/usr/bin/chromium'})
        chrome = {'node': '/usr/bin/node', 'executable': '/opt/google/chrome/chrome'}
        self.assertEqual(deploy_config.validate(self.example | {'browser': chrome})['browser'], chrome)

    def test_ansible_maps_the_same_keys(self):
        tasks = yaml.safe_load((SOURCE / 'deploy/tasks/target.yml').read_text())
        facts = set()
        for task in tasks:
            facts.update(task.get('ansible.builtin.set_fact', {}))
        facts.discard('deploy_config_path')
        facts.discard('litellm_uv')
        variables = deploy_config.variables(deploy_config.load(EXAMPLE))
        self.assertEqual(set(variables), facts)
        guard = next(task for task in tasks if 'ansible.builtin.assert' in task)
        allowed = re.search(r'difference\((\[[^]]*\])\)', ' '.join(guard['ansible.builtin.assert']['that']))
        self.assertEqual(set(ast.literal_eval(allowed.group(1))),
                         set(deploy_config.REQUIRED) | set(deploy_config.OPTIONAL))


if __name__ == '__main__':
    unittest.main()
