"""Read-only Ansible integration; opt in with ANSIBLE_TEST_BINARY."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import runtime_rollout as runtime

SOURCE = Path(__file__).resolve().parent.parent
ANSIBLE = os.environ.get('ANSIBLE_TEST_BINARY')


@unittest.skipUnless(ANSIBLE, 'set ANSIBLE_TEST_BINARY for read-only Ansible integration')
class AnsibleContracts(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_play(self, play, limit=None):
        path = self.directory / 'play.yml'
        path.write_text(yaml.safe_dump([play], sort_keys=False))
        argv = [ANSIBLE, '-i', str(SOURCE / 'deploy/inventory.yml'), str(path), '--check']
        if limit is not None:
            argv += ['--limit', limit]
        return subprocess.run(argv, text=True, capture_output=True, timeout=30,
            env=os.environ | {'ANSIBLE_LOCAL_TEMP': str(self.directory / 'local'),
                              'ANSIBLE_REMOTE_TEMP': str(self.directory / 'remote')})

    def test_guard_rejects_wrong_identity_and_unsupported_workflows(self):
        facts = {'hostname': 'love', 'user_id': 'agent',
                 'user_dir': runtime.inventory(SOURCE, 'love')['litellm_home']}
        for name, changes, supported, limit, success in [
            ('valid', {}, ['grace', 'love'], 'love', True),
            ('wrong-account', {'user_id': 'root'}, ['grace', 'love'], 'love', False),
            ('wrong-home', {'user_dir': '/home/agent'}, ['grace', 'love'], 'love', False),
            ('unsupported', {}, ['grace'], 'love', False),
            ('missing-selector', {}, ['grace', 'love'], None, False),
            ('multiple-selector', {}, ['grace', 'love'], 'grace,love', False),
        ]:
            with self.subTest(name=name):
                play = {'hosts': 'all', 'gather_facts': False,
                    'vars': {'ansible_facts': facts | changes, 'supported_hosts': supported,
                             'codex_mise': '/usr/bin/true'},
                    'tasks': [{'ansible.builtin.import_tasks': str(SOURCE / 'deploy/tasks/target.yml')}]}
                result = self.run_play(play, limit)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertIn('changed=0', result.stdout)

    def test_recovery_candidates_match_ansibles_actual_templates_on_both_hosts(self):
        templates = {'litellm.service.j2': '/etc/systemd/system/litellm.service',
            'compaction-recovery.conf.j2': '/etc/systemd/system/litellm.service.d/30-compaction-recovery.conf',
            'bedrock.conf.j2': '/etc/systemd/system/litellm.service.d/40-bedrock.conf',
            'vertex.conf.j2': '/etc/systemd/system/litellm.service.d/50-vertex.conf'}
        for host in ('grace', 'love'):
            with self.subTest(host=host):
                variables = runtime.inventory(SOURCE, host)
                home = Path(variables['litellm_home'])
                uv = '/selected/managed/uv'
                templates['deployment.json.j2'] = str(home / '.config/litellm/deployment.json')
                paths = {name: self.directory / (host + '-' + name) for name in templates}
                tasks = [{'ansible.builtin.template': {'src': str(SOURCE / 'deploy/templates' / name),
                          'dest': str(path)}, 'check_mode': False} for name, path in paths.items()]
                result = self.run_play({'hosts': 'all', 'gather_facts': False,
                    'vars': {'litellm_uv': uv, 'litellm_codex': str(home /
                        f".local/share/mise/installs/codex/{variables['codex_version']}/bin/codex")},
                    'tasks': tasks}, host)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                with patch.object(runtime, 'HOST', host), patch.object(runtime, 'HOME', home), \
                        patch.object(runtime, 'ROOT', Path(variables['litellm_root'])), patch.object(runtime, 'UV', uv):
                    candidates = runtime.candidates(SOURCE)
                for name, target in templates.items():
                    self.assertEqual(paths[name].read_text(), candidates[target]['text'], name)


if __name__ == '__main__':
    unittest.main()
