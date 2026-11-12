# Gemini in Codex

The repository-owned adapter targets LiteLLM 1.106.5 and Vertex Gemini routes.
Codex remains the sole client-tool executor and approval authority. This does
not introduce an Antigravity agent or change the shared-search backend.

## Native state contract

Native assistant parts, including thought signatures, are sealed with AES-256-GCM
in a versioned `reasoning.encrypted_content` carrier. The opaque carrier follows
the public assistant batch, so a signature arriving at stream end can be retained.
It authenticates the caller, provider/model, carrier ID, and original public
message/tool-call IDs and content. Citations and lifecycle fields are not part of
the content binding. A carrier is not a visible reasoning summary.
New Gemini tool IDs and tool-cache entries do not carry a second, unsealed copy
of the signature; otherwise model switching could replay incompatible state even
after removing the carrier. Native function-call IDs remain intact inside state.

Replay uses the same key after a restart; no tool cache or conversation database
is required. Corruption, cross-caller replay, missing batch items, or edited
model-visible content fail before inference. Model switching retains public
history but does not send foreign opaque state. Older chats without carriers
remain readable; already-lost signatures cannot be recovered.

Compaction deliberately establishes a new readable checkpoint. Retained original
assistant batches keep their own carriers. Removed batches and their signatures
must not be transplanted onto rewritten checkpoint text. Subsequent Gemini output
produces fresh native state.

Stock Codex records absent reasoning content as JSON `null`. Compaction's
readable-history conversion accepts those optional null fields, preserving any
readable summaries and completed tool relationships without copying the old
opaque carriers into a rewritten checkpoint. Normal compaction provider timeouts
and the existing configured recovery traversal remain unchanged.

## Key ownership and recovery

Deployment initializes `gemini/state.key` beneath the configured application
state directory, plus a non-secret `.provisioned` marker. The service receives
its location through `GEMINI_STATE_KEY_FILE`. The file is a 32-byte random key,
owned by the running account with mode `0600`; its directory is private. A
symlink, incorrect owner/mode, or malformed key is rejected.

Provisioning is atomic and idempotent. Neither runtime nor later deployments
replace a missing previously provisioned key. Restore the original key from your
normal state recovery mechanism. Do not generate a replacement to make the error
disappear: it would make existing carriers unreadable. Automatic key rotation is
not part of this change.

The runtime rollout journal intentionally excludes this durable key and marker.
Prepare/finish/rollback restores changed managed code and the dependency lock,
not the key or a whole environment. No provider authentication is modified.

## Offline verification

Configured `vertex_ai/gemini-*` routes default to the shipped
`gemini-instructions.md` using the existing catalog instructions-file mechanism.
Explicit `codex.base_instructions` or `codex.base_instructions_file` overrides
remain authoritative. Tool mode, schemas, and reasoning effort are unchanged.
Gemini catalog rows now advertise reasoning summaries with default `auto` after
offline end-to-end acceptance. Medium remains the default; high is selectable.
Explicit catalog overrides still win. The example drops only `prompt_cache_key`,
an OpenAI caching hint that Vertex does not implement, as existing routes do.

## Streaming contract

For streamed Gemini requests, the adapter consumes Vertex's real provider SSE
stream, captures every native part (including signature-only chunks), and relays
available text/marked summaries into the existing presentation queue. LiteLLM's
agentic search engine still receives a complete Chat response per iteration.
Internal search arguments are never streamed as client tool calls. Hosted search
items come from the existing search interceptor; client calls wait for their
complete assistant batch. The backend and search execution loop are unchanged.

Only native parts marked `thought: true` can become visible Gemini summaries.
They use reasoning-summary events; message items alone use answer-text events.
Opaque state is never decoded for display. One typed renderer owns stable item
IDs, ordered output indexes, and added/delta/done events. Completed output equals
the streamed items, including state carriers, citations, and usage. Late summaries
keep the order in which public items first appeared, rather than renumbering text
already sent to Codex. Compaction uses the same typed renderer but still buffers
its checkpoint atomically; it does not publish an unfinished summary.

Disconnects close/cancel the provider stream and the search task. Provider failures
are terminal Responses errors; sensitive provider exception bodies are not exposed.
Automatic router retries/fallbacks are disabled for streamed Gemini dispatches:
they could replace a batch already shown to the client. Explicit client retries
remain possible. Other providers retain their existing dispatch behavior.

