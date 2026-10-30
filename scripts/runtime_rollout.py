"""Narrow recovery journal for a locked-runtime deployment, excluding credentials and environments.

Prepare before deploy/deploy.yml, finish after live acceptance. On failure roll back
before provider activation. Restore only the changed managed files, re-sync the
saved dependency lock, and restart the service; never copy an entire environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
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
HOST = 'grace'
UV = shutil.which('uv')
SCOPE = 'full'
BEDROCK_FILES = ('launch.py', 'shared_search.py', 'bedrock_aws.py', 'bedrock-models.json',
    'configure_bedrock.py', 'refresh_models.py', 'verify_bedrock.py', 'verify_bedrock_codex.py', 'verify.py')


def inventory(source, host):
    data = yaml.safe_load((source/'deploy/inventory.yml').read_text())['all']
    if host not in data['hosts']:
        raise ValueError('Unknown deployment host')
    return data.get('vars', {}) | data['hosts'][host]


def configure(source, host):
    global HOME, ROOT, STATE, JOURNAL, HOST, UV
    variables = inventory(source, host)
    home = Path(variables['litellm_home'])
    account = pwd.getpwnam(variables['litellm_user'])
    if socket.gethostname().split('.')[0] != host or os.geteuid() != account.pw_uid or Path(account.pw_dir) != home:
        raise ValueError('Run locally as agent on the explicitly selected inventory host')
    UV = subprocess.check_output([variables['codex_mise'], 'which', 'uv'], cwd=source, text=True).strip()
    HOST, HOME = host, home
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
        if name.startswith('/etc/systemd/system/litellm.service'):
            if value is None:
                subprocess.run(['sudo','-n','rm','-f',name],check=True)
            else:
                subprocess.run(['sudo','-n','tee',name],input=value['text'],text=True,stdout=subprocess.DEVNULL,check=True)
                subprocess.run(['sudo','-n','chmod',oct(value['mode'])[2:],name],check=True)
        elif value is None:path.unlink(missing_ok=True)
        else:write(path,value['text'],value['mode'])


def candidates(source):
    if SCOPE == 'bedrock':
        result = {str(ROOT/'scripts'/name): {'text': (source/'scripts'/name).read_text(), 'mode': 0o700}
                  for name in BEDROCK_FILES}
        variables = inventory(source, HOST)
        templates = Environment(loader=FileSystemLoader(source/'deploy/templates'),
            undefined=StrictUndefined, keep_trailing_newline=True, trim_blocks=True)
        result['/etc/systemd/system/litellm.service.d/40-bedrock.conf'] = {
            'text': templates.get_template('bedrock.conf.j2').render(**variables), 'mode': 0o644}
        return result
    result={str(ROOT/name):{'text':(source/name).read_text(),'mode':0o600} for name in ('pyproject.toml','uv.lock')}
    for path in (source/'scripts').iterdir():
        if path.is_file():result[str(ROOT/'scripts'/path.name)]={'text':path.read_text(),'mode':0o700}
    variables=inventory(source, HOST)
    variables.update(litellm_uv=UV, litellm_codex=str(HOME/f'.local/share/mise/installs/codex/{variables["codex_version"]}/bin/codex'), inventory_hostname=HOST)
    templates=Environment(loader=FileSystemLoader(source/'deploy/templates'),undefined=StrictUndefined,keep_trailing_newline=True,trim_blocks=True)
    templates.filters.update({'bool':bool, 'ternary':lambda flag,yes,no:yes if flag else no})
    templates.filters['to_nice_json'] = lambda value: json.dumps(value, indent=4, sort_keys=True)
    for template,target in [('litellm.service.j2','/etc/systemd/system/litellm.service'),
        ('compaction-recovery.conf.j2','/etc/systemd/system/litellm.service.d/30-compaction-recovery.conf'),
        ('bedrock.conf.j2','/etc/systemd/system/litellm.service.d/40-bedrock.conf'),
        ('vertex.conf.j2','/etc/systemd/system/litellm.service.d/50-vertex.conf')]:
        result[target]={'text':templates.get_template(template).render(**variables),'mode':0o644}
    result[str(HOME/'.config/litellm/deployment.json')] = {
        'text': templates.get_template('deployment.json.j2').render(**variables), 'mode':0o600}
    config = HOME/'.config/litellm/config.yaml'
    if not config.exists():
        result[str(config)] = {'text':templates.get_template('config.yaml.j2').render(**variables), 'mode':0o600}
    return result


def protected():
    paths=[HOME/'.codex/config.toml',HOME/'.config/litellm/config.yaml',HOME/'.config/litellm/codex-models.json']
    paths.extend(STATE/name for name in ('subscription-models.json','modal-models.json','claude-models.json',
        'local-model.json','bedrock-models.json','vertex-models.json','voice-installed.json'))
    # A new installation creates its bootstrap config as a managed candidate.
    if SCOPE == 'bedrock':
        # The model-refresh journal owns these three files. All other provider
        # state remains protected by this journal, including the selected model.
        paths = [p for p in paths if p not in (HOME/'.config/litellm/config.yaml',
            HOME/'.config/litellm/codex-models.json', STATE/'bedrock-models.json')]
        paths.extend(ROOT/name for name in ('pyproject.toml', 'uv.lock', 'scripts/serve.py', 'scripts/live_voice.py'))
    return {str(p):record(p) for p in paths if p != HOME/'.config/litellm/config.yaml' or p.exists()}


def unrelated_models():
    """Allow only Bedrock entries to change during the paired model transaction."""
    values = {}
    for name, field, key, parse in [('config.yaml', 'model_list', 'model_name', yaml.safe_load),
                                  ('codex-models.json', 'models', 'slug', json.loads)]:
        path = HOME/'.config/litellm'/name
        if not path.exists():
            raise ValueError('Bedrock update requires existing model files')
        value = parse(path.read_text())
        if name == 'codex-models.json':
            # Shared catalog enrichment refreshes timestamped price metadata;
            # it does not alter the stock catalog or provider routes.
            from catalog_enrichment import stock_catalog
            value = stock_catalog(value)
        value[field] = sorted([r for r in value.get(field, []) if not r.get(key, '').startswith('bedrock/')],
                              key=lambda r: r[key])
        values[name] = value
    return values


def assert_protected(journal):
    assert_records(journal['protected'])
    if journal.get('scope') == 'bedrock' and unrelated_models() != journal['unrelated_models']:
        raise ValueError('Unrelated model configuration changed; recovery retained')
    if (STATE/'model-refresh').exists():
        raise ValueError('Restore or finish model routes/catalog before runtime recovery')


def prepare(source):
    if JOURNAL.exists():raise ValueError('Runtime upgrade already pending; finish or roll it back first')
    for name in ('voice-rollout.json','model-refresh','codex-runtime.json','cutover.json'):
        if (STATE/name).exists():raise ValueError('Finish the existing deployment transaction first: '+name)
    if subprocess.check_output(['git','status','--porcelain'],cwd=source).strip():
        raise ValueError('Runtime deployment requires committed source')
    after={name:value for name,value in candidates(source).items() if record(Path(name))!=value}
    journal={'schema':1,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
        'host':HOST, 'scope':SCOPE, 'uv':UV, 'service':service_state(), 'environment_existed':(ROOT/'.venv').exists(),
        'before':{name:record(Path(name)) for name in after},'after':after,'protected':protected()}
    if SCOPE == 'bedrock':
        journal['unrelated_models'] = unrelated_models()
        journal['model_before'] = {str(p): record(p) for p in
            (HOME/'.config/litellm/config.yaml', HOME/'.config/litellm/codex-models.json', STATE/'bedrock-models.json')}
    write(JOURNAL,json.dumps(journal))
    return {'prepared':True,'changed_files':len(after),'recovery':str(JOURNAL)}


def load_journal():
    journal=json.loads(JOURNAL.read_text())
    if journal.get('host', 'grace') != HOST:
        raise ValueError('Recovery journal belongs to a different host')
    if journal.get('scope', 'full') != SCOPE:
        raise ValueError('Recovery journal belongs to a different runtime scope')
    return journal


def finish():
    journal=load_journal()
    assert_protected(journal);assert_records(journal['after'])
    manifest = 'bedrock-runtime-installed.json' if SCOPE == 'bedrock' else 'runtime-installed.json'
    write(STATE/manifest,json.dumps({'schema':1,'commit':journal['commit'],
        'lock_sha256':hashlib.sha256((ROOT/'uv.lock').read_bytes()).hexdigest()},indent=2)+'\n')
    JOURNAL.unlink()
    return {'finished':True}


def rollback():
    journal=load_journal()
    assert_protected(journal)
    if journal.get('scope') == 'bedrock':
        assert_records(journal['model_before'])
    for name,before in journal['before'].items():
        if record(Path(name)) not in (before,journal['after'][name]):
            raise ValueError('Concurrent edit; recovery retained: '+name)
    service = journal.get('service', {'active':True, 'enabled':True})
    subprocess.run(['sudo','-n','systemctl','stop','litellm.service'],check=service['active'])
    if not service['enabled']:
        subprocess.run(['sudo','-n','systemctl','disable','litellm.service'],check=False)
    restore(journal['before'])
    if SCOPE == 'bedrock':
        pass  # Targeted script recovery must not sync or replace the environment.
    elif (ROOT/'uv.lock').exists():
        subprocess.run([journal.get('uv', UV),'sync','--locked','--project',str(ROOT)],check=True)
    elif not journal.get('environment_existed', True) and (ROOT/'.venv').exists():
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


def recover_bedrock(source):
    """Restore model files first, then matching scripts with the existing journals."""
    if SCOPE != 'bedrock':
        raise ValueError('Bedrock recovery requires the Bedrock runtime scope')
    load_journal()  # Verify host/scope before touching either journal.
    if (STATE/'model-refresh').exists():
        from configure_bedrock import BedrockSetup
        refresh = BedrockSetup(HOME, ROOT, UV)
        refresh.rollback()
        if refresh.originals() != refresh.journal()['originals']:
            raise ValueError('Model recovery failed; runtime recovery retained')
        shutil.rmtree(refresh.transaction)
    return rollback()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','finish','rollback','recover-bedrock'])
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--host', choices=['grace', 'love'], required=True)
    parser.add_argument('--scope', choices=['full', 'bedrock'], default='full')
    args=parser.parse_args()
    SCOPE = args.scope
    configure(args.source, args.host)
    result = (prepare(args.source) if args.action == 'prepare' else finish() if args.action == 'finish'
              else recover_bedrock(args.source) if args.action == 'recover-bedrock' else rollback())
    print(json.dumps(result))
