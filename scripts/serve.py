"""Install the request customization in the proxy server's process."""
import argparse

from chatgpt_responses import install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=4000)
    args = parser.parse_args()
    install()
    from claude_provider import install as install_claude
    from litellm.proxy.proxy_server import app
    install_claude(app)
    from shared_search import install as install_search
    install_search()
    from litellm.proxy.proxy_cli import run_server

    # Multiple workers/reload would import upstream in fresh processes and lose
    # the override. This deployment deliberately uses one in-process worker.
    run_server(args=[
        '--config', args.config, '--host', '127.0.0.1', '--port', str(args.port),
        '--num_workers', '1',
    ])


if __name__ == '__main__':
    main()
