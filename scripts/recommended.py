"""Blank separator below recommended rows in the stock Codex picker.

Stock Codex sorts the picker by ascending catalog priority, and subscription
ChatGPT rows start at 1. A configured model is recommended by giving it a
negative priority; one unrouted blank row then sits between those rows and
every other model.
"""
import copy

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


def with_separator(catalog):
    """Place the separator only while a recommended model is listed above it."""
    models = [m for m in catalog.get('models', []) if m.get('slug') != SEPARATOR]
    if any(isinstance(m.get('priority'), int) and m['priority'] < SEPARATOR_PRIORITY
           and m.get('visibility') == 'list' for m in models):
        models.append(copy.deepcopy(SEPARATOR_ROW))
    return dict(catalog, models=models)