Completed-search-response cache identities include `typed-events-v2`, so malformed
old envelopes are not replayed. Historical `ws_shared` receipts retain their IDs
and remain readable. No cache migration, search-history wipe, or compatibility
runtime is introduced.

`gemini_timing` logs contain only provider/model, dispatch timestamp, outcome, and
milliseconds to first provider content, first public content event, and completion.
Absent content stays null; cancellation also logs its outcome. No prompts, tool
arguments/results, state, signatures, or keys enter these timing records.
These records use a dedicated metadata-only stderr handler, installed once at
startup, so a quiet upstream root logger cannot suppress them. Other loggers are
unchanged.
Waiting for silent provider computation emits no invented progress. For JSON requests,
first-content time is the receipt of the complete provider body; for streaming it
is the first nonempty text/marked summary part.

Codex can append a background command's late result after a newer tool call.
LiteLLM's Gemini converter pairs a result only with the latest call and would
fail every retry, so the adapter repairs Gemini-bound history first: a first
result moves next to its call, and repeated or unmatched results are dropped.
Each repair logs one `gemini_history_repair` record with integer `dropped` and
`moved` counts only.

Shared search logs `search_request` (short request hash, model, stream flag, and
how many identical requests were already queued) and `search_dispatch` (lock wait
in milliseconds) for every hosted-search turn, also metadata only. Identical
concurrent requests deliberately wait to share one result; the proxy's
`cancel_on_disconnect` setting cancels an abandoned JSON request so it cannot hold
that lock after its client gives up.

## Acceptance and rollout

Use the existing locked environment, with `scripts` on `PYTHONPATH`:

```sh
uv run --no-sync -m unittest discover -s scripts -p 'test_*.py'
```

Set `GEMINI_TEST_CODEX` to the absolute path of the installed stock Codex binary
to require the maintained CLI acceptance test (otherwise it uses `codex` on PATH,
or skips that test when unavailable). Run from a writable checkout; the test needs
loopback sockets and creates then removes a private disposable Codex home beneath
the checkout. It never reads real auth, uses only fake credentials, and mocks all
Vertex HTTP. No live inference or toolchain installation is needed.

`test_gemini_native.py` exercises the actual pinned Router/Vertex/Responses bridge
with synthetic credentials and mocked HTTP. It covers JSON/streaming fidelity,
ordinary/custom and parallel/sequential tools, cache flushes, key reuse, content
binding, caller isolation, a newly spawned process, and model switching. Streaming
tests gate provider completion to prove that the first available summary **or text**
arrives earlier, both directly and with hosted search declared but unused. They
also cover signature-only chunks, actual/mixed search, citations, cancellation,
terminal quota errors, late/suppressed summaries, and completed-output equality.
The stock Codex test checks a synthetic Code Mode tool, exact-session resume after
adapter/cache reinitialization, and forced compaction through the real shared-search
and compaction wrappers. It asserts no inactive-item error, no summary in the final
answer, and no discarded signature/tool result in the rewritten checkpoint.
Offline stock-Codex acceptance passed with version 0.166.0.
The retained research probe describes the original
unadapted bridge; it is not a runtime acceptance test.

Live deployment, real-provider acceptance, and model-quality comparisons require
separate approval. Protocol fidelity and early feedback do not prove that the
provider's computation becomes faster or that Gemini matches another model.

After approval, use the existing runtime prepare/deploy/verify/finish transaction
and model-refresh transaction. Verify a new Carbon chat, exact-session resume, and
compaction before finish; roll back managed code/catalog/lock changes on failure.
Preserve the sealing key and its marker through rollback. No whole-environment
backup or silent legacy fallback is part of rollout.

The approval-gated quality comparison is four fixed workflows (synthetic prior-chat
lookup, debugging with tests, deferred-tool discovery, and resumed/compacted work),
run against baseline Carbon medium, corrected Carbon medium/high, and Sol high.
Fix prompts, fixture repositories, context, and tool availability before execution;
record completion, human interventions, invalid calls, latency, tokens, and cost
for each run. Agree the run/spend cap before calling providers. No quality results
are claimed by the offline acceptance suite.
