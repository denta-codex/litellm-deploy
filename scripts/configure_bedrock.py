"""Configure exact Bedrock routes through the shared model activation transaction."""
import copy
import json
from pathlib import Path
import subprocess

from bedrock_aws import MANIFEST, discover, encrypted_session
from refresh_models import Refresh, main

PREFIX = 'bedrock/'
CREDENTIAL = Path('/etc/credstore.encrypted/litellm-bedrock-aws')


def generate(source, config, catalog, selected):
    manifest = json.loads(MANIFEST.read_text())
    if source.get('region') != manifest['region']:
        raise ValueError('Unexpected Bedrock region')
    expected = {m['slug']: m for m in manifest['models']}
    routes, entries, seen = [], [], set()
    for model in source.get('models', []):
        name = model.get('slug')
        spec = expected.get(name)
        if spec is None or name in seen or any(model.get(k) != v for k, v in spec.items()):
            raise ValueError('Bedrock snapshot differs from the exact model allowlist')
        if not model.get('streaming'):
            raise ValueError('Bedrock route requires streaming support')
        seen.add(name)
        routes.append({'model_name': name, 'model_info': {'mode': 'chat'}, 'litellm_params': {
            'model': 'bedrock/converse/' + spec['profile_id'],
            'aws_region_name': source['region'],
            'aws_access_key_id': 'os.environ/AWS_ACCESS_KEY_ID',
            'aws_secret_access_key': 'os.environ/AWS_SECRET_ACCESS_KEY',
            'use_chat_completions_api': True,
            'allowed_openai_params': ['tools', 'tool_choice', 'reasoning_effort', 'parallel_tool_calls'],
            'num_retries': 0,
        }})
        instructions = (Path(__file__).with_name('claude-instructions.md').read_text()
            if spec['tool_mode'] == 'direct' else
            "You are Codex, a coding agent. Follow the user's request and use tools when needed.")
        entries.append({
            'slug': name, 'display_name': spec['display_name'],
            'description': spec['display_name'] + '; billed to AWS through the US Bedrock inference profile.',
            'base_instructions': instructions, 'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
            'context_window': spec['context_window'], 'max_context_window': spec['context_window'],
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'input_modalities': model['input_modalities'],
            'supported_reasoning_levels': [{'effort': e, 'description': e} for e in spec['reasoning_levels']],
            'default_reasoning_level': spec['default_reasoning_level'], 'default_reasoning_summary': 'none',
            'supports_parallel_tool_calls': True, 'supports_reasoning_summaries': False,
            'supports_search_tool': spec['tool_mode'] != 'direct', 'web_search_tool_type': 'text_and_image',
            'supports_reasoning_summary_parameter': False, 'supports_reasoning_effort_updates': False,
            'supports_image_detail_original': False, 'support_verbosity': False,
            'use_responses_lite': False, 'prefer_websockets': False,
            'tool_mode': spec['tool_mode'], 'shell_type': 'shell_command', 'apply_patch_tool_type': 'freeform',
            'experimental_supported_tools': [], 'service_tiers': [], 'additional_speed_tiers': [],
            'availability_nux': None, 'available_access_programs': {'cyber': []}, 'priority': 85,
        })
    if not seen:
        raise ValueError('No requested Bedrock models are available; no routing changes made')
    if selected.startswith(PREFIX) and selected not in seen:
        raise ValueError('Selected Bedrock model disappeared; select another model before configuring')
    before = {m['slug']: m for m in catalog.get('models', []) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']: r for r in config.get('model_list', []) if r['model_name'].startswith(PREFIX)}
    new_config, new_catalog = copy.deepcopy(config), copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list', []) if not r['model_name'].startswith(PREFIX)] + routes
    new_catalog['models'] = [m for m in new_catalog.get('models', []) if not m['slug'].startswith(PREFIX)] + entries
    after = {m['slug']: m for m in entries}
    changed = sorted(n for n in before.keys() & after.keys()
        if before[n] != after[n] or old_routes.get(n) != next(r for r in routes if r['model_name'] == n))
    added = sorted(after.keys() - before.keys())
    return new_config, new_catalog, {
        'added': added, 'removed': sorted(before.keys() - after.keys()), 'changed': changed,
        'changed_fields': {n: sorted(k for k in set(before[n]) | set(after[n])
            if before[n].get(k) != after[n].get(k)) for n in changed},
        'test_models': sorted(set(added) | set(changed)), 'before': sorted(before), 'after': sorted(after),
        'unavailable': source.get('unavailable', []),
    }


class BedrockSetup(Refresh):
    prefix = PREFIX
    playbook = 'bedrock.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home, root, uv)
        self.paths['snapshot'] = self.state / 'bedrock-models.json'

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        return {'version': 1, 'codex_version': version, 'catalog': discover(encrypted_session(CREDENTIAL))}

    def validate(self):
        # Run explicit hosted-search validation even for Claude direct-tool entries:
        # supports_search_tool controls deferred tool discovery, not hosted web search.
        self.journal()
        with (self.transaction / 'validation.log').open('w') as log:
            for name in self.journal()['summary']['test_models']:
                process = subprocess.run([self.uv, 'run', '--no-sync', '--project', str(self.root),
                    str(Path(__file__).with_name('verify_shared_search.py')), '--model', name,
                    '--catalog', str(self.transaction / 'catalog.json')],
                    stdout=log, stderr=subprocess.STDOUT, timeout=900)
                if process.returncode:
                    raise ValueError('Bedrock hosted-search validation failed; see private model-refresh/validation.log')
        # The shared validator writes the publication marker only after these
        # additional checks and its own streaming/Codex checks have all passed.
        return super().validate()


if __name__ == '__main__':
    main(BedrockSetup)
