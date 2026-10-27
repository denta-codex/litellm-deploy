"""Bedrock allowlist, restricted AWS policy, and credential regressions."""
import copy
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bedrock_aws import MANIFEST, credential_values, discover, runtime_policy
from configure_bedrock import BedrockSetup, generate
from launch import main as launch
from refresh_models import Refresh, encode, main as refresh_main, generate as refresh_subscription
import yaml


def fixture_source():
    manifest = json.loads(MANIFEST.read_text())
    return {'region': manifest['region'], 'models': [dict(m,
        profile_arn='arn:aws:bedrock:us-east-1:123456789012:inference-profile/' + m['profile_id'],
        model_arns=['arn:aws:bedrock:' + region + '::foundation-model/' + m['model_id']
            for region in ('us-east-1', 'us-east-2', 'us-west-2')],
        input_modalities=['text', 'image'], streaming=True) for m in manifest['models']], 'unavailable': []}


class BedrockTests(unittest.TestCase):
    def test_routes_are_exact_and_survive_subscription_refresh(self):
        source = fixture_source()
        native = {'slug': 'astra', 'visibility': 'list', 'base_instructions': 'Native'}
        reviewer = {'slug': 'codex-auto-review', 'visibility': 'hide', 'base_instructions': 'Reviewer'}
        original = {'model_list': [{'model_name': 'chatgpt/astra', 'litellm_params': {'model': 'chatgpt/astra'}}]}
        config, catalog, summary = generate(source, original, {'models': []}, 'chatgpt/astra')
        self.assertEqual(config['model_list'][0], original['model_list'][0])
        for route, spec in zip(config['model_list'][1:], source['models']):
            params = route['litellm_params']
            self.assertEqual(params['model'], 'bedrock/converse/' + spec['profile_id'])
            self.assertEqual(params['aws_access_key_id'], 'os.environ/AWS_ACCESS_KEY_ID')
            self.assertNotIn('fallbacks', params)
        self.assertEqual(summary['added'], sorted(m['slug'] for m in source['models']))
        again_config, again_catalog, again = generate(source, config, catalog, 'chatgpt/astra')
        self.assertEqual((again_config, again_catalog), (config, catalog))
        self.assertEqual(again['test_models'], [])
        refreshed, refreshed_catalog, _ = refresh_subscription({'models': [native, reviewer]}, config, catalog, 'chatgpt/astra')
        self.assertEqual([r for r in refreshed['model_list'] if r['model_name'].startswith('bedrock/')], config['model_list'][1:])
        self.assertEqual([m for m in refreshed_catalog['models'] if m['slug'].startswith('bedrock/')], catalog['models'])

    def test_no_substitution_or_silent_selected_model_removal(self):
        for edit in ('unknown', 'substitute', 'duplicate', 'empty', 'no_streaming'):
            source = fixture_source()
            if edit == 'unknown':
                source['models'][0]['slug'] = 'bedrock/unrequested'
            elif edit == 'substitute':
                source['models'][0]['profile_id'] = 'us.anthropic.claude-opus-5'
            elif edit == 'duplicate':
                source['models'].append(source['models'][0])
            elif edit == 'empty':
                source['models'] = []
            else:
                source['models'][0]['streaming'] = False
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                generate(source, {}, {}, '')
        source = fixture_source()
        source['models'] = source['models'][:1]
        with self.assertRaisesRegex(ValueError, 'Both requested'):
            generate(source, {}, {}, 'bedrock/gpt-6.1-sol')

    def test_runtime_policy_cannot_invoke_other_models_or_manage_iam(self):
        source = fixture_source()
        policy = runtime_policy(source)
        for statement in policy['Statement']:
            self.assertNotIn('*', statement['Resource'])
            self.assertFalse(any(a.startswith(('iam:', 'aws-marketplace:')) for a in statement['Action']))
        invocation = policy['Statement'][0]
        expected = {m['profile_arn'] for m in source['models']} | {a for m in source['models'] for a in m['model_arns']}
        self.assertEqual(set(invocation['Resource']), expected)
        with self.assertRaises(ValueError):
            runtime_policy({'models': []})

    def test_discovery_requires_exact_profile_and_account_access(self):
        source = fixture_source()
        client = Mock()
        models = {m['model_id']: m for m in source['models']}
        client.get_foundation_model.side_effect = lambda modelIdentifier: {'modelDetails': {
            'modelId': modelIdentifier, 'modelLifecycle': {'status': 'ACTIVE'},
            'inputModalities': ['TEXT', 'IMAGE'], 'responseStreamingSupported': True}}
        client.get_foundation_model_availability.return_value = {
            'authorizationStatus': 'AUTHORIZED', 'entitlementAvailability': 'AVAILABLE',
            'regionAvailability': 'AVAILABLE', 'agreementAvailability': {'status': 'AVAILABLE'}}
        client.get_inference_profile.side_effect = lambda inferenceProfileIdentifier: {
            'inferenceProfileId': inferenceProfileIdentifier, 'status': 'ACTIVE', 'type': 'SYSTEM_DEFINED',
            'inferenceProfileArn': next(m['profile_arn'] for m in models.values() if m['profile_id'] == inferenceProfileIdentifier),
            'models': [{'modelArn': a} for m in models.values() if m['profile_id'] == inferenceProfileIdentifier for a in m['model_arns']]}
        session = Mock()
        session.client.return_value = client
        self.assertEqual(discover(session), source)
        client.get_foundation_model_availability.return_value['agreementAvailability'] = {'status': 'NOT_AVAILABLE'}
        self.assertEqual(discover(session)['models'], [])
        self.assertEqual(len(discover(session, require_agreement=False)['models']), 2)
        client.get_inference_profile.side_effect = None
        client.get_inference_profile.return_value = {'inferenceProfileId': 'us.wrong', 'status': 'ACTIVE',
            'type': 'SYSTEM_DEFINED', 'models': [{'modelArn': 'arn:aws:bedrock:us-east-1::foundation-model/wrong'}]}
        with self.assertRaisesRegex(ValueError, 'exact allowlisted'):
            discover(session, require_agreement=False)

    def test_launcher_loads_only_encrypted_pair_and_scrubs_ambient_credentials(self):
        value = {'access_key_id': 'AKIA' + 'A' * 16, 'secret_access_key': 'B' * 40,
            'account_id': '123456789012', 'user_name': 'litellm-grace', 'region': 'us-east-1'}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'litellm-proxy-key').write_text('sk-fixture')
            (directory / 'modal-inference-token').write_text('wk-fixture.ws-fixture')
            (directory / 'bedrock-aws-credentials').write_text(json.dumps(value))
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary, 'AWS_SESSION_TOKEN': 'unrelated',
                    'AWS_BEARER_TOKEN_BEDROCK': 'unrelated', 'AWS_PROFILE': 'unrelated'}), \
                    patch('launch.os.execv') as execute, patch('launch.sys.argv', ['launch.py', '/config']):
                launch()
                self.assertEqual(os.environ['AWS_ACCESS_KEY_ID'], value['access_key_id'])
                self.assertEqual(os.environ['AWS_SECRET_ACCESS_KEY'], value['secret_access_key'])
                self.assertNotIn('AWS_SESSION_TOKEN', os.environ)
                self.assertNotIn('AWS_BEARER_TOKEN_BEDROCK', os.environ)
                self.assertNotIn('AWS_PROFILE', os.environ)
                execute.assert_called_once()
            (directory / 'bedrock-aws-credentials').write_text('malformed secret')
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': temporary}), patch('launch.os.execv') as execute:
                with self.assertRaises(SystemExit) as error:
                    launch()
                self.assertNotIn('malformed', str(error.exception))
                execute.assert_not_called()
        for field in value:
            bad = copy.deepcopy(value)
            bad[field] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                credential_values(json.dumps(bad))


class BedrockActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.refresh = BedrockSetup(self.home, self.home / 'runtime', 'uv')
        self.refresh.pricing_sources = lambda: {'openai': None, 'models_dev': None}
        self.refresh.state.mkdir(parents=True)
        (self.home / '.codex').mkdir()
        (self.home / '.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.config = {'model_list': [
            {'model_name': name, 'litellm_params': {'model': name}}
            for name in ('chatgpt/astra', 'local/qwen3.8-27b', 'modal/example', 'claude/opus-5.5')]}
        self.catalog = {'models': [{'slug': r['model_name']} for r in self.config['model_list']]}
        self.refresh.paths['config'].write_text(yaml.safe_dump(self.config))
        self.refresh.paths['catalog'].write_text(encode(self.catalog))
        self.snapshot = {'version': 1, 'codex_version': 'test', 'catalog': fixture_source()}
        self.originals = self.refresh.originals()

    def prepare(self):
        candidate = self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh, 'discover', return_value=self.snapshot), patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0, candidate['candidates']['catalog'], '')):
            return self.refresh.prepare()

    def test_failure_blocks_publication_and_rollback_restores_files(self):
        self.prepare()
        self.refresh.activate()
        with patch('configure_bedrock.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            with self.assertRaisesRegex(ValueError, 'capability validation failed'):
                self.refresh.validate()
        with self.assertRaisesRegex(ValueError, 'not passed'):
            self.refresh.finish()
        self.refresh.rollback()
        with patch('sys.argv', ['configure_bedrock.py', 'cleanup-rollback', '--home', str(self.home)]):
            refresh_main(BedrockSetup)
        self.assertEqual(self.refresh.originals(), self.originals)
        self.assertFalse(self.refresh.transaction.exists())

    def test_shared_lock_identifies_bedrock_recovery_owner(self):
        self.prepare()
        other = Refresh(self.home, self.home / 'runtime', 'uv')
        with self.assertRaisesRegex(ValueError, 'bedrock.yml'):
            other.rollback()

    def test_publish_after_all_probes_preserves_selection_and_converges(self):
        self.prepare()
        self.refresh.activate()
        self.assertEqual(self.refresh.paths['catalog'].read_text(), self.originals['catalog'])
        with patch('configure_bedrock.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.refresh.validate()
        self.assertEqual(run.call_count, 7)
        self.refresh.finish()
        catalog = json.loads(self.refresh.paths['catalog'].read_text())
        self.assertEqual(catalog['models'][:4], self.catalog['models'])
        self.assertEqual((self.home / '.codex/config.toml').read_text(),
                         'model_provider="litellm"\nmodel="chatgpt/astra"\n')
        result = self.refresh.candidate(self.snapshot)
        self.assertFalse(result['summary']['any_changes'])
        self.assertEqual(result['summary']['test_models'], [])

    def test_preserves_explicit_pricing_policy(self):
        config, catalog, _ = generate(fixture_source(), self.config, self.catalog, '')
        policy = {'provider': 'amazon-bedrock', 'model': 'anthropic.claude-opus-5-5'}
        config['model_list'][-2]['model_info']['catalog_pricing'] = policy
        regenerated, _, summary = generate(fixture_source(), config, catalog, '')
        self.assertEqual(regenerated['model_list'][-2]['model_info']['catalog_pricing'], policy)
        self.assertEqual(summary['test_models'], [])


if __name__ == '__main__':
    unittest.main()
