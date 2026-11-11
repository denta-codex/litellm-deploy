# Making Gemini Carbon first-class in Codex

Investigation: November 11, 2026. This is a research report and proposal, not a
runtime change or deployment. No model-quality comparison has been run yet.

## Bottom line

Keep Codex as the workspace, tool executor, and approval authority. Improve
Gemini's protocol fidelity and operating instructions before changing harnesses.
One concrete loss of native state is reproducible in the pinned LiteLLM bridge;
other differences are configuration gaps or hypotheses to evaluate.

## What was inspected

- This repository and the host's non-secret installed model/catalog snapshots.
- Installed LiteLLM 1.106.5, including Vertex generation, Responses translation,
  streaming, and signature handling.
- Google's public Antigravity CLI 1.5.6 Linux x64 release. The SHA-512 checksum
  matched its release manifest. The executable was inspected statically, never
  installed or run. No authentication was changed.
- Official Antigravity and Gemini documentation; Google's separate open-source
  Gemini CLI documentation was used only as a reference, not treated as the
  Antigravity implementation.

The Antigravity executable is a stripped ELF with recoverable runtime function
names, source-path strings, protocol descriptors, and embedded prompt fragments.
This was **not a complete decompilation**. Static presence does not prove which
code paths or prompt experiments are active in a particular session.

Inspected artifact:
[CLI 1.5.6 Linux x64 archive](https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.6-6212266538958848/linux-x64/cli_linux_x64.tar.gz).
Its SHA-512 was
`0c67824908caf9cc3156699802b8097f2fd02c4608ae9293c9aa7f17e1bcd342c4b9c2e158388253a37804ef6bd9c6b1d25cfaca3c07bbb7587378fc91839bb4`.
Downloaded binaries and scratch files were removed after verification; only this
report and the offline diagnostic remain.

Notable identifiers include `rehydrateNativePayload`,
`native_payload_rehydration.go`, `context_summary.go`,
`prompt/template_provider/template_provider.go`, and tool converters. Embedded
identity templates branch on experiment and autonomy settings. These are evidence
of a structured, stateful harness, not a single universal prompt to copy.

