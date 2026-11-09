"""Context snapshots and aggregate receipts never share a counter."""
import json
from pathlib import Path
import tempfile
import unittest

from claude_context import REQUEST
from usage_accounting import accounting, publish, sample
from verify_claude import usage_report


class AccountingTests(unittest.TestCase):
    def test_cache_reads_and_writes_are_part_of_context_and_consumption(self):
        latest = sample({'input_tokens': 2, 'output_tokens': 3,
                         'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 10}, claude=True)
        result = accounting(latest, [{'id': 'first', 'usage': latest}, {'id': 'second', 'usage': latest}])
        self.assertEqual(result['latest_request_usage']['total_tokens'], 115)
        self.assertEqual(result['consumed_usage']['total_tokens'], 230)
        self.assertEqual(result['consumed_usage']['input_tokens_details'], {
            'cached_tokens': 200, 'cache_write_tokens': 20})

    def test_receipts_are_exactly_once_and_request_scoped(self):
        receipt = {'id': 'worker:message', 'model': 'claude-opus-5-5',
                   'usage': sample({'input_tokens': 10, 'output_tokens': 2})}
        first = {}
        token = REQUEST.set(first)
        try:
            publish([receipt, receipt])
            self.assertEqual(len(first['usage_receipts']), 1)
            receipt['usage']['input_tokens'] = 999
            self.assertEqual(next(iter(first['usage_receipts'].values()))['usage']['input_tokens'], 10)
        finally:
            REQUEST.reset(token)
        second = {}
        token = REQUEST.set(second)
        try:
            publish([])
            self.assertEqual(second['usage_receipts'], {})
        finally:
            REQUEST.reset(token)

    def test_usage_report_excludes_conversation_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journals = root / 'claude/journals'
            journals.mkdir(parents=True)
            latest = sample({'input_tokens': 10, 'output_tokens': 2})
            (journals / 'new.json').write_text(json.dumps({
                'usage_accounting': accounting(latest, [{'id': 'x', 'usage': latest}]),
                'delivered': {'private': 'SECRET CONVERSATION'}}))
            (journals / 'old.json').write_text('{"responses": {}}')
            report = usage_report(root)
            self.assertEqual(report['legacy_records'], 1)
            self.assertEqual(report['records'][0]['latest_request_usage']['total_tokens'], 12)
            self.assertNotIn('SECRET', json.dumps(report))

    def test_unavailable_usage_is_not_a_zero_context_snapshot(self):
        self.assertIsNone(sample(None))
        self.assertIsNone(sample({'input_tokens': 2}, claude=True))
