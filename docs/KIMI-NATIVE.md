# Kimi in Codex

Codex remains the tool executor and permission authority. Kimi K3.1 Pro uses the
existing Modal OpenAI-compatible Chat Completions route. Search continues through
the existing shared-search backend; this change does not replace that backend or
change provider authentication.

## State contract

The official Kimi K3.1 Pro model card requires the complete prior assistant
message for multi-turn conversations and tool calls, including
`reasoning_content` and `tool_calls`. LiteLLM 1.106.5 already converts Responses
reasoning items into that field and merges them into the following assistant
message. The repository therefore does **not** add an encrypted-state adapter.

Offline tests exercise JSON and streaming responses, cache flushes, router and
hook reinitialization, ordinary/custom/parallel tools, and shared-search
continuations. A readable compaction checkpoint starts without the discarded
turn's native reasoning. Portable cross-provider history may quote readable
reasoning for the compactor, but it is not attached as native reasoning to a
rewritten assistant message.

Source: <https://huggingface.co/moonshotai/Kimi-K3.1-Pro/blob/main/README.md>

## Instructions and reasoning display

`kimi-instructions.md` covers Codex's actual tools, permissions, deferred-tool
discovery, literal patches, long-context discipline, focused investigation, and
verification. Routes identified as Moonshot Chat Completions models use it unless
the owner explicitly configures different instructions.

Raw `reasoning_content` is retained for provider continuation, not advertised as
a provider-authored concise summary. `supports_reasoning_summaries`, its default,
and its request-parameter flag remain disabled unless an owner deliberately
overrides them. Answer deltas still stream directly; no completed-response
buffering wrapper is added for ordinary Kimi traffic.

## Typed events

The pinned bridge aligns final reasoning/message items with their streamed IDs
and preserves raw replay content without rendering it as answer text. Shared
search and compaction use `ResponsesEvents` for ordered item/delta/done events.
Only explicit summary arrays produce reasoning-summary events; reasoning content
never becomes answer text. Search's completed-response cache identity is
versioned so old malformed envelopes are not replayed.

## Reviewable commits

1. `Preserve Kimi reasoning through Responses continuations` covers the pinned
   bridge, shared renderer, Kimi bridge tests, and typed-event tests.
2. `Give Kimi Codex-native operating instructions` adds the prompt, automatic
   Moonshot catalog selection, and its configuration tests.
3. `Document Kimi rollout and acceptance` records the contract, verification,
   rollout, and recovery procedure.

## Verification and rollout

Run the locked offline suite:

```sh
uv run --no-sync -m unittest discover -s scripts -p 'test_*.py'
```

The Kimi tests use synthetic Modal credentials and mocked HTTP. They verify that
the next provider request contains the exact prior `reasoning_content`, content,
and tool-call IDs, and that a fresh readable checkpoint has no stale reasoning.
No paid model-quality benchmark is part of this rollout.

Deploy the committed source with the existing `runtime_rollout.py`
prepare/deploy/finish transaction and publish the catalog with
`refresh-models.yml`. Roll back runtime files with `runtime_rollout.py rollback`
and model metadata with `refresh-models.yml -e refresh_action=rollback` if live
acceptance fails. No additional whole-environment backup is created.
