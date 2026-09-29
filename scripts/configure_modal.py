"""Prepare the fixed Modal routes using the shared model activation transaction."""
import copy
import json
from pathlib import Path
import re
import subprocess

from refresh_models import Refresh, main

PREFIX = 'modal/'
API_BASE = 'https://inference.us-west.modal.direct/v1'
MANIFEST = Path(__file__).with_name('modal-models.json')


def generate(source, config, catalog, selected):
    specs = source.get('models')
    if not isinstance(specs, list) or not specs:
        raise ValueError('Modal manifest must contain models')
    routes, entries, seen = [], [], set()
    for spec in specs:
        endpoint = spec.get('endpoint', '')
        if not re.fullmatch(r'[a-z0-9-]+\.us-west\.modal\.direct', endpoint) or endpoint in seen:
            raise ValueError('Invalid or duplicate Modal endpoint')
        seen.add(endpoint)
        levels = spec.get('reasoning_levels', [])
        if (not levels or len(set(levels)) != len(levels)
                or any(e not in ('none', 'low', 'high', 'xhigh', 'max') for e in levels)
                or spec.get('default_reasoning_level') not in levels):
            raise ValueError('Invalid Modal reasoning levels')
        if not isinstance(spec.get('context_window'), int) or spec['context_window'] <= 0:
            raise ValueError('Invalid Modal context window')
        if not isinstance(spec.get('display_name'), str) or not spec['display_name'].strip():
            raise ValueError('Missing Modal display name')
        name = PREFIX + endpoint
        routes.append({
            'model_name': name,
            'model_info': {'mode': 'chat'},
            'litellm_params': {
                'model': 'openai/' + endpoint,
                'api_base': API_BASE,
                'api_key': 'os.environ/MODAL_API_KEY',
                'use_chat_completions_api': True,
            },
        })
        entries.append({
            'slug': name, 'display_name': spec['display_name'],
            'description': spec['display_name'] + ' through LiteLLM and Modal Chat Completions.',
            'base_instructions': "You are Codex, a coding agent. Follow the user's request and use tools when needed.",
            'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
            'context_window': spec['context_window'], 'max_context_window': spec['context_window'],
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'input_modalities': ['text', 'image'],
            'supported_reasoning_levels': [{'effort': e, 'description': e} for e in levels],
            'default_reasoning_level': spec['default_reasoning_level'],
            'default_reasoning_summary': 'none', 'supports_parallel_tool_calls': True,
            'supports_reasoning_summaries': False, 'supports_search_tool': True,
            'web_search_tool_type': 'text_and_image',
            'supports_reasoning_summary_parameter': False, 'supports_reasoning_effort_updates': False,
            'supports_image_detail_original': False, 'support_verbosity': False,
            'use_responses_lite': False, 'prefer_websockets': False,
            'tool_mode': 'code_mode_only', 'shell_type': 'shell_command',
            'apply_patch_tool_type': 'freeform', 'experimental_supported_tools': [],
            'service_tiers': [], 'additional_speed_tiers': [], 'availability_nux': None,
            'available_access_programs': {}, 'priority': 90,
        })
    after = {m['slug']: m for m in entries}
    if selected.startswith(PREFIX) and selected not in after:
        raise ValueError('Selected Modal model disappeared; select another model before configuring')
    before = {m['slug']: m for m in catalog.get('models', []) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']: r for r in config.get('model_list', []) if r['model_name'].startswith(PREFIX)}
    new_config, new_catalog = copy.deepcopy(config), copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list', []) if not r['model_name'].startswith(PREFIX)] + routes
    new_catalog['models'] = [m for m in new_catalog.get('models', []) if not m['slug'].startswith(PREFIX)] + entries
    changed_fields = {name: sorted(k for k in set(before[name]) | set(after[name]) if before[name].get(k) != after[name].get(k))
                      for name in before.keys() & after.keys() if before[name] != after[name]}
    changed_routes = {r['model_name'] for r in routes if old_routes.get(r['model_name']) != r}
    added = sorted(after.keys() - before.keys())
    changed = sorted(set(changed_fields) | (changed_routes & before.keys()))
    return new_config, new_catalog, {
        'added': added, 'removed': sorted(before.keys() - after.keys()), 'changed': changed,
        'changed_fields': changed_fields, 'test_models': sorted(set(added) | set(changed)),
        'before': sorted(before), 'after': sorted(after),
    }


class ModalSetup(Refresh):
    prefix = PREFIX
    playbook = 'modal.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home, root, uv)
        # Share the activation lock with subscription refresh; save discovery separately.
        self.paths['snapshot'] = self.state / 'modal-models.json'

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        if not re.fullmatch(r'[A-Za-z0-9.+_-]+', version):
            raise ValueError('Cannot determine installed Codex version')
        return {'version': 1, 'codex_version': version, 'catalog': json.loads(MANIFEST.read_text())}


if __name__ == '__main__':
    main(ModalSetup)
