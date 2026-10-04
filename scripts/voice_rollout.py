"""Targeted voice cutover/recovery; only two application files and four config values.

Ansible owns restart/health checks. The journal is the sole recovery copy and is
removed after rollback or acceptance; unrelated Codex settings are never restored.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import tomlkit

FILES = ('serve.py', 'live_voice.py')
SETTINGS = {
    'experimental_realtime_webrtc_call_base_url':'http://127.0.0.1:4000/v1',
    'experimental_realtime_ws_base_url':'http://127.0.0.1:4000/v1',
    'realtime.version':'v3', 'realtime.voice':'cove',
}


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as out:
        tmp = Path(out.name)
        out.write(text); out.flush(); os.fsync(out.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def setting(doc, name):
    parts = name.split('.')
    node = doc
    for part in parts[:-1]:
        node = node.get(part, {})
    return {'present':parts[-1] in node, 'value':node.get(parts[-1])}


def change(doc, name, value):
    parts = name.split('.')
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, tomlkit.table())
    if value['present']:
        node[parts[-1]] = value['value']
    else:
        node.pop(parts[-1], None)


class Rollout:
    def __init__(self, home, source):
        self.home, self.source = home, source
        self.state = home / '.local/state/litellm'
        self.journal = self.state / 'voice-rollout.json'
        self.accepted = self.state / 'voice-installed.json'
        self.config = home / '.codex/config.toml'
        self.scripts = home / '.local/share/litellm/scripts'

    def read_file(self, name):
        path = self.scripts / name
        return {'text':path.read_text(), 'mode':path.stat().st_mode & 0o777} if path.exists() else None

    def prepare(self, commit='test'):
        for name in ('model-refresh', 'codex-runtime.json', 'cutover.json'):
            if (self.state / name).exists():
                raise RuntimeError('Finish or recover the existing deployment transaction first')
        if self.journal.exists():
            raise RuntimeError('Voice cutover already pending; verify, finish or roll back it')
        doc = tomlkit.parse(self.config.read_text())
        if doc.get('model_provider') != 'litellm':
            raise RuntimeError('Expected the existing LiteLLM provider')
        before = {name:setting(doc, name) for name in SETTINGS}
        candidate = {name:(self.source / 'scripts' / name).read_text() for name in FILES}
        after = {name:{'text':text,'mode':0o700} for name,text in candidate.items()}
        journal = {'schema':1, 'commit':commit, 'before':{name:self.read_file(name) for name in FILES},
                   'after':after, 'settings_before':before, 'settings_after':SETTINGS,
                   'realtime_table_present':'realtime' in doc}
        write(self.journal, json.dumps(journal))
        for name, record in after.items():
            write(self.scripts / name, record['text'], record['mode'])
        for name, value in SETTINGS.items():
            change(doc, name, {'present':True,'value':value})
        write(self.config, tomlkit.dumps(doc))
        return {'changed':True, 'commit':commit, 'recovery':str(self.journal)}

    def inspect(self):
        journal = json.loads(self.journal.read_text()) if self.journal.exists() else None
        doc = tomlkit.parse(self.config.read_text())
        if journal:
            for name, expected in journal['after'].items():
                if self.read_file(name) != expected:
                    raise RuntimeError('Managed voice application file changed: ' + name)
        for name, value in SETTINGS.items():
            if setting(doc,name) != {'present':True,'value':value}:
                raise RuntimeError('Voice setting mismatch: ' + name)
        return {'changed':False, 'configured':True, 'pending':bool(journal)}

    def rollback(self):
        if not self.journal.exists():
            return {'changed':False}
        journal = json.loads(self.journal.read_text())
        doc = tomlkit.parse(self.config.read_text())
        # Permit an interrupted partial prepare, but never overwrite a later edit.
        for name, before in journal['before'].items():
            if self.read_file(name) not in (before, journal['after'][name]):
                raise RuntimeError('Concurrent application edit; recovery retained: ' + name)
        for name,before in journal['settings_before'].items():
            if setting(doc,name) not in (before, {'present':True,'value':journal['settings_after'][name]}):
                raise RuntimeError('Concurrent voice setting edit; recovery retained: ' + name)
        for name,before in journal['before'].items():
            if before is None:
                (self.scripts / name).unlink(missing_ok=True)
            else:
                write(self.scripts / name, before['text'], before['mode'])
        for name,before in journal['settings_before'].items():
            change(doc,name,before)
        if not journal['realtime_table_present'] and not doc.get('realtime'):
            doc.pop('realtime',None)
        write(self.config,tomlkit.dumps(doc))
        self.journal.unlink()
        return {'changed':True,'restored':True}

    def finish(self, receipt, desktop):
        self.inspect()
        if not desktop:
            raise RuntimeError('Actual desktop voice acceptance is required')
        evidence = json.loads(receipt.read_text())
        if (not evidence.get('ok') or not evidence.get('connected') or not evidence.get('stopped')
                or evidence.get('errors') or evidence.get('control_transport') != 'remote-wss'
                or evidence.get('transport') != 'webrtc' or evidence.get('audible_frames', 0) <= 0
                or not any(c.get('exitCode') == 0 for c in evidence.get('commands', []))):
            raise RuntimeError('Passing live voice/tool receipt required')
        journal = json.loads(self.journal.read_text())
        write(self.accepted,json.dumps({'schema':1,'commit':journal['commit'],'desktop_validated':True,
            'receipt':str(receipt),'receipt_sha256':hashlib.sha256(receipt.read_bytes()).hexdigest(),
            'settings':SETTINGS},indent=2)+'\n')
        self.journal.unlink()
        return {'changed':True,'accepted':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['preview','prepare','verify','rollback','finish'])
    parser.add_argument('--home',type=Path,default=Path('/home/agent'))
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--receipt',type=Path)
    parser.add_argument('--desktop-validated',action='store_true')
    args=parser.parse_args()
    if args.action == 'finish' and args.receipt is None:
        parser.error('finish requires --receipt')
    rollout=Rollout(args.home,args.source)
    if args.action=='preview':
        print(json.dumps({'files':list(FILES),'settings':SETTINGS,'pending':rollout.journal.exists()}));return
    rollout.state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (rollout.state/'voice-rollout.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='prepare':
            if subprocess.check_output(['git','status','--porcelain'],cwd=args.source).strip():
                raise RuntimeError('Voice deployment requires committed source')
            commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.source,text=True).strip()
            result=rollout.prepare(commit)
        elif args.action=='verify':result=rollout.inspect()
        elif args.action=='rollback':result=rollout.rollback()
        else:result=rollout.finish(args.receipt,args.desktop_validated)
    print(json.dumps(result))


if __name__=='__main__':
    main()
