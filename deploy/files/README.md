# Codex model metadata

`codex-models.json` is derived from the stock OpenAI Codex 0.155.1 catalog:
https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/models-manager/models.json

OpenAI Codex, Copyright 2025 OpenAI. Apache-2.0; see CODEX-LICENSE.

Changes: append a copy of `gpt-6-astra` as `chatgpt/gpt-6-astra`, with
`use_responses_lite=false`. Hide the original entries from the picker because
they are not LiteLLM aliases; retain their metadata for existing native tasks.
No instructions, context limits, tool modes, or other capabilities are changed.

Codex 0.155.1 suppresses hosted Responses web-search tools for models using
Responses Lite. Its alternative standalone search calls `/v1/alpha/search`,
which this stock LiteLLM deployment does not expose. The supported
`model_catalog_json` configuration selects the hosted Responses search path.
This is static client configuration, not a proxy endpoint or source patch.

This overrides remote catalog refresh globally. Rebase on the installed
Codex catalog and rerun `verify_codex.py` after Codex upgrades. When adding a
LiteLLM model, add accurate metadata and test its tool/search behavior; do not
advertise native search for arbitrary OpenAI-compatible inference endpoints.
Remove this override when stock Codex/LiteLLM interoperation no longer needs it.
