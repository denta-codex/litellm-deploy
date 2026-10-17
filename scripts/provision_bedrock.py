"""Enroll allowlisted models and provision encrypted, restricted Bedrock credentials.

Bootstrap keys are obtained in memory through op-bridge. The private provisioning
record contains only identity/resource metadata and is removed after retirement.
Never replay an uncertain AWS write; inspect AWS and the record before recovery.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from bedrock_aws import (AWS_CONFIG, CREDENTIAL_NAME, MANIFEST, aws_session,
    bootstrap_session, credential_values, discover, encrypted_session, runtime_policy)
from refresh_models import atomic_write, encode

WRITE_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={'total_max_attempts': 1})
POLICY_NAME = 'SelectedBedrockModels'
CLEANUP_POLICY = 'RetireBedrockBootstrap'


def enroll(session, company, website, industry, use_case):
    reader = session.client('bedrock', config=AWS_CONFIG)
    writer = session.client('bedrock', config=WRITE_CONFIG)
    try:
        registered = bool(reader.get_use_case_for_model_access().get('formData'))
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
        registered = False
    if not registered:
        if not company or not website or not industry or not use_case:
            raise ValueError('Anthropic registration requires the actual company, website, industry, and use case')
        form = {'companyName': company, 'companyWebsite': website, 'intendedUsers': '0',
            'industryOption': industry, 'otherIndustryOption': '', 'useCases': use_case}
        writer.put_use_case_for_model_access(formData=json.dumps(form))
        print(encode({'anthropic_registration': 'submitted'}), flush=True)
    for spec in json.loads(MANIFEST.read_text())['models']:
        model_id = spec['model_id']
        access = reader.get_foundation_model_availability(modelId=model_id)
        if access.get('agreementAvailability', {}).get('status') in ('AVAILABLE', 'PENDING'):
            continue
        offers = reader.list_foundation_model_agreement_offers(modelId=model_id, offerType='PUBLIC')['offers']
        if len(offers) != 1:
            raise ValueError('Expected one public usage-based agreement offer for ' + model_id)
        terms = offers[0].get('termDetails', {})
        if 'usageBasedPricingTerm' not in terms or set(terms) - {'usageBasedPricingTerm', 'legalTerm', 'supportTerm'}:
            raise ValueError('Unexpected agreement terms; inspect the offer before accepting')
        writer.create_foundation_model_agreement(modelId=model_id, offerToken=offers[0]['offerToken'])
        print(encode({'model': spec['slug'], 'agreement': 'submitted'}), flush=True)
    return discover(session)


def install_encrypted(value, destination):
    process = subprocess.run(['sudo', '-n', 'systemd-creds', 'encrypt', '--name=' + CREDENTIAL_NAME, '-', '-'],
        input=json.dumps(value).encode(), capture_output=True, timeout=30)
    if process.returncode or not process.stdout:
        raise ValueError('Bedrock credential encryption failed; secret output suppressed')
    with tempfile.NamedTemporaryFile(prefix='bedrock-encrypted-', dir='/tmp') as encrypted:
        encrypted.write(process.stdout)
        encrypted.flush()
        result = subprocess.run(['sudo', '-n', 'install', '-o', 'root', '-g', 'root', '-m', '0600',
            encrypted.name, str(destination)], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Encrypted Bedrock credential installation failed; output suppressed')


def credential_metadata(path):
    result = subprocess.run(['sudo', '-n', 'systemd-creds', 'decrypt', '--name=' + CREDENTIAL_NAME,
        str(path), '-'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Cannot load the encrypted runtime credential; output suppressed')
    return credential_values(result.stdout)


def credential_exists(path):
    result = subprocess.run(['sudo', '-n', 'test', '-e', str(path)], capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot inspect the encrypted credential location')
    return result.returncode == 0


def provision(session, account, user, credential, record):
    identity = session.client('sts', config=AWS_CONFIG).get_caller_identity()
    if identity['Account'] != account or ':user/' not in identity['Arn'] or identity['Arn'].endswith('/' + user):
        raise ValueError('Expected the configured account and a separate bootstrap IAM user')
    source = discover(session)
    policy = runtime_policy(source)
    iam = session.client('iam', config=WRITE_CONFIG)
    if credential_exists(credential):
        value = credential_metadata(credential)
        if value['account_id'] != account or value['user_name'] != user:
            raise ValueError('Existing Bedrock credential belongs to a different identity')
        existing = iam.get_user_policy(UserName=user, PolicyName=POLICY_NAME)['PolicyDocument']
        if existing != policy:
            raise ValueError('Existing runtime policy differs; reconcile explicitly instead of rotating credentials')
        if {m['slug'] for m in discover(aws_session(value))['models']} != {m['slug'] for m in source['models']}:
            raise ValueError('Existing runtime credential cannot discover the selected models')
        if record.exists():
            metadata = json.loads(record.read_text())
            metadata['provisioned'] = True
            atomic_write(record, encode(metadata))
        return {'runtime_user': user, 'credential': str(credential), 'retained': True, 'models': source['models']}
    if record.exists():
        raise ValueError('Unfinished Bedrock provisioning; inspect the private record before retrying')
    try:
        iam.get_user(UserName=user)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'NoSuchEntity':
            raise
    else:
        raise ValueError('Runtime IAM user already exists without its credential; refusing to adopt or rotate it')
    metadata = {'account_id': account, 'bootstrap_user': identity['Arn'].rsplit('/', 1)[-1],
        'bootstrap_key_id': session.get_credentials().access_key, 'runtime_user': user,
        'credential': str(credential), 'models': [m['slug'] for m in source['models']], 'created_user': False}
    atomic_write(record, encode(metadata))
    key_id = None
    try:
        iam.create_user(UserName=user, Tags=[{'Key': 'ManagedBy', 'Value': 'litellm-deploy'}])
        metadata['created_user'] = True
        atomic_write(record, encode(metadata))
        iam.put_user_policy(UserName=user, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy))
        key = iam.create_access_key(UserName=user)['AccessKey']
        key_id = key['AccessKeyId']
        metadata['runtime_key_id'] = key_id
        atomic_write(record, encode(metadata))
        value = dict(access_key_id=key_id, secret_access_key=key['SecretAccessKey'],
            account_id=account, user_name=user, region='us-east-1')
        install_encrypted(value, credential)
        # IAM propagation can lag. Repeat only read checks, never key creation.
        for attempt in range(6):
            try:
                runtime_identity = aws_session(value).client('sts', config=AWS_CONFIG).get_caller_identity()
                if runtime_identity['Arn'] != 'arn:aws:iam::' + account + ':user/' + user:
                    raise ValueError('Runtime AWS identity does not match the provisioned user')
                discovered = discover(aws_session(value))
                if {m['slug'] for m in discovered['models']} != set(metadata['models']):
                    raise ValueError('Restricted runtime identity cannot discover the selected models')
                break
            except ClientError:
                if attempt == 5:
                    raise
                time.sleep(2)
        metadata['provisioned'] = True
        atomic_write(record, encode(metadata))
    except Exception:
        # Keep the encrypted credential and the one metadata record if installation
        # succeeded. They are the recovery mechanism; do not create backup keys.
        if credential_exists(credential):
            raise ValueError('Provisioning verification failed; encrypted credential and private provisioning record retained') from None
        if key_id is not None:
            iam.delete_access_key(UserName=user, AccessKeyId=key_id)
        if metadata['created_user']:
            iam.delete_user_policy(UserName=user, PolicyName=POLICY_NAME)
            iam.delete_user(UserName=user)
        record.unlink()
        raise
    return {'runtime_user': user, 'credential': str(credential), 'models': metadata['models'], 'created': True}


def retire(session, record, snapshot):
    metadata = json.loads(record.read_text())
    validated = json.loads(snapshot.read_text())['catalog']
    if set(metadata['models']) != {m['slug'] for m in validated['models']}:
        raise ValueError('Validated Bedrock snapshot does not match the provisioned models')
    iam = session.client('iam', config=WRITE_CONFIG)
    bootstrap, runtime = metadata['bootstrap_user'], metadata['runtime_user']
    bootstrap_arn = 'arn:aws:iam::' + metadata['account_id'] + ':user/' + bootstrap
    runtime_arn = 'arn:aws:iam::' + metadata['account_id'] + ':user/' + runtime
    attached = iam.list_attached_user_policies(UserName=bootstrap)['AttachedPolicies']
    keys = iam.list_access_keys(UserName=bootstrap)['AccessKeyMetadata']
    if ({p['PolicyName'] for p in attached} - {'IAMFullAccess', 'AmazonBedrockFullAccess'}
            or iam.list_user_policies(UserName=bootstrap)['PolicyNames']
            or iam.list_groups_for_user(UserName=bootstrap)['Groups']
            or {k['AccessKeyId'] for k in keys} != {metadata['bootstrap_key_id']}):
        raise ValueError('Bootstrap user has unrelated credentials or permissions; refusing to delete it')
    try:
        iam.get_login_profile(UserName=bootstrap)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'NoSuchEntity':
            raise
    else:
        raise ValueError('Bootstrap user has console access; refusing to delete it')
    cleanup = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['iam:DetachUserPolicy', 'iam:DeleteAccessKey', 'iam:DeleteUser'], 'Resource': bootstrap_arn},
        {'Effect': 'Allow', 'Action': ['iam:DeleteUserPolicy'], 'Resource': runtime_arn}]}
    iam.put_user_policy(UserName=runtime, PolicyName=CLEANUP_POLICY, PolicyDocument=json.dumps(cleanup))
    time.sleep(3)
    runtime_iam = encrypted_session(Path(metadata['credential'])).client('iam', config=WRITE_CONFIG)
    try:
        for policy in attached:
            runtime_iam.detach_user_policy(UserName=bootstrap, PolicyArn=policy['PolicyArn'])
        runtime_iam.delete_access_key(UserName=bootstrap, AccessKeyId=metadata['bootstrap_key_id'])
        runtime_iam.delete_user(UserName=bootstrap)
    finally:
        runtime_iam.delete_user_policy(UserName=runtime, PolicyName=CLEANUP_POLICY)
    record.unlink()
    return {'retired_bootstrap_user': bootstrap, 'runtime_user': runtime}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'enroll', 'provision', 'retire'])
    parser.add_argument('--access-key-ref', required=True)
    parser.add_argument('--secret-key-ref', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--runtime-user', default='litellm-grace')
    parser.add_argument('--credential', type=Path, default=Path('/etc/credstore.encrypted/litellm-bedrock-aws'))
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/litellm')
    parser.add_argument('--company')
    parser.add_argument('--website')
    parser.add_argument('--industry')
    parser.add_argument('--use-case')
    args = parser.parse_args()
    os.umask(0o077)
    session = bootstrap_session(args.access_key_ref, args.secret_key_ref)
    identity = session.client('sts', config=AWS_CONFIG).get_caller_identity()
    if identity['Account'] != args.account_id or ':user/' not in identity['Arn']:
        raise ValueError('AWS identity differs from the expected account or is not an IAM user')
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state / 'bedrock-provision.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = args.state / 'bedrock-provision.json'
        if args.action == 'inspect':
            result = discover(session, require_agreement=False)
        elif args.action == 'enroll':
            result = enroll(session, args.company, args.website, args.industry, args.use_case)
        elif args.action == 'provision':
            result = provision(session, args.account_id, args.runtime_user, args.credential, record)
        else:
            result = retire(session, record, args.state / 'bedrock-models.json')
        print(encode(result), flush=True)


if __name__ == '__main__':
    try:
        main()
    except ClientError as exc:
        raise SystemExit('AWS operation failed: ' + exc.response.get('Error', {}).get('Code', 'Unknown')) from None
    except BotoCoreError as exc:
        raise SystemExit('AWS operation failed: ' + type(exc).__name__ + '; inspect state before retrying a write') from None
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc)) from None
