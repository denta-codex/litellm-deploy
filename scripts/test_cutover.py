"""Exercise preservation and conflict-safe rollback using disposable config."""
import os
from pathlib import Path
import subprocess
import tempfile

script = Path(__file__).with_name('cutover.py')
project = script.parent.parent
uv = '/home/agent/.local/share/mise/installs/uv/0.12.11/.mise-bins/uv'
with tempfile.TemporaryDirectory(prefix='litellm-cutover-test-') as tmp:
    home = Path(tmp)
    config = home / '.codex/config.toml'
    config.parent.mkdir()
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
    assert 'systemd-creds' in prepared and 'env_key' not in prepared
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
