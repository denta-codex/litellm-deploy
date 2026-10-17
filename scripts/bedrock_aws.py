"""Bounded AWS discovery and credential parsing for the explicit Bedrock allowlist."""
import json
from pathlib import Path
import re
import subprocess

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

MANIFEST = Path(__file__).with_name('bedrock-models.json')
CREDENTIAL_NAME = 'bedrock-aws-credentials'
AWS_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={'total_max_attempts': 2})


def credential_values(text):
    value = json.loads(text)
    if (not isinstance(value, dict)
            or set(value) != {'access_key_id', 'secret_access_key', 'account_id', 'user_name', 'region'}
            or not all(isinstance(v, str) for v in value.values())
            or not re.fullmatch(r'AKIA[A-Z0-9]{16}', value.get('access_key_id', ''))
            or not re.fullmatch(r'[A-Za-z0-9/+=]{40}', value.get('secret_access_key', ''))
            or not re.fullmatch(r'[0-9]{12}', value.get('account_id', ''))
            or not re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,64}', value.get('user_name', ''))
            or value.get('region') != 'us-east-1'):
        raise ValueError('Invalid encrypted Bedrock AWS credential')
    return value


def aws_session(value):
    return boto3.Session(aws_access_key_id=value['access_key_id'],
        aws_secret_access_key=value['secret_access_key'], region_name=value['region'])


def bootstrap_session(access_key_ref, secret_key_ref):
    for ref in (access_key_ref, secret_key_ref):
        if not ref.startswith('op://') or any(c in ref for c in '\r\n{}'):
            raise ValueError('Expected a literal 1Password field reference')
    try:
        result = subprocess.run(['op-bridge', '--timeout', '180', 'inject'],
            input='{{ ' + access_key_ref + ' }}\n{{ ' + secret_key_ref + ' }}',
            capture_output=True, text=True, timeout=195)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('1Password credential retrieval failed; secret output suppressed') from None
    if result.returncode:
        raise ValueError('1Password credential retrieval failed; secret output suppressed')
    fields = result.stdout.strip().splitlines()
    if (len(fields) != 2 or not re.fullmatch(r'AKIA[A-Z0-9]{16}', fields[0])
            or not re.fullmatch(r'[A-Za-z0-9/+=]{40}', fields[1])):
        raise ValueError('1Password did not return a valid AWS access key pair')
    return boto3.Session(aws_access_key_id=fields[0], aws_secret_access_key=fields[1], region_name='us-east-1')


def encrypted_session(path):
    result = subprocess.run(['sudo', '-n', 'systemd-creds', 'decrypt',
        '--name=' + CREDENTIAL_NAME, str(path), '-'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Cannot decrypt the Bedrock credential; secret output suppressed')
    return aws_session(credential_values(result.stdout))


def discover(session, require_agreement=True):
    """Resolve only requested US profiles; record unavailable exact models without substitutes."""
    manifest = json.loads(MANIFEST.read_text())
    client = session.client('bedrock', config=AWS_CONFIG)
    available, unavailable = [], []
    for spec in manifest['models']:
        try:
            model = client.get_foundation_model(modelIdentifier=spec['model_id'])['modelDetails']
            access = client.get_foundation_model_availability(modelId=spec['model_id'])
            profile = client.get_inference_profile(inferenceProfileIdentifier=spec['profile_id'])
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code', 'Unknown')
            if code not in ('ResourceNotFoundException', 'ValidationException'):
                raise ValueError('Bedrock discovery failed: ' + code) from None
            unavailable.append({'slug': spec['slug'], 'reason': code})
            continue
        ready = (model.get('modelLifecycle', {}).get('status') == 'ACTIVE'
            and access.get('authorizationStatus') == 'AUTHORIZED'
            and access.get('entitlementAvailability') == 'AVAILABLE'
            and access.get('regionAvailability') == 'AVAILABLE'
            and profile.get('status') == 'ACTIVE'
            and (not require_agreement or access.get('agreementAvailability', {}).get('status') == 'AVAILABLE'))
        if not ready:
            unavailable.append({'slug': spec['slug'], 'reason': 'Account access or agreement is not ready',
                'authorization': access.get('authorizationStatus'), 'entitlement': access.get('entitlementAvailability'),
                'region': access.get('regionAvailability'), 'agreement': access.get('agreementAvailability', {}).get('status')})
            continue
        if (model['modelId'] != spec['model_id'] or profile['inferenceProfileId'] != spec['profile_id']
                or profile.get('type') != 'SYSTEM_DEFINED' or not profile.get('models')
                or any(m['modelArn'].rsplit('/', 1)[-1] != spec['model_id'] for m in profile['models'])):
            raise ValueError('Bedrock profile does not resolve to the exact allowlisted model')
        available.append(dict(spec, profile_arn=profile['inferenceProfileArn'],
            model_arns=sorted(m['modelArn'] for m in profile['models']),
            input_modalities=[m.lower() for m in model['inputModalities']],
            streaming=model.get('responseStreamingSupported', False)))
    return {'region': manifest['region'], 'models': available, 'unavailable': unavailable}


def runtime_policy(source):
    profiles = sorted(m['profile_arn'] for m in source['models'])
    models = sorted({a for m in source['models'] for a in m['model_arns']})
    if not profiles or not models:
        raise ValueError('No available requested Bedrock models; no runtime identity will be created')
    return {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'SelectedModelInvocation', 'Effect': 'Allow',
         'Action': ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'], 'Resource': profiles + models},
        {'Sid': 'SelectedModelDiscovery', 'Effect': 'Allow',
         'Action': ['bedrock:GetFoundationModel', 'bedrock:GetFoundationModelAvailability'], 'Resource': models},
        {'Sid': 'SelectedProfileDiscovery', 'Effect': 'Allow',
         'Action': ['bedrock:GetInferenceProfile'], 'Resource': profiles},
    ]}
