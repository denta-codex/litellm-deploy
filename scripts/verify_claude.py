"""Live Claude acceptance. --isolated starts a disposable proxy, never a global install."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import zlib

import httpx
import tomlkit
import yaml

from configure_claude import MANIFEST, generate

ROOT = Path(__file__).resolve().parent.parent
MODEL = 'claude/opus-5.5'


class RPC:
    def __init__(self, process):
        self.process, self.pending, self.events = process, {}, []
        self.changed = asyncio.Event()
        self.counter = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if 'id' in message and 'method' not in message:
                future = self.pending.pop(message['id'], None)
                if future and not future.done():
                    future.set_result(message)
            else:
                self.events.append(message)
                self.changed.set()
                if 'id' in message:
                    await self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unexpected approval in isolated acceptance'}})

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params):
        self.counter += 1
        sequence = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = future
        await self.send({'id': sequence, 'method': method, 'params': params})
        result = await asyncio.wait_for(future, 30)
        if 'error' in result:
            raise RuntimeError(str(result['error']))
        return result['result']

    async def until(self, predicate, offset=0):
        async with asyncio.timeout(240):
            while True:
                self.changed.clear()
                for event in self.events[offset:]:
                    if predicate(event):
                        return event
                await self.changed.wait()

    async def turn(self, thread, prompt, **extra):
        offset = len(self.events)
        start = await self.call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}], 'effort': 'low', **extra})
        turn_id = start['turn']['id']
        event = await self.until(lambda e: e.get('method') == 'turn/completed' and e.get('params', {}).get('turn', {}).get('id') == turn_id, offset)
        if event['params']['turn']['status'] != 'completed':
            raise RuntimeError('Codex turn failed: ' + str(event['params']['turn'].get('error')))
        items = [e['params']['item'] for e in self.events[offset:] if e.get('method') == 'item/completed' and e['params'].get('threadId') == thread]
        return '\n'.join(i.get('text', '') for i in items if i['type'] == 'agentMessage'), items


async def stop(process):
    if process and process.returncode is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 50)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


class Acceptance:
    def __init__(self, args, directory):
        self.args, self.directory = args, directory
        self.proxy = None
        self.logs = []
        self.passes = []
        self.base = args.base_url.rstrip('/')
        self.key = os.environ.get('CLAUDE_TEST_PROXY_KEY')
        self.env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False')

    def passed(self, name):
        self.passes.append(name)
        print('PASS ' + name, flush=True)
        if self.args.report:
            self.args.report.write_text(json.dumps({'passed': self.passes, 'complete': False}, indent=2))

    async def start_proxy(self):
        existing = json.loads((Path.home() / '.config/litellm/codex-models.json').read_text())
        existing['models'] = [m for m in existing['models'] if m['slug'] == 'chatgpt/gpt-6-astra']
        config, catalog, _ = generate(json.loads(MANIFEST.read_text()), {'model_list': [
            {'model_name': 'chatgpt/gpt-6-astra', 'model_info': {'mode': 'responses'},
             'litellm_params': {'model': 'chatgpt/gpt-6-astra'}}]}, existing, '')
        config['general_settings'] = {'master_key': 'os.environ/CLAUDE_TEST_PROXY_KEY', 'disable_spend_logs': True}
        config['litellm_settings'] = {'turn_off_message_logging': True, 'num_retries': 0}
        path = self.directory / 'config.yaml'
        path.write_text(yaml.safe_dump(config))
        self.catalog = self.directory / 'catalog.json'
        self.catalog.write_text(json.dumps(catalog))
        log = (self.directory / 'proxy.log').open('a')
        self.logs.append(log)
        self.proxy = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'scripts/serve.py'),
            '--config', str(path), '--port', self.base.rsplit(':', 1)[1], env=self.env,
            stdout=log, stderr=log, start_new_session=True)
        async with httpx.AsyncClient() as client:
            for _ in range(120):
                if self.proxy.returncode is not None:
                    raise RuntimeError('Isolated proxy failed; see ' + str(self.directory / 'proxy.log'))
                try:
                    response = await client.get(self.base + '/health/liveliness')
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        raise RuntimeError('Isolated proxy startup timed out')

    async def request(self, prompt, *, chat=None, stream=True, **extra):
        payload = {'model': MODEL, 'input': prompt, 'stream': stream, **extra}
        headers = {'Authorization': 'Bearer ' + self.key, 'x-claude-chat-id': chat or secrets.token_hex(12)}
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(self.base + '/v1/responses', json=payload, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f'Claude HTTP {response.status_code}: {response.text[-1500:]}')
        if not stream:
            return response.json(), []
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
        errors = [e for e in events if e.get('type') in ('error', 'response.failed') or 'error' in e and e['error']]
        if errors:
            raise RuntimeError(str(errors))
        completed = next((e['response'] for e in reversed(events) if e.get('type') == 'response.completed'), None)
        if completed is None:
            raise RuntimeError('Claude stream did not complete: ' + response.text[-1500:])
        items = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        if items:
            completed['output'] = items
        return completed, events

    @staticmethod
    def reply(response):
        return ''.join(part.get('text', '') for item in response.get('output', []) if item['type'] == 'message'
                       for part in item.get('content', []) if part['type'] == 'output_text')

    async def api_checks(self):
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            response, events = await self.request('Reply exactly OPUS_ACCEPTED.', reasoning={'effort': effort})
            assert 'OPUS_ACCEPTED' in self.reply(response)
            assert response.get('usage', {}).get('output_tokens', 0) > 0
            self.passed('Opus 5.5 streaming, usage, effort=' + effort)
        response, _ = await self.request('Reply exactly NONSTREAM_OK.', stream=False)
        assert 'NONSTREAM_OK' in self.reply(response)
        self.passed('nonstreaming Responses')
        schema = {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['SCHEMA_OK']}}, 'required': ['status'], 'additionalProperties': False}
        response, _ = await self.request('Return a JSON object with status SCHEMA_OK.', text={'format': {'type': 'json_schema', 'name': 'probe', 'strict': True, 'schema': schema}})
        assert json.loads(self.reply(response)) == {'status': 'SCHEMA_OK'}
        self.passed('schema-constrained output')
        # A 32x32 red PNG built without an image dependency.
        def png_chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        png = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0))
        png += png_chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\x00\x00' * 32) * 32)) + png_chunk(b'IEND', b'')
        response, _ = await self.request([{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Name the dominant color. Reply with one word.'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(png).decode()}]}])
        assert 'red' in self.reply(response).lower()
        self.passed('image input')
        response, events = await self.request('Find the smallest positive integer n such that n mod 7 = 3, n mod 11 = 5, and n mod 13 = 9. Verify it before answering.', reasoning={'effort': 'high'})
        assert any('reasoning_summary' in e['type'] for e in events), 'No reasoning summary was emitted'
        self.passed('reasoning summary events')
        function = {'type': 'function', 'name': 'probe', 'description': 'Read the requested synthetic marker.', 'parameters': {
            'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}}
        for tool in (function, {'type': 'namespace', 'name': 'functions', 'tools': [function]},
                     {'type': 'custom', 'name': 'apply_patch', 'description': 'Submit a patch; the client executes it.', 'format': {'type': 'text'}}):
            chat = secrets.token_hex(12)
            prompt = 'Call the supplied tool once. Then report its result.'
            first, _ = await self.request(prompt, chat=chat, tools=[tool], tool_choice='required')
            calls = [item for item in first['output'] if item['type'] in ('function_call', 'custom_tool_call')]
            assert len(calls) == 1, calls
            call = calls[0]
            if tool['type'] == 'namespace':
                assert call.get('namespace') == 'functions'
            result = {'type': 'custom_tool_call_output' if call['type'] == 'custom_tool_call' else 'function_call_output', 'call_id': call['call_id'], 'output': 'TOOL_RESULT_OK'}
            history = [{'role': 'user', 'content': prompt}, *first['output'], result]
            second, _ = await self.request(history, chat=chat, tools=[tool], tool_choice='required')
            assert 'TOOL_RESULT_OK' in self.reply(second)
            retry, _ = await self.request(history, chat=chat, tools=[tool], tool_choice='required')
            assert retry['output'] == second['output'] or self.reply(retry) == self.reply(second)
            self.passed(tool['type'] + ' tool continuation and retry')
        chat = secrets.token_hex(12)
        prompt = 'Call probe twice in parallel, once with value alpha and once with value beta. Then report both returned markers.'
        first, _ = await self.request(prompt, chat=chat, tools=[function], tool_choice='required', parallel_tool_calls=True)
        calls = [item for item in first['output'] if item['type'] == 'function_call']
        assert len(calls) == 2, 'Parallel probe must produce two calls in one response'
        result_b = {'type': 'function_call_output', 'call_id': calls[1]['call_id'], 'output': 'B_RESULT'}
        history = [{'role': 'user', 'content': prompt}, *first['output'], result_b]
        partial, _ = await self.request(history, chat=chat, tools=[function], tool_choice='required', parallel_tool_calls=True)
        remaining = [item for item in partial['output'] if item['type'] == 'function_call']
        assert [item['call_id'] for item in remaining] == [calls[0]['call_id']]
        history += [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': 'A_RESULT'}]
        complete, _ = await self.request(history, chat=chat, tools=[function], tool_choice='required', parallel_tool_calls=True)
        assert all(marker in self.reply(complete) for marker in ('A_RESULT', 'B_RESULT'))
        self.passed('parallel calls with partial and out-of-order results')

    async def codex_checks(self):
        home, work = self.directory / 'codex', self.directory / 'work'
        home.mkdir(exist_ok=True)
        work.mkdir(exist_ok=True)
        marker = 'CODEX_' + secrets.token_hex(12)
        (work / 'marker.txt').write_text(marker)
        config = {'model': MODEL, 'model_provider': 'claude_acceptance', 'model_catalog_json': str(self.catalog),
            'model_reasoning_effort': 'low', 'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'disabled',
            'features': {'shell_snapshot': False, 'fast_mode': self.args.fast}, 'shell_environment_policy': {'exclude': ['CLAUDE_TEST_PROXY_KEY']},
            'model_providers': {'claude_acceptance': {'name': 'Claude acceptance', 'base_url': self.base + '/v1',
                'wire_api': 'responses', 'env_key': 'CLAUDE_TEST_PROXY_KEY', 'supports_websockets': False,
                'requires_openai_auth': False, 'request_max_retries': 0, 'stream_max_retries': 0}}}
        if self.args.fast:
            # Exercise the same account-backed Fast control as the desktop, with
            # only a disposable login copy and the isolated proxy credential.
            native = json.loads((Path.home() / '.codex/auth.json').read_text())
            native['tokens']['refresh_token'] = ''
            native['OPENAI_API_KEY'] = None
            (home / 'auth.json').write_text(json.dumps(native))
            config['service_tier'] = 'default'
            config['model_providers']['claude_acceptance']['requires_openai_auth'] = True
        (home / 'config.toml').write_text(tomlkit.dumps(config))
        log = (self.directory / 'codex.log').open('w')
        self.logs.append(log)
        process = await asyncio.create_subprocess_exec('codex', 'app-server', cwd=work,
            env=self.env | {'CODEX_HOME': str(home), 'CLAUDE_TEST_PROXY_KEY': self.key}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=log, start_new_session=True)
        rpc = RPC(process)
        try:
            await rpc.call('initialize', {'clientInfo': {'name': 'claude_acceptance', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
            await rpc.send({'method': 'initialized', 'params': {}})
            async def new_thread(sandbox='read-only'):
                # Use the catalog's Claude instructions, as a real chat would.
                result = await rpc.call('thread/start', {'model': MODEL, 'modelProvider': 'claude_acceptance', 'cwd': str(work),
                    'approvalPolicy': 'never', 'sandbox': sandbox})
                return result['thread']['id']
            if self.args.fast:
                account = await rpc.call('account/read', {'refreshToken': False})
                assert account['account']['type'] == 'chatgpt' and account['requiresOpenaiAuth']
                await self.fast_checks(rpc, await new_thread(), work, marker)
                if self.args.fast_only:
                    return
            thread = await new_thread()
            reply, items = await rpc.turn(thread, 'Run cat ' + str(work / 'marker.txt') + ' using your shell and report the exact output.')
            assert marker in reply and any(i['type'] == 'commandExecution' for i in items)
            self.passed('real Codex native execution and result continuation')
            patch_thread = await new_thread('workspace-write')
            _, patch_items = await rpc.turn(patch_thread, 'Use apply_patch to create acceptance.txt containing exactly PATCH_ACCEPTED and a newline. Do not use the shell to write the file.')
            assert (work / 'acceptance.txt').read_text() == 'PATCH_ACCEPTED\n'
            assert any(i['type'] == 'fileChange' for i in patch_items)
            self.passed('real Codex apply_patch execution')
            tricky = work / 'tricky.txt'
            original = 'name = "O\'Brien"\npath = C:\\temp\\new\\n\necho "$HOME and ${VAR}" `date`\n\tindented = 1\nstatus = draft\ntrailer = done\n'
            replacement = 'status = "final" # it\'s $DONE \\o/'
            tricky.write_text(original)
            _, edit_items = await rpc.turn(patch_thread, 'In tricky.txt, replace the line `status = draft` with `' + replacement + '`. Change nothing else in the file.')
            assert tricky.read_text() == original.replace('status = draft', replacement), repr(tricky.read_text())
            assert any(i['type'] == 'fileChange' for i in edit_items)
            writes = [i['command'] for i in edit_items if i['type'] == 'commandExecution' and 'tricky.txt' in i.get('command', '')
                      and (re.search(r'>>?\s*\S*tricky\.txt', i['command']) or any(w in i['command'] for w in ('sed -i', 'tee ', 'python', 'perl', 'node ')))]
            assert not writes, writes
            self.passed('quote-heavy edit through apply_patch')
            reply, _ = await rpc.turn(thread, 'Repeat the marker without using tools.')
            assert marker in reply
            fork = await rpc.call('thread/fork', {'threadId': thread, 'model': MODEL, 'modelProvider': 'claude_acceptance'})
            reply, items = await rpc.turn(fork['thread']['id'], 'Repeat the earlier marker without tools.')
            assert marker in reply and not any(i['type'] == 'commandExecution' for i in items)
            self.passed('follow-up and real Codex fork')
            async def independent():
                chat = await new_thread()
                label = secrets.token_hex(12)
                await rpc.turn(chat, 'Remember ' + label + '. Reply OK.')
                reply, _ = await rpc.turn(chat, 'Repeat my label without tools.')
                assert label in reply
            await asyncio.gather(independent(), independent())
            self.passed('concurrent chat isolation')
            offset = len(rpc.events)
            await rpc.call('thread/compact/start', {'threadId': thread})
            await rpc.until(lambda e: e.get('method') == 'turn/completed' and e.get('params', {}).get('threadId') == thread, offset)
            reply, _ = await rpc.turn(thread, 'Repeat the marker from the earlier command output without tools.')
            assert marker in reply
            self.passed('real Codex compaction and continuity')
            reply, _ = await rpc.turn(thread, 'Repeat the earlier command marker without tools.', model='chatgpt/gpt-6-astra')
            assert marker in reply
            reply, _ = await rpc.turn(thread, 'Repeat the earlier command marker without tools.', model=MODEL)
            assert marker in reply
            self.passed('Claude to ChatGPT and back with retained history')
            offset = len(rpc.events)
            turn = await rpc.call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': 'Run sleep 30 using the shell, with yield_time_ms 30000. Do not retry.'}], 'effort': 'low'})
            await rpc.until(lambda e: e.get('method') == 'item/started' and e.get('params', {}).get('item', {}).get('type') == 'commandExecution', offset)
            await rpc.call('turn/interrupt', {'threadId': thread, 'turnId': turn['turn']['id']})
            await rpc.until(lambda e: e.get('method') == 'turn/completed' and e.get('params', {}).get('turn', {}).get('id') == turn['turn']['id'], offset)
            reply, _ = await rpc.turn(thread, 'Reply exactly AFTER_INTERRUPT. Do not use tools.')
            assert 'AFTER_INTERRUPT' in reply
            self.passed('new user prompt following an aborted command')
            if self.args.isolated:
                # Destroy only this harness's proxy group; no live service is touched.
                os.killpg(self.proxy.pid, signal.SIGKILL)
                await self.proxy.wait()
                await self.start_proxy()
                reply, items = await rpc.turn(thread, 'Repeat the marker from the earlier command output without tools.')
                assert marker in reply and not any(i['type'] == 'commandExecution' for i in items)
                self.passed('proxy crash recovery from completed Codex history')
        finally:
            await stop(process)
            rpc.reader.cancel()
            await asyncio.gather(rpc.reader, return_exceptions=True)

    def speed_observation(self, thread, tier):
        journals = [json.loads(p.read_text()) for p in (self.directory / 'state/journals').glob('*.json')]
        matches = [j for j in journals if j['key'][1] == thread and j['key'][-1] == 'chat']
        effective = matches[0]['effective'] if len(matches) == 1 else {}
        # Print only the adapter's fixed speed/status labels, never journal history.
        print(f'SPEED tier={tier} requested={effective.get("requested_speed", "unknown")} '
              f'observed={effective.get("speed") or "unknown"} '
              f'verified_messages={effective.get("verified_messages", 0)} '
              f'failure_status={effective.get("failure_status", "none")}', flush=True)
        return effective

    async def fast_checks(self, rpc, thread, work, marker):
        for tier, expected, prompt in (
            ('default', 'standard', 'Remember the label STANDARD_BASELINE. Reply OK without tools.'),
            ('priority', 'fast', 'Run cat ' + str(work / 'marker.txt') + ' using your shell and report the exact output.'),
            ('default', 'standard', 'Repeat the marker from the earlier command output without tools.'),
        ):
            try:
                reply, items = await rpc.turn(thread, prompt, serviceTier=tier)
            finally:
                effective = self.speed_observation(thread, tier)
            if tier == 'priority':
                assert marker in reply and any(i['type'] == 'commandExecution' for i in items)
            elif 'Repeat' in prompt:
                assert marker in reply and not any(i['type'] == 'commandExecution' for i in items)
            observed = effective.get('speed')
            assert effective['requested_speed'] == expected
            if expected == 'fast':
                assert observed == 'fast', 'Pinned runtime did not report Fast speed'
                assert effective.get('verified_messages', 0) >= 2, 'Must verify tool call and result continuation'
            else:
                assert observed in (None, 'standard'), 'Standard request unexpectedly used Fast'
            self.passed('real Codex Claude speed=' + expected)

    async def run(self):
        if self.args.isolated:
            self.key = 'sk-claude-test-' + secrets.token_hex(20)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                self.base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
            auth_dir = self.directory / 'claude-config'
            auth_dir.mkdir()
            (auth_dir / '.credentials.json').symlink_to(Path.home() / '.claude/.credentials.json')
            self.env.update(CLAUDE_TEST_PROXY_KEY=self.key, CLAUDE_ADAPTER_STATE=str(self.directory / 'state'),
                            CLAUDE_CONFIG_DIR=str(auth_dir),
                            CHATGPT_TOKEN_DIR=str(Path.home() / '.local/state/litellm/chatgpt'))
            await self.start_proxy()
        else:
            self.catalog = self.args.catalog
            if not self.key:
                self.key = subprocess.check_output(['systemd-creds', 'decrypt', '--user', '--name=litellm-proxy-key',
                    str(Path.home() / '.config/litellm/proxy-key.cred'), '-'], text=True).strip()
        try:
            if not self.args.codex_only and not self.args.fast_only:
                await self.api_checks()
            if not self.args.api_only:
                await self.codex_checks()
            if self.args.report:
                self.args.report.write_text(json.dumps({'passed': self.passes, 'complete': True}, indent=2))
        finally:
            await stop(self.proxy)
            for log in self.logs:
                log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated', action='store_true')
    parser.add_argument('--api-only', action='store_true')
    parser.add_argument('--codex-only', action='store_true')
    parser.add_argument('--fast', action='store_true', help='Opt in to paid Claude Fast acceptance, including Codex tool continuation')
    parser.add_argument('--fast-only', action='store_true', help='Run only the paid standard/Fast/standard Codex probe; implies --fast')
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    parser.add_argument('--catalog', type=Path, default=Path.home() / '.config/litellm/codex-models.json')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    args.fast = args.fast or args.fast_only
    if args.fast and (not args.isolated or args.api_only):
        parser.error('--fast/--fast-only requires --isolated and cannot be combined with --api-only')
    os.umask(0o077)
    # Codex requires a home outside /tmp. This directory is removed on success.
    directory = Path(tempfile.mkdtemp(prefix='.claude-acceptance-', dir=ROOT))
    try:
        asyncio.run(Acceptance(args, directory).run())
    except BaseException:
        # Retain diagnostics, not the disposable native login or credential link.
        (directory / 'codex/auth.json').unlink(missing_ok=True)
        (directory / 'claude-config/.credentials.json').unlink(missing_ok=True)
        print('Acceptance failed; diagnostic state retained at ' + str(directory), file=sys.stderr)
        raise
    else:
        import shutil
        shutil.rmtree(directory)


if __name__ == '__main__':
    main()
