"""Pricing copies embedded in LiteLLM's canonical Codex catalog, never inventory."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import urllib.request

OPENAI_URL = 'https://developers.openai.com/api/docs/pricing.md'
MODELS_DEV_URL = 'https://models.dev/api.json'
RATE_FIELDS = ('input_per_million', 'output_per_million', 'cached_input_per_million', 'cache_write_per_million')
HEADER = ['Model', 'Short context input', 'Short context cached input', 'Short context cache writes',
          'Short context output', 'Long context input', 'Long context cached input',
          'Long context cache writes', 'Long context output']


def stock_catalog(document):
    return {key: value for key, value in document.items() if key != 'remote_codex'}


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_metadata(metadata):
    if (not isinstance(metadata, dict) or type(metadata.get('schema_version')) is not int or metadata.get('schema_version') != 1
            or not isinstance(metadata.get('revision'), str)
            or not re.fullmatch('[a-f0-9]{64}', metadata['revision'])
            or not isinstance(metadata.get('models'), list)):
        raise ValueError('Invalid catalog enrichment schema')
    identities = set()
    for row in metadata['models']:
        if not isinstance(row, dict):
            raise ValueError('Invalid enrichment record')
        provider, model = row.get('provider'), row.get('model')
        aliases = row.get('aliases', [])
        if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip() or not isinstance(aliases, list):
            raise ValueError('Invalid enrichment identity')
        for identity in [model, *aliases]:
            if not isinstance(identity, str) or not identity.strip() or (provider, identity) in identities:
                raise ValueError('Ambiguous enrichment identity')
            identities.add((provider, identity))
        upstream = row.get('upstream')
        if upstream is not None and (not isinstance(upstream, dict) or any(
                not isinstance(upstream.get(key), str) or not upstream[key].strip() for key in ('provider', 'model'))):
            raise ValueError('Invalid upstream pricing identity')
        price = row.get('pricing')
        if price is None:
            continue
        if (not isinstance(price, dict) or price.get('currency') != 'USD'
                or price.get('status') not in ('fresh', 'stale')
                or not isinstance(price.get('source_url'), str) or not price['source_url']
                or not isinstance(price.get('rates'), list) or not price['rates']):
            raise ValueError('Invalid catalog pricing')
        try:
            timestamp = datetime.fromisoformat(price['fetched_at'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('Missing pricing time zone')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('Invalid pricing timestamp') from None
        seen_rates = set()
        for rate in price['rates']:
            if (not isinstance(rate, dict) or rate.get('model') != model or not isinstance(rate.get('tier'), str)
                    or not rate['tier'] or rate.get('context') not in ('short', 'long')
                    or (rate['tier'], rate['context']) in seen_rates):
                raise ValueError('Invalid pricing variant')
            seen_rates.add((rate['tier'], rate['context']))
            for field in RATE_FIELDS:
                if field not in rate and field in RATE_FIELDS[2:]:
                    continue
                if not valid_number(rate.get(field)):
                    raise ValueError('Invalid catalog rate')
    return metadata


def parse_openai(markdown):
    """Read only official flagship Standard, Flex and Fast-mode tables."""
    result, tier, flagship = {}, None, False
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if line == 'Flagship models':
            flagship = True
        elif flagship and line == 'Cyber models':
            break
        if not flagship:
            continue
        if line.startswith('### '):
            # Bind rates to the table's heading, not preceding UI tab text.
            # Unrecognized tables (Batch, Ultrafast, etc.) must reset the tier.
            tier = {'### Standard pricing data': 'standard', '### Flex pricing data': 'flex',
                    '### Fast pricing data': 'fast', '### Fast mode pricing data': 'fast'}.get(line)
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        if tier is None or cells != HEADER:
            continue
        if index >= len(lines) or any(cell.strip(' :-') for cell in lines[index].strip().strip('|').split('|')):
            raise ValueError('Invalid OpenAI pricing table')
        index += 1
        while index < len(lines) and lines[index].strip().startswith('|'):
            values = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
            index += 1
            if len(values) != len(HEADER):
                raise ValueError('Invalid OpenAI pricing row')
            model = re.sub(r' \(<272K context length\)$', '', values[0]).strip()
            for context, offset in (('short', 1), ('long', 5)):
                rates = {}
                for field, value in zip((RATE_FIELDS[0], RATE_FIELDS[2], RATE_FIELDS[3], RATE_FIELDS[1]), values[offset:offset + 4]):
                    if value in ('', '-'):
                        continue
                    if not value.startswith('$'):
                        raise ValueError('Invalid OpenAI price')
                    number = float(value[1:].replace(',', ''))
                    if not valid_number(number):
                        raise ValueError('Invalid OpenAI price')
                    rates[field] = number
                if RATE_FIELDS[0] in rates and RATE_FIELDS[1] in rates:
                    result.setdefault(model, []).append({'tier': tier, 'context': context, **rates})
    for model, rows in result.items():
        if not model or len({(r['tier'], r['context']) for r in rows}) != len(rows):
            raise ValueError('Duplicate OpenAI pricing variant')
    if {r['tier'] for rows in result.values() for r in rows} != {'standard', 'flex', 'fast'}:
        raise ValueError('Incomplete OpenAI flagship pricing tables')
    return result


def _fetch(url, maximum):
    # models.dev rejects urllib's default agent; identify the refresh client.
    request = urllib.request.Request(url, headers={
        'User-Agent': 'litellm-deploy-model-refresh/1',
        'Accept': 'application/json' if url == MODELS_DEV_URL else 'text/markdown',
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('Pricing source too large')
    return data


def fetch_sources():
    def fetch(name):
        try:
            if name == 'openai':
                return parse_openai(_fetch(OPENAI_URL, 8 * 1024 * 1024).decode('utf-8'))
            data = json.loads(_fetch(MODELS_DEV_URL, 32 * 1024 * 1024))
            if not isinstance(data, dict) or not data or not any(
                    isinstance(p, dict) and isinstance(p.get('models'), dict) and p['models'] for p in data.values()):
                raise ValueError('Invalid models.dev source')
            return data
        except Exception:
            # A failed source is represented explicitly, without credentials or
            # upstream response bodies in operational logs.
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        return dict(zip(('openai', 'models_dev'), executor.map(fetch, ('openai', 'models_dev'))))


def _identity(name, routes, modal_models):
    route = routes.get(name, {})
    policy = route.get('model_info', {}).get('catalog_pricing', {})
    if not isinstance(policy, dict) or type(policy.get('zero_rate', False)) is not bool:
        raise ValueError('Invalid explicit pricing policy')
    upstream = None
    model = route.get('litellm_params', {}).get('model', '')
    if policy.get('zero_rate'):
        upstream = {'provider': policy.get('provider', 'local'), 'model': policy.get('model', name)}
    elif 'provider' in policy or 'model' in policy:
        upstream = {'provider': policy.get('provider'), 'model': policy.get('model')}
    elif name.startswith('modal/'):
        # Endpoint names and display names are not upstream model identities.
        upstream = copy.deepcopy(modal_models.get(name, {}).get('pricing_model'))
    elif isinstance(model, str) and model.startswith('chatgpt/'):
        upstream = {'provider': 'openai', 'model': model.removeprefix('chatgpt/')}
    elif isinstance(model, str) and model.startswith('claudesdk/'):
        upstream = {'provider': 'anthropic', 'model': model.removeprefix('claudesdk/')}
    elif isinstance(model, str) and '/' in model:
        provider, native = model.split('/', 1)
        upstream = {'provider': provider, 'model': native}
    elif 'chatgpt/' + name in routes:
        # Hidden native IDs are explicitly paired with their configured route.
        routed, _ = _identity('chatgpt/' + name, routes, modal_models)
        upstream = routed
    return upstream, policy


def enrich_catalog(document, config, modal_manifest, sources, fetched_at=None):
    """Generate enrichment from existing inventory; never add stock model rows."""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    try:
        previous = validate_metadata(document.get('remote_codex'))
    except ValueError:
        previous = {'models': []}
    old = {(r['provider'], r['model']): r for r in previous['models']}
    records = copy.deepcopy(old)
    routes = {r['model_name']: r for r in config.get('model_list', [])}
    modal_models = {'modal/' + spec['endpoint']: spec for spec in modal_manifest.get('models', [])}
    policies = {}
    for row in document.get('models', []):
        name = row['slug']
        upstream, policy = _identity(name, routes, modal_models)
        record = {'provider': 'litellm', 'model': name, 'estimate_kind': 'api_equivalent'}
        if upstream is not None:
            record['upstream'] = upstream
        if 'aliases' in policy:
            record['aliases'] = policy['aliases']
        records[('litellm', name)] = record
        policies[name] = policy
    stale, unavailable = 0, 0
    for key, record in records.items():
        record.pop('pricing', None)
        upstream = record.get('upstream')
        previous_row = old.get(key, {})
        pricing = None
        if policies.get(record['model'], {}).get('zero_rate'):
            pricing = {'source_url': 'local://configured-zero-rate', 'rates': [{
                'tier': 'standard', 'context': 'short', **dict.fromkeys(RATE_FIELDS, 0.0)}]}
        elif upstream:
            source = sources.get('openai' if upstream.get('provider') == 'openai' else 'models_dev')
            try:
                rates = None
                url = OPENAI_URL if upstream.get('provider') == 'openai' else MODELS_DEV_URL
                if source is None:
                    raise ValueError('Pricing source unavailable')
                if upstream.get('provider') == 'openai':
                    rates = source.get(upstream['model'])
                else:
                    model = source.get(upstream['provider'], {}).get('models', {}).get(upstream['model'], {})
                    cost = model.get('cost')
                    if cost is not None:
                        if not isinstance(cost, dict) or any(not valid_number(cost.get(k)) for k in ('input', 'output')):
                            raise ValueError('Invalid provider rates')
                        rates = [{'tier': 'standard', 'context': 'short', 'input_per_million': cost['input'], 'output_per_million': cost['output']}]
                        for src, dst in (('cache_read', RATE_FIELDS[2]), ('cache_write', RATE_FIELDS[3])):
                            if src in cost:
                                if not valid_number(cost[src]):
                                    raise ValueError('Invalid provider cache price')
                                rates[0][dst] = cost[src]
                if rates:
                    # A public zero-priced listing is not our policy for hosting
                    # costs. Only an explicit route zero_rate policy grants zero.
                    if all(rate.get('input_per_million') == 0 and rate.get('output_per_million') == 0 for rate in rates):
                        rates = None
                    else:
                        pricing = {'source_url': url, 'rates': copy.deepcopy(rates)}
            except (ValueError, TypeError, AttributeError, KeyError):
                # Retain only a validated price for this exact upstream identity.
                if previous_row.get('upstream') == upstream and previous_row.get('pricing'):
                    record['pricing'] = copy.deepcopy(previous_row['pricing'])
                    record['pricing']['status'] = 'stale'
                    stale += 1
        if pricing:
            record['pricing'] = {'currency': 'USD', 'fetched_at': fetched_at, 'status': 'fresh', **pricing}
            for rate in record['pricing']['rates']:
                rate['model'] = record['model']
        elif 'pricing' not in record:
            unavailable += 1
    metadata = {'schema_version': 1, 'models': [records[k] for k in sorted(records)]}
    content = {'catalog': stock_catalog(document), 'models': metadata['models']}
    metadata['revision'] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    validate_metadata(metadata)
    result = dict(document, remote_codex=metadata)
    return result, {'stale_prices': stale, 'unpriced_models': unavailable,
                    'pricing_sources': {k: ('fresh' if v is not None else 'unavailable') for k, v in sources.items()}}
