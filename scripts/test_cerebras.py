"""Offline checks for key isolation, route preservation, and service wiring."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

from configure_cerebras import CerebrasSetup, MODEL, ROUTE, generate
from launch import main as launch
from provision_cerebras import provision, read_key
from refresh_models import generate as subscription


class CerebrasTests(unittest.TestCase):
    def test_configuration_is_idempotent_and_subscription_refresh_preserves_route(self):
        config = {'model_list': [{'model_name': 'modal/custom', 'litellm_params': {'model': 'openai/custom'}}],
                  'router_settings': {'num_retries': 2}}
        source = {'models': [{'slug': 'astra', 'visibility': 'list', 'base_instructions': 'Native'},
                            {'slug': 'codex-auto-review', 'visibility': 'hide', 'base_instructions': 'Reviewer'}]}
        catalog = {'models': []}
        routed, picker, _ = generate({'model': MODEL}, config, catalog, '')
        self.assertEqual(picker, catalog)
        self.assertEqual(routed['model_list'][0], config['model_list'][0])
        route = routed['model_list'][-1]
        self.assertEqual(route['litellm_params']['api_key'], 'os.environ/CEREBRAS_API_KEY')
        self.assertEqual(route['litellm_params']['reasoning_effort'], 'none')
        self.assertEqual(routed['router_settings']['num_retries'], 2)
        self.assertTrue(all(v == 0 for v in routed['router_settings']['model_group_retry_policy'][ROUTE].values()))
        again, again_picker, diff = generate({'model': MODEL}, routed, picker, '')
        self.assertEqual((again, again_picker), (routed, picker))
        self.assertEqual(diff['added'] + diff['changed'], [])
        refreshed, _, _ = subscription(source, routed, picker, '')
        self.assertIn(route, refreshed['model_list'])
        self.assertEqual(refreshed['router_settings'], routed['router_settings'])
        with self.assertRaises(ValueError):
            generate({'model': 'wrong-model'}, config, catalog, '')

    def test_dotenv_is_data_and_invalid_key_never_appears_in_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / '.env'
            source.write_text('CEREBRAS_API_KEY="fixture-${UNEXPANDED}"\n')
            self.assertEqual(read_key(source), 'fixture-${UNEXPANDED}')
            for value in ('', 'CEREBRAS_API_KEY="secret with whitespace"\n'):
                source.write_text(value)
                with self.assertRaises(ValueError) as error:
                    read_key(source)
                self.assertNotIn('secret', str(error.exception))

    def test_provisioning_passes_plaintext_only_on_stdin_and_keeps_ciphertext_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / '.env'
            destination = Path(temporary) / 'encrypted' / 'credential'
            source.write_text('CEREBRAS_API_KEY=fixture-secret\n')
            def encrypt(argv, **kwargs):
                self.assertEqual(kwargs['input'], b'fixture-secret')
                self.assertNotIn('fixture-secret', ' '.join(argv))
                Path(argv[-1]).write_bytes(b'ciphertext')
                return subprocess.CompletedProcess(argv, 0)
            with patch('provision_cerebras.subprocess.run', side_effect=encrypt):
                self.assertTrue(provision(source, destination))
            self.assertEqual(destination.read_bytes(), b'ciphertext')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with patch('provision_cerebras.os.link') as link, patch('provision_cerebras.Path.lstat') as info:
                info.return_value.st_mode = 0o100600
                info.return_value.st_uid = 0
                self.assertFalse(provision(source, destination))
                link.assert_not_called()

    def test_launcher_and_unit_load_host_credential_without_embedding_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'litellm-proxy-key').write_text('fixture-proxy')
            (directory / 'modal-inference-token').write_text('wk-fixture.ws-fixture')
            (directory / 'cerebras-api-key').write_text('fixture-cerebras')
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary}), patch('launch.os.execv') as execute, \
                    patch('launch.sys.argv', ['launch.py', '/fixture/config.yaml']):
                launch()
                self.assertEqual(os.environ['CEREBRAS_API_KEY'], 'fixture-cerebras')
                self.assertNotIn('fixture-cerebras', str(execute.call_args))
            (directory / 'cerebras-api-key').write_text('invalid secret')
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary}), patch('launch.os.execv') as execute:
                with self.assertRaises(SystemExit):
                    launch()
                execute.assert_not_called()
        template = Environment(loader=FileSystemLoader(Path(__file__).resolve().parent.parent / 'deploy/templates'),
                               undefined=StrictUndefined).get_template('litellm.service.j2')
        values = {'litellm_user': 'agent', 'litellm_home': '/home/agent', 'litellm_root': '/fixture',
                  'litellm_state': '/fixture/state', 'litellm_config': '/fixture/config', 'litellm_uv': '/bin/uv',
                  'litellm_modal_credential': '/fixture/modal', 'litellm_cerebras_credential': '/fixture/cerebras'}
        self.assertNotIn('cerebras-api-key:', template.render(**values))
        self.assertIn('LoadCredentialEncrypted=cerebras-api-key:/fixture/cerebras',
                      template.render(**values, litellm_cerebras_enabled=True))


    def test_cerebras_activation_does_not_publish_until_validation_and_uses_shared_lock(self):
        from refresh_models import Refresh, encode
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            refresh = CerebrasSetup(home, home / 'runtime', 'uv')
            refresh.state.mkdir(parents=True)
            (home / '.codex').mkdir()
            (home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
            refresh.paths['config'].parent.mkdir(parents=True)
            original_config = {'model_list': [{'model_name': 'chatgpt/astra', 'litellm_params': {'model': 'chatgpt/astra'}}]}
            refresh.paths['config'].write_text(yaml.safe_dump(original_config))
            refresh.paths['catalog'].write_text(encode({'models': []}))
            snapshot = {'version': 1, 'codex_version': 'fixture', 'catalog': {'model': MODEL}}
            originals = refresh.originals()
            with patch.object(refresh, 'discover', return_value=snapshot), patch('refresh_models.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0, '{"models":[]}', '')):
                refresh.prepare()
            with self.assertRaisesRegex(ValueError, 'cerebras.yml'):
                Refresh(home, home / 'runtime', 'uv').rollback()
            refresh.activate()
            self.assertEqual(refresh.paths['catalog'].read_text(), originals['catalog'])
            with self.assertRaisesRegex(ValueError, 'not passed inference validation'):
                refresh.finish()
            with patch('configure_cerebras.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                refresh.validate()
            self.assertEqual(run.call_count, 1)
            self.assertTrue(any('verify_cerebras.py' in arg for arg in run.call_args.args[0]))
            refresh.finish()
            self.assertFalse(refresh.transaction.exists())
            self.assertTrue(refresh.check_snapshot()['snapshot_present'])
            with patch.object(refresh, 'discover', return_value=snapshot):
                self.assertFalse(refresh.prepare(preview=True)['any_changes'])

    def test_cerebras_rejected_validation_restores_previous_routes(self):
        from refresh_models import encode
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            refresh = CerebrasSetup(home, home / 'runtime', 'uv')
            refresh.state.mkdir(parents=True)
            (home / '.codex').mkdir()
            (home / '.codex/config.toml').write_text('model_provider="litellm"\n')
            refresh.paths['config'].parent.mkdir(parents=True)
            refresh.paths['config'].write_text(yaml.safe_dump({'model_list': []}))
            refresh.paths['catalog'].write_text(encode({'models': []}))
            snapshot = {'version': 1, 'codex_version': 'fixture', 'catalog': {'model': MODEL}}
            originals = refresh.originals()
            with patch.object(refresh, 'discover', return_value=snapshot), patch('refresh_models.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0, '{"models":[]}', '')):
                refresh.prepare()
            refresh.activate()
            with patch('configure_cerebras.subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'private response', 'private error')):
                with self.assertRaisesRegex(ValueError, 'failed inference validation') as error:
                    refresh.validate()
                self.assertNotIn('private', str(error.exception))
            self.assertFalse((refresh.transaction / 'validated').exists())
            self.assertTrue(refresh.rollback()['restart_required'])
            self.assertEqual(refresh.originals(), originals)
