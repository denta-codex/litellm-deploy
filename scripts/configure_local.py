"""Activate the pinned local Qwen route using the shared refresh transaction."""
import copy
import json
from pathlib import Path
import subprocess

from refresh_models import Refresh, main

PREFIX = 'local/'
MANIFEST = Path(__file__).with_name('local-model.json')


def generate(source, config, catalog, selected):
    if source != json.loads(MANIFEST.read_text()):
        raise ValueError('Local manifest changed; explicitly activate local.yml before deployment')
    name = PREFIX + source['name']
    route = {
        'model_name': name,
        'model_info': {'mode': 'chat'},
        'litellm_params': {
            'model': 'openai/' + source['name'],
            'api_base': f"http://127.0.0.1:{source['port']}/v1",
            'api_key': 'local-loopback',
            'use_chat_completions_api': True,
            'timeout': 300, 'stream_timeout': 300, 'max_retries': 0, 'num_retries': 0,
        },
    }
    entry = {
        'slug': name, 'display_name': source['display_name'],
        'description': 'Local Qwen through llama.cpp; unloads after five idle minutes. Search uses the subscription helper.',
        'base_instructions': "You are Codex, a coding agent. Follow the user's request and use tools when needed.",
        'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
        'context_window': source['context_window'], 'max_context_window': source['context_window'],
        'truncation_policy': {'mode': 'tokens', 'limit': 2000},
        'input_modalities': ['text', 'image'],
        'supported_reasoning_levels': [{'effort': 'none', 'description': 'Direct answers'}],
        'default_reasoning_level': 'none', 'default_reasoning_summary': 'none',
        'supports_parallel_tool_calls': False, 'supports_reasoning_summaries': False,
        'supports_search_tool': True, 'web_search_tool_type': 'text_and_image',
        'supports_reasoning_summary_parameter': False, 'supports_reasoning_effort_updates': False,
        'supports_image_detail_original': False, 'support_verbosity': False,
        'use_responses_lite': False, 'prefer_websockets': False,
        'tool_mode': 'code_mode_only', 'shell_type': 'shell_command',
        'apply_patch_tool_type': 'freeform', 'experimental_supported_tools': [],
        'service_tiers': [], 'additional_speed_tiers': [], 'availability_nux': None,
        'available_access_programs': {'cyber': []}, 'priority': 100,
    }
    if selected.startswith(PREFIX) and selected != name:
        raise ValueError('Selected local model disappeared; select another model first')
    before = {m['slug']: m for m in catalog.get('models', []) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']: r for r in config.get('model_list', []) if r['model_name'].startswith(PREFIX)}
    new_config, new_catalog = copy.deepcopy(config), copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list', []) if not r['model_name'].startswith(PREFIX)] + [route]
    new_catalog['models'] = [m for m in new_catalog.get('models', []) if not m['slug'].startswith(PREFIX)] + [entry]
    fields = sorted(k for k in set(before.get(name, {})) | set(entry) if before.get(name, {}).get(k) != entry.get(k))
    changed = name in before and (bool(fields) or old_routes.get(name) != route)
    return new_config, new_catalog, {
        'added': [] if name in before else [name], 'removed': sorted(set(before) - {name}),
        'changed': [name] if changed else [],
        'changed_fields': {name: fields} if name in before and fields else {},
        'test_models': [name] if changed or name not in before else [],
        'before': sorted(before), 'after': [name],
    }


class LocalSetup(Refresh):
    prefix = PREFIX
    playbook = 'local.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home, root, uv)
        self.paths['snapshot'] = self.state / 'local-model.json'

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        return {'version': 1, 'codex_version': version, 'catalog': json.loads(MANIFEST.read_text())}

    def candidate(self, snapshot):
        candidate = super().candidate(snapshot)
        previous = json.loads(candidate['originals']['snapshot'] or '{}')
        if previous.get('catalog') != snapshot['catalog']:
            candidate['summary']['test_models'] = candidate['summary']['after']
        return candidate

    def validate(self):
        # Image and light-tool acceptance is required before publishing the picker.
        subprocess.run([self.uv, 'run', '--no-sync', '--project', str(self.root),
                        str(Path(__file__).with_name('verify_local.py')), '--proxy'], check=True, timeout=600)
        return super().validate()


if __name__ == '__main__':
    main(LocalSetup)
