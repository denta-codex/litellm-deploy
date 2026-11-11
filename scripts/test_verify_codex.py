"""Stock-Codex acceptance cannot hide provider failures or missing tool results."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import verify_codex

from verify_codex import TOOL_COMMAND, TOOL_MARKER, require_shell_result, shell_prompt


def completed(item):
    return {'type': 'item.completed', 'item': item}


def message(text):
    return completed({'type': 'agent_message', 'text': text})


def command(exit_code=0, output=TOOL_MARKER):
    return completed({'type': 'command_execution', 'command': f"/bin/bash -lc 'printf {TOOL_MARKER}'",
                      'exit_code': exit_code, 'aggregated_output': output})


class ShellProbeTests(unittest.TestCase):
    def test_refusal_without_a_command_fails_and_reports_the_reply(self):
        refusal = 'I can’t run that command: the shell is read-only.'
        with self.assertRaises(AssertionError) as failure:
            require_shell_result([message(refusal), {'type': 'turn.completed'}])
        self.assertIn('attempted no command', str(failure.exception))
        self.assertIn('shell is read-only', str(failure.exception))

    def test_failed_or_wrong_command_output_fails(self):
        for events in ([command(exit_code=1)], [command(output='something else')], [command(exit_code=None)]):
            with self.subTest(events=events), self.assertRaises(AssertionError) as failure:
                require_shell_result(events + [message(TOOL_MARKER)])
            self.assertIn('no successful', str(failure.exception))

    def test_successful_command_result_passes(self):
        require_shell_result([command(exit_code=1, output=''), command(), message(TOOL_MARKER)])

    def test_requested_command_prints_exactly_the_checked_marker(self):
        result = subprocess.run(['/bin/sh', '-c', TOOL_COMMAND], capture_output=True, text=True, check=True)
        self.assertEqual((result.stdout, result.stderr), (TOOL_MARKER, ''))
        for mode in ('code_mode_only', 'direct', None):
            with self.subTest(mode=mode):
                self.assertIn(f'`{TOOL_COMMAND}`', shell_prompt('the URL.', mode))

    def test_code_mode_prompt_names_the_nested_shell_tool(self):
        # Code-mode models only see `exec`; "the shell tool" alone let them conclude none exists.
        self.assertIn('`tools.exec_command`', shell_prompt('the URL.', 'code_mode_only'))
        self.assertNotIn('tools.exec_command', shell_prompt('the URL.', 'direct'))


class CLIProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / '.codex').mkdir()
        (self.root / '.codex/auth.json').write_text(json.dumps({
            'tokens': {'refresh_token': 'fixture-refresh'}, 'OPENAI_API_KEY': 'fixture-api'}))
        self.probe_home = self.root / 'probe'
        self.probe_home.mkdir()
        self.args = Namespace(model='bedrock/fable-5.2', catalog=self.root / 'catalog.json',
            skip_search=True, service_tier=None, base_url='http://127.0.0.1:4567', launcher=Path('/fixture/codex'))
        self.enterContext(patch('verify_codex.Path.home', return_value=self.root))
        self.enterContext(patch('verify_codex.proxy_key', return_value='fixture-key'))
        self.enterContext(redirect_stdout(io.StringIO()))

    def first(self, stderr='', returncode=0):
        events = [{'type': 'thread.started', 'thread_id': 'fixture-thread'},
                  message('LITELLM_CONTEXT_OK'), {'type': 'turn.completed'}]
        return subprocess.CompletedProcess([], returncode,
            '\n'.join(json.dumps(e) for e in events), stderr)

    def test_no_request_or_stream_retry_and_exact_resume(self):
        second = subprocess.CompletedProcess([], 0, '\n'.join(json.dumps(e) for e in (
            command(), message(TOOL_MARKER + ' LITELLM_CONTEXT_OK'), {'type': 'turn.completed'})), '')
        with patch('verify_codex.subprocess.run', side_effect=[self.first(), second]) as run:
            verify_codex.probe(self.args, {'tool_mode': 'code_mode_only'}, 'high', self.probe_home)
        config = tomlkit.parse((self.probe_home / 'config.toml').read_text())
        provider = config['model_providers']['litellm']
        self.assertEqual(provider['request_max_retries'], 0)
        self.assertEqual(provider['stream_max_retries'], 0)
        self.assertEqual(provider['base_url'], self.args.base_url + '/v1')
        self.assertEqual(config['model_reasoning_effort'], 'high')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][2:4], ['resume', 'fixture-thread'])

    def test_invalid_item_lifecycle_is_a_failure_even_with_completed_turn(self):
        with patch('verify_codex.subprocess.run', return_value=self.first(
                stderr='WARN OutputTextDelta without active item')) as run:
            with self.assertRaisesRegex(AssertionError, 'invalid text-item lifecycle'):
                verify_codex.probe(self.args, {}, 'high', self.probe_home)
        self.assertEqual(run.call_count, 1)

    def test_provider_failure_does_not_start_another_attempt_or_resume(self):
        with patch('verify_codex.subprocess.run', return_value=self.first(returncode=1)) as run:
            with self.assertRaisesRegex(RuntimeError, 'Codex probe failed'):
                verify_codex.probe(self.args, {}, 'high', self.probe_home)
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
