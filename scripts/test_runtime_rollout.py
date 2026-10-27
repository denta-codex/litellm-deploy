"""Runtime recovery restores only managed changes and refuses concurrent edits."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import runtime_rollout as runtime


class RuntimeRecovery(unittest.TestCase):
    def setUp(self):
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
        self.assertFalse(runtime.JOURNAL.exists())


if __name__=='__main__':unittest.main()
