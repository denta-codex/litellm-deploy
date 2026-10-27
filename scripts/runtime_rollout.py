"""Narrow recovery journal for a locked-runtime deployment, excluding credentials and environments.

Prepare before deploy/deploy.yml, finish after live acceptance. On failure roll back
before provider activation. Restore only the changed managed files, re-sync the
saved dependency lock, and restart the service; never copy an entire environment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml
from voice_rollout import write

HOME = Path('/home/agent')
ROOT = HOME/'.local/share/litellm'
STATE = HOME/'.local/state/litellm'
JOURNAL = STATE/'runtime-upgrade.json'


def record(path):
    return {'text':path.read_text(),'mode':path.stat().st_mode & 0o777} if path.exists() else None


def assert_records(expected):
    for name,value in expected.items():
        if record(Path(name)) != value:raise ValueError('Managed file changed: '+name)


def restore(records):
    for name,value in records.items():
        path=Path(name)
        if name.startswith('/etc/systemd/system/litellm.service'):
            if value is None:
                subprocess.run(['sudo','-n','rm','-f',name],check=True)
            else:
                subprocess.run(['sudo','-n','tee',name],input=value['text'],text=True,stdout=subprocess.DEVNULL,check=True)
                subprocess.run(['sudo','-n','chmod',oct(value['mode'])[2:],name],check=True)
        elif value is None:path.unlink(missing_ok=True)
        else:write(path,value['text'],value['mode'])


def candidates(source):
    result={str(ROOT/name):{'text':(source/name).read_text(),'mode':0o600} for name in ('pyproject.toml','uv.lock')}
    for path in (source/'scripts').iterdir():
        if path.is_file():result[str(ROOT/'scripts'/path.name)]={'text':path.read_text(),'mode':0o700}
    variables=yaml.safe_load((source/'deploy/inventory.yml').read_text())['all']['hosts']['grace']
    templates=Environment(loader=FileSystemLoader(source/'deploy/templates'),undefined=StrictUndefined,keep_trailing_newline=True,trim_blocks=True)
    for template,target in [('litellm.service.j2','/etc/systemd/system/litellm.service'),
        ('compaction-recovery.conf.j2','/etc/systemd/system/litellm.service.d/30-compaction-recovery.conf'),
        ('bedrock.conf.j2','/etc/systemd/system/litellm.service.d/40-bedrock.conf'),
        ('vertex.conf.j2','/etc/systemd/system/litellm.service.d/50-vertex.conf')]:
        result[target]={'text':templates.get_template(template).render(**variables),'mode':0o644}
    return result


def protected():
    paths=[HOME/'.codex/config.toml',HOME/'.config/litellm/config.yaml',HOME/'.config/litellm/codex-models.json']
    paths.extend(STATE/name for name in ('subscription-models.json','modal-models.json','claude-models.json',
        'local-model.json','bedrock-models.json','vertex-models.json','voice-installed.json'))
    return {str(p):record(p) for p in paths}


def prepare(source):
    if JOURNAL.exists():raise ValueError('Runtime upgrade already pending; finish or roll it back first')
    for name in ('voice-rollout.json','model-refresh','codex-runtime.json','cutover.json'):
        if (STATE/name).exists():raise ValueError('Finish the existing deployment transaction first: '+name)
    if subprocess.check_output(['git','status','--porcelain'],cwd=source).strip():
        raise ValueError('Runtime deployment requires committed source')
    after={name:value for name,value in candidates(source).items() if record(Path(name))!=value}
    journal={'schema':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
        'before':{name:record(Path(name)) for name in after},'after':after,'protected':protected()}
    write(JOURNAL,json.dumps(journal))
    return {'prepared':True,'changed_files':len(after),'recovery':str(JOURNAL)}


def finish():
    journal=json.loads(JOURNAL.read_text())
    assert_records(journal['protected']);assert_records(journal['after'])
    write(STATE/'runtime-installed.json',json.dumps({'schema':1,'commit':journal['commit'],
        'lock_sha256':hashlib.sha256((ROOT/'uv.lock').read_bytes()).hexdigest()},indent=2)+'\n')
    JOURNAL.unlink()
    return {'finished':True}


def rollback():
    journal=json.loads(JOURNAL.read_text())
    assert_records(journal['protected'])
    for name,before in journal['before'].items():
        if record(Path(name)) not in (before,journal['after'][name]):
            raise ValueError('Concurrent edit; recovery retained: '+name)
    subprocess.run(['sudo','-n','systemctl','stop','litellm.service'],check=True)
    restore(journal['before'])
    uv=str(HOME/'.local/share/mise/installs/uv/0.12.11/.mise-bins/uv')
    subprocess.run([uv,'sync','--locked','--project',str(ROOT)],check=True)
    subprocess.run(['sudo','-n','systemctl','daemon-reload'],check=True)
    subprocess.run(['sudo','-n','systemctl','restart','litellm.service'],check=True)
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness',timeout=1) as response:
                if response.status==200:break
        except OSError:pass
        time.sleep(1)
    else:raise ValueError('Restored runtime is not healthy; recovery retained')
    assert_records(journal['before'])
    JOURNAL.unlink()
    return {'restored':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','finish','rollback'])
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent.parent)
    args=parser.parse_args()
    result=prepare(args.source) if args.action=='prepare' else finish() if args.action=='finish' else rollback()
    print(json.dumps(result))
