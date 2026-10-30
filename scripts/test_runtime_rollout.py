"""Runtime recovery restores only managed changes and refuses concurrent edits."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import runtime_rollout as runtime


class RuntimeTemplates(unittest.TestCase):
    def test_current_service_templates_can_be_rendered(self):
        source=Path(__file__).resolve().parent.parent
        result=runtime.candidates(source)
        compaction=result['/etc/systemd/system/litellm.service.d/30-compaction-recovery.conf']['text']
        self.assertIn('COMPACTION_FALLBACK_ENABLED=true',compaction)
        self.assertIn('LoadCredentialEncrypted=vertex-ai-credentials:',
            result['/etc/systemd/system/litellm.service.d/50-vertex.conf']['text'])

    def test_bedrock_candidates_match_exact_ansible_copy_list(self):
        import yaml
        source = Path(__file__).resolve().parent.parent
        tasks = yaml.safe_load((source/'deploy/tasks/bedrock-runtime.yml').read_text())
        copied = next(t['loop'] for t in tasks if t.get('name') == 'Install Bedrock runtime and matching validation utilities')
        with patch.object(runtime, 'SCOPE', 'bedrock'):
            targets = runtime.candidates(source)
        expected = {str(runtime.ROOT/'scripts'/name) for name in copied}
        expected.add('/etc/systemd/system/litellm.service.d/40-bedrock.conf')
        self.assertEqual(set(targets), expected)

    def test_bedrock_packages_host_settings_helper_for_installed_validators(self):
        self.assertIn('deployment.py', runtime.BEDROCK_FILES)
        import yaml
        source = Path(__file__).resolve().parent.parent
        tasks = yaml.safe_load((source/'deploy/tasks/bedrock-runtime.yml').read_text())
        for name in ('Verify existing provider snapshots', 'Verify retained Bedrock routes'):
            task = next(t for t in tasks if t.get('name') == name)
            # Preflight uses the installed generator first; source resolves
            # helper imports until the journalled runtime copy installs them.
            self.assertEqual(task['environment']['PYTHONPATH'], '{{ source_root }}/scripts')

class RuntimeRecovery(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(runtime,'service_state',return_value={'active':True,'enabled':True}))
        self.home=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root=self.home/'runtime';self.root.mkdir()
        self.state=self.home/'state';self.state.mkdir()
        for name,value in [('HOME',self.home),('ROOT',self.root),('STATE',self.state),('JOURNAL',self.state/'journal.json')]:
            self.enterContext(patch.object(runtime,name,value))
        self.file=self.root/'serve.py';self.file.write_text('old');self.file.chmod(0o700)
        self.lock=self.root/'uv.lock';self.lock.write_text('old lock');self.lock.chmod(0o600)
        self.config=self.home/'config';self.config.write_text('Bedrock routes')
        self.enterContext(patch.object(runtime,'protected',return_value={str(self.config):runtime.record(self.config)}))
        self.after={str(self.file):{'text':'new','mode':0o700},str(self.lock):{'text':'new lock','mode':0o600}}
        self.enterContext(patch.object(runtime,'candidates',return_value=self.after))
        self.enterContext(patch('runtime_rollout.subprocess.check_output',side_effect=[b'', 'fixture-commit']))
        runtime.prepare(self.home)

    def test_finish_requires_all_candidates_and_preserves_recovery(self):
        with self.assertRaises(ValueError):runtime.finish()
        self.assertTrue(runtime.JOURNAL.exists())
        runtime.restore(self.after)
        runtime.finish()
        self.assertFalse(runtime.JOURNAL.exists())
        self.assertEqual(json.loads((self.state/'runtime-installed.json').read_text())['commit'],'fixture-commit')

    def test_rollback_refuses_provider_or_concurrent_runtime_changes(self):
        self.file.write_text('other runtime')
        with patch('runtime_rollout.subprocess.run') as run:
            with self.assertRaises(ValueError):runtime.rollback()
            run.assert_not_called()
        self.file.write_text('new')
        self.config.write_text('new Vertex routes')
        with self.assertRaises(ValueError):runtime.rollback()
        self.assertTrue(runtime.JOURNAL.exists())

    def test_partial_deployment_can_restore_saved_lock_and_files(self):
        self.file.write_text('new')
        with patch('runtime_rollout.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run, \
                patch('runtime_rollout.urllib.request.urlopen') as health:
            health.return_value.__enter__.return_value.status=200
            runtime.rollback()
        self.assertEqual(self.file.read_text(),'old')
        self.assertEqual(self.lock.read_text(),'old lock')
        self.assertEqual(self.config.read_text(),'Bedrock routes')
        self.assertTrue(any('sync' in c.args[0] and '--locked' in c.args[0] for c in run.call_args_list))

    def test_failed_first_install_does_not_sync_or_restart_a_missing_previous_runtime(self):
        self.lock.unlink()
        journal=json.loads(runtime.JOURNAL.read_text())
        journal['before'][str(self.lock)]=None
        journal['service']={'active':False,'enabled':False}
        journal['environment_existed']=False
        runtime.JOURNAL.write_text(json.dumps(journal))
        (self.root/'.venv').mkdir()
        runtime.restore(self.after)
        with patch('runtime_rollout.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run:
            runtime.rollback()
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.root/'.venv').exists())
        self.assertFalse(any('sync' in call.args[0] or 'restart' in call.args[0] for call in run.call_args_list))
        self.assertFalse(runtime.JOURNAL.exists())

    def test_journal_from_another_host_is_rejected_before_mutation(self):
        journal=json.loads(runtime.JOURNAL.read_text())
        journal['host']='love'
        runtime.JOURNAL.write_text(json.dumps(journal))
        with patch('runtime_rollout.subprocess.run') as run:
            for action in (runtime.finish, runtime.rollback):
                with self.assertRaisesRegex(ValueError, 'different host'):
                    action()
            run.assert_not_called()
        self.assertTrue(runtime.JOURNAL.exists())


class BedrockRuntimeRecovery(RuntimeRecovery):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(runtime, 'SCOPE', 'bedrock'))
        self.enterContext(patch.object(runtime, 'unrelated_models', return_value={'fixture': 'unchanged'}))
        journal = json.loads(runtime.JOURNAL.read_text())
        journal.update(scope='bedrock', unrelated_models={'fixture': 'unchanged'},
                       model_before={str(self.config): runtime.record(self.config)})
        runtime.JOURNAL.write_text(json.dumps(journal))

    def test_bedrock_does_not_sync_environment(self):
        runtime.restore(self.after)
        with patch('runtime_rollout.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run, patch.object(runtime, 'wait_for_health'):
            runtime.rollback()
        self.assertEqual(self.file.read_text(), 'old')
        self.assertFalse(any('sync' in c.args[0] for c in run.call_args_list))
        self.assertFalse(runtime.JOURNAL.exists())

    def test_pending_model_recovery_blocks_runtime_restore(self):
        (self.state/'model-refresh').mkdir()
        with patch('runtime_rollout.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'routes/catalog'):
                runtime.rollback()
            run.assert_not_called()

    def test_recovery_restores_models_before_runtime(self):
        from unittest.mock import Mock
        transaction = self.state/'model-refresh'
        transaction.mkdir()
        refresh = Mock()
        refresh.transaction = transaction
        refresh.originals.return_value = {'config': 'old'}
        refresh.journal.return_value = {'originals': {'config': 'old'}}
        order = []
        refresh.rollback.side_effect = lambda: order.append('models')
        def rollback():
            self.assertFalse(transaction.exists())
            order.append('runtime')
            return {'restored': True}
        with patch('configure_bedrock.BedrockSetup', return_value=refresh), patch.object(runtime, 'rollback', side_effect=rollback):
            self.assertEqual(runtime.recover_bedrock(self.home), {'restored': True})
        self.assertEqual(order, ['models', 'runtime'])

    def test_model_files_must_be_restored_before_runtime(self):
        self.config.write_text('new Bedrock route')
        with patch.object(runtime, 'assert_records', wraps=runtime.assert_records), patch('runtime_rollout.subprocess.run') as run:
            with self.assertRaises(ValueError):
                runtime.rollback()
            run.assert_not_called()

    def test_unrelated_model_change_is_rejected(self):
        with patch.object(runtime, 'unrelated_models', return_value={'fixture': 'other'}), patch('runtime_rollout.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'Unrelated model'):
                runtime.finish()
            run.assert_not_called()

    def test_scope_mismatch_rejected(self):
        with patch.object(runtime, 'SCOPE', 'full'):
            with self.assertRaisesRegex(ValueError, 'scope'):
                runtime.rollback()

    def test_finish_requires_all_candidates_and_preserves_recovery(self):
        with self.assertRaises(ValueError):
            runtime.finish()
        runtime.restore(self.after)
        runtime.finish()
        self.assertFalse(runtime.JOURNAL.exists())
        self.assertTrue((self.state/'bedrock-runtime-installed.json').exists())
        self.assertFalse((self.state/'runtime-installed.json').exists())

    def test_partial_deployment_can_restore_saved_lock_and_files(self):
        self.test_bedrock_does_not_sync_environment()

    def test_failed_first_install_does_not_sync_or_restart_a_missing_previous_runtime(self):
        # Bedrock scope is only permitted on an existing gateway, not a first install.
        self.test_bedrock_does_not_sync_environment()


if __name__=='__main__':unittest.main()
