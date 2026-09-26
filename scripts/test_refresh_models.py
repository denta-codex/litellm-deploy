"""Behavioral tests for discovery preservation and recoverable activation."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
from refresh_models import Refresh, encode, generate


def model(slug, visibility='list'):
    return {'slug': slug, 'visibility': visibility, 'base_instructions': 'Native instructions',
            'display_name': slug, 'use_responses_lite': True, 'supports_search_tool': True,
            'context_window': 272000, 'supported_reasoning_levels': [{'effort': 'high'}],
            'future_capability': {'keep': True}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.source = {'models': [model('astra'), model('sol'), model('review', 'hide')]}
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

    def test_reject_empty_duplicate_missing_fields_and_selected_removal(self):
        for source in ({'models': []}, {'models': [model('astra'), model('astra')]},
                       {'models': [{'slug': 'astra', 'visibility': 'list'}]},
                       {'models': [model('sol')]}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                generate(source, self.config, self.catalog, 'chatgpt/astra')

    def test_removed_models_leave_picker_and_routes(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        source = {'models': [model('astra')]}
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
