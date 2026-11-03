"""Narrow recovery journal for a locked-runtime deployment, excluding credentials and environments.

Prepare before deploy/deploy.yml, finish after live acceptance. On failure roll back
before activating new models. Restore only the changed managed files (including
files the deployment removes), re-sync the saved dependency lock, and restart the
service; never copy an entire environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
import urllib.request

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

import deploy_config
from voice_rollout import write

HOME = Path.home()
ROOT = HOME/'.local/share/litellm'
STATE = HOME/'.local/state/litellm'
JOURNAL = STATE/'runtime-upgrade.json'
UV = shutil.which('uv')
CONFIG = None
UNIT = '/etc/systemd/system/litellm.service'


def configure(path):
    global HOME, ROOT, STATE, JOURNAL, UV, CONFIG
    config = deploy_config.load(path)
    variables = deploy_config.variables(config)
    home = Path(variables['litellm_home'])
    account = pwd.getpwnam(variables['litellm_user'])
    if os.geteuid() != account.pw_uid or Path(account.pw_dir) != home:
        raise ValueError('Run as the configured user, whose home must match the configuration')
    UV = subprocess.check_output([variables['codex_mise'], 'which', 'uv'], cwd=home, text=True).strip()
    CONFIG, HOME = config, home
    ROOT, STATE = Path(variables['litellm_root']), Path(variables['litellm_state'])
    JOURNAL = STATE/'runtime-upgrade.json'


def service_state():
    def state(operation):
        result = subprocess.run(['systemctl', operation, 'litellm.service'], capture_output=True, text=True)
        return result.stdout.strip()
    return {'active': state('is-active') == 'active', 'enabled': state('is-enabled') == 'enabled'}


def record(path):
    return {'text':path.read_text(),'mode':path.stat().st_mode & 0o777} if path.exists() else None


def assert_records(expected):
    for name,value in expected.items():
        if record(Path(name)) != value:raise ValueError('Managed file changed: '+name)


def restore(records):
    for name,value in records.items():
        path=Path(name)
        if name.startswith(UNIT):
            if value is None:
                subprocess.run(['sudo','-n','rm','-f',name],check=True)
            else:
                subprocess.run(['sudo','-n','tee',name],input=value['text'],text=True,stdout=subprocess.DEVNULL,check=True)
                subprocess.run(['sudo','-n','chmod',oct(value['mode'])[2:],name],check=True)
        elif value is None:path.unlink(missing_ok=True)
        else:write(path,value['text'],value['mode'])


def templates(source):
    result = Environment(loader=FileSystemLoader(source/'deploy/templates'), undefined=StrictUndefined,
                         keep_trailing_newline=True, trim_blocks=True)
    result.filters.update({'bool': bool, 'ternary': lambda flag, yes, no: yes if flag else no,
                           'to_nice_json': lambda value: json.dumps(value, indent=4, sort_keys=True)})
    return result


def candidates(source):
    """Every managed file the deployment writes, with ``None`` for files it removes."""
    result={str(ROOT/name):{'text':(source/name).read_text(),'mode':0o600} for name in ('pyproject.toml','uv.lock')}
    for path in (source/'scripts').iterdir():
        if path.is_file():result[str(ROOT/'scripts'/path.name)]={'text':path.read_text(),'mode':0o700}
    retired = yaml.safe_load((source/'deploy/retired.yml').read_text())
    result.update({str(ROOT/'scripts'/name): None for name in retired['scripts']})
    result.update({f'{UNIT}.d/{name}': None for name in retired['dropins']})
    variables = deploy_config.variables(CONFIG) | {'litellm_uv': UV}
    render = templates(source)
    config = HOME/'.config/litellm'
    result[UNIT] = {'text': render.get_template('litellm.service.j2').render(**variables), 'mode': 0o644}
    result[str(config/'deployment.json')] = {
        'text': render.get_template('deployment.json.j2').render(**variables), 'mode': 0o600}
    result[str(config/'credentials.json')] = {
        'text': render.get_template('credentials.json.j2').render(**variables), 'mode': 0o600}
    if not (config/'config.yaml').exists():
        result[str(config/'config.yaml')] = {'text': render.get_template('config.yaml.j2').render(**variables),
                                            'mode': 0o600}
    return result


def protected():
    """Model and client state owned by other transactions must not change underneath this one."""
    config = HOME/'.config/litellm'
    paths = [HOME/'.codex/config.toml', config/'config.yaml', config/'codex-models.json', config/'models.json',
             STATE/'subscription-models.json', STATE/'voice-installed.json']
    # A new installation creates its bootstrap config as a managed candidate.
    return {str(p):record(p) for p in paths if p != config/'config.yaml' or p.exists()}


def assert_protected(journal):
    assert_records(journal['protected'])
    if (STATE/'model-refresh').exists():
        raise ValueError('Restore or finish model routes/catalog before runtime recovery')


def prepare(source):
    if JOURNAL.exists():raise ValueError('Runtime upgrade already pending; finish or roll it back first')
    for name in ('voice-rollout.json','model-refresh','codex-runtime.json'):
        if (STATE/name).exists():raise ValueError('Finish the existing deployment transaction first: '+name)
    if subprocess.check_output(['git','status','--porcelain'],cwd=source).strip():
        raise ValueError('Runtime deployment requires committed source')
    after={name:value for name,value in candidates(source).items() if record(Path(name))!=value}
    journal={'schema':2,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
        'home':str(HOME), 'uv':UV, 'service':service_state(), 'environment_existed':(ROOT/'.venv').exists(),
        'before':{name:record(Path(name)) for name in after},'after':after,'protected':protected()}
    write(JOURNAL,json.dumps(journal))
    return {'prepared':True,'changed_files':len(after),'recovery':str(JOURNAL)}


def load_journal():
    journal=json.loads(JOURNAL.read_text())
    if journal.get('schema') != 2 or journal.get('home') != str(HOME):
        raise ValueError('Recovery journal belongs to a different configuration or rollout layout')
    return journal


def finish():
    journal=load_journal()
    assert_protected(journal);assert_records(journal['after'])
    write(STATE/'runtime-installed.json',json.dumps({'schema':1,'commit':journal['commit'],
        'lock_sha256':hashlib.sha256((ROOT/'uv.lock').read_bytes()).hexdigest()},indent=2)+'\n')
    JOURNAL.unlink()
    return {'finished':True}


def rollback():
    journal=load_journal()
    assert_protected(journal)
    for name,before in journal['before'].items():
        if record(Path(name)) not in (before,journal['after'][name]):
            raise ValueError('Concurrent edit; recovery retained: '+name)
    service = journal['service']
    subprocess.run(['sudo','-n','systemctl','stop','litellm.service'],check=service['active'])
    if not service['enabled']:
        subprocess.run(['sudo','-n','systemctl','disable','litellm.service'],check=False)
    restore(journal['before'])
    if (ROOT/'uv.lock').exists():
        subprocess.run([journal['uv'],'sync','--locked','--project',str(ROOT)],check=True)
    elif not journal['environment_existed'] and (ROOT/'.venv').exists():
        shutil.rmtree(ROOT/'.venv')
    subprocess.run(['sudo','-n','systemctl','daemon-reload'],check=True)
    if service['enabled']:
        subprocess.run(['sudo','-n','systemctl','enable','litellm.service'],check=True)
    if service['active']:
        subprocess.run(['sudo','-n','systemctl','restart','litellm.service'],check=True)
        wait_for_health()
    assert_records(journal['before'])
    JOURNAL.unlink()
    return {'restored':True}


def wait_for_health():
    for attempt in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness',timeout=1) as response:
                if response.status==200:break
        except OSError:pass
        time.sleep(1)
    else:raise ValueError('Restored runtime is not healthy; recovery retained')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','finish','rollback'])
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--config',type=Path,default=deploy_config.DEFAULT)
    args=parser.parse_args()
    configure(args.config)
    result = prepare(args.source) if args.action == 'prepare' else finish() if args.action == 'finish' else rollback()
    print(json.dumps(result))
