"""Import the existing dotenv key into a root-owned encrypted systemd credential."""
import argparse
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from dotenv import dotenv_values


def read_key(path):
    key = dotenv_values(path, interpolate=False).get('CEREBRAS_API_KEY')
    if not key or any(c.isspace() for c in key) or any(c in key for c in '\x00\r\n'):
        raise ValueError('Missing or invalid CEREBRAS_API_KEY in credential source')
    return key


def provision(source, destination):
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Existing Cerebras credential must be a root-owned private regular file')
        return False
    key = read_key(source)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cerebras-', dir=destination.parent) as temporary:
        encrypted = Path(temporary) / 'credential'
        result = subprocess.run(['/usr/bin/systemd-creds', 'encrypt', '--with-key=host',
            '--name=cerebras-api-key', '-', str(encrypted)], input=key.encode(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError('Could not encrypt Cerebras credential')
        encrypted.chmod(0o600)
        # Refuse replacement if another import completed concurrently.
        os.link(encrypted, destination)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--credential', type=Path, default=Path('/etc/credstore.encrypted/cerebras-api-key'))
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise SystemExit('Credential provisioning requires root')
    try:
        print('created' if provision(args.env_file, args.credential) else 'retained')
    except (OSError, ValueError):
        raise SystemExit('Cerebras credential import failed; check the source and encrypted destination') from None


if __name__ == '__main__':
    main()
