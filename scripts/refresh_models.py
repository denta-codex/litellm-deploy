"""Subscription discovery plus configured models, with recoverable activation; invoked by Ansible.

ChatGPT subscription rows come from discovery. Every other route and picker row
comes from the ``models`` list of the per-machine deployment configuration
(``--config``). A validated refresh publishes that list as ``models.json``
beside the LiteLLM config, so rollback restores it with the routes and later
snapshot checks compare against what is installed. Each entry's ``litellm``
mapping is passed to LiteLLM with documented provider defaults;
``codex`` overrides the stock catalog row defaults.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from catalog_enrichment import enrich_catalog, fetch_sources, stock_catalog
import deploy_config
from deployment import codex_binary
from recommended import with_separator

PREFIX = 'chatgpt/'
REVIEW_MODEL = 'codex-auto-review'
SCRIPTS = Path(__file__).parent
CONFIGURED_NAME = re.compile(r'[a-z0-9][a-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*')
# Acceptance validation only; routes never retry inference. A failed check whose
# output shows provider unavailability or throttling is rerun once in full, and
# the rerun alone decides the result. Real defects fail both runs.
TRANSIENT_PROVIDER = re.compile(
    r'ServiceUnavailable|Service Unavailable|Bedrock is unable to process|Too Many Requests|RateLimitError'
    r'|ThrottlingException|overloaded_error|\b(?:status|code|HTTP)\W{0,3}(?:429|503|529)\b', re.IGNORECASE)
TRANSIENT_RETRY_DELAY = 15
# Stock Codex catalog defaults for configured rows; ``codex`` keys override them.
ROW_DEFAULTS = {
    'base_instructions': "You are Codex, a coding agent. Follow the user's request and use tools when needed.",
    'model_messages': {}, 'visibility': 'list', 'supported_in_api': True,
    'truncation_policy': {'mode': 'tokens', 'limit': 10000}, 'input_modalities': ['text'],
    'default_reasoning_summary': 'none', 'supports_parallel_tool_calls': True,
    'supports_reasoning_summaries': False, 'supports_search_tool': True,
    'web_search_tool_type': 'text_and_image', 'supports_reasoning_summary_parameter': False,
    'supports_reasoning_effort_updates': False, 'supports_image_detail_original': False,
    'support_verbosity': False, 'use_responses_lite': False, 'prefer_websockets': False,
    'tool_mode': 'code_mode_only', 'shell_type': 'shell_command', 'apply_patch_tool_type': 'freeform',
    'experimental_supported_tools': [], 'service_tiers': [], 'additional_speed_tiers': [],
    'availability_nux': None, 'available_access_programs': {'cyber': []}, 'priority': 100,
}
REQUIRED_ROW = ('display_name', 'context_window', 'supported_reasoning_levels', 'default_reasoning_level')


def encode(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_optional(path):
    return path.read_text() if path.exists() else None


def compatible_model(row):
    """Normalize legacy catalog metadata required by current stock Codex."""
    normalized = copy.deepcopy(row)
    programs = normalized.get('available_access_programs')
    if isinstance(programs, dict) and 'cyber' not in programs:
        programs['cyber'] = []
    return normalized


def validate_source(source):
    if not isinstance(source, dict) or not isinstance(source.get('models'), list) or not source['models']:
        raise ValueError('Discovery returned an empty or malformed catalog')
    seen = set()
    for row in source['models']:
        if not isinstance(row, dict):
            raise ValueError('Malformed model entry')
        slug = row.get('slug')
        if not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', slug) or slug in seen:
            raise ValueError('Invalid or duplicate upstream model ID')
        seen.add(slug)
        if row.get('visibility') not in ('list', 'hide', 'none'):
            raise ValueError(f'Unrecognized visibility for {slug}')
        if not isinstance(row.get('base_instructions'), str):
            raise ValueError(f'Missing native instructions for {slug}; refusing invented metadata')
    if not any(row['visibility'] == 'list' for row in source['models']):
        raise ValueError('Discovery returned no selectable models')


def configured_model(spec):
    """Return (route, catalog row, verification) for one ``models`` entry."""
    if not isinstance(spec, dict) or set(spec) - {'name', 'litellm', 'model_info', 'codex', 'verify'}:
        raise ValueError('Configured models accept only name, litellm, model_info, codex and verify')
    name = spec.get('name')
    if (not isinstance(name, str) or not CONFIGURED_NAME.fullmatch(name) or name.startswith(PREFIX)):
        raise ValueError(f'Invalid configured model name {name!r}; use provider/model outside {PREFIX}')
    litellm = spec.get('litellm')
    if not isinstance(litellm, dict) or not isinstance(litellm.get('model'), str):
        raise ValueError(f'{name}: litellm.model is required')
    model_info = copy.deepcopy(spec.get('model_info') or {})
    if not isinstance(model_info, dict):
        raise ValueError(f'{name}: model_info must be a mapping')
    pricing = model_info.get('catalog_pricing', {})
    if not isinstance(pricing, dict) or type(pricing.get('zero_rate', False)) is not bool:
        raise ValueError(f'{name}: catalog_pricing must be a mapping with a boolean zero_rate')
    model_info.setdefault('mode', 'chat')
    route = {'model_name': name, 'model_info': model_info, 'litellm_params': copy.deepcopy(litellm)}
    fable = (litellm['model'].startswith('bedrock/')
             and litellm['model'].split('/')[-1].split('.')[-1] == 'claude-fable-5-2')
    if fable:
        # The pinned bundled map predates this Bedrock model. Router registers
        # these capabilities for its underlying ID; otherwise LiteLLM silently
        # strips output_config.effort or translates adaptive to manual thinking.
        model_info.setdefault('supports_reasoning', True)
        model_info.setdefault('supports_adaptive_thinking', True)
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            model_info.setdefault('supports_' + effort + '_reasoning_effort', True)
        # Adaptive thinking shares this ceiling with the answer. LiteLLM's
        # implicit 4096 default can end a high-effort turn before any answer.
        budget = route['litellm_params'].setdefault('max_tokens', 65536)
        if type(budget) is not int or not 1 <= budget <= 128000:
            raise ValueError(f'{name}: max_tokens must be an integer from 1 to 128000')
    codex = copy.deepcopy(spec.get('codex') or {})
    if not isinstance(codex, dict) or 'slug' in codex:
        raise ValueError(f'{name}: codex must be a mapping of catalog fields other than slug')
    missing = [key for key in REQUIRED_ROW if key not in codex]
    if missing:
        raise ValueError(f'{name}: codex requires {", ".join(missing)}')
    # Provider-specific operating instructions are a shipped catalog default,
    # not an injected second agent runtime. Explicit user prompts still win.
    native_gemini = litellm['model'].startswith(('vertex_ai/gemini-', 'vertex_ai_beta/gemini-'))
    if native_gemini and not {'base_instructions', 'base_instructions_file'}.intersection(codex):
        codex['base_instructions_file'] = 'gemini-instructions.md'
    kimi = ((model_info.get('catalog_pricing') or {}).get('provider') == 'moonshotai'
            and litellm.get('use_chat_completions_api') is True)
    if kimi and not {'base_instructions', 'base_instructions_file'}.intersection(codex):
        codex['base_instructions_file'] = 'kimi-instructions.md'
    instructions = codex.pop('base_instructions_file', None)
    row = {'slug': name, 'description': codex['display_name'], **copy.deepcopy(ROW_DEFAULTS)}
    if native_gemini:
        # Enabled after stock Codex + search + compaction offline acceptance.
        # These flags expose provider-marked summaries, not decrypted state.
        row.update(supports_reasoning_summaries=True, supports_reasoning_summary_parameter=True,
                   default_reasoning_summary='auto')
    row['max_context_window'] = codex['context_window']
    row.update(codex)
    if instructions is not None:
        if not isinstance(instructions, str) or '/' in instructions:
            raise ValueError(f'{name}: base_instructions_file names a file shipped in scripts/')
        row['base_instructions'] = (SCRIPTS / instructions).read_text()
    levels = []
    for level in row['supported_reasoning_levels']:
        if isinstance(level, str):
            level = {'effort': level, 'description': level}
        if not isinstance(level, dict) or not isinstance(level.get('effort'), str):
            raise ValueError(f'{name}: invalid reasoning level')
        levels.append(level)
    row['supported_reasoning_levels'] = levels
    if row['default_reasoning_level'] not in {level['effort'] for level in levels}:
        raise ValueError(f'{name}: default_reasoning_level must be a supported level')
    if row['visibility'] not in ('list', 'hide'):
        raise ValueError(f'{name}: visibility must be list or hide')
    verify = spec.get('verify') or {}
    args, validators, timeout = verify.get('args', []), verify.get('validators', []), verify.get('timeout', 600)
    if (not isinstance(verify, dict) or set(verify) - {'args', 'validators', 'timeout'}
            or not isinstance(args, list) or not all(isinstance(a, str) for a in args)
            or not isinstance(validators, list)
            or not all(isinstance(v, list) and v and all(isinstance(a, str) for a in v) and '/' not in v[0]
                       for v in validators)
            or type(timeout) is not int or timeout <= 0):
        raise ValueError(f'{name}: verify accepts args, validators (script from scripts/ plus arguments) and timeout')
    args = list(args)
    if fable and '--auto-tool-choice' not in args:
        # Forced tool use is unsupported by Fable's always-on adaptive thinking.
        args.append('--auto-tool-choice')
    return route, row, {'args': args, 'validators': validators, 'timeout': timeout}


def configured_models(specs):
    if not isinstance(specs, list):
        raise ValueError('models must be a list')
    result = [configured_model(spec) for spec in specs]
    names = [route['model_name'] for route, _, _ in result]
    if len(names) != len(set(names)):
        raise ValueError('Configured model names must be unique')
    return result


def generate_subscription(source, config, catalog, selected):
    validate_source(source)
    rows = [compatible_model(row) for row in source['models']]
    if not any(row['slug'] == REVIEW_MODEL for row in rows):
        raise ValueError('Subscription catalog is missing codex-auto-review; refusing to remove or substitute the native reviewer')
    aliases = []
    for row in rows:
        if row['visibility'] == 'list' and row['slug'] != REVIEW_MODEL:
            alias = copy.deepcopy(row)
            alias['slug'] = PREFIX + row['slug']
            alias['use_responses_lite'] = False
            aliases.append(alias)
    names = {m['slug'] for m in aliases}
    disabled_upgrades = []
    # Upgrade actions must use the same selectable namespace as the picker.
    for alias in aliases:
        upgrade = alias.get('upgrade')
        if upgrade is None:
            continue
        target = PREFIX + upgrade['model']
        if target in names:
            upgrade['model'] = target
        else:
            disabled_upgrades.append({'model': alias['slug'], 'target': upgrade['model'],
                                      'reason': 'No selectable chatgpt route'})
            alias['upgrade'] = None
    if selected.startswith(PREFIX) and selected not in names:
        raise ValueError(f'Selected model {selected} disappeared; select another model before refreshing')
    original_previous = {m['slug']: m for m in catalog.get('models', [])}
    previous = {slug: compatible_model(row) for slug, row in original_previous.items()}
    compatibility_changed = sorted(slug for slug in previous
                                   if previous[slug] != original_previous[slug])
    # Keep native metadata for existing tasks, including retired native IDs.
    # Only their routed aliases are listed. Unrelated provider entries survive.
    native = {m['slug']: dict(m, visibility='hide') for m in rows}
    native[REVIEW_MODEL]['use_responses_lite'] = False
    preserved = []
    for slug, row in previous.items():
        if slug.startswith(PREFIX):
            continue
        if slug in native:
            continue
        preserved.append(copy.deepcopy(row))
    result_catalog = dict(catalog, models=[*preserved, *native.values(), *aliases])
    result_config = copy.deepcopy(config)
    old_routes = {m['model_name']: m for m in config.get('model_list', [])}
    other_routes = [m for m in config.get('model_list', [])
                    if not m['model_name'].startswith(PREFIX) and m['model_name'] != REVIEW_MODEL]
    routes = []
    for row in aliases:
        name = row['slug']
        route = copy.deepcopy(old_routes.get(name, {}))
        route['model_name'] = name
        route.setdefault('model_info', {})['mode'] = 'responses'
        route.setdefault('litellm_params', {})['model'] = name
        routes.append(route)
    # Codex requests this reserved name verbatim, even though it is hidden from
    # the picker. Forward it to the subscription's native approval reviewer.
    reviewer = copy.deepcopy(old_routes.get(REVIEW_MODEL, {}))
    reviewer['model_name'] = REVIEW_MODEL
    reviewer.setdefault('model_info', {})['mode'] = 'responses'
    reviewer.setdefault('litellm_params', {})['model'] = PREFIX + REVIEW_MODEL
    routes.append(reviewer)
    reviewer_changed = (old_routes.get(REVIEW_MODEL) != reviewer
                        or previous.get(REVIEW_MODEL) != native[REVIEW_MODEL])
    result_config['model_list'] = [*other_routes, *routes]
    before = {name: row for name, row in previous.items() if name.startswith(PREFIX) and row.get('visibility') == 'list'}
    after = {row['slug']: row for row in aliases}
    changed_fields = {name: sorted(k for k in set(before[name]) | set(after[name]) if before[name].get(k) != after[name].get(k))
                      for name in before.keys() & after.keys() if before[name] != after[name]}
    changed_routes = {r['model_name'] for r in routes if old_routes.get(r['model_name']) != r}
    added = sorted(after.keys() - before.keys())
    changed = sorted(set(changed_fields) | (changed_routes & before.keys()))
    return result_config, result_catalog, {
        'added': added, 'removed': sorted(before.keys() - after.keys()),
        'changed': changed, 'changed_fields': changed_fields,
        'test_models': sorted(set(added) | set(changed)),
        'reviewer_changed': reviewer_changed, 'test_reviewer': reviewer_changed,
        'before': sorted(before), 'after': sorted(after),
        'disabled_upgrades': disabled_upgrades,
        'compatibility_changed': compatibility_changed,
    }


def generate(source, config, catalog, selected, specs=()):
    """Subscription rows from discovery; every other route and row from configuration."""
    models = configured_models(list(specs))
    names = {route['model_name'] for route, _, _ in models}
    if '/' in selected and not selected.startswith(PREFIX) and selected not in names:
        raise ValueError(f'Selected model {selected} is no longer configured; select another model first')
    result_config, result_catalog, summary = generate_subscription(source, config, catalog, selected)
    owned = [r for r in result_config['model_list'] if r['model_name'].startswith(PREFIX) or r['model_name'] == REVIEW_MODEL]
    result_config['model_list'] = [*(route for route, _, _ in models), *owned]
    kept = [r for r in result_catalog['models'] if '/' not in r['slug'] or r['slug'].startswith(PREFIX)]
    result_catalog['models'] = [*kept, *(row for _, row, _ in models)]
    old_routes = {r['model_name']: r for r in config.get('model_list', [])}
    old_rows = {r['slug']: compatible_model(r) for r in catalog.get('models', [])}
    before = {slug for slug, row in old_rows.items()
              if '/' in slug and not slug.startswith(PREFIX) and row.get('visibility') == 'list'}
    after = {row['slug'] for _, row, _ in models if row['visibility'] == 'list'}
    changed = {route['model_name'] for route, row, _ in models
               if old_routes.get(route['model_name']) != route or old_rows.get(row['slug']) != row}
    summary.update(
        added=sorted(set(summary['added']) | (after - before)),
        removed=sorted(set(summary['removed']) | (before - after)),
        changed=sorted(set(summary['changed']) | (changed & before)),
        test_models=sorted(set(summary['test_models']) | changed),
        before=sorted(set(summary['before']) | before),
        after=sorted(set(summary['after']) | after),
        configured=sorted(names),
        verification={route['model_name']: verify for route, _, verify in models})
    return result_config, result_catalog, summary


class Refresh:
    playbook = 'refresh-models.yml'

    def __init__(self, home, root, uv, models=None):
        self.home, self.root, self.uv = home, root, uv
        self.models = models
        self.codex = home / '.local/bin/codex'
        self.state = home / '.local/state/litellm'
        self.transaction = self.state / 'model-refresh'
        self.paths = {
            'config': home / '.config/litellm/config.yaml',
            'catalog': home / '.config/litellm/codex-models.json',
            'models': home / '.config/litellm/models.json',
            'snapshot': self.state / 'subscription-models.json',
        }

    def originals(self):
        return {name: read_optional(path) for name, path in self.paths.items()}

    def discover(self):
        version = subprocess.check_output([self.codex, '--version'], text=True).strip().removeprefix('codex-cli ')
        if not re.fullmatch(r'[A-Za-z0-9.+_-]+', version):
            raise ValueError('Cannot determine installed Codex version')
        auth = json.loads((self.state / 'chatgpt/auth.json').read_text())
        if not auth.get('access_token'):
            raise ValueError('LiteLLM ChatGPT login missing; run the documented login command')
        headers = {'Authorization': 'Bearer ' + auth['access_token'], 'Accept': 'application/json'}
        if auth.get('account_id'):
            headers['Chatgpt-Account-Id'] = auth['account_id']
        request = urllib.request.Request(
            'https://chatgpt.com/backend-api/codex/models?client_version=' + version, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Subscription catalog exceeds size limit')
            source = json.loads(data)
        except urllib.error.HTTPError as exc:
            raise ValueError(f'Subscription discovery HTTP {exc.code}; no installation changes made. Check LiteLLM login.') from None
        validate_source(source)
        return {'version': 1, 'codex_version': version, 'catalog': source}

    def pricing_sources(self):
        return fetch_sources()

    def candidate(self, snapshot, refresh_pricing=False):
        originals = self.originals()
        if originals['config'] is None:
            raise ValueError('Deploy LiteLLM before refreshing models')
        # The deployment configuration is authoritative; the installed copy
        # only records what the active routes were generated from.
        if self.models is not None:
            specs = copy.deepcopy(self.models)
        else:
            specs = json.loads(originals['models']) if originals['models'] is not None else []
        config = yaml.safe_load(originals['config'])
        catalog = json.loads(originals['catalog']) if originals['catalog'] else {'models': []}
        current = tomllib.loads((self.home / '.codex/config.toml').read_text())
        selected = current.get('model', '') if current.get('model_provider') == 'litellm' else ''
        new_config, new_catalog, summary = generate(snapshot['catalog'], config, catalog, selected, specs)
        verification = summary.pop('verification')
        new_catalog = with_separator(new_catalog)
        # Row order alone must not trigger a restart or a picker publication.
        for generated, current, field, key in ((new_config, config, 'model_list', 'model_name'),
                                                (new_catalog, catalog, 'models', 'slug')):
            if sorted(generated.get(field, []), key=lambda r: r[key]) == sorted(current.get(field, []), key=lambda r: r[key]):
                generated[field] = current.get(field, [])
        previous_snapshot = json.loads(originals['snapshot']) if originals['snapshot'] else {}
        summary['codex_version_changed'] = previous_snapshot.get('codex_version') != snapshot['codex_version']
        if summary['codex_version_changed']:
            summary['test_models'] = summary['after']
            summary['test_reviewer'] = True
        pricing_summary = {}
        if refresh_pricing:
            new_catalog, pricing_summary = enrich_catalog(new_catalog, new_config, self.pricing_sources())
        candidates = {
            'config': originals['config'] if new_config == config else yaml.safe_dump(new_config, sort_keys=False),
            'catalog': originals['catalog'] if new_catalog == catalog else encode(new_catalog),
            'models': (originals['models'] if originals['models'] is not None
                       and json.loads(originals['models']) == specs else encode(specs)),
            'snapshot': encode(snapshot),
        }
        summary.update(routing_changed=new_config != config,
                       catalog_changed=stock_catalog(new_catalog) != stock_catalog(catalog),
                       enrichment_changed=new_catalog.get('remote_codex') != catalog.get('remote_codex'),
                       catalog_document_changed=new_catalog != catalog,
                       models_changed=originals['models'] != candidates['models'],
                       snapshot_changed=originals['snapshot'] != candidates['snapshot'], **pricing_summary)
        summary['any_changes'] = any(originals[k] != candidates[k] for k in originals)
        return {'originals': originals, 'candidates': candidates, 'summary': summary, 'verification': verification,
                'codex_config_sha256': hashlib.sha256((self.home / '.codex/config.toml').read_bytes()).hexdigest()}

    def prepare(self, preview=False):
        if self.transaction.exists():
            raise ValueError(f'Unfinished refresh exists. Run {self.playbook} -e refresh_action=rollback first')
        journal = self.candidate(self.discover(), refresh_pricing=True)
        if preview or not journal['summary']['any_changes']:
            return journal['summary']
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Atomic mkdir prevents two refresh commands from activating over each other.
        self.transaction.mkdir(mode=0o700)
        try:
            atomic_write(self.transaction / 'journal.json', encode(journal))
            atomic_write(self.transaction / 'catalog.json', journal['candidates']['catalog'])
            with tempfile.TemporaryDirectory(prefix='catalog-probe-', dir=self.state) as temporary:
                result = subprocess.run([self.codex, 'debug', 'models', '-c',
                                         'model_catalog_json=' + json.dumps(str(self.transaction / 'catalog.json'))],
                                        env=os.environ | {'CODEX_HOME': temporary}, capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise ValueError('Stock Codex rejected the candidate catalog: ' + result.stderr[-1500:])
                parsed = json.loads(result.stdout)
                expected = set(journal['summary']['after'])
                actual = {r['slug'] for r in parsed['models'] if r.get('visibility') == 'list' and '/' in r['slug']}
                if expected != actual:
                    raise ValueError('Stock Codex did not load the expected selectable models')
        except BaseException:
            shutil.rmtree(self.transaction)
            raise
        return journal['summary']

    def journal(self):
        return json.loads((self.transaction / 'journal.json').read_text())

    def activate(self):
        journal = self.journal()
        if self.originals() != journal['originals']:
            raise ValueError('Configuration changed since discovery; refusing activation')
        if hashlib.sha256((self.home / '.codex/config.toml').read_bytes()).hexdigest() != journal['codex_config_sha256']:
            raise ValueError('Codex selection changed since discovery; rerun refresh')
        if journal['summary']['routing_changed']:
            atomic_write(self.paths['config'], journal['candidates']['config'])
        return journal['summary']

    def validate(self):
        journal = self.journal()
        rows = {r['slug']: r for r in json.loads(journal['candidates']['catalog'])['models']}
        log = self.transaction / 'validation.log'
        catalog = str(self.transaction / 'catalog.json')

        def script(name, *args):
            return [self.uv, 'run', '--no-sync', '--project', str(self.root), str(SCRIPTS / name), *args]

        retried = {}

        def run(name, command, timeout):
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            note = ''
            if result.returncode and TRANSIENT_PROVIDER.search(result.stdout + result.stderr):
                retried.setdefault(name, []).append(Path(command[5]).name)
                note = ' after one retry for a transient provider error'
                time.sleep(TRANSIENT_RETRY_DELAY)
                result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                raise ValueError(f'{name}: validation failed{note}\n{result.stdout[-1500:]}\n{result.stderr[-2000:]}')

        def verify(name):
            if name == REVIEW_MODEL:
                run(name, script('verify_review.py'), 240)
                return name
            spec = journal.get('verification', {}).get(name, {'args': [], 'validators': [], 'timeout': 600})
            # Configured validators run first: provider-specific checks fail fastest.
            commands = [script(*(arg.format(model=name, catalog=catalog) for arg in validator))
                        for validator in spec['validators']]
            commands.append(script('verify.py', '--model', name, *spec['args']))
            commands.append(script('verify_codex.py', '--model', name, '--catalog', catalog,
                                   *([] if rows[name].get('supports_search_tool', False) else ['--skip-search'])))
            for command in commands:
                run(name, command, spec['timeout'])
            return name
        # Bound subscription use and latency. Each probe owns a disposable Codex home.
        test_models = journal['summary']['test_models'] + ([REVIEW_MODEL] if journal['summary'].get('test_reviewer', False) else [])
        with log.open('w') as output, ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(verify, name): name for name in test_models}
            failures = []
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    checks = 'review route, streaming, JSON output' if name == REVIEW_MODEL else 'streaming, tools, context, advertised search'
                    output.write(f'PASS {name}: {checks}')
                except Exception as exc:
                    failures.append(str(exc))
                    output.write(f'FAIL {name}')
                if name in retried:
                    output.write(f' (retried after transient provider error: {", ".join(retried[name])})')
                output.write('\n')
                output.flush()
        if failures:
            raise ValueError('\n'.join(failures))
        atomic_write(self.transaction / 'validated', 'yes\n')
        return {'validated': test_models, 'transient_retries': retried}

    def finish(self):
        journal = self.journal()
        if not (self.transaction / 'validated').exists():
            raise ValueError('Refresh has not passed inference validation')
        if hashlib.sha256((self.home / '.codex/config.toml').read_bytes()).hexdigest() != journal['codex_config_sha256']:
            raise ValueError('Codex configuration changed during validation; refusing publication')
        expected = journal['originals'] | {'config': journal['candidates']['config']}
        if self.originals() != expected:
            raise ValueError('Configuration changed during validation; refusing to overwrite')
        for name in ('catalog', 'models', 'snapshot'):
            if journal['originals'][name] != journal['candidates'][name]:
                atomic_write(self.paths[name], journal['candidates'][name])
        summary = journal['summary']
        shutil.rmtree(self.transaction)
        return summary

    def rollback(self):
        journal = self.journal()
        for name, text in self.originals().items():
            if text not in (journal['originals'][name], journal['candidates'][name]):
                raise ValueError(f'Concurrent edit to {name}; recovery retained at {self.transaction}')
        for name, text in journal['originals'].items():
            if text is None:
                self.paths[name].unlink(missing_ok=True)
            else:
                atomic_write(self.paths[name], text)
        return {'restart_required': journal['summary']['routing_changed']}

    def check_snapshot(self, routes_only=False):
        if self.transaction.exists():
            raise ValueError('Unfinished model refresh: recover before ordinary deployment')
        if not self.paths['snapshot'].exists():
            return {'snapshot_present': False}
        candidate = self.candidate(json.loads(self.paths['snapshot'].read_text()))
        summary = candidate['summary']
        if summary['routing_changed'] or ((summary['catalog_changed'] or summary['models_changed']) and not routes_only):
            raise ValueError(f'Installed model files differ from saved discovery; run {self.playbook} to reconcile')
        return {'snapshot_present': True, 'models': candidate['summary']['after']}

    def check_routes(self):
        """Owner preflight: routes must match saved discovery.

        Catalog-only drift, such as a picker priority introduced by newer
        scripts, is reconciled by the owner's own validated publication.
        """
        return self.check_snapshot(routes_only=True)


def main(refresh_type=Refresh):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['preview', 'prepare', 'activate', 'validate', 'finish', 'rollback', 'cleanup-rollback',
                                      'check-snapshot', 'check-routes'])
    p.add_argument('--uv', default=shutil.which('uv'))
    p.add_argument('--config', type=Path, default=deploy_config.DEFAULT, help='per-machine deployment configuration')
    args = p.parse_args()
    os.umask(0o077)
    config = deploy_config.load(args.config)
    home = Path(config['home'])
    if home != Path.home():
        raise SystemExit('Run as the configured deployment user')
    # Only discovery reads the configured list; checks compare installed files.
    models = config['models'] if args.action in ('preview', 'prepare') else None
    refresh = refresh_type(home, home / '.local/share/litellm', args.uv, models)
    if args.action in ('preview', 'prepare', 'validate'):
        refresh.codex = Path(codex_binary(home))
    if args.action in ('preview', 'prepare'):
        result = refresh.prepare(preview=args.action == 'preview')
    elif args.action == 'cleanup-rollback':
        journal = refresh.journal()
        if refresh.originals() != journal['originals']:
            raise ValueError('Rollback files have not been restored')
        shutil.rmtree(refresh.transaction)
        result = {'recovered': True}
    else:
        result = getattr(refresh, args.action.replace('-', '_'))()
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
