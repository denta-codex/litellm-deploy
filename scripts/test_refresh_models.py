"""Behavioral tests for discovery, configured models, and recoverable activation."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
import refresh_models
from refresh_models import REVIEW_MODEL, Refresh, configured_model, configured_models, encode

SPEC = {'name': 'modal/custom',
        'litellm': {'model': 'openai/private', 'api_key': 'os.environ/MODAL_KEY'},
        'codex': {'display_name': 'Custom', 'context_window': 1000,
                  'supported_reasoning_levels': ['high'], 'default_reasoning_level': 'high'},
        'verify': {'validators': [['verify_capabilities.py', '--model', '{model}', '--catalog', '{catalog}']]}}
ROUTE, ROW, _ = configured_model(SPEC)


def generate(source, config, catalog, selected, specs=(SPEC,)):
    return refresh_models.generate(source, config, catalog, selected, specs)


def model(slug, visibility='list'):
    return {'slug': slug, 'visibility': visibility, 'base_instructions': 'Native instructions',
            'display_name': slug, 'use_responses_lite': True, 'supports_search_tool': True,
            'context_window': 272000, 'supported_reasoning_levels': [{'effort': 'high'}],
            'future_capability': {'keep': True}}


def upgrade(target):
    return {'model': target, 'migration_markdown': 'Switch to the replacement model.',
            'retirement_at': '2026-10-14T19:00:00Z', 'future_metadata': {'keep': True}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.source = {'models': [model('astra'), model('sol'), model(REVIEW_MODEL, 'hide')]}
        self.external = copy.deepcopy(ROUTE)
        self.config = {'model_list': [self.external], 'general_settings': {'master_key': 'os.environ/LITELLM_MASTER_KEY'}}
        self.catalog = {'models': [copy.deepcopy(ROW), model('retired-native', 'hide')]}

    def test_gemini_prompt_defaults_and_explicit_prompt_override(self):
        spec = copy.deepcopy(SPEC)
        spec['name'] = 'vertex/gemini-4-carbon'
        spec['litellm'] = {'model': 'vertex_ai/gemini-4-carbon', 'reasoning_effort': 'medium'}
        spec['codex'].update(supported_reasoning_levels=['low', 'medium', 'high'], default_reasoning_level='medium')
        original = copy.deepcopy(spec)
        route, row, _ = configured_model(spec)
        self.assertEqual(spec, original)
        self.assertEqual(row['base_instructions'], (refresh_models.SCRIPTS / 'gemini-instructions.md').read_text())
        self.assertEqual(row['tool_mode'], 'code_mode_only')
        self.assertEqual(row['apply_patch_tool_type'], 'freeform')
        self.assertEqual(row['default_reasoning_level'], 'medium')
        self.assertTrue(row['supports_reasoning_summaries'])
        self.assertTrue(row['supports_reasoning_summary_parameter'])
        self.assertEqual(row['default_reasoning_summary'], 'auto')
        self.assertEqual([l['effort'] for l in row['supported_reasoning_levels']], ['low', 'medium', 'high'])
        self.assertEqual(route['litellm_params']['reasoning_effort'], 'medium')
        spec['codex']['base_instructions'] = 'Explicit owner prompt'
        self.assertEqual(configured_model(spec)[1]['base_instructions'], 'Explicit owner prompt')
        spec['codex'].pop('base_instructions')
        spec['codex']['base_instructions_file'] = 'claude-instructions.md'
        self.assertEqual(configured_model(spec)[1]['base_instructions'],
                         (refresh_models.SCRIPTS / 'claude-instructions.md').read_text())
        self.assertEqual(configured_model(SPEC)[1], ROW)
        spec['codex']['supports_reasoning_summaries'] = False
        self.assertFalse(configured_model(spec)[1]['supports_reasoning_summaries'])

    def test_subscription_routes_metadata_and_external_preservation(self):
        config, catalog, summary = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        self.assertEqual(config['model_list'][0], self.external)
        self.assertEqual(config['general_settings'], self.config['general_settings'])
        visible = [m for m in catalog['models'] if m['visibility'] == 'list']
        self.assertEqual({m['slug'] for m in visible}, {'modal/custom', 'chatgpt/astra', 'chatgpt/sol'})
        alias = next(m for m in visible if m['slug'] == 'chatgpt/sol')
        expected = self.source['models'][1] | {'slug': 'chatgpt/sol', 'use_responses_lite': False}
        self.assertEqual(alias, expected)
        self.assertIn('retired-native', [m['slug'] for m in catalog['models']])
        self.assertEqual(summary['added'], ['chatgpt/astra', 'chatgpt/sol'])
        again_config, again_catalog, again = generate(self.source, config, catalog, 'chatgpt/astra')
        self.assertEqual(again_config, config)
        self.assertEqual(again_catalog, catalog)
        self.assertEqual(again['test_models'], [])
        self.assertFalse(again['test_reviewer'])

    def test_modal_kimi_prompt_defaults_and_overrides(self):
        spec = copy.deepcopy(SPEC)
        spec['model_info'] = {'catalog_pricing': {'provider': 'moonshotai', 'model': 'kimi-k3.1-pro'}}
        spec['litellm']['use_chat_completions_api'] = True
        original = copy.deepcopy(spec)
        _, row, _ = configured_model(spec)
        self.assertEqual(spec, original)
        self.assertEqual(row['base_instructions'], (refresh_models.SCRIPTS / 'kimi-instructions.md').read_text())
        # reasoning_content is replay state, not a provider-authored summary.
        self.assertFalse(row['supports_reasoning_summaries'])
        self.assertEqual(row['default_reasoning_summary'], 'none')
        self.assertFalse(row['supports_reasoning_summary_parameter'])
        self.assertEqual(row['default_reasoning_level'], 'high')
        for key, value in {'base_instructions': 'Explicit owner prompt',
                           'supports_reasoning_summaries': True,
                           'default_reasoning_summary': 'auto',
                           'supports_reasoning_summary_parameter': True}.items():
            variant = copy.deepcopy(spec)
            variant['codex'][key] = value
            self.assertEqual(configured_model(variant)[1][key], value)

    def test_legacy_access_program_metadata_is_normalized_and_converges(self):
        source = copy.deepcopy(self.source)
        catalog = copy.deepcopy(self.catalog)
        for row in source['models'] + catalog['models']:
            row['available_access_programs'] = {'future_program': ['keep']}
        config, normalized, summary = generate(source, self.config, catalog, 'chatgpt/astra')
        rows = {row['slug']: row for row in normalized['models']}
        for slug in ('astra', 'sol', REVIEW_MODEL, 'chatgpt/astra', 'chatgpt/sol',
                     'retired-native'):
            self.assertEqual(rows[slug]['available_access_programs'], {
                'future_program': ['keep'], 'cyber': []})
        # Configured rows are regenerated from configuration, not carried forward.
        self.assertEqual(rows['modal/custom'], ROW)
        self.assertEqual(summary['compatibility_changed'], ['modal/custom', 'retired-native'])
        again_config, again_catalog, again = generate(source, config, normalized, 'chatgpt/astra')
        self.assertEqual((again_config, again_catalog), (config, normalized))
        self.assertEqual(again['compatibility_changed'], [])

    def test_native_reviewer_routes_without_appearing_in_picker(self):
        config, catalog, summary = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        reviewer = next(r for r in config['model_list'] if r['model_name'] == REVIEW_MODEL)
        self.assertEqual(reviewer['litellm_params']['model'], 'chatgpt/codex-auto-review')
        self.assertEqual(reviewer['model_info']['mode'], 'responses')
        metadata = next(r for r in catalog['models'] if r['slug'] == REVIEW_MODEL)
        self.assertEqual(metadata['visibility'], 'hide')
        self.assertFalse(metadata['use_responses_lite'])
        self.assertNotIn('chatgpt/codex-auto-review', [r['slug'] for r in catalog['models']])
        self.assertTrue(summary['test_reviewer'])
        self.assertNotIn(REVIEW_MODEL, summary['test_models'])

    def test_missing_native_reviewer_rejects_refresh(self):
        source = {'models': [model('astra'), model('sol')]}
        with self.assertRaisesRegex(ValueError, 'missing codex-auto-review'):
            generate(source, self.config, self.catalog, 'chatgpt/astra')

    def test_stock_litellm_resolves_bootstrap_and_discovered_reviewer(self):
        with patch.dict(os.environ, {'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}):
            from litellm import Router, get_llm_provider
            from litellm.llms.chatgpt.authenticator import Authenticator
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
        templates = Path(__file__).resolve().parent.parent / 'deploy/templates'
        template = Environment(loader=FileSystemLoader(templates), undefined=StrictUndefined).get_template('config.yaml.j2')
        bootstrap = yaml.safe_load(template.render(litellm_model='chatgpt/gpt-6-astra'))
        discovered, _, _ = generate(self.source, {'model_list': []}, {'models': []}, 'chatgpt/astra')
        # Exercise real router/provider selection without reading credentials,
        # starting device login, or making subscription requests.
        with patch.object(Authenticator, '_ensure_token_dir'), \
                patch.object(Authenticator, 'get_access_token', return_value='offline-test-token'):
            for config in (bootstrap, discovered):
                router = Router(model_list=config['model_list'])
                route = router.get_available_deployment(model=REVIEW_MODEL, messages=[])
                model_name, provider, _, _ = get_llm_provider(route['litellm_params']['model'])
                self.assertEqual((model_name, provider), (REVIEW_MODEL, 'chatgpt'))

    def test_reviewer_stays_hidden_if_upstream_marks_it_selectable(self):
        self.source['models'][-1]['visibility'] = 'list'
        config, catalog, summary = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        self.assertEqual(sum(r['model_name'] == REVIEW_MODEL for r in config['model_list']), 1)
        self.assertNotIn('chatgpt/codex-auto-review', summary['after'])
        self.assertEqual(next(r['visibility'] for r in catalog['models'] if r['slug'] == REVIEW_MODEL), 'hide')

    def test_missing_route_repaired_without_revalidating_unchanged_chat_models(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        config['model_list'] = [r for r in config['model_list'] if r['model_name'] != REVIEW_MODEL]
        repaired, _, summary = generate(self.source, config, catalog, 'chatgpt/astra')
        self.assertEqual(sum(r['model_name'] == REVIEW_MODEL for r in repaired['model_list']), 1)
        self.assertTrue(summary['test_reviewer'])
        self.assertEqual(summary['test_models'], [])

    def test_reviewer_route_and_metadata_changes_require_validation(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        for change in ('route', 'metadata'):
            with self.subTest(change=change):
                changed_config, changed_source = copy.deepcopy(config), copy.deepcopy(self.source)
                if change == 'route':
                    route = next(r for r in changed_config['model_list'] if r['model_name'] == REVIEW_MODEL)
                    route['litellm_params']['model'] = 'chatgpt/astra'
                else:
                    changed_source['models'][-1]['context_window'] = 1000000
                repaired, _, summary = generate(changed_source, changed_config, catalog, 'chatgpt/astra')
                self.assertTrue(summary['test_reviewer'])
                self.assertEqual(summary['test_models'], [])
                self.assertEqual(next(r['litellm_params']['model'] for r in repaired['model_list']
                                      if r['model_name'] == REVIEW_MODEL), 'chatgpt/codex-auto-review')

    def test_reject_empty_duplicate_missing_fields_and_selected_removal(self):
        for source in ({'models': []}, {'models': [model('astra'), model('astra')]},
                       {'models': [{'slug': 'astra', 'visibility': 'list'}]},
                       {'models': [model('sol'), model(REVIEW_MODEL, 'hide')]}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                generate(source, self.config, self.catalog, 'chatgpt/astra')

    def test_removed_models_leave_picker_and_routes(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        source = {'models': [model('astra'), model(REVIEW_MODEL, 'hide')]}
        new_config, new_catalog, diff = generate(source, config, catalog, 'chatgpt/astra')
        self.assertEqual(diff['removed'], ['chatgpt/sol'])
        self.assertNotIn('chatgpt/sol', [m['model_name'] for m in new_config['model_list']])
        self.assertIn('sol', [m['slug'] for m in new_catalog['models'] if m['visibility'] == 'hide'])

    def test_changed_capabilities_require_validation(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        source = copy.deepcopy(self.source)
        source['models'][1]['context_window'] = 1000000
        _, _, diff = generate(source, config, catalog, 'chatgpt/astra')
        self.assertEqual(diff['test_models'], ['chatgpt/sol'])
        self.assertEqual(diff['changed_fields']['chatgpt/sol'], ['context_window'])


class ConfiguredModelTests(unittest.TestCase):
    def setUp(self):
        DiscoveryTests.setUp(self)

    def test_route_is_litellm_config_verbatim_and_row_uses_stock_defaults(self):
        spec = copy.deepcopy(SPEC)
        spec['litellm'].update(api_base='https://example.invalid/v1', use_chat_completions_api=True)
        spec['model_info'] = {'catalog_pricing': {'provider': 'zai', 'model': 'glm'}}
        spec['codex'].update(priority=-3, input_modalities=['text', 'image'],
                             supported_reasoning_levels=['low', {'effort': 'high', 'description': 'Deep'}])
        route, row, verify = configured_model(spec)
        self.assertEqual(route, {'model_name': 'modal/custom', 'litellm_params': spec['litellm'],
                                 'model_info': {'catalog_pricing': {'provider': 'zai', 'model': 'glm'}, 'mode': 'chat'}})
        self.assertEqual(row['description'], 'Custom')
        self.assertEqual(row['max_context_window'], 1000)
        self.assertEqual((row['priority'], row['tool_mode']), (-3, 'code_mode_only'))
        self.assertEqual(row['supported_reasoning_levels'], [
            {'effort': 'low', 'description': 'low'}, {'effort': 'high', 'description': 'Deep'}])
        self.assertEqual(verify['timeout'], 600)

    def test_instructions_file_is_read_from_scripts(self):
        spec = copy.deepcopy(SPEC)
        spec['codex']['base_instructions_file'] = 'claude-instructions.md'
        _, row, _ = configured_model(spec)
        self.assertEqual(row['base_instructions'], (Path(refresh_models.__file__).parent / 'claude-instructions.md').read_text())
        self.assertNotIn('base_instructions_file', row)

    def test_invalid_specs_are_rejected(self):
        def variant(**changes):
            spec = copy.deepcopy(SPEC)
            for key, value in changes.items():
                if value is None:
                    spec.pop(key)
                else:
                    spec[key] = value
            return spec
        invalid = [
            variant(name='chatgpt/sol'), variant(name='no-provider'), variant(litellm=None),
            variant(codex={'display_name': 'x'}), variant(unknown=True),
            variant(codex=dict(SPEC['codex'], default_reasoning_level='max')),
            variant(codex=dict(SPEC['codex'], visibility='none')),
            variant(codex=dict(SPEC['codex'], base_instructions_file='../secret')),
            variant(model_info={'catalog_pricing': 'zai/glm'}),
            variant(model_info={'catalog_pricing': {'zero_rate': 'true'}}),
            variant(verify={'validators': [['/bin/sh', '-c', 'x']]}),
            variant(verify={'timeout': 0}),
        ]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                configured_model(spec)
        with self.assertRaisesRegex(ValueError, 'unique'):
            configured_models([SPEC, SPEC])

    def test_removed_configuration_drops_route_and_row(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        new_config, new_catalog, summary = generate(self.source, config, catalog, 'chatgpt/astra', specs=())
        self.assertNotIn('modal/custom', [r['model_name'] for r in new_config['model_list']])
        self.assertNotIn('modal/custom', [r['slug'] for r in new_catalog['models']])
        self.assertEqual(summary['removed'], ['modal/custom'])
        with self.assertRaisesRegex(ValueError, 'no longer configured'):
            generate(self.source, config, catalog, 'modal/custom', specs=())

    def test_changed_configuration_requires_validation(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        spec = copy.deepcopy(SPEC)
        spec['litellm']['timeout'] = 30
        _, _, summary = generate(self.source, config, catalog, 'chatgpt/astra', specs=(spec,))
        self.assertEqual((summary['changed'], summary['test_models']), (['modal/custom'], ['modal/custom']))
        added = dict(copy.deepcopy(SPEC), name='bedrock/other')
        _, _, summary = generate(self.source, config, catalog, 'chatgpt/astra', specs=(SPEC, added))
        self.assertEqual((summary['added'], summary['test_models']), (['bedrock/other'], ['bedrock/other']))


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        DiscoveryTests.setUp(self)
        self.source = {'models': [model('gpt-5.5'), model('gpt-5.6-sol'), model(REVIEW_MODEL, 'hide')]}
        self.source['models'][0]['upgrade'] = upgrade('gpt-5.6-sol')
        self.source['models'][1]['upgrade'] = None
        self.catalog['models'][1]['upgrade'] = upgrade('retired-replacement')
        self.catalog['future_setting'] = {'keep': True}

    def assert_routable_upgrades(self, config, catalog):
        routes = {r['model_name']: r for r in config['model_list']}
        selectable = {r['slug'] for r in catalog['models'] if r['visibility'] == 'list'}
        for row in catalog['models']:
            if row['slug'].startswith('chatgpt/') and row.get('upgrade') is not None:
                target = row['upgrade']['model']
                self.assertIn(target, selectable)
                self.assertIn(target, routes)
                self.assertEqual(routes[target]['litellm_params']['model'], target)

    def test_rewrite_preserves_metadata_native_external_and_inputs(self):
        originals = copy.deepcopy((self.source, self.config, self.catalog))
        config, catalog, summary = generate(self.source, self.config, self.catalog, 'chatgpt/gpt-5.5')
        rows = {r['slug']: r for r in catalog['models']}
        expected = copy.deepcopy(self.source['models'][0])
        expected.update(slug='chatgpt/gpt-5.5', use_responses_lite=False)
        expected['upgrade']['model'] = 'chatgpt/gpt-5.6-sol'
        self.assertEqual(rows['chatgpt/gpt-5.5'], expected)
        for row in self.source['models']:
            native = row | {'visibility': 'hide'}
            if row['slug'] == REVIEW_MODEL:
                native['use_responses_lite'] = False
            self.assertEqual(rows[row['slug']], native)
        for row in self.catalog['models']:
            self.assertEqual(rows[row['slug']], row)
        self.assertEqual(catalog['future_setting'], self.catalog['future_setting'])
        self.assertEqual(config['model_list'][0], self.external)
        self.assertEqual(config['general_settings'], self.config['general_settings'])
        self.assertEqual(len(rows), len(catalog['models']))
        self.assertEqual(len({r['model_name'] for r in config['model_list']}), len(config['model_list']))
        self.assertEqual((self.source, self.config, self.catalog), originals)
        self.assertEqual(summary['disabled_upgrades'], [])
        self.assert_routable_upgrades(config, catalog)

    def test_unavailable_targets_clear_only_routed_upgrade_and_report(self):
        for target, visibility in (('gpt-5.6-sol', None), ('gpt-5.6-sol', 'hide'),
                                   ('gpt-5.6-sol', 'none'), (REVIEW_MODEL, 'hide'),
                                   (REVIEW_MODEL, 'list'), ('modal/custom', None)):
            with self.subTest(target=target, visibility=visibility):
                source = copy.deepcopy(self.source)
                source['models'][0]['upgrade'] = upgrade(target)
                if target == 'gpt-5.6-sol':
                    if visibility is None:
                        del source['models'][1]
                    else:
                        source['models'][1]['visibility'] = visibility
                elif target == REVIEW_MODEL:
                    source['models'][-1]['visibility'] = visibility
                # A stale installed route/catalog entry is not an available target.
                config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/gpt-5.5')
                config, catalog, summary = generate(source, config, catalog, 'chatgpt/gpt-5.5')
                rows = {r['slug']: r for r in catalog['models']}
                self.assertEqual(rows['chatgpt/gpt-5.5'], source['models'][0] | {
                    'slug': 'chatgpt/gpt-5.5', 'use_responses_lite': False, 'upgrade': None})
                self.assertEqual(rows['gpt-5.5']['upgrade'], upgrade(target))
                self.assertEqual(source['models'][0]['upgrade'], upgrade(target))
                self.assertEqual(summary['disabled_upgrades'], [{
                    'model': 'chatgpt/gpt-5.5', 'target': target, 'reason': 'No selectable chatgpt route'}])
                self.assert_routable_upgrades(config, catalog)
                again_config, again_catalog, again = generate(source, config, catalog, 'chatgpt/gpt-5.5')
                self.assertEqual((again_config, again_catalog), (config, catalog))
                self.assertEqual(again['test_models'], [])
                self.assertFalse(again['test_reviewer'])
                self.assertEqual(again['disabled_upgrades'], summary['disabled_upgrades'])

    def test_repairs_existing_upgrade_without_changing_routes_and_converges(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/gpt-5.5')
        rows = {r['slug']: r for r in catalog['models']}
        rows['chatgpt/gpt-5.5']['upgrade']['model'] = 'gpt-5.6-sol'
        repaired_config, repaired_catalog, summary = generate(self.source, config, catalog, 'chatgpt/gpt-5.5')
        self.assertEqual(repaired_config, config)
        self.assertEqual(summary['changed_fields'], {'chatgpt/gpt-5.5': ['upgrade']})
        self.assertEqual(summary['test_models'], ['chatgpt/gpt-5.5'])
        self.assertFalse(summary['test_reviewer'])
        again_config, again_catalog, again = generate(self.source, repaired_config, repaired_catalog, 'chatgpt/gpt-5.5')
        self.assertEqual((again_config, again_catalog), (repaired_config, repaired_catalog))
        self.assertEqual(again['test_models'], [])
        self.assertFalse(again['test_reviewer'])
        self.assert_routable_upgrades(repaired_config, repaired_catalog)

    @unittest.skipUnless(shutil.which('codex'), 'Stock Codex is not installed')
    def test_stock_codex_parses_routed_and_disabled_upgrades(self):
        for available in (True, False):
            with self.subTest(available=available), tempfile.TemporaryDirectory() as temporary:
                source = copy.deepcopy(self.source)
                if not available:
                    source['models'][1]['visibility'] = 'hide'
                config, catalog, _ = generate(source, self.config, self.catalog, 'chatgpt/gpt-5.5')
                # Supply the remaining required stock fields without copying native prompts.
                for row in catalog['models']:
                    row.update(description='Offline fixture', default_reasoning_level='high',
                               supported_reasoning_levels=[{'effort': 'high', 'description': 'High'}],
                               shell_type='shell_command', supported_in_api=True, priority=1,
                               supports_reasoning_summaries=True, support_verbosity=True,
                               default_verbosity='low', experimental_supported_tools=[],
                               truncation_policy={'mode': 'tokens', 'limit': 10000})
                path = Path(temporary) / 'catalog.json'
                path.write_text(encode(catalog))
                result = subprocess.run(['codex', 'debug', 'models', '-c', 'model_catalog_json=' + json.dumps(str(path))],
                                        env=os.environ | {'CODEX_HOME': temporary}, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                rows = {r['slug']: r for r in parsed['models']}
                expected = 'chatgpt/gpt-5.6-sol' if available else None
                actual = rows['chatgpt/gpt-5.5'].get('upgrade')
                self.assertEqual(actual['model'] if actual else None, expected)
                if available:
                    self.assertEqual(actual['migration_markdown'], upgrade('gpt-5.6-sol')['migration_markdown'])
                    self.assertEqual(actual['retirement_at'], upgrade('gpt-5.6-sol')['retirement_at'])
                self.assertEqual(rows['gpt-5.5']['upgrade']['model'], 'gpt-5.6-sol')
                self.assertEqual(rows[REVIEW_MODEL]['visibility'], 'hide')
                self.assert_routable_upgrades(config, parsed)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        DiscoveryTests.setUp(self)
        self.source['models'][0]['upgrade'] = upgrade('sol')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.refresh = Refresh(self.home, self.home / 'runtime', 'uv')
        self.refresh.pricing_sources = lambda: {'openai': None, 'models_dev': None}
        self.refresh.state.mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump(self.config))
        self.refresh.paths['catalog'].write_text(encode(self.catalog))
        self.stage([SPEC])
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': self.source}
        self.originals = self.refresh.originals()

    def stage(self, specs):
        self.refresh.models = specs

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            return self.refresh.prepare()

    def test_preview_does_not_write_or_run_model_validation(self):
        self.source['models'][1]['visibility'] = 'hide'
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            summary = self.refresh.prepare(preview=True)
        self.assertTrue(summary['any_changes'])
        self.assertEqual(summary['disabled_upgrades'], [{
            'model': 'chatgpt/astra', 'target': 'sol', 'reason': 'No selectable chatgpt route'}])
        self.assertEqual(self.refresh.originals(), self.originals)
        self.assertFalse(self.refresh.transaction.exists())
        run.assert_not_called()

    def test_failed_validation_restores_exact_originals(self):
        self.prepare()
        self.refresh.activate()
        self.assertNotEqual(self.refresh.originals()['config'], self.originals['config'])
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'injected failure')):
            with self.assertRaisesRegex(ValueError, 'validation failed'):
                self.refresh.validate()
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_publication_and_unchanged_rerun(self):
        self.prepare()
        self.refresh.activate()
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'PASS', '')):
            self.refresh.validate()
        self.refresh.finish()
        self.assertFalse(self.refresh.transaction.exists())
        self.assertEqual(json.loads(self.refresh.paths['models'].read_text()), [SPEC])
        self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            result = self.refresh.prepare()
        self.assertFalse(result['any_changes'])
        run.assert_not_called()

    def test_configuration_edit_is_drift_until_refreshed_and_rollback_restores_models(self):
        self.install_inventory_baseline()
        spec = copy.deepcopy(SPEC)
        spec['codex']['display_name'] = 'Renamed'
        self.stage([spec])
        with self.assertRaisesRegex(ValueError, 'differ from saved discovery'):
            self.refresh.check_snapshot()
        # Verification-only edits are drift too: the installed record must match configuration.
        verify_only = copy.deepcopy(SPEC)
        verify_only['verify']['timeout'] = 30
        self.stage([verify_only])
        with self.assertRaisesRegex(ValueError, 'differ from saved discovery'):
            self.refresh.check_snapshot()
        self.stage([spec])
        summary = self.prepare()
        self.assertEqual(summary['test_models'], ['modal/custom'])
        self.assertTrue(summary['models_changed'])
        self.refresh.activate()
        commands = []
        def probe(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 1, '', 'injected failure')
        with patch('refresh_models.subprocess.run', side_effect=probe):
            with self.assertRaisesRegex(ValueError, 'modal/custom: validation failed'):
                self.refresh.validate()
        # Configured validators run first with placeholders resolved.
        self.assertTrue(commands[0][-5].endswith('verify_capabilities.py'))
        self.assertEqual(commands[0][-4:], ['--model', 'modal/custom', '--catalog',
                                            str(self.refresh.transaction / 'catalog.json')])
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)
        self.assertEqual(json.loads(self.refresh.paths['models'].read_text()), [SPEC])

    def install_inventory_baseline(self):
        candidate = self.refresh.candidate(self.snapshot)
        for name, text in candidate['candidates'].items():
            self.refresh.paths[name].write_text(text)
        self.originals = self.refresh.originals()

    def test_owner_route_check_tolerates_catalog_only_drift(self):
        self.install_inventory_baseline()
        catalog = json.loads(self.originals['catalog'])
        owned = next(m for m in catalog['models'] if m['slug'].startswith('chatgpt/'))
        owned['priority'] = owned.get('priority', 0) + 50
        self.refresh.paths['catalog'].write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, 'differ from saved discovery'):
            self.refresh.check_snapshot()
        self.assertTrue(self.refresh.check_routes()['snapshot_present'])
        config = yaml.safe_load(self.originals['config'])
        dropped = next(r for r in config['model_list'] if r['model_name'].startswith('chatgpt/'))
        config['model_list'].remove(dropped)
        self.refresh.paths['config'].write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, 'differ from saved discovery'):
            self.refresh.check_routes()

    def test_pricing_only_publication_skips_inference_and_routing_changes(self):
        self.install_inventory_baseline()
        self.refresh.pricing_sources = lambda: {'openai': {'astra': [{
            'tier': 'standard', 'context': 'short', 'input_per_million': 2, 'output_per_million': 10}]},
            'models_dev': {}}
        summary = self.prepare()
        self.assertTrue(summary['enrichment_changed'])
        self.assertTrue(summary['catalog_document_changed'])
        self.assertFalse(summary['catalog_changed'])
        self.assertFalse(summary['routing_changed'])
        self.assertFalse(summary['test_reviewer'])
        self.assertEqual(summary['test_models'], [])
        self.refresh.activate()
        with patch('refresh_models.subprocess.run') as run:
            self.assertEqual(self.refresh.validate(), {'validated': []})
            run.assert_not_called()
        self.refresh.finish()
        published = self.refresh.originals()
        self.assertEqual(published['config'], self.originals['config'])
        self.assertEqual(json.loads(published['catalog'])['models'], json.loads(self.originals['catalog'])['models'])
        row = next(r for r in json.loads(published['catalog'])['remote_codex']['models'] if r['model'] == 'chatgpt/astra')
        self.assertEqual(row['pricing']['rates'][0]['input_per_million'], 2)
        with patch.object(self.refresh, 'pricing_sources', side_effect=AssertionError('Unexpected source refresh')):
            self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
            self.assertFalse(self.refresh.candidate(self.snapshot)['summary']['any_changes'])
        self.assertEqual(published, self.refresh.originals())

    def test_pricing_publication_and_recovery_preserve_concurrent_catalog_edit(self):
        self.install_inventory_baseline()
        self.prepare()
        self.refresh.activate()
        with patch('refresh_models.subprocess.run') as run:
            self.refresh.validate()
            run.assert_not_called()
        edited = json.loads(self.refresh.paths['catalog'].read_text())
        edited['concurrent_editor'] = True
        self.refresh.paths['catalog'].write_text(encode(edited))
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            self.refresh.finish()
        with self.assertRaisesRegex(ValueError, 'Concurrent edit'):
            self.refresh.rollback()
        self.assertEqual(json.loads(self.refresh.paths['catalog'].read_text()), edited)

    def test_two_refreshes_cannot_own_the_same_transaction(self):
        self.install_inventory_baseline()
        self.prepare()
        other = Refresh(self.home, self.home / 'runtime', 'uv')
        with self.assertRaisesRegex(ValueError, 'Unfinished refresh'):
            other.prepare()
        self.assertEqual(self.refresh.originals(), self.originals)
        self.refresh.rollback()

    def test_reviewer_failure_blocks_publication_and_rolls_back(self):
        self.prepare()
        self.refresh.activate()
        commands = []
        def probe(command, **kwargs):
            commands.append(command)
            failed = any(arg.endswith('verify_review.py') for arg in command)
            return subprocess.CompletedProcess(command, int(failed), '', 'review route failed' if failed else '')
        with patch('refresh_models.subprocess.run', side_effect=probe):
            with self.assertRaisesRegex(ValueError, 'codex-auto-review: validation failed'):
                self.refresh.validate()
        self.assertTrue(any(any(arg.endswith('verify_review.py') for arg in command) for command in commands))
        self.assertFalse((self.refresh.transaction / 'validated').exists())
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_codex_version_change_revalidates_reviewer(self):
        self.prepare()
        self.refresh.activate()
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'PASS', '')):
            self.refresh.validate()
        self.refresh.finish()
        updated = self.snapshot | {'codex_version': 'next'}
        summary = self.refresh.candidate(updated)['summary']
        self.assertFalse(summary['reviewer_changed'])
        self.assertTrue(summary['test_reviewer'])
        self.assertEqual(summary['test_models'], summary['after'])

    def test_concurrent_edit_is_not_overwritten(self):
        self.prepare()
        self.refresh.paths['config'].write_text('concurrent: true\n')
        with self.assertRaisesRegex(ValueError, 'changed since'):
            self.refresh.activate()
        with self.assertRaisesRegex(ValueError, 'Concurrent edit'):
            self.refresh.rollback()
        self.assertEqual(self.refresh.paths['config'].read_text(), 'concurrent: true\n')

    def test_catalog_rejection_leaves_no_recovery_or_installed_changes(self):
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 1, '', 'invalid catalog')):
            with self.assertRaisesRegex(ValueError, 'rejected'):
                self.refresh.prepare()
        self.assertEqual(self.refresh.originals(), self.originals)
        self.assertFalse(self.refresh.transaction.exists())


if __name__ == '__main__':
    unittest.main()
