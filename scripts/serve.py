"""Install the request customization in the proxy server's process."""
import argparse
import os

from chatgpt_responses import install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=4000)
    args = parser.parse_args()
    install()
    from litellm.proxy.proxy_server import app
    if os.environ.get('LITELLM_CLAUDE_ENABLED', 'true') == 'true':
        from claude_provider import install as install_claude
        install_claude(app)
    else:
        # Shared search/compaction and Responses errors also need request context.
        from claude_context import CaptureRequest
        app.add_middleware(CaptureRequest)
    # Temporary subscription signaling adapter; retirement steps: docs/VOICE.md.
    if os.environ.get('LITELLM_VOICE_ENABLED', 'true') == 'true':
        from live_voice import install as install_voice
        install_voice(app)
    from shared_search import install as install_search
    install_search()
    from compaction_fallback import install as install_compaction
    install_compaction()
    from litellm.proxy.proxy_cli import run_server

    # Multiple workers/reload would import upstream in fresh processes and lose
    # the override. This deployment deliberately uses one in-process worker.
    run_server(args=[
        '--config', args.config, '--host', '127.0.0.1', '--port', str(args.port),
        '--num_workers', '1',
    ])


if __name__ == '__main__':
    main()
