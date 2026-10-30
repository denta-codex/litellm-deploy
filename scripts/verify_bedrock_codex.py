"""Stock Codex must view a local image after search, then retain tool/image context."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import tomlkit

from deployment import codex_binary, proxy_key
from verify_bedrock import image_fixture
from verify_claude import RPC, stop


async def verify(args):
    state = Path.home()/'.local/state/litellm'
    directory = Path(tempfile.mkdtemp(prefix='bedrock-image-acceptance-', dir=state))
    process = None
    success = False
    try:
        image = directory/'fixture.png'
        image.write_bytes(base64.b64decode(image_fixture().split(',', 1)[1]))
        config = {'model': args.model, 'model_provider': 'image_acceptance',
            'model_catalog_json': str(args.catalog.resolve()), 'model_reasoning_effort': 'low',
            'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'live',
            'features': {'shell_snapshot': False, 'code_mode_host': True},
            'shell_environment_policy': {'exclude': ['BEDROCK_TEST_PROXY_KEY']},
            'model_providers': {'image_acceptance': {'name': 'Bedrock image acceptance',
                'base_url': args.base_url.rstrip('/')+'/v1', 'wire_api': 'responses',
                'env_key': 'BEDROCK_TEST_PROXY_KEY', 'requires_openai_auth': False,
                'supports_websockets': False, 'request_max_retries': 0, 'stream_max_retries': 0}}}
        (directory/'config.toml').write_text(tomlkit.dumps(config))
        with (directory/'codex.log').open('w') as log:
            process = await asyncio.create_subprocess_exec(str(codex_binary()), 'app-server', cwd=directory,
                env=os.environ | {'CODEX_HOME': str(directory), 'BEDROCK_TEST_PROXY_KEY': proxy_key()},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log, start_new_session=True)
            rpc = RPC(process)
            try:
                await rpc.call('initialize', {'clientInfo': {'name': 'bedrock_image_acceptance', 'version': '1'},
                    'capabilities': {'experimentalApi': True}})
                await rpc.send({'method': 'initialized', 'params': {}})
                thread = (await rpc.call('thread/start', {'model': args.model, 'modelProvider': 'image_acceptance',
                    'cwd': str(directory), 'approvalPolicy': 'never', 'sandbox': 'read-only',
                    'baseInstructions': 'Follow the user. Be concise. Use requested tools.'}))['thread']['id']
                reply, items = await rpc.turn(thread, 'Use native web search to find the official uv Python '
                    'installation documentation. Do not use shell or another tool for research. Give its full URL.')
                (directory/'search-items.json').write_text(json.dumps(items, indent=2))
                assert any(i['type'] == 'webSearch' for i in items), 'Missing native search item'
                assert 'https://docs.astral.sh/uv/' in reply, 'Missing official source URL'
                reply, items = await rpc.turn(thread, 'Use view_image to inspect '+str(image)+
                    '. Do not inspect its bytes with shell or code. What is its dominant color? '
                    'Reply with only the color name.')
                (directory/'image-items.json').write_text(json.dumps(items, indent=2))
                assert any(i['type'] == 'imageView' for i in items), 'Missing stock Codex view_image item'
                assert reply.strip().lower().rstrip('.') == 'blue', 'Image was not recognized'
                print('PASS '+args.model+': stock Codex search followed by view_image recognition', flush=True)
                reply, items = await rpc.turn(thread, 'Use the shell tool to run exactly: printf IMAGE_CONTEXT_OK . '
                    'Report its output, the earlier image color, and the official URL from the earlier search. '
                    'Do not view the image or search again.')
                (directory/'followup-items.json').write_text(json.dumps(items, indent=2))
                assert any(i['type'] == 'commandExecution' and i.get('exitCode') == 0
                    and 'IMAGE_CONTEXT_OK' in i.get('aggregatedOutput', '') for i in items), 'Missing shell result'
                assert 'IMAGE_CONTEXT_OK' in reply and 'blue' in reply.lower() and 'https://docs.astral.sh/uv/' in reply,                     'Lost image/search/client-tool context'
                print('PASS '+args.model+': shell result and image/search continuation', flush=True)
                success = True
            finally:
                await stop(process)
                rpc.reader.cancel()
                await asyncio.gather(rpc.reader, return_exceptions=True)
    finally:
        if success:
            shutil.rmtree(directory)
        else:
            print('Retained failed image acceptance evidence: '+str(directory))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='bedrock/gpt-6.1-sol')
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:4000')
    asyncio.run(verify(parser.parse_args()))
