"""Explicit subscription discovery and recoverable activation; invoked by Ansible."""
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
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

PREFIX = 'chatgpt/'
REVIEW_MODEL = 'codex-auto-review'


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


def generate(source, config, catalog, selected):
    validate_source(source)
    rows = source['models']
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
    if selected.startswith(PREFIX) and selected not in names:
        raise ValueError(f'Selected model {selected} disappeared; select another model before refreshing')
    previous = {m['slug']: m for m in catalog.get('models', [])}
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
    }


class Refresh:
    def __init__(self, home, root, uv):
        self.home, self.root, self.uv = home, root, uv
        self.state = home / '.local/state/litellm'
        self.transaction = self.state / 'model-refresh'
        self.paths = {
            'config': home / '.config/litellm/config.yaml',
            'catalog': home / '.config/litellm/codex-models.json',
            'snapshot': self.state / 'subscription-models.json',
        }

    def originals(self):
        return {name: read_optional(path) for name, path in self.paths.items()}

    def discover(self):
        version = subprocess.check_output(['codex', '--version'], text=True).strip().removeprefix('codex-cli ')
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

    def candidate(self, snapshot):
        originals = self.originals()
        if originals['config'] is None:
            raise ValueError('Deploy LiteLLM before refreshing models')
        config = yaml.safe_load(originals['config'])
        catalog = json.loads(originals['catalog']) if originals['catalog'] else {'models': []}
        current = tomllib.loads((self.home / '.codex/config.toml').read_text())
        selected = current.get('model', '') if current.get('model_provider') == 'litellm' else ''
        new_config, new_catalog, summary = generate(snapshot['catalog'], config, catalog, selected)
        previous_snapshot = json.loads(originals['snapshot']) if originals['snapshot'] else {}
        summary['codex_version_changed'] = previous_snapshot.get('codex_version') != snapshot['codex_version']
        if summary['codex_version_changed']:
            summary['test_models'] = summary['after']
            summary['test_reviewer'] = True
        candidates = {
            'config': originals['config'] if new_config == config else yaml.safe_dump(new_config, sort_keys=False),
            'catalog': originals['catalog'] if new_catalog == catalog else encode(new_catalog),
            'snapshot': encode(snapshot),
        }
        summary.update(routing_changed=new_config != config, catalog_changed=new_catalog != catalog,
                       snapshot_changed=originals['snapshot'] != candidates['snapshot'])
        summary['any_changes'] = any(originals[k] != candidates[k] for k in originals)
        return {'originals': originals, 'candidates': candidates, 'summary': summary,
                'codex_config_sha256': hashlib.sha256((self.home / '.codex/config.toml').read_bytes()).hexdigest()}

    def prepare(self, preview=False):
        if self.transaction.exists():
            raise ValueError('Unfinished refresh exists. Run refresh-models.yml -e refresh_action=rollback first')
        journal = self.candidate(self.discover())
        if preview or not journal['summary']['any_changes']:
            return journal['summary']
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Atomic mkdir prevents two refresh commands from activating over each other.
        self.transaction.mkdir(mode=0o700)
        try:
            atomic_write(self.transaction / 'journal.json', encode(journal))
            atomic_write(self.transaction / 'catalog.json', journal['candidates']['catalog'])
            with tempfile.TemporaryDirectory(prefix='catalog-probe-', dir=self.state) as temporary:
                result = subprocess.run(['codex', 'debug', 'models', '-c',
                                         'model_catalog_json=' + json.dumps(str(self.transaction / 'catalog.json'))],
                                        env=os.environ | {'CODEX_HOME': temporary}, capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise ValueError('Stock Codex rejected the candidate catalog: ' + result.stderr[-1500:])
                parsed = json.loads(result.stdout)
                expected = set(journal['summary']['after'])
                actual = {r['slug'] for r in parsed['models'] if r.get('visibility') == 'list' and r['slug'].startswith(PREFIX)}
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
        def verify(name):
            if name == REVIEW_MODEL:
                command = [self.uv, 'run', '--no-sync', '--project', str(self.root),
                           str(Path(__file__).with_name('verify_review.py'))]
                result = subprocess.run(command, capture_output=True, text=True, timeout=240)
                if result.returncode:
                    raise ValueError(f'{name}: validation failed\n{result.stdout[-1500:]}\n{result.stderr[-2000:]}')
                return name
            commands = [
                [self.uv, 'run', '--no-sync', '--project', str(self.root), str(Path(__file__).with_name('verify.py')), '--model', name],
                [self.uv, 'run', '--no-sync', '--project', str(self.root), str(Path(__file__).with_name('verify_codex.py')),
                 '--model', name, '--catalog', str(self.transaction / 'catalog.json')],
            ]
            if not rows[name].get('supports_search_tool', False):
                commands[1].append('--skip-search')
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True, timeout=600)
                if result.returncode:
                    raise ValueError(f'{name}: validation failed\n{result.stdout[-1500:]}\n{result.stderr[-2000:]}')
            return name
        # Bound subscription use and latency. Each probe owns a disposable Codex home.
        test_models = journal['summary']['test_models'] + ([REVIEW_MODEL] if journal['summary']['test_reviewer'] else [])
        with log.open('w') as output, ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(verify, name): name for name in test_models}
            failures = []
            for future in as_completed(futures):
                try:
                    name = future.result()
                    checks = 'review route, streaming, JSON output' if name == REVIEW_MODEL else 'streaming, tools, context, advertised search'
                    output.write(f'PASS {name}: {checks}\n')
                except Exception as exc:
                    failures.append(str(exc))
                    output.write(f'FAIL {futures[future]}\n')
                output.flush()
        if failures:
            raise ValueError('\n'.join(failures))
        atomic_write(self.transaction / 'validated', 'yes\n')
        return {'validated': test_models}

    def finish(self):
        journal = self.journal()
        if not (self.transaction / 'validated').exists():
            raise ValueError('Refresh has not passed inference validation')
        if hashlib.sha256((self.home / '.codex/config.toml').read_bytes()).hexdigest() != journal['codex_config_sha256']:
            raise ValueError('Codex configuration changed during validation; refusing publication')
        expected = journal['originals'] | {'config': journal['candidates']['config']}
        if self.originals() != expected:
            raise ValueError('Configuration changed during validation; refusing to overwrite')
        for name in ('catalog', 'snapshot'):
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

    def check_snapshot(self):
        if self.transaction.exists():
            raise ValueError('Unfinished model refresh: recover before ordinary deployment')
        if not self.paths['snapshot'].exists():
            return {'snapshot_present': False}
        candidate = self.candidate(json.loads(self.paths['snapshot'].read_text()))
        if candidate['summary']['routing_changed'] or candidate['summary']['catalog_changed']:
            raise ValueError('Installed model files differ from saved discovery; run refresh-models.yml to reconcile')
        return {'snapshot_present': True, 'models': candidate['summary']['after']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['preview', 'prepare', 'activate', 'validate', 'finish', 'rollback', 'cleanup-rollback', 'check-snapshot'])
    p.add_argument('--home', type=Path, default=Path.home())
    p.add_argument('--root', type=Path, default=Path.home() / '.local/share/litellm')
    p.add_argument('--uv', default=shutil.which('uv'))
    args = p.parse_args()
    os.umask(0o077)
    refresh = Refresh(args.home, args.root, args.uv)
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
