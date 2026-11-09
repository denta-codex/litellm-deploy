"""Separate the latest request's context from aggregate inference consumption."""
import copy

from claude_context import REQUEST


def sample(usage, *, claude=False):
    if not usage:
        return None
    if claude:
        if 'input_tokens' not in usage or 'output_tokens' not in usage:
            return None
        reads = usage.get('cache_read_input_tokens', 0)
        writes = usage.get('cache_creation_input_tokens', 0)
        inputs = usage['input_tokens'] + reads + writes
        outputs = usage['output_tokens']
    else:
        inputs = usage.get('input_tokens', usage.get('prompt_tokens', 0))
        outputs = usage.get('output_tokens', usage.get('completion_tokens', 0))
        details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
        reads = details.get('cached_tokens', 0) or 0
        writes = details.get('cache_write_tokens', 0) or details.get('cache_creation_tokens', 0) or 0
    return {'input_tokens': inputs, 'output_tokens': outputs, 'total_tokens': inputs + outputs,
            'input_tokens_details': {'cached_tokens': reads, 'cache_write_tokens': writes}}


def total(samples):
    result = sample({'input_tokens': 0, 'output_tokens': 0})
    for value in samples:
        if value is None:
            continue
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            result[key] += value[key]
        for key in ('cached_tokens', 'cache_write_tokens'):
            result['input_tokens_details'][key] += value['input_tokens_details'].get(key, 0)
    return result


def accounting(latest, receipts):
    return {'usage_accounting_version': 2, 'latest_request_usage': copy.deepcopy(latest),
            'consumed_usage': total(r['usage'] for r in receipts), 'receipts': copy.deepcopy(receipts)}


def publish(receipts):
    ledger = REQUEST.get().setdefault('usage_receipts', {})
    for receipt in receipts:
        ledger.setdefault(receipt['id'], copy.deepcopy(receipt))
