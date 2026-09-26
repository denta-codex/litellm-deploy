"""Narrow, recoverable Codex config change. Run with the locked uv environment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomlkit

p = argparse.ArgumentParser()
p.add_argument('action', choices=['prepare', 'rollback', 'check-restart', 'finish'])
p.add_argument('--home', type=Path, default=Path('/home/agent'))
p.add_argument('--model', default='chatgpt/gpt-6-astra')
args = p.parse_args()
os.umask(0o077)
config = args.home / '.codex/config.toml'
state = args.home / '.local/state/litellm/cutover.json'
socket = args.home / '.codex/app-server-control/app-server-control.sock'

def identity():
    if not socket.exists():
        return None
    s = socket.stat()
    return [s.st_ino, s.st_mtime_ns, s.st_ctime_ns]

def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
        temporary = f.name
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

if args.action == 'prepare':
    original = config.read_text()
    if state.exists():
        pending = json.loads(state.read_text())
        assert digest(original) == pending['prepared_sha256'], 'Pending cutover differs; review before retry'
        print('Cutover already prepared; restart Grace through the owning desktop.')
    else:
        cleaned = re.sub(r'^# BEGIN CODEX GATEWAY MANAGED\n.*?^# END CODEX GATEWAY MANAGED\n', '', original, flags=re.M | re.S)
        doc = tomlkit.parse(cleaned)
        doc.pop('openai_base_url', None)
        doc['model_provider'] = 'litellm'
        doc['model'] = args.model
        catalog = args.home / '.config/litellm/codex-models.json'
        entries = json.loads(catalog.read_text())['models']
        assert any(m['slug'] == args.model and not m['use_responses_lite'] for m in entries), 'Model needs validated hosted Responses metadata'
        assert 'model_catalog_json' not in doc, 'Existing custom model catalog requires review'
        doc['model_catalog_json'] = str(catalog)
        doc['web_search'] = 'live'
        providers = doc.setdefault('model_providers', tomlkit.table())
        if 'litellm' in providers:
            raise RuntimeError('An unmanaged litellm provider already exists; refusing to overwrite')
        providers['litellm'] = {
            'name': 'LiteLLM', 'base_url': 'http://127.0.0.1:4000/v1',
            'wire_api': 'responses', 'supports_websockets': False,
            'auth': {'command': '/usr/bin/systemd-creds',
                     'args': ['decrypt', '--user', '--name=litellm-proxy-key',
                              str(args.home / '.config/litellm/proxy-key.cred'), '-'],
                     'refresh_interval_ms': 0},
        }
        prepared = tomlkit.dumps(doc)
        tomlkit.parse(prepared)
        write(state, json.dumps({'original': original, 'prepared_sha256': digest(prepared),
                                'socket_before': identity(), 'model': args.model}))
        write(config, prepared)
        print('Prepared. Restart Grace through the owning desktop, then validate before finalize.')
elif args.action == 'rollback':
    pending = json.loads(state.read_text())
    assert digest(config.read_text()) == pending['prepared_sha256'], 'Config changed since prepare; refusing to overwrite newer edits'
    write(config, pending['original'])
    state.unlink()
    print('Restored prior config. Restart Grace through the owning desktop.')
else:
    pending = json.loads(state.read_text())
    current = tomlkit.parse(config.read_text())
    assert current['model_provider'] == 'litellm', 'Default provider changed'
    assert current['model'] == pending['model'], 'Default model changed'
    assert current['model_providers']['litellm']['base_url'] == 'http://127.0.0.1:4000/v1'
    assert identity() is not None and identity() != pending['socket_before'], 'Owning desktop has not replaced the app server'
    if args.action == 'finish':
        state.unlink()
        print('Removed completed cutover recovery state.')
    else:
        print('PASS app-server socket replaced and LiteLLM routing configured.')
