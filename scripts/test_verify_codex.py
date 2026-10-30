"""The stock-Codex shell probe must pass only on a real, successful tool result."""
import subprocess
import unittest

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


if __name__ == '__main__':
    unittest.main()
