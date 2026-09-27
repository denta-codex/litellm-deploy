"""Behavioral tests for discovery preservation and recoverable activation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
from refresh_models import REVIEW_MODEL, Refresh, encode, generate


def model(slug, visibility='list'):
    return {'slug': slug, 'visibility': visibility, 'base_instructions': 'Native instructions',
            'display_name': slug, 'use_responses_lite': True, 'supports_search_tool': True,
            'context_window': 272000, 'supported_reasoning_levels': [{'effort': 'high'}],
            'future_capability': {'keep': True}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.source = {'models': [model('astra'), model('sol'), model(REVIEW_MODEL, 'hide')]}
        self.external = {'model_name': 'modal/custom', 'litellm_params': {'model': 'openai/private', 'api_key': 'os.environ/MODAL_KEY'}}
        self.config = {'model_list': [self.external], 'general_settings': {'master_key': 'os.environ/LITELLM_MASTER_KEY'}}
        self.catalog = {'models': [model('modal/custom'), model('retired-native', 'hide')]}

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


class ActivationTests(unittest.TestCase):
    def setUp(self):
        DiscoveryTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.refresh = Refresh(self.home, self.home / 'runtime', 'uv')
        self.refresh.state.mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump(self.config))
        self.refresh.paths['catalog'].write_text(encode(self.catalog))
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': self.source}
        self.originals = self.refresh.originals()

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            return self.refresh.prepare()

    def test_preview_does_not_write_or_run_model_validation(self):
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            summary = self.refresh.prepare(preview=True)
        self.assertTrue(summary['any_changes'])
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
        self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            result = self.refresh.prepare()
        self.assertFalse(result['any_changes'])
        run.assert_not_called()

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
