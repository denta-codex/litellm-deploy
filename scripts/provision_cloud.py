"""Provision Love using restricted cloud credentials already accepted on Grace.

Plaintext travels only in process memory over SSH. Love encrypts it with its own
systemd host key; existing target credentials are validated and never replaced.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import shlex
import subprocess

from bedrock_aws import CREDENTIAL_NAME as AWS_NAME, credential_values as aws_values
from vertex_google import CREDENTIAL, CREDENTIAL_NAME as GOOGLE_NAME, credential_values as google_values


DESTINATION = '/mnt/HC_Volume_103914271/agent/home'
STORE = '/etc/credstore.encrypted'


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=60, **kwargs)
    if result.returncode:
        raise ValueError('Cloud credential provisioning failed; private output suppressed')
    return result.stdout


def provision(provider):
    account = pwd.getpwnam('agent')
    if (socket.gethostname().split('.')[0] != 'grace' or Path(account.pw_dir) != Path('/home/agent')
            or os.geteuid() != account.pw_uid):
        raise ValueError('Run as agent on Grace to provision the explicit Love target')
    name, path, validate = ((AWS_NAME, STORE + '/litellm-bedrock-aws', aws_values)
                           if provider == 'bedrock' else (GOOGLE_NAME, str(CREDENTIAL), google_values))
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'UpdateHostKeys=no', 'love']
    identity = command(ssh + ['hostname; id -un; getent passwd agent'], text=True).splitlines()
    if len(identity) != 3 or identity[:2] != ['love', 'agent'] or identity[2].split(':')[5] != DESTINATION:
        raise ValueError('Love host, account or home identity differs')
    exists = subprocess.run(ssh + ['sudo -n test -f ' + path], capture_output=True, timeout=20)
    if exists.returncode == 0:
        value = command(ssh + ['sudo -n systemd-creds decrypt --name=' + name + ' ' + path + ' -'], text=True)
        validate(value)
        return {'provider': provider, 'host': 'love', 'changed': False, 'credential': path}
    if exists.returncode != 1:
        raise ValueError('Cannot inspect Love credential; no changes made')
    value = command(['sudo', '-n', 'systemd-creds', 'decrypt', '--name=' + name, path, '-'], text=True)
    validate(value)
    # Staging contains ciphertext only. A hard link prevents replacing a credential
    # created concurrently. The trap removes the narrow stage on success or failure.
    script = ('set -eu; umask 077; '
        + 'test ! -e ' + path + '; stage=$(mktemp ' + STORE + '/.litellm-cloud.XXXXXX); '
        + 'trap \'rm -f "$stage"\' EXIT; '
        + 'systemd-creds encrypt --name=' + name + ' - "$stage" >/dev/null 2>&1; '
        + 'chmod 0600 "$stage"; ln "$stage" ' + path)
    command(ssh + [shlex.join(['sudo', '-n', '/bin/sh', '-c', script])], input=value.encode())
    retained = command(ssh + ['sudo -n systemd-creds decrypt --name=' + name + ' ' + path + ' -'], text=True)
    validate(retained)
    if retained != value:
        raise ValueError('Love cloud credential did not retain the accepted value')
    return {'provider': provider, 'host': 'love', 'changed': True, 'credential': path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['love'], required=True)
    parser.add_argument('--provider', choices=['bedrock', 'vertex'], required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args.provider), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Cloud credential provisioning failed; private output suppressed') from None


if __name__ == '__main__':
    main()
