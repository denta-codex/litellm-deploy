"""Configure the suggestions-only Cerebras route using the shared activation transaction."""
import copy
from pathlib import Path
import re
import subprocess

from refresh_models import Refresh, atomic_write, main

PREFIX = 'cerebras/'
MODEL = 'qwen-3.8-27b'
ROUTE = PREFIX + MODEL


def generate(source, config, catalog, selected):
    if source != {'model': MODEL}:
        raise ValueError('Unexpected Cerebras model identifier')
    route = {'model_name': ROUTE, 'model_info': {'mode': 'chat'}, 'litellm_params': {
        'model': ROUTE, 'api_key': 'os.environ/CEREBRAS_API_KEY',
        'reasoning_effort': 'none', 'max_retries': 0,
        'extra_headers': {'User-Agent': 'remote-codex-suggestions/1'},
    }}
    current = next((r for r in config.get('model_list', []) if r['model_name'] == ROUTE), None)
    result = copy.deepcopy(config)
    result['model_list'] = [r for r in result.get('model_list', []) if r['model_name'] != ROUTE] + [route]
    retry = result.setdefault('router_settings', {}).setdefault('model_group_retry_policy', {})
    previous_retry = retry.get(ROUTE)
    retry[ROUTE] = {'DefaultRetries': 0, 'BadRequestErrorRetries': 0,
        'AuthenticationErrorRetries': 0, 'TimeoutErrorRetries': 0, 'RateLimitErrorRetries': 0,
        'ContentPolicyViolationErrorRetries': 0, 'InternalServerErrorRetries': 0,
        'ServiceUnavailableErrorRetries': 0}
    changed = current != route or previous_retry != retry[ROUTE]
    # This route generates UI text; it does not advertise a coding model in the picker.
    return result, copy.deepcopy(catalog), {
        'added': [ROUTE] if current is None else [], 'removed': [],
        'changed': [ROUTE] if current is not None and changed else [],
        'changed_fields': {}, 'before': [], 'after': [], 'test_models': [],
    }


class CerebrasSetup(Refresh):
    prefix = PREFIX
    playbook = 'cerebras.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home, root, uv)
        self.paths['snapshot'] = self.state / 'cerebras-model.json'

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        if not re.fullmatch(r'[A-Za-z0-9.+_-]+', version):
            raise ValueError('Cannot determine installed Codex version')
        return {'version': 1, 'codex_version': version, 'catalog': {'model': MODEL}}

    def candidate(self, snapshot, refresh_pricing=False):
        return super().candidate(snapshot)

    def validate(self):
        self.journal()
        result = subprocess.run([self.uv, 'run', '--no-sync', '--project', str(self.root),
            str(Path(__file__).with_name('verify_cerebras.py'))], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Cerebras suggestion route failed inference validation; restore previous routes')
        atomic_write(self.transaction / 'validated', 'yes\n')
        return {'validated': [ROUTE]}


if __name__ == '__main__':
    main(CerebrasSetup)
