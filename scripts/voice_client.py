# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["aiortc==1.15.0", "websockets==15.0.1"]
# ///
"""Stock Codex V3 WebRTC reference client; run with uv run --script.

Connects only to the app-server endpoint. It never reads subscription or gateway
credentials. See docs/VOICE.md for contract, acceptance, and client boundaries.
Input/output are WAV files so the same harness runs on headless Grace or a laptop.
"""
import argparse
import array
import asyncio
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import time

import av
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaRecorder
from websockets.asyncio.client import connect, unix_connect


def load_audio(path):
    if not path:
        return b''
    resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
    result = bytearray()
    with av.open(str(path)) as source:
        for frame in source.decode(audio=0):
            for frame in resampler.resample(frame):
                result.extend(bytes(frame.planes[0])[:frame.samples * 2])
            if len(result) > 64 * 1024 * 1024:
                raise ValueError('Input audio exceeds the reference-client limit')
    for frame in resampler.resample(None):
        result.extend(bytes(frame.planes[0])[:frame.samples * 2])
    return bytes(result)


class InputTrack(MediaStreamTrack):
    kind = 'audio'
    def __init__(self, data, ready):
        super().__init__()
        self.data, self.ready = data, ready
        self.offset = self.clock = 0
        self.started = None
    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started + self.clock / 48000 - time.monotonic()))
        data = b''
        if self.ready.is_set():
            data = self.data[self.offset:self.offset + 1920]
            self.offset += len(data)
        frame = av.AudioFrame(format='s16', layout='mono', samples=960)
        frame.planes[0].update(data.ljust(1920, b'\0'))
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.clock, Fraction(1, 48000)
        self.clock += 960
        return frame


class OutputTrack(MediaStreamTrack):
    kind = 'audio'
    def __init__(self, source, report):
        super().__init__()
        self.source, self.report = source, report
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
    async def recv(self):
        frame = await self.source.recv()
        self.report['audio_frames'] += 1
        for mono in self.resampler.resample(frame):
            samples = array.array('h', bytes(mono.planes[0])[:mono.samples * 2])
            if samples and max(abs(n) for n in samples) > 100:
                self.report['audible_frames'] += 1
        return frame


class RPC:
    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.reader = asyncio.create_task(self.read())
    async def read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if 'id' in message and ('result' in message or 'error' in message):
                    future = self.pending.pop(message['id'], None)
                    if future and not future.done():
                        future.set_result(message)
                else:
                    await self.events.put(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('App-server connection closed'))
    async def call(self, method, params):
        self.counter += 1
        identifier = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        await self.ws.send(json.dumps({'id':identifier, 'method':method, 'params':params}))
        response = await asyncio.wait_for(future, 60)
        if 'error' in response:
            raise RuntimeError('RPC failed: ' + method + ' code=' + str(response['error'].get('code')))
        return response['result']
    async def close(self):
        await self.ws.close()
        await asyncio.gather(self.reader, return_exceptions=True)


