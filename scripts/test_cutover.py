"""Exercise preservation and conflict-safe rollback using disposable config."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import shutil

script = Path(__file__).with_name('cutover.py')
project = script.parent.parent
uv = shutil.which('uv')
with tempfile.TemporaryDirectory(prefix='litellm-cutover-test-') as tmp:
    home = Path(tmp)
    config = home / '.codex/config.toml'
    config.parent.mkdir()
    catalog = home / '.config/litellm/codex-models.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({'models': [{'slug': 'chatgpt/gpt-6-astra', 'use_responses_lite': False}]}))
    original = '''# BEGIN CODEX GATEWAY MANAGED
openai_base_url = "http://127.0.0.1:48766/v1"
# END CODEX GATEWAY MANAGED
model = "original"
model_provider = "openai"
[plugins.example]
enabled = true
'''
    config.write_text(original)
    def run(action, success=True):
        result = subprocess.run([uv, 'run', '--no-sync', '--project', str(project), str(script), action, '--home', tmp], capture_output=True, text=True)
        assert (result.returncode == 0) == success, result.stderr
    run('prepare')
    prepared = config.read_text()
    assert 'enabled = true' in prepared and '48766' not in prepared
    assert 'systemd-creds' not in prepared and 'env_key = "LITELLM_PROXY_KEY"' in prepared
    assert 'requires_openai_auth = true' in prepared
    assert 'exclude = ["LITELLM_PROXY_KEY"]' in prepared
    assert str(catalog) in prepared and 'web_search = "live"' in prepared
    run('prepare')
    run('check-restart', False)
    config.write_text(prepared + '\n# concurrent edit\n')
    run('rollback', False)
    assert 'concurrent edit' in config.read_text()
    config.write_text(prepared)
    run('rollback')
    assert config.read_text() == original
    assert not (home / '.local/state/litellm/cutover.json').exists()
print('PASS prepare, retry, restart gate, edit preservation, and rollback')
