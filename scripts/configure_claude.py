"""Configure the Claude subscription routes using the shared activation transaction."""
import copy
import json
from pathlib import Path
import subprocess

from catalog_enrichment import stock_catalog
from recommended import priority
from refresh_models import Refresh, atomic_write, main

MANIFEST = Path(__file__).with_name('claude-models.json')
INSTRUCTIONS = Path(__file__).with_name('claude-instructions.md')
PREFIX = 'claude/'


def generate(source, config, catalog, selected):
    expected = json.loads(MANIFEST.read_text())
    if source != expected:
        raise ValueError('Claude snapshot differs from the pinned manifest; run claude.yml to validate changes')
    before = {m['slug']: m for m in stock_catalog(catalog).get('models', []) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']: r for r in config.get('model_list', []) if r['model_name'].startswith(PREFIX)}
    routes, entries, names = [], [], []
    for spec in source['models']:
        name = spec['slug']
        names.append(name)
        fast = name == 'claude/opus-5.5'
        route = {'model_name': name, 'model_info': {'mode': 'chat'}, 'litellm_params': {
            'model': 'claudesdk/' + spec['model'], 'use_chat_completions_api': True,
            'allowed_openai_params': ['reasoning_effort', 'response_format', 'parallel_tool_calls'],
            'num_retries': 0,
        }}
        policy = old_routes.get(name, {}).get('model_info', {}).get('catalog_pricing')
        if policy is not None:
            route['model_info']['catalog_pricing'] = copy.deepcopy(policy)
        entry = {
            'slug': name, 'display_name': spec['display_name'],
            'description': spec['display_name'] + ' through your Claude subscription.',
            'base_instructions': INSTRUCTIONS.read_text(),
            'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
            'context_window': spec['context_window'], 'max_context_window': spec['context_window'],
            'truncation_policy': {'mode': 'tokens', 'limit': 10000}, 'input_modalities': ['text', 'image'],
            'supported_reasoning_levels': [{'effort': effort, 'description': effort} for effort in spec['reasoning_levels']],
            'default_reasoning_level': spec['default_reasoning_level'], 'default_reasoning_summary': 'auto',
            # supports_search_tool enables Codex's client-side tool_search (deferred tool discovery), which the
            # adapter cannot relay; deferred tools then arrive as namespaces. Web search is unaffected.
            'supports_parallel_tool_calls': True, 'supports_reasoning_summaries': True, 'supports_search_tool': False,
            'web_search_tool_type': 'text_and_image',
            'supports_reasoning_summary_parameter': False, 'supports_reasoning_effort_updates': True,
            'supports_image_detail_original': False, 'support_verbosity': False,
            'use_responses_lite': False, 'prefer_websockets': False,
            # Claude reasons over each tool result, so it calls Codex tools directly.
            'tool_mode': 'direct', 'shell_type': 'shell_command', 'apply_patch_tool_type': 'freeform',
            'experimental_supported_tools': [],
            'service_tiers': ([{'id': 'priority', 'name': 'Fast',
                'description': 'Faster Opus responses; uses paid Claude usage credits'}] if fast else []),
            'additional_speed_tiers': ['fast'] if fast else [],
            'availability_nux': None, 'available_access_programs': {'cyber': []}, 'priority': priority(name, 80),
        }
        routes.append(route)
        entries.append(entry)
    if selected.startswith(PREFIX) and selected not in names:
        raise ValueError('Selected Claude model disappeared; select another model before configuring')
    new_config, new_catalog = copy.deepcopy(config), copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list', []) if not r['model_name'].startswith(PREFIX)] + routes
    new_catalog['models'] = [m for m in new_catalog.get('models', []) if not m['slug'].startswith(PREFIX)] + entries
    after = {m['slug']: m for m in entries}
    new_routes = {r['model_name']: r for r in routes}
    changed = [n for n in names if n in before and (before[n] != after[n] or old_routes.get(n) != new_routes[n])]
    changed_fields = {n: sorted(k for k in set(before[n]) | set(after[n]) if before[n].get(k) != after[n].get(k))
                      for n in changed}
    added = [n for n in names if n not in before]
    return new_config, new_catalog, {
        'added': added, 'removed': sorted(set(before) - set(names)),
        'changed': changed, 'changed_fields': changed_fields,
        'test_models': [n for n in names if n in added or n in changed], 'before': sorted(before), 'after': names,
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
        journal = self.journal()
        test_models = journal['summary']['test_models']
        if not test_models:
            return super().validate()
        model_args = [arg for m in test_models for arg in ('--model', m)]
        command = [self.uv, 'run', '--no-sync', '--project', str(self.root),
                   str(Path(__file__).with_name('verify_claude.py')),
                   '--catalog', str(self.transaction / 'catalog.json'), *model_args]
        with (self.transaction / 'validation.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            if result.returncode == 0:
                result = subprocess.run([self.uv, 'run', '--no-sync', '--project', str(self.root),
                    str(Path(__file__).with_name('verify_shared_search.py')), *model_args,
                    '--catalog', str(self.transaction / 'catalog.json')],
                    stdout=log, stderr=subprocess.STDOUT, timeout=900)
        if result.returncode:
            raise ValueError('Claude validation failed; see private model-refresh/validation.log')
        atomic_write(self.transaction / 'validated', 'yes\n')
        return {'validated': test_models}


if __name__ == '__main__':
    main(ClaudeSetup)
