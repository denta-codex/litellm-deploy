"""Configure only Gemini 4 Argon through the shared activation transaction."""
import copy
import subprocess
from pathlib import Path

from catalog_enrichment import stock_catalog
from refresh_models import Refresh, main
from vertex_google import PROJECT, LOCATION, MODEL, SLUG, discover, encrypted_credentials

PREFIX = 'vertex/'


def generate(source, config, catalog, selected):
    if source != {'project':PROJECT, 'location':LOCATION, 'model':MODEL, 'inference_verified':True}:
        raise ValueError('Vertex snapshot must identify the verified exact Argon project and endpoint')
    if selected.startswith(PREFIX) and selected != SLUG:
        raise ValueError('Selected Vertex model is outside the exact Argon allowlist')
    route = {'model_name':SLUG, 'model_info':{'mode':'chat'}, 'litellm_params':{
        'model':'vertex_ai/'+MODEL, 'vertex_project':PROJECT, 'vertex_location':LOCATION,
        'vertex_credentials':'os.environ/VERTEX_CREDENTIALS_PATH',
        'use_chat_completions_api':True, 'reasoning_effort':'medium',
        'additional_drop_params':['prompt_cache_key'], 'num_retries':0}}
    entry = {
        'slug':SLUG, 'display_name':'Gemini 4 Argon (Vertex AI)',
        'description':'Gemini 4 Argon; billed to the Grandstand Google Cloud project.',
        'base_instructions':"You are Codex, a coding agent. Follow the user's request and use tools when needed.",
        'model_messages':{}, 'visibility':'list', 'supported_in_api':True,
        'context_window':1048576, 'max_context_window':1048576,
        'truncation_policy':{'mode':'tokens','limit':10000}, 'input_modalities':['text','image'],
        'supported_reasoning_levels':[{'effort':e,'description':e} for e in ('low','medium','high')],
        'default_reasoning_level':'medium', 'default_reasoning_summary':'none',
        'supports_parallel_tool_calls':True, 'supports_reasoning_summaries':False,
        'supports_search_tool':True, 'web_search_tool_type':'text_and_image',
        'supports_reasoning_summary_parameter':False, 'supports_reasoning_effort_updates':False,
        'supports_image_detail_original':False, 'support_verbosity':False,
        'use_responses_lite':False, 'prefer_websockets':False,
        'tool_mode':'code_mode_only', 'shell_type':'shell_command', 'apply_patch_tool_type':'freeform',
        'experimental_supported_tools':[], 'service_tiers':[], 'additional_speed_tiers':[],
        'availability_nux':None, 'available_access_programs':{'cyber':[]}, 'priority':86,
    }
    before = {m['slug']:m for m in stock_catalog(catalog).get('models',[]) if m['slug'].startswith(PREFIX)}
    old_routes = {r['model_name']:r for r in config.get('model_list',[]) if r['model_name'].startswith(PREFIX)}
    policy = old_routes.get(SLUG,{}).get('model_info',{}).get('catalog_pricing')
    if policy is not None:route['model_info']['catalog_pricing'] = copy.deepcopy(policy)
    new_config,new_catalog = copy.deepcopy(config),copy.deepcopy(catalog)
    new_config['model_list'] = [r for r in new_config.get('model_list',[]) if not r['model_name'].startswith(PREFIX)] + [route]
    new_catalog['models'] = [r for r in new_catalog.get('models',[]) if not r['slug'].startswith(PREFIX)] + [entry]
    added = [] if SLUG in before else [SLUG]
    changed = [SLUG] if SLUG in before and (before[SLUG] != entry or old_routes.get(SLUG) != route) else []
    return new_config,new_catalog,{
        'added':added, 'removed':sorted(set(before)-{SLUG}), 'changed':changed,
        'changed_fields':{SLUG:sorted(k for k in set(before[SLUG])|set(entry) if before[SLUG].get(k)!=entry.get(k))} if changed else {},
        'test_models':added+changed, 'before':sorted(before), 'after':[SLUG]}


class VertexSetup(Refresh):
    prefix = PREFIX
    playbook = 'vertex.yml'
    generate = staticmethod(generate)

    def __init__(self, home, root, uv):
        super().__init__(home,root,uv)
        self.paths['snapshot'] = self.state / 'vertex-models.json'

    def discover(self):
        version = subprocess.check_output([self.codex,'--version'],text=True).strip().removeprefix('codex-cli ')
        return {'version':1, 'codex_version':version, 'catalog':discover(encrypted_credentials())}

    def validate(self):
        self.journal()
        with (self.transaction/'vertex-validation.log').open('w') as log:
            for script,extra in [('verify_vertex.py',[]), ('verify_shared_search.py',['--model',SLUG])]:
                result = subprocess.run([self.uv,'run','--no-sync','--project',str(self.root),
                    str(Path(__file__).with_name(script)),'--catalog',str(self.transaction/'catalog.json'),*extra],
                    stdout=log,stderr=subprocess.STDOUT,timeout=900)
                if result.returncode:
                    raise ValueError('Vertex capability/search validation failed; see private model-refresh/vertex-validation.log')
        return super().validate()


if __name__ == '__main__':main(VertexSetup)
