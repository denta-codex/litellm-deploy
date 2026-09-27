"""Offline Modal configuration, credential, and activation regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from configure_modal import API_BASE, MANIFEST, ModalSetup, generate
from launch import main as launch, modal_token
from refresh_models import Refresh, encode, generate as generate_subscription


class CredentialTests(unittest.TestCase):
    def test_launcher_exports_credentials_only_to_child_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'litellm-proxy-key').write_text('sk-test-proxy\n')
            (directory / 'modal-inference-token').write_text('WK_SECRET=wk-test\nWS_SECRET=ws-test\n')
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary}), patch('launch.os.execv') as execute, \
                    patch('launch.sys.argv', ['launch.py', '/fixture/config.yaml']):
                launch()
                self.assertEqual(os.environ['LITELLM_MASTER_KEY'], 'sk-test-proxy')
                self.assertEqual(os.environ['MODAL_API_KEY'], 'wk-test.ws-test')
                server = str(Path(__file__).with_name('serve.py'))
                execute.assert_called_once_with(sys.executable, [sys.executable, server, '--config', '/fixture/config.yaml',
                                                                '--port', '4000'])
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ['litellm-proxy-key', 'modal-inference-token'])

    def test_missing_credential_fails_before_starting_proxy(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary}), \
                patch('launch.os.execv') as execute:
            with self.assertRaisesRegex(SystemExit, 'Cannot load LiteLLM systemd credentials'):
                launch()
            execute.assert_not_called()

    def test_retained_credential_formats(self):
        for value in ('wk-test.ws-test\n', 'MODAL_PROXY_TOKEN="wk-test.ws-test"',
                      "# retained credential\nexport WK_SECRET='wk-test'\nWS_SECRET=ws-test\n"):
            self.assertEqual(modal_token(value), 'wk-test.ws-test')

    def test_malformed_credentials_fail_without_exposing_content(self):
        for value in ('', 'WK_SECRET=wk-test', 'wk-test.ws-test\rInjected: secret',
                      'WK_SECRET=$(secret)\nWS_SECRET=ws-test', 'MODAL_PROXY_TOKEN=wk-test.ws-test\nUNKNOWN=secret',
                      'WK_SECRET=wk-test\nWK_SECRET=wk-again\nWS_SECRET=ws-test'):
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                modal_token(value)
            self.assertNotIn('secret', str(error.exception))


class ModalTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads(MANIFEST.read_text())
        self.subscription = {'model_name': 'chatgpt/astra', 'model_info': {'mode': 'responses'},
                             'litellm_params': {'model': 'chatgpt/astra', 'extra_headers': {'keep': 'yes'}}}
        self.native = {'slug': 'astra', 'visibility': 'list', 'base_instructions': 'Native',
                       'supports_search_tool': True, 'context_window': 272000}
        self.reviewer = {'slug': 'codex-auto-review', 'visibility': 'hide', 'base_instructions': 'Reviewer',
                         'use_responses_lite': False}
        self.review_route = {'model_name': 'codex-auto-review', 'model_info': {'mode': 'responses'},
                             'litellm_params': {'model': 'chatgpt/codex-auto-review'}}
        self.config = {'model_list': [self.subscription, self.review_route],
                       'general_settings': {'master_key': 'os.environ/LITELLM_MASTER_KEY'}}
        self.catalog = {'models': [self.native | {'slug': 'chatgpt/astra', 'use_responses_lite': False},
                                   self.native | {'visibility': 'hide'}, self.reviewer]}

    def test_routes_preserve_subscription_and_converge(self):
        config, catalog, summary = generate(self.source, self.config, self.catalog, 'chatgpt/astra')
        self.assertEqual(config['model_list'][0], self.subscription)
        self.assertEqual(config['model_list'][1], self.review_route)
        self.assertEqual(config['general_settings'], self.config['general_settings'])
        self.assertEqual(catalog['models'][:3], self.catalog['models'])
        self.assertEqual(len(summary['added']), 3)
        for route in config['model_list'][2:]:
            params = route['litellm_params']
            self.assertEqual(params['api_base'], API_BASE)
            self.assertEqual(params['api_key'], 'os.environ/MODAL_API_KEY')
            self.assertTrue(params['use_chat_completions_api'])
            self.assertEqual(route['model_info']['mode'], 'chat')
        for entry in catalog['models'][3:]:
            self.assertFalse(entry['supports_search_tool'])
            self.assertFalse(entry['prefer_websockets'])
            self.assertFalse(entry['use_responses_lite'])
        again_config, again_catalog, again = generate(self.source, config, catalog, 'chatgpt/astra')
        self.assertEqual((config, catalog), (again_config, again_catalog))
        self.assertEqual(again['test_models'], [])
        refreshed_config, refreshed_catalog, _ = generate_subscription({'models': [self.native, self.reviewer]}, config, catalog, 'chatgpt/astra')
        self.assertEqual([m for m in refreshed_catalog['models'] if m['slug'].startswith('modal/')], catalog['models'][3:])
        self.assertEqual([r for r in refreshed_config['model_list'] if r['model_name'].startswith('modal/')], config['model_list'][2:])

    def test_manifest_rejections_and_selected_removal(self):
        for source in ({'models': []}, {'models': [self.source['models'][0]] * 2},
                       {'models': [self.source['models'][0] | {'endpoint': 'https://untrusted.invalid'}]},
                       {'models': [self.source['models'][0] | {'context_window': 0}]}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                generate(source, self.config, self.catalog, '')
        with self.assertRaisesRegex(ValueError, 'Selected Modal model'):
            generate(self.source, self.config, self.catalog, 'modal/removed')

    def test_changed_route_is_validated(self):
        config, catalog, _ = generate(self.source, self.config, self.catalog, '')
        config['model_list'][2]['litellm_params']['api_base'] = 'https://old.invalid'
        _, _, summary = generate(self.source, config, catalog, '')
        self.assertEqual(summary['test_models'], [config['model_list'][2]['model_name']])


class ActivationTests(ModalTests):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.refresh = ModalSetup(self.home, self.home / 'runtime', 'uv')
        self.refresh.state.mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump(self.config))
        self.refresh.paths['catalog'].write_text(encode(self.catalog))
        self.subscription_snapshot = self.refresh.state / 'subscription-models.json'
        self.subscription_snapshot.write_text(encode({'version': 1, 'codex_version': 'test', 'catalog': {'models': [self.native, self.reviewer]}}))
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': self.source}
        self.originals = self.refresh.originals()

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            return self.refresh.prepare()

    def test_preview_never_changes_files_or_runs_inference(self):
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            result = self.refresh.prepare(preview=True)
        self.assertEqual(len(result['added']), 3)
        self.assertEqual(self.refresh.originals(), self.originals)
        self.assertFalse(self.refresh.transaction.exists())
        run.assert_not_called()

    def test_activation_checks_all_models_then_publishes_and_converges(self):
        subscription_before = self.subscription_snapshot.read_bytes()
        self.prepare()
        self.refresh.activate()
        self.assertEqual(self.refresh.paths['catalog'].read_text(), self.originals['catalog'])
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.refresh.validate()
        self.assertEqual(run.call_count, 6)
        codex_calls = [c.args[0] for c in run.call_args_list if any('verify_codex.py' in a for a in c.args[0])]
        self.assertEqual(len(codex_calls), 3)
        self.assertTrue(all('--skip-search' in c for c in codex_calls))
        self.refresh.finish()
        self.assertEqual(self.subscription_snapshot.read_bytes(), subscription_before)
        self.assertTrue(self.refresh.check_snapshot()['snapshot_present'])
        self.assertTrue(Refresh(self.home, self.home / 'runtime', 'uv').check_snapshot()['snapshot_present'])
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run') as run:
            self.assertFalse(self.refresh.prepare()['any_changes'])
        run.assert_not_called()

    def test_failure_restores_exact_previous_state(self):
        self.prepare()
        self.refresh.activate()
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
            with self.assertRaisesRegex(ValueError, 'validation failed'):
                self.refresh.validate()
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_followup_modal_change_does_not_probe_reviewer(self):
        self.prepare()
        self.refresh.activate()
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.refresh.validate()
        self.refresh.finish()
        self.snapshot['catalog']['models'][0]['display_name'] += ' revised'
        summary = self.prepare()
        self.assertEqual(len(summary['test_models']), 1)
        self.assertFalse(summary.get('test_reviewer', False))
        self.refresh.activate()
        with patch('refresh_models.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.refresh.validate()
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any('verify_review.py' in arg for call in run.call_args_list for arg in call.args[0]))
        self.refresh.finish()
        self.assertIn(self.review_route, yaml.safe_load(self.refresh.paths['config'].read_text())['model_list'])

    def test_shared_lock_and_recovery_ownership(self):
        self.prepare()
        other = Refresh(self.home, self.home / 'runtime', 'uv')
        with self.assertRaisesRegex(ValueError, 'modal.yml'):
            other.prepare()
        with self.assertRaisesRegex(ValueError, 'modal.yml'):
            other.rollback()
        self.assertEqual(self.refresh.originals(), self.originals)

    def test_rollback_preserves_concurrent_edits(self):
        self.prepare()
        self.refresh.activate()
        self.refresh.paths['catalog'].write_text('concurrent edit')
        with self.assertRaisesRegex(ValueError, 'Concurrent edit'):
            self.refresh.rollback()
        self.assertEqual(self.refresh.paths['catalog'].read_text(), 'concurrent edit')


if __name__ == '__main__':
    unittest.main()
