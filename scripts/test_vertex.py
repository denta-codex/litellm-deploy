"""Vertex namespace isolation, actual parameter mapping, and failure recovery."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml
from configure_vertex import VertexSetup, generate
from launch import main as launch
from refresh_models import Refresh, encode
from vertex_google import ACCOUNT, PROJECT, LOCATION, MODEL, SLUG, PERMISSIONS, credential_values


def source():return {'project':PROJECT,'location':LOCATION,'model':MODEL,'inference_verified':True}
def credential():return {'type':'service_account','project_id':PROJECT,'client_email':ACCOUNT,
    'token_uri':'https://oauth2.googleapis.com/token','private_key':'-----BEGIN PRIVATE KEY-----\nfixture',
    'private_key_id':'a'*40}


class VertexTests(unittest.TestCase):
    def test_exact_route_and_picker_preserve_other_providers_and_converge(self):
        config={'model_list':[{'model_name':'bedrock/opus-5.5','litellm_params':{'model':'bedrock/exact'}}]}
        catalog={'models':[{'slug':'bedrock/opus-5.5','visibility':'list'}]}
        routed,picker,summary=generate(source(),config,catalog,'chatgpt/astra')
        self.assertEqual(routed['model_list'][:1],config['model_list'])
        self.assertEqual(picker['models'][:1],catalog['models'])
        params=routed['model_list'][-1]['litellm_params']
        self.assertEqual(params['model'],'vertex_ai/'+MODEL)
        self.assertEqual(params['vertex_project'],PROJECT)
        self.assertEqual(params['vertex_location'],'global')
        self.assertEqual(params['vertex_credentials'],'os.environ/VERTEX_CREDENTIALS_PATH')
        self.assertNotIn('api_key',params)
        self.assertEqual(summary['added'],[SLUG])
        self.assertEqual(picker['models'][-1]['tool_mode'],'code_mode_only')
        again_config,again_catalog,again=generate(source(),routed,picker,'chatgpt/astra')
        self.assertEqual((again_config,again_catalog),(routed,picker))
        self.assertEqual(again['test_models'],[])

    def test_refuses_substitution_and_unverified_snapshot(self):
        for name in ('project','location','model','inference_verified'):
            bad=source();bad[name]='wrong'
            with self.subTest(field=name),self.assertRaises(ValueError):generate(bad,{}, {},'')
        with self.assertRaises(ValueError):generate(source(),{}, {},'vertex/other')

    def test_pinned_adapter_sends_levels_not_budgets_and_drops_codex_cache_hint(self):
        from litellm.utils import get_optional_params
        for effort in ('low','medium','high'):
            result=get_optional_params(model=MODEL,custom_llm_provider='vertex_ai',reasoning_effort=effort,
                prompt_cache_key='fixture',additional_drop_params=['prompt_cache_key'])
            self.assertNotIn('reasoning_effort',result)
            self.assertNotIn('prompt_cache_key',result)
            self.assertEqual(result['thinkingConfig']['thinkingLevel'],effort)
            self.assertNotIn('thinkingBudget',json.dumps(result))

    def test_credential_cannot_target_other_accounts_or_token_servers(self):
        self.assertEqual(credential_values(json.dumps(credential())),credential())
        for field in ('type','project_id','client_email','token_uri','private_key','private_key_id'):
            bad=credential();bad[field]='https://attacker.invalid'
            with self.subTest(field=field),self.assertRaises(ValueError):credential_values(json.dumps(bad))
        self.assertEqual(PERMISSIONS,['aiplatform.endpoints.predict','serviceusage.services.use'])

    def test_launcher_passes_credential_path_and_scrubs_ambient_google_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'litellm-proxy-key').write_text('sk-fixture')
            (root/'modal-inference-token').write_text('wk-fixture.ws-fixture')
            (root/'vertex-ai-credentials').write_text(json.dumps(credential()))
            with (patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':temporary,'GEMINI_API_KEY':'ambient',
                    'VERTEXAI_CREDENTIALS':'ambient'}),patch('launch.os.execv') as execute,
                    patch('launch.sys.argv',['launch.py','/config'])):
                launch()
                self.assertEqual(os.environ['VERTEX_CREDENTIALS_PATH'],str(root/'vertex-ai-credentials'))
                self.assertNotIn('GEMINI_API_KEY',os.environ)
                self.assertNotIn('VERTEXAI_CREDENTIALS',os.environ)
                execute.assert_called_once()


class VertexRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=self.enterContext(tempfile.TemporaryDirectory())
        self.home=Path(temporary)
        self.refresh=VertexSetup(self.home,self.home/'runtime','uv')
        self.refresh.state.mkdir(parents=True)
        (self.home/'.codex').mkdir()
        (self.home/'.codex/config.toml').write_text('model_provider="litellm"\nmodel="chatgpt/astra"\n')
        self.refresh.paths['config'].parent.mkdir(parents=True)
        self.refresh.paths['config'].write_text(yaml.safe_dump({'model_list':[
            {'model_name':'bedrock/opus-5.5','litellm_params':{'model':'bedrock/exact'}}]}))
        self.refresh.paths['catalog'].write_text(encode({'models':[{'slug':'bedrock/opus-5.5'}]}))
        self.snapshot={'version':1,'codex_version':'test','catalog':source()}
        self.originals=self.refresh.originals()
        self.refresh.pricing_sources=lambda:{'openai':None,'models_dev':None}
        candidate=self.refresh.candidate(self.snapshot)
        with patch.object(self.refresh,'discover',return_value=self.snapshot),patch('refresh_models.subprocess.run',
                return_value=subprocess.CompletedProcess([],0,candidate['candidates']['catalog'],'')):
            self.refresh.prepare()

    def test_failed_vertex_validation_can_restore_bedrock_without_publication(self):
        self.refresh.activate()
        with patch('configure_vertex.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','')):
            with self.assertRaisesRegex(ValueError,'capability/search'):self.refresh.validate()
        with self.assertRaisesRegex(ValueError,'not passed'):self.refresh.finish()
        self.refresh.rollback()
        self.assertEqual(self.refresh.originals(),self.originals)

    def test_other_namespaces_identify_vertex_recovery_owner(self):
        with self.assertRaisesRegex(ValueError,'vertex.yml'):Refresh(self.home,self.home/'runtime','uv').rollback()

    def test_success_preserves_default_and_converges(self):
        config=(self.home/'.codex/config.toml').read_bytes()
        self.refresh.activate()
        with patch('configure_vertex.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')):
            self.refresh.validate()
        self.refresh.finish()
        self.assertEqual(config,(self.home/'.codex/config.toml').read_bytes())
        self.assertFalse(self.refresh.candidate(self.snapshot)['summary']['any_changes'])


if __name__=='__main__':unittest.main()
