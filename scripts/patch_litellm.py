"""Preserve ChatGPT service_tier in the pinned LiteLLM release until upstream fixes it."""
import argparse
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import tempfile

VERSION = '1.102.1'
SOURCE_SHA256 = 'cb474993f56e1dec8a458b1852dad28d0f7a2afc93a737bd0b917259f5c0498c'
ANCHOR = b'            "truncation",\n'
REPLACEMENT = ANCHOR + b'            "service_tier",\n'


def patched_source(source):
    if hashlib.sha256(source).hexdigest() == SOURCE_SHA256:
        if source.count(ANCHOR) != 1:
            raise ValueError('Unexpected ChatGPT allowlist; review the service-tier patch')
        return source.replace(ANCHOR, REPLACEMENT, 1)
    if source.count(REPLACEMENT) == 1:
        original = source.replace(REPLACEMENT, ANCHOR, 1)
        if hashlib.sha256(original).hexdigest() == SOURCE_SHA256:
            return source
    raise ValueError('Unrecognized LiteLLM ChatGPT source; review the service-tier patch')


def apply_patch(path, version, check=False, restore=False):
    if version != VERSION:
        raise ValueError('LiteLLM version changed; review or retire the service-tier patch')
    source = path.read_bytes()
    candidate = patched_source(source)
    if restore:
        candidate = candidate.replace(REPLACEMENT, ANCHOR, 1)
    changed = candidate != source
    if changed and not check:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(candidate)
                output.flush()
                os.fsync(output.fileno())
                temporary.chmod(path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return {'changed': changed, 'check': check, 'restore': restore, 'version': version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--restore', action='store_true', help='Restore the verified upstream source for rollback')
    args = parser.parse_args()
    package = util.find_spec('litellm')
    if package is None or package.origin is None:
        raise ValueError('Install the locked LiteLLM environment first')
    path = Path(package.origin).parent / 'llms/chatgpt/responses/transformation.py'
    print(json.dumps(apply_patch(path, metadata.version('litellm'), check=args.check, restore=args.restore)))


if __name__ == '__main__':
    main()
