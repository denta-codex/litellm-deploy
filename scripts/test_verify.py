"""The ordinary provider probe supports authenticated stateful tool continuations."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch
import urllib.error

import deployment


def stream(*events):
    return io.BytesIO(b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events))


class StatefulProbeTests(unittest.TestCase):
    def test_authenticated_stream_and_tool_loop_keep_the_same_session(self):
        session = None
        posts = 0
        tool_prefix = None

        def upstream(request, **kwargs):
            nonlocal session, posts, tool_prefix
            headers = {name.lower(): value for name, value in request.header_items()}
            if headers.get('authorization') != 'Bearer fixture-key':
                raise urllib.error.HTTPError(request.full_url, 401, 'unauthorized', {}, None)
            if request.data is None:
                return io.BytesIO(b'{"data":[{"id":"claude/fixture"}]}')
            identity = headers.get('thread-id') or headers.get('session-id') or headers.get('x-claude-chat-id')
            if not identity or session is not None and session != identity:
                raise urllib.error.HTTPError(request.full_url, 400, 'missing or changed session identity', {}, None)
            session = identity
            payload = json.loads(request.data)
            posts += 1
            completed = {'type': 'response.completed', 'response': {'status': 'completed'}}
            if posts == 1:
                return stream({'type': 'response.output_text.delta', 'delta': 'LITELLM_STREAM_OK'}, completed)
            if posts == 2:
                tool_prefix = payload['input']
                self.assertEqual(payload['tool_choice']['name'], 'deployment_probe')
                item = {'type': 'function_call', 'name': 'deployment_probe', 'call_id': 'fixture-call',
                        'arguments': '{}'}
            else:
                self.assertEqual(posts, 3)
                self.assertEqual(payload['input'][:len(tool_prefix)], tool_prefix,
                                 'Signed thinking requires an unchanged input prefix')
                results = [item for item in payload['input'] if item.get('type') == 'function_call_output']
                self.assertEqual(results, [{'type': 'function_call_output', 'call_id': 'fixture-call',
                                           'output': 'DEPLOYMENT_TOOL_OK'}])
                item = {'type': 'message', 'content': [{'type': 'output_text', 'text': 'DEPLOYMENT_TOOL_OK'}]}
            return stream({'type': 'response.output_item.done', 'output_index': 0, 'item': item}, completed)

        with patch.object(deployment, 'proxy_key', return_value='fixture-key'), \
                patch('urllib.request.urlopen', side_effect=upstream), \
                patch('sys.argv', ['verify.py', '--model', 'claude/fixture']), \
                redirect_stdout(io.StringIO()) as output:
            runpy.run_path(str(Path(__file__).with_name('verify.py')), run_name='__main__')
        self.assertEqual(posts, 3)
        self.assertNotIn('fixture-key', output.getvalue())
        self.assertIn('PASS function call and tool-result continuation', output.getvalue())


if __name__ == '__main__':
    unittest.main()
