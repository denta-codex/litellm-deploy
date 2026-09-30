"""Configure the Claude subscription route using the shared activation transaction."""
import copy
import json
from pathlib import Path
import subprocess

from refresh_models import Refresh, atomic_write, main

MANIFEST = Path(__file__).with_name('claude-models.json')
PREFIX = 'claude/'


def generate(source, config, catalog, selected):
    expected = json.loads(MANIFEST.read_text())
    if source != expected:
        raise ValueError('Claude snapshot differs from the pinned manifest; run claude.yml to validate changes')
    spec, = source['models']
    name = spec['slug']
    if selected.startswith(PREFIX) and selected != name:
        raise ValueError('Selected Claude model disappeared; select another model before configuring')
    route = {'model_name': name, 'model_info': {'mode': 'chat'}, 'litellm_params': {
        'model': 'claudesdk/' + spec['model'], 'use_chat_completions_api': True,
        'allowed_openai_params': ['reasoning_effort', 'response_format', 'parallel_tool_calls'],
        'num_retries': 0,
    }}
    entry = {
        'slug': name, 'display_name': spec['display_name'],
        'description': 'Claude Opus 5.5 through your Claude subscription.',
        'base_instructions': "You are Codex, a coding agent. Follow the user's request and use tools when needed.",
        'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
        'context_window': spec['context_window'], 'max_context_window': spec['context_window'],
        'truncation_policy': {'mode': 'tokens', 'limit': 10000}, 'input_modalities': ['text', 'image'],
        'supported_reasoning_levels': [{'effort': effort, 'description': effort} for effort in spec['reasoning_levels']],
        'default_reasoning_level': spec['default_reasoning_level'], 'default_reasoning_summary': 'auto',
        'supports_parallel_tool_calls': True, 'supports_reasoning_summaries': True, 'supports_search_tool': True,
        'web_search_tool_type': 'text_and_image',
        'supports_reasoning_summary_parameter': False, 'supports_reasoning_effort_updates': True,
        'supports_image_detail_original': False, 'support_verbosity': False,
        'use_responses_lite': False, 'prefer_websockets': False,
        'tool_mode': 'code_mode_only', 'shell_type': 'shell_command', 'apply_patch_tool_type': 'freeform',
        'experimental_supported_tools': [], 'service_tiers': [], 'additional_speed_tiers': [],
        'availability_nux': None, 'available_access_programs': {'cyber': []}, 'priority': 80,
    }
    before = {m['slug']: m for m in catalog.get('models', []) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']: r for r in config.get('model_list', []) if r['model_name'].startswith(PREFIX)}
    new_config, new_catalog = copy.deepcopy(config), copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list', []) if not r['model_name'].startswith(PREFIX)] + [route]
    new_catalog['models'] = [m for m in new_catalog.get('models', []) if not m['slug'].startswith(PREFIX)] + [entry]
    changed = name in before and (before[name] != entry or old_routes.get(name) != route)
    fields = sorted(k for k in set(before.get(name, {})) | set(entry) if before.get(name, {}).get(k) != entry.get(k))
    return new_config, new_catalog, {
        'added': [name] if name not in before else [], 'removed': sorted(set(before) - {name}),
        'changed': [name] if changed else [], 'changed_fields': {name: fields} if changed else {},
        'test_models': [name] if changed or name not in before else [], 'before': sorted(before), 'after': [name],
    }


class ClaudeSetup(Refresh):
    prefix = PREFIX
    playbook = 'claude.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home, root, uv)
        self.paths['snapshot'] = self.state / 'claude-models.json'

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        return {'version': 1, 'codex_version': version, 'catalog': json.loads(MANIFEST.read_text())}

    def validate(self):
        self.journal()
        command = [self.uv, 'run', '--no-sync', '--project', str(self.root),
                   str(Path(__file__).with_name('verify_claude.py')),
                   '--catalog', str(self.transaction / 'catalog.json')]
        with (self.transaction / 'validation.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            if result.returncode == 0:
                result = subprocess.run([self.uv, 'run', '--no-sync', '--project', str(self.root),
                    str(Path(__file__).with_name('verify_shared_search.py')), '--model', 'claude/opus-5.5',
                    '--catalog', str(self.transaction / 'catalog.json')],
                    stdout=log, stderr=subprocess.STDOUT, timeout=900)
        if result.returncode:
            raise ValueError('Claude validation failed; see private model-refresh/validation.log')
        atomic_write(self.transaction / 'validated', 'yes\n')
        return {'validated': ['claude/opus-5.5']}


if __name__ == '__main__':
    main(ClaudeSetup)