def credential(args):
    if args.token_env:
        token = os.environ.get(args.token_env, '')
    else:
        result = subprocess.run(['systemd-creds', 'decrypt', '--user', '--name=connection-token',
                                 str(args.credential), '-'], capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('Cannot load the app-server connection credential')
        token = result.stdout.decode().strip()
    if not token or any(c.isspace() for c in token):
        raise RuntimeError('Missing or invalid app-server connection credential')
    return token


async def run(args):
    audio = load_audio(args.input)
    if args.socket:
        ws = await unix_connect(str(args.socket), uri='ws://localhost', compression=None, max_size=16 * 1024 * 1024)
    else:
        if not args.url.startswith('wss://'):
            raise ValueError('Remote connections require WSS')
        ws = await connect(args.url, additional_headers={'Authorization':'Bearer ' + credential(args)},
                           compression=None, max_size=16 * 1024 * 1024, open_timeout=20)
    rpc = RPC(ws)
    pc = RTCPeerConnection()
    recorder = MediaRecorder(str(args.output))
    ready = asyncio.Event()
    report = {'schema':1, 'transport':'webrtc', 'control_transport':'unix' if args.socket else 'remote-wss', 'voice':'cove', 'connected':False,
              'audio_frames':0, 'audible_frames':0, 'transcripts':[], 'commands':[], 'errors':[], 'stopped':False}
    pc.addTrack(InputTrack(audio, ready))
    channel = pc.createDataChannel('oai-events')
    @channel.on('message')
    def data(raw):
        event = json.loads(raw)
        if event.get('type') == 'session.started':
            report['connected'] = True
            ready.set()
        if event.get('type') == 'error':
            report['errors'].append({'source':'voice', 'code':event.get('error', {}).get('code')})
    @pc.on('track')
    async def track(source):
        if source.kind == 'audio':
            recorder.addTrack(OutputTrack(source, report))
            await recorder.start()
    tid = None
    try:
        await rpc.call('initialize', {'clientInfo':{'name':'subscription_voice_reference','version':'1'},
                                     'capabilities':{'experimentalApi':True}})
        await ws.send('{"method":"initialized"}')
        config = {}
        if args.voice_base:
            config.update(experimental_realtime_webrtc_call_base_url=args.voice_base,
                          experimental_realtime_ws_base_url=args.voice_base)
        params = {'cwd':str(args.cwd), 'approvalPolicy':'never', 'sandbox':'read-only', 'config':config}
        if args.model:
            params['model'] = args.model
        # 'never' means approval-requiring actions fail; it does not grant approval.
        if args.thread_id:
            params['threadId'] = args.thread_id
            result = await rpc.call('thread/resume', params)
        else:
            params['ephemeral'] = True
            result = await rpc.call('thread/start', params)
        tid = result['thread']['id']
        report['thread_id'] = tid
        await pc.setLocalDescription(await pc.createOffer())
        await rpc.call('thread/realtime/start', {'threadId':tid, 'version':'v3', 'voice':'cove',
                       'outputModality':'audio', 'includeStartupContext':False,
                       'transport':{'type':'webrtc','sdp':pc.localDescription.sdp}})
        deadline = time.monotonic() + args.duration
        said = False
        while time.monotonic() < deadline:
            if ready.is_set() and args.say and not said:
                await rpc.call('thread/realtime/appendText', {'threadId':tid, 'text':args.say})
                said = True
            try:
                event = await asyncio.wait_for(rpc.events.get(), 0.25)
            except asyncio.TimeoutError:
                continue
            method, p = event.get('method', ''), event.get('params', {})
            if method == 'thread/realtime/sdp':
                await pc.setRemoteDescription(RTCSessionDescription(sdp=p['sdp'], type='answer'))
            elif method == 'thread/realtime/transcript/done':
                report['transcripts'].append({'role':p['role'], 'text':p['text']})
                print(p['role'] + ': ' + p['text'], flush=True)
            elif method == 'item/completed' and p.get('item', {}).get('type') == 'commandExecution':
                item = p['item']
                report['commands'].append({k:item.get(k) for k in ('command','aggregatedOutput','exitCode')})
            elif method == 'thread/realtime/error':
                report['errors'].append({'source':'codex', 'code':'realtime_error'})
                break
            elif 'id' in event and 'method' in event:
                # Do not automatically grant permission. A normal client routes this to its UI.
                report['errors'].append({'source':'approval', 'code':'requires_interactive_client'})
                break
    finally:
        if tid:
            try:
                await rpc.call('thread/realtime/stop', {'threadId':tid})
                report['stopped'] = True
            except Exception:
                report['errors'].append({'source':'cleanup', 'code':'stop_unconfirmed'})
        await recorder.stop()
        await pc.close()
        await rpc.close()
        report['ok'] = report['connected'] and report['audible_frames'] > 0 and report['stopped'] and not report['errors']
        if args.require_tool:
            report['ok'] = report['ok'] and any(c['exitCode'] == 0 for c in report['commands'])
        if args.expect:
            normalize = lambda x: ''.join(c for c in x.lower() if c.isalnum())
            spoken = [t['text'] for t in report['transcripts'] if t['role'] == 'assistant']
            report['ok'] = report['ok'] and any(normalize(args.expect) in normalize(t) for t in spoken)
            if args.require_tool:
                report['ok'] = report['ok'] and any(args.expect in (c['aggregatedOutput'] or '') for c in report['commands'])
        args.receipt.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k:report[k] for k in ('ok','connected','audio_frames','audible_frames','stopped','errors')}))
    return 0 if report['ok'] else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='wss://grace.taila198f.ts.net/codex/rpc')
    parser.add_argument('--credential', type=Path, default=Path('/home/agent/.local/share/remote-codex/connection-token.cred'))
    parser.add_argument('--token-env', help='Portable alternative: environment variable containing the app-server connection token')
    parser.add_argument('--socket', type=Path, help='Diagnostic local Unix app-server socket instead of remote WSS')
    parser.add_argument('--voice-base', help='Diagnostic per-thread voice gateway override')
    parser.add_argument('--thread-id')
    parser.add_argument('--model', help='Optional existing Codex text-model alias')
    parser.add_argument('--cwd', type=Path, default=Path('/tmp'))
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=45)
    parser.add_argument('--say', help='Text to append, useful only to generate a synthetic spoken test fixture')
    parser.add_argument('--expect', help='Required phrase in the spoken answer')
    parser.add_argument('--require-tool', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.duration <= 300:
        parser.error('duration must be between 1 and 300 seconds')
    try:
        return asyncio.run(run(args))
    except Exception as error:
        # Network/library exception strings can contain credentials or SDP.
        print(json.dumps({'ok':False, 'error_type':type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