Google describes Antigravity's shared harness as co-trained with Gemini. A Vertex
model ID behind a generic protocol bridge does not automatically reproduce that
environment. That is a reasonable explanation for some differences, not proof
that Carbon is less capable or that an adapter can guarantee parity.
[Antigravity overview](https://antigravity.google/docs/overview?tab=cli)

## Confirmed local findings

### 1. Final-text signatures are not replayed

An offline HTTP fixture exercised `Router.aresponses` with the actual pinned
Vertex adapter and Responses bridge. It returned a signed tool call, then signed
final text, then received a new user request. The tool-definition cache was
flushed before each continuation.

| Mode | Tool | Tool signature replayed | Final-text signature visible in response | Final-text signature replayed |
| --- | --- | --- | --- | --- |
| JSON | Function | Yes | No | No |
| JSON | Free-form custom | Yes | No | No |
| Streaming | Function | Yes | Yes | No |
| Streaming | Free-form custom | Yes | Yes | No |

In streaming, the signature was detectable somewhere in the response envelope;
that did not make it part of replayable output history. The fixture did not
execute tools, run the Codex client, or measure model quality.

Google recommends returning signatures on non-function-call content to maintain
reasoning quality. Unlike current-turn function-call signatures, these are not
strictly validated, so their loss can remain invisible to basic success checks.
This is a demonstrated protocol gap and a plausible contributor to quality loss,
not a measurement of its effect on the user's conversations.
[Thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)

**Proposed fix:** preserve native signed content and its exact part placement in
a Codex-replayable opaque state representation. Do not rely on response-envelope
extras or an in-memory tool cache. Confirm that stock Codex persists the chosen
representation through resume and compaction. Bind state to its originating
provider/model; do not send another model's opaque state to Gemini. Prefer an
upstream bridge fix; if a repository-owned correction is necessary, keep it
narrow and version-tested. Do not decode signatures into visible reasoning.

### 2. Carbon has a minimal operating prompt

Its catalog uses the 83-character generic `base_instructions` from
`scripts/refresh_models.py`. Claude Opus has a 5,976-character Codex-specific
prompt via `base_instructions_file`. Carbon also has `model_messages: {}`.

This is only the **base prompt**, not the entire request: Codex still supplies
tools, developer instructions, user context, and project instructions. Bedrock
GPT-6.1 Sol also has the same short base prompt, so prompt length alone does not
explain the user's comparison.

**Proposed fix:** add an original Gemini-specific operating prompt through the
existing `base_instructions_file` mechanism. Explain Codex's environment and
permission messages, Default versus Plan mode, skill activation, deferred-tool
discovery, JavaScript Code Mode, literal patch input, testing, and concise progress
reporting. Preserve safety and approvals. Do not paste Antigravity tool names or
instructions for runtime features Codex does not provide.

### 3. Reasoning works, but its defaults and presentation differ

Carbon defaults to `medium` in both its route and catalog; Bedrock Sol defaults
to `high`. The fixture confirmed that a requested `high` reaches Vertex as
`thinkingLevel: high` with `includeThoughts: true`.

Carbon advertises `supports_reasoning_summaries: false`, yet the unwrapped
streaming fixture produced reasoning-summary events. This establishes bridge
support, not end-to-end app support for every request path.

**Proposed fix:** compare high-effort Carbon on real tasks, then choose its default
deliberately. Verify summaries through stock Codex, the shared-search wrapper, and
resume before advertising that capability. Show provider-supplied summaries, not
raw private reasoning. A settings change can improve visibility without improving
model accuracy; measure those separately.

### 4. Gemini's hosted search currently uses a ChatGPT helper

`scripts/shared_search.py` routes non-subscription hosted search through
`ChatGPTSearch`, defaulting to `chatgpt/gpt-6-luna`. It returns a short synthesized
answer and sources, not the entire search evidence. Carbon can reason about that
answer, but this is not native Gemini grounding. Explicit external search tools
are a separate path.

**Proposed fix:** evaluate a Google-backed search/URL-reading backend with matching
citations and usage accounting, or a richer provider-neutral evidence backend.
Keep the existing backend until a deliberate validated cutover; do not add silent
fallbacks. Verify tool mixing against the chosen Vertex API and pinned adapter:
the installed adapter contains a path that drops native search tools when mixed
with function declarations. Changing a search flag alone is not enough.

## Recommended sequence

1. **Protocol fidelity:** turn the signature diagnostic into regression tests for
   final text, signature-only chunks, parallel calls, sequential calls, restarts,
   shared search, and stock Codex resume. Fix state replay first.
2. **Native operating instructions and feedback:** introduce the Gemini prompt;
   test Code Mode versus direct tools on the same workflows, not by assumption.
   Validate reasoning-summary display and high-effort behavior.
3. **Task evaluation:** compare baseline Carbon, corrected Carbon, and Sol on
   debugging with tests, deferred MCP tools, search, images, and a resumed long
   task. Record successful completion, human interventions, invalid calls,
   verification quality, latency, tokens, and cost. Use equivalent task inputs and
   declared settings; effort labels do not imply equal compute across providers.
4. **Only if needed, native runtime:** prototype an Antigravity SDK-backed adapter
   similar in architecture to the existing Claude adapter. The SDK documents
   Vertex routing, custom tools/MCP, and session persistence. It is not a drop-in
   Responses provider. Establish that suspended tool requests, cancellation, and
   replay can be mapped to Codex before committing to it. Disable independent
   filesystem/shell execution; retain Codex's sandbox and approvals as the sole
   execution authority. Adopt one validated path through an explicit cutover,
   rather than maintaining two competing agent workflows.

[SDK overview and Vertex configuration](https://antigravity.google/docs/sdk/overview),
[custom tools](https://antigravity.google/docs/sdk/tools),
[session persistence](https://antigravity.google/docs/sdk/lifecycle).

## Reproduction and verification

`docs/research/gemini_bridge_probe.py` retains the offline diagnostic. In an
environment synced from this repository's lock, run:

```sh
uv run --no-sync docs/research/gemini_bridge_probe.py
```

It uses fake credentials and in-memory HTTP responses, prints observations, and
does not assert that today's lossy behavior is correct. Investigation used the
already-installed locked environment with `uv run --no-sync --project` instead
of installing another environment. All four diagnostic combinations completed.

Three existing targeted tests also passed: the Vertex shared-search credential
and provider continuation test, namespaced/custom-tool history continuation, and
chat-bridge reasoning forwarding. Those tests do not cover final-text signatures
or establish real-model quality. No paid inference, live service restart, or
configuration rollout was performed.

## Real-chat follow-up: Syurf password lookup

Inspected chat `01a1cbe2-d87b-7031-aef8-dc35651f800a` on the application host after the user
reported six minutes without an answer. Sources: native chat history, its archived
rollout, thread-scoped Codex diagnostic logs, and shared-search response receipts.
No reasoning text or credential values were reproduced.

The turn ran from 18:33:29 to 18:39:45 UTC on November 11 (376.165 seconds), using
`vertex/gemini-4-carbon` at **high** effort. The current metadata says low, but
that setting was applied after interruption. Three completed Code Mode calls
took about 0.219 seconds combined. Model/proxy response waits were approximately
115 seconds, 17 seconds, 113 seconds, then 130 seconds before interruption. Four
client requests received HTTP 200 headers promptly; the thread-scoped diagnostic
logs contain no client retry or authentication-recovery sequence.

Matched response receipts establish that this chat went through the shared-search
wrapper even though its completed outputs contain no web-search calls. That
wrapper forces upstream `stream=False` and emits reasoning/tool output only after
the complete response arrives. This explains the absence of incremental model
feedback, not the underlying provider latency.

There is also a concrete presentation defect: each matched response contains a
`reasoning` item with `content[].type=output_text`, rather than `summary` entries.
The wrapper emits ordinary `response.output_text.delta` events for that content.
Codex logged `OutputTextDelta without active item` for this exact thread on all
three completed responses. No reasoning item or assistant reply survived in the
archived transcript. This establishes a typed-stream incompatibility; it does not
establish that a final answer was generated and lost. Do not fix it by blindly
displaying raw reasoning: preserve native state and expose only supported,
provider-supplied summaries through the proper event types.

The agent also took an inefficient lookup path: after learning that the initial
password had been delivered in an earlier task response and was not saved in a
plaintext file, it listed workspaces, inspected Git/worktrees, and dumped roughly
41 KB of thread/app tool descriptions. It never actually searched prior chats
before interruption. Improve targeted history-tool discovery and stopping/reporting
behavior alongside stream fidelity. High effort and growing context are possible
latency contributors, but these records cannot separate Vertex queueing, inference,
and proxy-internal delays. Provider-stage timing is needed for that attribution.
