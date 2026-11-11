# Fable in Codex

Codex owns permissions and executes tools. Fable uses the existing Bedrock
Anthropic **Invoke/Messages** route, translated to Responses by pinned LiteLLM
1.106.5. Shared search uses the existing GPT-6-Luna backend. This does not add
another agent runtime, change credentials, downgrade effort, drop thinking, or
fall back to a different model. Bedrock does not provide native Responses for
Fable. Converse is not this deployment's validated Fable transport.

## What failed

The investigated chat showed an `OutputTextDelta without active item` warning,
a `content_filter` result, and a reasoning-only `max_output_tokens` failure with
exactly 4096 output/reasoning tokens. Its context was well below the advertised
window. The route was published experimentally with known longer-session
Bedrock 503 failures, not a full acceptance pass.

Reproducible integration defects were found separately from provider failures:

- The global Chat/Responses hook removed signed `encrypted_content` from streamed
  reasoning. Raw replay text alone cannot replace Anthropic signatures.
- Returning plain dictionaries from the response converter defeated upstream
  streamed-item ID alignment, which expects objects with attributes/model_copy.
- The shared-search follow-up conversion used inspection mode rather than
  `replay_reasoning=True`, discarding signed state before calling Bedrock again.
- The route relied on LiteLLM's implicit 4096 output ceiling. Adaptive thinking
  and the answer share that ceiling.
- The pinned Bedrock capability map lacked Fable metadata, so effort could be
  silently stripped or adaptive thinking translated to legacy manual thinking.
- The ordinary tool-loop verifier changed the original user message on replay.
  Signed thinking requires an unchanged preceding prompt/tool prefix.

These findings do not prove that every historical 503 or filter response was
caused by the bridge. Those remain honest failures, not completed empty answers.

## Route defaults

For `bedrock/...claude-fable-5-2`, `configured_model` supplies:

- `max_tokens: 65536` when omitted; explicit integer budgets from 1 to 128000
  are preserved. A client's explicit `max_output_tokens` still wins.
- Adaptive-thinking and low/medium/high/xhigh/max effort capability metadata,
  registered by the stock Router for the underlying Bedrock model.
- `--auto-tool-choice` for ordinary acceptance; Fable cannot accept forced tool
  use with its always-on adaptive thinking.

65536 is headroom, not a guarantee that every high/max-effort request finishes.
Limits and refusals must remain `response.incomplete`, with their real reason.
There is no automatic token escalation, effort downgrade, or inference retry.

The host's existing `use_chat_completions_api: true`, `allowed_openai_params:
[tools, tool_choice]`, and narrow `additional_drop_params: [prompt_cache_key]`
remain part of its configured route. No blanket `drop_params` is required.

## Replay and stream contract

The stock signed-thinking codec is used unchanged. Empty signed thinking and
redacted blocks survive JSON, streaming, tool continuations, and cache/router
resets. Raw thinking is replay state, not an answer or a provider-authored
summary. All falsely labeled summary part/delta/done events are suppressed.

The hook retains typed output objects for upstream ID/index alignment, attaches
signed state at the reasoning-done boundary, and numbers events at emission.
Item-done envelopes and terminal output agree. Answer text remains incremental;
ordinary requests are not buffered until completion. Shared-search's completed
response cache namespace is advanced to avoid reusing old malformed envelopes;
durable search-result records are not deleted or rewritten.

Exact continuation preserves the preceding messages and tool schema. Compaction
starts from a readable checkpoint without attaching old signatures to rewritten
history. It cannot reconstruct signatures already lost by older bridge versions.

The hook is pinned and must be revalidated on a LiteLLM upgrade. Remove it when
upstream alone passes the Kimi and Fable regression suites, including signed
replay and strict item/delta/done/terminal lifecycles.

## Verification and rollout

Offline `scripts/test_fable_bridge.py` uses real Router/Responses conversion and
AWS binary eventstream fixtures split across transport packets. It checks signed
and redacted replay, explicit/default budgets and all efforts on the wire,
parallel/namespaced/custom tools, incomplete/refusal outcomes, shared-search
prefix/tool preservation and budget-sensitive caching, incremental
answers, and stock Codex tool execution, exact resume after cache/hook resets,
and forced compaction. Run socket-based checks with host permissions.

`scripts/verify_fable.py` starts a private disposable candidate proxy. It loads
the existing systemd credentials through `launch.exports`, never copies or
prints them, and does not restart or modify the live service. It runs bounded,
no-retry API/tool-loop, capability/image, high-effort stock-Codex, parallel search,
and stock-Codex search → image → shell continuation acceptance. Successful
staging is removed; a complete pass saves only fingerprints/check results as
`fable-acceptance.json` in the chosen state directory. Failed evidence is kept
until the defect is resolved and acceptance passes, or the route is retired.

Deploy committed source using the existing runtime transaction; finish it only
after live smoke checks. Then use the separate model-refresh transaction to
activate the budget/capability defaults and validate the changed Fable route.
Rollback each transaction with its own journal; do not overlap transactions or
create additional environment/home archives. Retain the historical experimental
failure report until the live route passes full acceptance or is retired.

## Provider documentation

- [Fable overview](https://platform.claude.com/docs/en/models/fable-5-2/overview)
- [Fable migration guide](https://platform.claude.com/docs/en/models/fable-5-2/migration-guide)
- [AWS model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-fable-5-2.html)
