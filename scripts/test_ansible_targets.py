"""Read-only Ansible integration; opt in with ANSIBLE_TEST_BINARY."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import deploy_config
import runtime_rollout as runtime

SOURCE = Path(__file__).resolve().parent.parent
ANSIBLE = os.environ.get('ANSIBLE_TEST_BINARY')
HOME = '/mnt/volume/example/home'


@unittest.skipUnless(ANSIBLE, 'set ANSIBLE_TEST_BINARY for read-only Ansible integration')
class AnsibleContracts(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.example = yaml.safe_load((SOURCE / 'deploy/config.example.yml').read_text())
        self.example |= {'user': 'example', 'home': HOME}

    def write_config(self, value):
        path = self.directory / 'config.yml'
        path.write_text(yaml.safe_dump(value, sort_keys=False))
        return path

    def run_play(self, play, config):
        path = self.directory / 'play.yml'
        path.write_text(yaml.safe_dump([play], sort_keys=False))
        argv = [ANSIBLE, '-i', str(SOURCE / 'deploy/inventory.yml'), str(path), '--check',
                '-e', 'config=' + str(config)]
        return subprocess.run(argv, text=True, capture_output=True, timeout=60,
            env=os.environ | {'ANSIBLE_LOCAL_TEMP': str(self.directory / 'local'),
                              'ANSIBLE_REMOTE_TEMP': str(self.directory / 'remote')})

    def test_guard_requires_a_documented_configuration_for_the_running_account(self):
        facts = {'user_id': 'example', 'user_dir': HOME}
        for name, config, fact_changes, success in [
            ('valid', {}, {}, True),
            ('unknown-key', {'modal': True}, {}, False),
            ('wrong-account', {}, {'user_id': 'root'}, False),
            ('wrong-home', {}, {'user_dir': '/home/example'}, False),
        ]:
            with self.subTest(name=name):
                path = self.write_config(self.example | {'mise': '/usr/bin/true'} | config)
                play = {'hosts': 'all', 'gather_facts': False,
                    'vars': {'ansible_facts': facts | fact_changes},
                    'tasks': [{'ansible.builtin.import_tasks': str(SOURCE / 'deploy/tasks/target.yml')}]}
                result = self.run_play(play, path)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertIn('changed=0', result.stdout)

    def test_recovery_candidates_match_ansibles_actual_templates(self):
        config = deploy_config.validate(self.example)
        uv = '/selected/managed/uv'
        variables = deploy_config.variables(config) | {'litellm_uv': uv}
        home = Path(HOME)
        templates = {'litellm.service.j2': runtime.UNIT,
                     'deployment.json.j2': str(home / '.config/litellm/deployment.json'),
                     'credentials.json.j2': str(home / '.config/litellm/credentials.json')}
        paths = {name: self.directory / name for name in templates}
        tasks = [{'ansible.builtin.template': {'src': str(SOURCE / 'deploy/templates' / name),
                  'dest': str(path)}, 'check_mode': False} for name, path in paths.items()]
        result = self.run_play({'hosts': 'all', 'gather_facts': False, 'vars': variables, 'tasks': tasks},
                               self.write_config(self.example))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with patch.object(runtime, 'CONFIG', config), patch.object(runtime, 'HOME', home), \
                patch.object(runtime, 'ROOT', Path(variables['litellm_root'])), patch.object(runtime, 'UV', uv):
            candidates = runtime.candidates(SOURCE)
        for name, target in templates.items():
            self.assertEqual(paths[name].read_text(), candidates[target]['text'], name)

    def test_recovery_journal_matches_deploys_cancel_on_disconnect_edit(self):
        deploy = yaml.safe_load((SOURCE / 'deploy/deploy.yml').read_text())
        task = next(task for play in deploy for task in play.get('tasks', [])
                    if 'cancel_on_disconnect' in task.get('ansible.builtin.lineinfile', {}).get('line', ''))
        samples = {'missing': 'model_list: []\ngeneral_settings:\n  master_key: k\nlitellm_settings: {}\n',
                   'present': 'general_settings:\n  cancel_on_disconnect: true\n  master_key: k\n',
                   'disabled': 'general_settings:\n  master_key: k\n  cancel_on_disconnect: false\n'}
        tasks = []
        for name, text in samples.items():
            (self.directory / name).write_text(text)
            tasks.append({'ansible.builtin.lineinfile': task['ansible.builtin.lineinfile'] | {
                'path': str(self.directory / name)}, 'check_mode': False})
        result = self.run_play({'hosts': 'all', 'gather_facts': False, 'tasks': tasks},
                               self.write_config(self.example))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, text in samples.items():
            self.assertEqual((self.directory / name).read_text(), runtime.with_cancel_on_disconnect(text), name)


if __name__ == '__main__':
    unittest.main()
