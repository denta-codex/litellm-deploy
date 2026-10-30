"""Recommended rows at the top of the stock Codex picker, followed by a blank separator.

Stock Codex sorts the picker by ascending catalog priority, and subscription
ChatGPT rows start at 1. Recommended rows therefore use negative priorities, and
one unrouted blank row sits between them and every other model.
"""
import copy

RECOMMENDED = ('claude/opus-5.5', 'bedrock/gpt-6.1-sol', 'vertex/gemini-4-argon')
SEPARATOR = 'separator'
SEPARATOR_PRIORITY = -1

# A display row only: it has no LiteLLM route, so compaction fallback and
# pricing enrichment skip it. Choosing it fails with LiteLLM's unknown-model error.
SEPARATOR_ROW = {
    'slug': SEPARATOR, 'display_name': '', 'description': '', 'visibility': 'list',
    'priority': SEPARATOR_PRIORITY, 'base_instructions': '', 'context_window': None,
    'supported_reasoning_levels': [], 'default_reasoning_level': None,
    'shell_type': 'shell_command', 'supported_in_api': True,
    'truncation_policy': {'mode': 'tokens', 'limit': 10000},
    'supports_reasoning_summaries': False, 'support_verbosity': False, 'default_verbosity': None,
    'experimental_supported_tools': [], 'input_modalities': ['text'], 'apply_patch_tool_type': None,
    'supports_parallel_tool_calls': False, 'availability_nux': None, 'upgrade': None,
}


def priority(slug, default):
    """Picker priority for a provider row: recommended order first, otherwise the provider default."""
    if slug in RECOMMENDED:
        return SEPARATOR_PRIORITY - len(RECOMMENDED) + RECOMMENDED.index(slug)
    return default


def with_separator(catalog):
    """Place the separator only while a recommended model is listed above it."""
    models = [m for m in catalog.get('models', []) if m.get('slug') != SEPARATOR]
    if any(m.get('slug') in RECOMMENDED and m.get('visibility') == 'list' for m in models):
        models.append(copy.deepcopy(SEPARATOR_ROW))
    return dict(catalog, models=models)
