import json
from pathlib import Path
import tempfile
import unittest

import tomlkit
from voice_rollout import Rollout, FILES


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=self.enterContext(tempfile.TemporaryDirectory())
        root=Path(self.tmp)
        self.rollout=Rollout(root/'home',root/'source')
        self.rollout.config.parent.mkdir(parents=True)
        self.original='model_provider = "litellm"\nmodel = "original"\n'
        self.rollout.config.write_text(self.original)
        self.rollout.scripts.mkdir(parents=True)
        (self.rollout.scripts/'serve.py').write_text('old server')
        (root/'source/scripts').mkdir(parents=True)
        for name in FILES:(root/'source/scripts'/name).write_text('new '+name)

    def test_rollback_preserves_unrelated_config_edit(self):
        self.rollout.prepare()
        self.rollout.inspect()
        doc=tomlkit.parse(self.rollout.config.read_text());doc['model']='new selection'
        self.rollout.config.write_text(tomlkit.dumps(doc))
        self.rollout.rollback()
        doc=tomlkit.parse(self.rollout.config.read_text())
        self.assertEqual(doc,{'model_provider':'litellm','model':'new selection'})
        self.assertEqual((self.rollout.scripts/'serve.py').read_text(),'old server')
        self.assertFalse((self.rollout.scripts/'live_voice.py').exists())
        self.assertFalse(self.rollout.journal.exists())

    def test_conflicting_voice_edit_preserves_recovery(self):
        self.rollout.prepare()
        (self.rollout.scripts/'serve.py').write_text('another edit')
        with self.assertRaises(RuntimeError):self.rollout.rollback()
        self.assertTrue(self.rollout.journal.exists())

    def test_partial_prepare_can_be_recovered(self):
        self.rollout.prepare()
        self.rollout.config.write_text(self.original)
        self.rollout.rollback()
        self.assertEqual(self.rollout.config.read_text(),self.original)

    def test_finish_requires_desktop_and_live_tools(self):
        self.rollout.prepare()
        receipt=Path(self.tmp)/'receipt.json'
        receipt.write_text(json.dumps({'ok':True,'commands':[{'exitCode':0}],'stopped':True,
                                      'connected':True,'control_transport':'remote-wss',
                                      'transport':'webrtc','audible_frames':5,'errors':[]}))
        with self.assertRaises(RuntimeError):self.rollout.finish(receipt,False)
        self.rollout.finish(receipt,True)
        self.assertTrue(self.rollout.accepted.exists())
        self.assertFalse(self.rollout.journal.exists())


if __name__=='__main__':unittest.main()
