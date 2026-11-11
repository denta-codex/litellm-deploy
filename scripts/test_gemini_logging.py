"""Timing visibility must not depend on upstream's root logging configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class TimingLogging(unittest.TestCase):
    def test_quiet_server_records_only_metadata_once(self):
        source = '''
import logging
from gemini_native import LOG, configure_timing_logging, feedback_event
root = logging.getLogger()
root.handlers.clear()
root.setLevel(logging.WARNING)
configure_timing_logging()
configure_timing_logging()
assert root.level == logging.WARNING and root.handlers == []
assert len([h for h in LOG.handlers if h.name == 'gemini-native-metadata']) == 1
feedback_event(None, ('model_timing', {
    'origin': ('vertex_ai', 'gemini-fixture'), 'dispatch_at': 1000,
    'started_at': 1, 'first_content_at': 2, 'first_public_at': 3,
    'completed_at': 4, 'outcome': 'completed',
    'parts': [{'text': 'DO_NOT_LOG_PROMPT', 'thoughtSignature': 'DO_NOT_LOG_STATE'}],
    'key': 'DO_NOT_LOG_KEY',
}))
'''
        result = subprocess.run([sys.executable, '-c', source], capture_output=True,
                                text=True, timeout=30,
                                env=os.environ | {'PYTHONPATH': str(Path(__file__).parent)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertTrue(lines[0].startswith('gemini_timing '))
        self.assertNotIn('DO_NOT_LOG', result.stderr)
        self.assertEqual(json.loads(lines[0].removeprefix('gemini_timing ')), {
            'provider': 'vertex_ai', 'model': 'gemini-fixture', 'dispatch_at': 1000,
            'outcome': 'completed', 'first_content_ms': 1000,
            'first_public_ms': 2000, 'completion_ms': 3000,
        })
