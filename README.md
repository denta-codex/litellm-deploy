# Grace LiteLLM deployment

LiteLLM 1.102.1 with a small repository-owned ChatGPT request customization,
uv, and systemd on Grace, for stock Codex through a named
`litellm` Responses provider. Default model: `chatgpt/gpt-6-astra`. Discover subscription models with
the explicit refresh command below. This is a private, single-host deployment.

## Deploy

Use a clean committed checkout. Python 3.14 and uv use Grace's existing toolchain.
The repository's `.mise.toml` selects Grace's already-installed Ansible and uv.
Trust that repo-local file with `mise trust` once after reviewing a new checkout;
no additional toolchain installation is needed on Grace.

```sh
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --syntax-check
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
```

The proxy listens on `127.0.0.1:4000`. It runs with a locked uv environment and
no runtime dependency downloads. No database or Redis server is configured;
stock upstream may include their client libraries among its dependencies.
No virtual-key administration or installed dependency source edits.
Codex uses a locally discovered model catalog to enable hosted Responses search;
see the compatibility note below.
Application files live under `~/.local/share/litellm`, configuration under
`~/.config/litellm`, and mutable state under `~/.local/state/litellm`.
The root-owned unit is `/etc/systemd/system/litellm.service`.

### ChatGPT Responses customization

`scripts/chatgpt_responses.py` subclasses the pinned upstream adapter and changes
only request instructions and field preservation. Explicit client instructions,
including an empty string, survive unchanged; absent instructions use upstream's
fallback. It restores `text` (`format` and `verbosity`), `parallel_tool_calls`,
`prompt_cache_key`, and `service_tier` after upstream transformation. Authentication, token refresh,
headers, HTTP transport, streaming, response parsing, and errors remain upstream.
Upstream still forces `store=false`, streaming, and encrypted reasoning inclusion.

`scripts/start-litellm` runs `scripts/serve.py` through the existing locked uv
environment. Before invoking the upstream CLI, it replaces the
`litellm.ChatGPTResponsesAPIConfig` export used by `ProviderConfigManager`.
The server runs in the same process with one worker; reload and alternate worker
launchers are not exposed. Running the stock `litellm` command directly bypasses
this customization. Ansible installs these three runtime files and treats changes
to them as restart-requiring; utility-only updates still do not restart inference.
The launcher's optional fourth argument selects a port for isolated tests; the
systemd invocation continues to use port 4000.

The override fails startup unless LiteLLM is exactly 1.102.1. Any upstream upgrade
must revalidate dispatch, transformation, and launcher tests, then revisit or
remove the override. There is no copied adapter or separate deployed service.

Isolated live subscription probes on September 27, 2026 used this checkout's
launcher with an access-only snapshot of the existing login, no refresh token,
and no live installation/configuration changes. Additional format/cache controls
called the backend directly with the same transformer and upstream header helper,
reading the existing access token without invoking login or refresh. Observations:

| Field | Subscription-backend evidence and limits |
| --- | --- |
| `text.format: json_schema` | Both `gpt-6-astra` and `codex-auto-review` returned the schema-required enum despite conflicting non-JSON instructions. Both rejected invalid schemas with HTTP 400 / `invalid_json_schema`. This tests schema processing, beyond merely prompting for JSON; it is not an exhaustive schema-keyword guarantee. |
| `text.format: text` | Accepted on `gpt-6-astra`. |
| `text.format: json_object` | Accepted on `gpt-6-astra` when input explicitly mentioned JSON; input without that keyword was rejected. JSON-object mode is not schema enforcement. |
| `text.verbosity` | `low` was accepted and echoed; an invalid value was rejected, listing `low`, `medium`, and `high`. Output-length effects were not measured. |
| `parallel_tool_calls` | `false` and `true` were echoed and produced one and two calls respectively when asked for two tools. Streamed tool-result continuation passed. |
| `prompt_cache_key` | Preserved on the outgoing wire, but the backend ignored the body value, even an invalid object. A supplied `session_id` header became the response cache key; without it the backend generated a key. Body-key control and cache-hit benefits are not supported by this evidence. Upstream session headers are unchanged. |

These are account/model-specific observations, not guarantees for every model.
The reserved reviewer also completed the existing probe's streaming JSON payload.
That probe alone does not establish schema enforcement or Codex approval decisions.

Complete a separate ChatGPT device login using the same subscription account:

```sh
CHATGPT_TOKEN_DIR="$HOME/.local/state/litellm/chatgpt" \
  uv run --no-sync --project "$HOME/.local/share/litellm" \
  "$HOME/.local/share/litellm/scripts/login.py"
```

The login prints only the temporary device authorization code, never tokens.
After login, run `deploy/refresh-models.yml` before the initial Codex cutover.
Stock LiteLLM OAuth token files are private (0700 directory, 0600 files), but
are not encrypted by this deployment. Codex's own login is not changed.

## Encrypted proxy key

Provisioning generates one random key and encrypts it twice without a plaintext
disk file: a system credential under `/etc/credstore.encrypted/litellm-proxy-key`
and an agent-scoped credential under `~/.config/litellm/proxy-key.cred`.
Systemd's host key is used; this machine has no usable TPM. Disk encryption is
still needed for protection against theft of the whole disk including its host
key. Root or a compromised live agent account can access the running credential.

Systemd `LoadCredentialEncrypted` supplies LiteLLM's credential. A tiny launcher
sets its master-key process environment from the runtime credential. Codex uses
stock `model_providers.litellm.auth.command` to run `systemd-creds decrypt --user`.
There is no proxy key in `.codex/.env`, the unit, config.toml, or Git. No sudo is
required by the Codex credential command. A partial credential pair fails closed
rather than generating a replacement that could desynchronize the clients.

## One-time cutover

```sh
ansible-playbook -i deploy/inventory.yml deploy/cutover.yml
# Restart Grace's connection using the owning desktop's Restart action.
ansible-playbook -i deploy/inventory.yml deploy/cutover.yml -e cutover_action=check-restart
```

Prepare verifies authenticated streaming and a complete tool round trip, then
replaces gateway routing with the named `litellm` provider, standard model name,
HTTP Responses streaming, and credential command. The old gateway stays active
only during this unfinished migration. One private configuration recovery record
is retained in `~/.local/state/litellm/cutover.json` and removed on completion.
Check mode for cutover intentionally performs no inference/config mutation.

Prepare also requires a real stock Codex native-search event and a resumed
shell-tool round trip with retained search context. A raw API search test alone
is insufficient to pass this gate.

Before finalizing, verify a new desktop task uses LiteLLM, tool execution and
follow-up turns work, existing tasks remain visible/reopenable, and cancellation
and restarts work. Exercise web search and compaction; report material limitations
before committing to them. Old tasks are not relabeled and may retain `openai`.
The native provider returns to its normal backend once its gateway override is
removed. Dynamic merged model discovery is not a cutover requirement.

After desktop validation, uninstall the old gateway completely:

```sh
ansible-playbook -i deploy/inventory.yml deploy/finalize.yml \
  -e litellm_desktop_validated=true
```

Finalize checks the desktop replaced the app-server socket, validates LiteLLM,
removes the old service and installation, checks its listener is gone, verifies
LiteLLM again, and deletes migration recovery state. Keep Git checkouts; they are
source, not an installed fallback service. Repeat deploy.yml to check convergence.

## Recovery

Before finalize:

```sh
ansible-playbook -i deploy/inventory.yml deploy/cutover.yml -e cutover_action=rollback
# Restart Grace's connection using the desktop.
```

Rollback refuses to overwrite intervening Codex configuration edits. Review and
merge such edits manually using the retained private recovery record.

After uninstall, redeploy the old gateway from:
https://github.com/denta-codex/codex-gateway/tree/gateway-paused-2026-09-26

The frozen revision is `7883f72f9d9a7ca9075fcef81155b4d23bb7ed55`, including its
existing Ansible. The encrypted Modal credential is retained at its existing path
`/etc/credstore.encrypted/codex-gateway-modal`; use that
repository's `bin/deploy-grace prepare`, desktop Restart, and
`bin/deploy-grace finalize`. Remove LiteLLM's provider selection/configuration
as part of that deliberate recovery; do not run parallel routing workflows.
Retain the Modal credential for imminent provider reuse and gateway recovery.
It is independent of the uninstalled gateway service; do not copy it into
1Password or delete it during this migration.

## Validation

### Claude subscription adapter

`claude/opus-5.5` is an opt-in route to the exact `claude-opus-5-5` model through
Claude Agent SDK 0.2.161. Its picker name is **Claude Opus 5.5**. It offers low,
medium, high, xhigh, and max effort, defaults to high, and advertises a conservative
200,000-token context window. The default model remains `chatgpt/gpt-6-astra`.
There is no model fallback or API-key fallback.

The adapter uses the agent account's existing `claude auth login`. To renew login,
run that command as `agent`, then retry the request. No Claude token belongs in
LiteLLM YAML, systemd environment variables, or Git. The repository's `claude-cli`
wrapper starts the SDK-bundled binary with a minimal environment; unrelated proxy,
Modal, and OpenAI credentials are excluded. Workers use an empty private working
directory, no settings sources, and only the declared MCP server. The sole native
tool allowed for schema output is the SDK's local `StructuredOutput` formatter.
Every client command or file edit still runs through Codex and its approvals.

The adapter supports streaming/nonstreaming Responses, inline PNG/JPEG/GIF/WebP
images, JSON schema output, summarized reasoning, and client tools including
namespaced functions and freeform patches. It does not fetch image URLs. Sampling,
token-budget, verbosity, priority-tier and server-side Responses retrieval controls
are rejected. Codex metadata and cache hints are accepted as transport metadata;
they do not force a Claude cache policy. Opaque encrypted reasoning is not exported.
Hosted search uses the shared ChatGPT-backed interceptor described below.

Direct clients must send `thread-id` or `x-claude-chat-id` on authenticated requests
and supply full conversation history. Session identity also includes the caller,
model, and Codex context window. Concurrent requests for one session serialize;
different chats run independently. Partial tool-result batches return outstanding
calls with their original IDs. Repeated results must agree. Completed request
retries are replayable from the last 16 saved responses per worker generation.

Forks, compaction, configuration changes and lost workers use readable completed
history. This preserves useful context, not native role structure or hidden model
state. Unknown tool execution is never blindly replayed. Private atomic journals
under `~/.local/state/litellm/claude/journals` contain tool arguments/results and
recent responses; treat them as conversation data. SDK-owned session data remains
in the normal Claude configuration directory. Usage excludes the SDK's list-price
cost estimate and avoids recounting earlier tool boundaries as new inference.

Stop during an outstanding client tool still lacks an immediate adapter signal
after the HTTP response has ended. The next request reconciles the aborted tool
and new prompt, interrupting and closing the superseded worker. Explicit close,
active-request failure, and service shutdown clean up owned workers. Idle eviction,
disconnected-tool deadlines and sustained resource testing remain in the deferred
cancellation task; ordinary idle sessions currently stay available in memory.

Validate in isolation, commit the implementation, deploy it, and activate the route:

```sh
uv sync --locked
uv run --no-sync scripts/verify_claude.py --isolated
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
ansible-playbook -i deploy/inventory.yml deploy/claude.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/claude.yml
```

The isolated check starts a disposable loopback proxy and Codex home. It uses the
existing subscription logins, writes only synthetic fixtures, and removes its
temporary state on success. Failed checks retain a printed diagnostic directory;
remove it once the failure is resolved. Nothing is installed globally. Tests include
all efforts, images, schema output, summaries, tool continuations, native execution
and patches, forks, compaction, switching to ChatGPT and back, cancellation follow-up,
and proxy crash recovery. Existing offline tests cover error mapping, retries,
configuration preservation and rollback without deliberately exhausting a quota.

Activation publishes the picker only after the live acceptance suite passes;
failure restores the previous configuration. Recover an interrupted transaction
with `deploy/claude.yml -e refresh_action=rollback`. Activation changes no Codex
default. Restart Grace's connection through the owning desktop to load the picker.
The official SDK remains an agent runtime; this adapter is not full raw-inference
API equivalence. Packaging for public distribution is a later decision.

On September 29, 2026, the isolated full suite passed 21 live checks with Opus 5.5,
SDK 0.2.161 and Codex 0.155.1; all 93 offline regression tests passed. The live
parallel-call test verified two distinct SDK calls in one response, partial result
delivery, and reversed result order. The SDK can make ancillary helper-model
requests internally; the adapter verifies that conversational answers use Opus
5.5. Reported inference usage comes from the SDK's response usage, not an invented
subscription bill or a guarantee of accounting for every internal helper request.

### Existing provider checks

```sh
uv run --no-sync scripts/test_cutover.py
uv run --no-sync scripts/verify.py
uv run --no-sync scripts/verify_codex.py
uv run --no-sync scripts/verify_review.py
```

`verify.py` sends small subscription requests. It checks unauthenticated rejection,
model configuration, streaming completion, and a function-call/result round trip.
It does not print credentials or conversation content. Routine updates need only
deploy.yml; restart the desktop connection if its configuration changes.

The locked `prisma==0.15.0` client dependency is included solely because stock
LiteLLM's database-free authentication error handler imports it unconditionally
(upstream issue https://github.com/BerriAI/litellm/issues/38978). No Prisma engine,
schema generation, database connection, or database server is configured.

## Fast mode

The shared `scripts/chatgpt_responses.py` override preserves an explicitly supplied
`service_tier`, which LiteLLM 1.102.1 otherwise drops from ChatGPT requests.
Ordinary requests do not gain a tier or enable priority by default. This uses the
same version-checked launcher as the other request fixes; it does not edit the
installed LiteLLM package. Changes to the module trigger the existing Ansible
restart handler. No catalog refresh or desktop restart is required for forwarding.

After deployment, verify both tiers and the real Codex search/tool flow.
These probes consume subscription usage:

```sh
uv run --no-sync scripts/verify.py --service-tier priority
uv run --no-sync scripts/verify.py --service-tier default
uv run --no-sync scripts/verify_codex.py --service-tier priority
```

The API probes report requested and returned tiers separately. ChatGPT subscription
responses can report `default` after a `priority` request, including when native
Codex bypasses LiteLLM. The returned field is not a reliable Fast acceptance gate
for this subscription endpoint; the earlier equality assertion was incorrect.
See the [OpenAI contributor's explanation](https://github.com/openai/codex/issues/14204#issuecomment-4033184620).
Offline regressions strictly check priority forwarding, omission on ordinary
requests, and unchanged response metadata. These checks do not measure throughput.

The Codex probe enables its fast-mode feature and exercises an explicit tier with
native search and resumed tool execution. Both probes accept `--base-url` for an
isolated proxy. Check the desktop Fast toggle before declaring fast mode verified.
Remove the forwarding override when a pinned upstream release preserves the tier
and passes the same checks.

### Grace Codex runtime

The desktop's Fast controls require the host's genuine ChatGPT identity.
Grace's `~/.codex/config.toml` is user-managed. The runtime deployment validates
that its LiteLLM provider uses `requires_openai_auth = true`,
`env_key = "LITELLM_PROXY_KEY"`, the local endpoint, standard speed, disabled
shell snapshots, and proxy-key exclusion. It reports exact corrections when
those prerequisites are missing, but never rewrites, snapshots, digest-locks,
or restores the file. The native ChatGPT login, catalog, model, plugins, and
other preferences remain under Grace's control. No desktop or installed package
is patched.

The dedicated runtime deployment owns Grace's full `codex-app-server.service`,
the existing Desktop attach setting, the credential-loading launcher, its
`~/.local/bin/codex` entry point, and the pinned Codex binary selection. The
launcher decrypts the existing credential into the process environment,
rejects empty/invalid credentials, and executes a committed exact Codex version
with arguments unchanged. Explicit environment exclusion keeps the key out of
shell tools. **Shell snapshots are disabled**:
the live stock-Codex probe found that snapshots could restore the startup key
after exclusion. Disabling them also prevents persisting that startup environment.
No key is written to TOML, logs, or shell startup files.

The inventory pins Codex `0.155.1`. Ordinary LiteLLM deployment does not install,
select, or restart Codex. An explicit runtime update installs its committed
version alongside existing Mise versions, validates the current catalog with
that exact binary, and stages the runtime without interrupting active tasks.
Desktop's Restart action activates it. The repository does not change Mise's
global `latest` selection, patch Codex, update the desktop application, or run a
scheduled upgrade.

The runtime playbook has five explicit actions:

```sh
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=apply
# Restart Grace through the owning desktop.
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=verify
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=finish -e codex_desktop_validated=true
```

The default action is read-only preview. `apply` validates the user-managed
configuration, installs the exact binary if needed, and stages the launcher,
unit, and attach setting. It also removes the temporary emergency drop-in after
recording it for rollback. `verify`
requires a replaced app-server socket, checks the live systemd process uses the
pinned executable and proxy credential, and runs a real shell turn proving the
key is absent from tools. It then checks subscription streaming, tool
continuation, native search/resume, and the reserved reviewer route. `finish`
records the accepted version and managed-file digests.

One private transaction at `~/.local/state/litellm/codex-runtime.json` contains
only the repository-owned runtime files, prior version selection, and pre-restart
socket identity. It never contains `config.toml`. Rollback refuses managed files
that differ from the staged revision, restores the previous runtime together,
leaves Grace's configuration untouched, and then requires Desktop Restart:

```console
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=rollback
```

After acceptance, the persistent installed manifest is the comparison baseline
for later version changes. A later update aborts on any intervening managed-file
edit instead of overwriting it. Candidate binaries and the previous binary remain
installed; rollback changes selection rather than deleting shared Mise state.

Desktop acceptance passed on September 27, 2026: the user's Fast-on turn sent
`priority`, a follow-up after a desktop connection restart still sent `priority`,
and the Fast-off turn omitted the field. These were correlated to this existing
task at the outgoing adapter boundary. Shell execution also confirmed that the
proxy key was absent from tool environments after restart. The earlier isolated
checks covered streaming, tool continuation, native search/resume, the reserved
reviewer route, and unchanged returned-tier metadata. Repeat those desktop checks
after each explicit runtime update. Returned `default` is not a subscription Fast
failure, and throughput benchmarking is outside runtime acceptance.

## Refresh subscription models explicitly

From a clean committed checkout on Grace:

```sh
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml
```

This fetches the account's current Codex catalog using LiteLLM's existing
ChatGPT login and the installed Codex version. It updates both LiteLLM routes
and the Codex picker. There is no timer, background synchronizer, or extra server.
Discovery failures leave the installation unchanged. An expired login is reported
without starting an interactive login or rotating credentials; use the documented
stock login command if needed, then retry.

`chatgpt/` and the reserved `codex-auto-review` route are owned by refresh.
Each upstream selectable chat model gets one `chatgpt/<slug>` route and one
visible picker entry. Hidden native entries
retain metadata for existing tasks. Other provider routes, their settings, and
catalog entries are preserved. The selected default is not changed; removing it
from the discovered list aborts the refresh rather than substituting another model.
Old tasks can retain their native model selection; no task history is rewritten.
Upgrade actions on routed entries point to the corresponding selectable
`chatgpt/` route, preserving the upstream explanation and retirement date.
If that target is unavailable, the routed upgrade action is cleared and reported
in `disabled_upgrades` in the refresh/preview output; native metadata is retained.

Codex sends approval-review requests using the exact name `codex-auto-review`.
Refresh routes that name to `chatgpt/codex-auto-review`, the subscription's native
hidden reviewer. It stays hidden in the picker, uses full Responses transport,
and is never replaced with a general chat model. Discovery that omits the native
reviewer is rejected before activation. Bootstrap configuration includes the same
route; the initial refresh after login verifies its availability.

Preview shows added/removed model IDs and changed metadata fields, with before/after
lists. It performs discovery but does not install files, refresh OAuth tokens,
restart services, validate with inference, or print credentials/model instructions.
Live refresh checks the candidate catalog with stock Codex, restarts LiteLLM only
if routes changed, and tests streaming, function-call continuation, and stock Codex
search/tools/context for new or changed models (two concurrent probes maximum).
Search is tested only when the discovered model advertises it. Validation consumes
subscription usage. The picker is published only after every probe succeeds.
New or changed review routing/metadata also requires a streaming JSON response
from the reserved name. A Codex version change revalidates both chat models and
the reviewer. Reviewer failure triggers the same rollback as chat-model failure.

A successful discovery is stored privately in
`~/.local/state/litellm/subscription-models.json`; generated configuration remains
in `~/.config/litellm`. Neither discovery results nor credentials are committed.
Ordinary deployment checks and preserves these files; it never reinstalls the
one-model catalog. If generated files are missing or inconsistent, deployment
stops and directs you to run refresh. New installations bootstrap the single
configured default until the first explicit refresh after login.

Unchanged refreshes perform no inference or service restart. After catalog changes,
restart Grace's connection through the owning desktop to load the new picker.
Verify the models are listed once in a new task; an existing task can still show
its old selected native model alongside the routed choices.

Failed activation or validation restores the previous config, catalog, and discovery,
restarts restored routes when necessary, and checks liveness. One private recovery
directory is kept only during unfinished work. If the command is interrupted:

```sh
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml -e refresh_action=rollback
```

Recovery refuses to overwrite concurrent edits. In that case inspect
`~/.local/state/litellm/model-refresh` and reconcile before retrying. The directory
is removed after successful activation or successful recovery. Ordinary deployment
refuses to proceed while a refresh is unfinished.

### Native web-search compatibility

Validated initially with Codex 0.155.1 and LiteLLM 1.102.1. Models using Responses
Lite suppress hosted web search in this Codex version; its alternative standalone
search uses `/v1/alpha/search`, which this LiteLLM deployment does not serve.
The generated routed entries set `use_responses_lite=false` so search uses
`/v1/responses`. Other upstream metadata is retained, including instructions,
reasoning levels, context limits, and capabilities. Invalid/incomplete catalogs
are rejected rather than filling them with invented defaults.

The supported `model_catalog_json` setting still points to
`~/.config/litellm/codex-models.json`. This snapshot is loaded at Codex startup;
run refresh explicitly after model availability or Codex version changes.
Claude and Modal entries advertise hosted search after their activation checks pass.

### Shared search for Claude and Modal

`shared_search.py` extends LiteLLM 1.102.1's `WebSearchInterceptionLogger`.
LiteLLM's existing agentic loop performs model continuations; the local extension
supplies the subscription backend, preserves mixed client/search tool batches,
and presents standard Responses search events to stock Codex. No additional
gateway or Codex patch is required. Native ChatGPT search keeps its existing path.

The helper is the configured `chatgpt/gpt-6-luna` route at low effort. Set
`SHARED_SEARCH_MODEL` in the service environment to select another configured
ChatGPT helper explicitly. Authentication and refresh remain in the existing
ChatGPT provider; its credentials never enter Claude workers or Modal requests.
The backend accepts a query and search settings and returns normalized text and
source records, so a future Exa backend can use the same interceptor and Codex
presentation. Exa is not enabled or configured by this release.

Search-enabled model iterations are buffered. Search progress is emitted while
the helper runs, then model text/reasoning and client tool calls are returned in
batches. Ordinary requests without shared search/history keep the stock path.
The limit is three helper queries per Responses request and 60 seconds per query,
including retries. A client-tool continuation starts a new request. Results keep
up to 4,000 answer characters and eight sources per query. Failed/empty searches
remain explicit, and no alternate provider is silently substituted. Client tool
execution and approvals remain with Codex, including mixed and parallel batches.

Private journals in `~/.local/state/litellm/search` contain search calls, findings,
sources, helper usage, and completed response replays. They are scoped to the
authenticated caller and use atomic owner-only files. Completed records are
pruned after 30 days or at a 100 MiB per-caller budget; pending client-tool
continuations are protected. Missing required records fail explicitly. Treat the
journals as conversation data. `SHARED_SEARCH_STATE` overrides the directory for
disposable testing. Active helper cancellation follows the owning request;
broader Claude worker cleanup remains a separate task.

Validate and activate with the existing model refresh transactions:

```sh
uv run --no-sync scripts/test_shared_search.py
uv run --no-sync scripts/verify_shared_search.py --isolated --all-efforts
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
ansible-playbook -i deploy/inventory.yml deploy/modal.yml
ansible-playbook -i deploy/inventory.yml deploy/claude.yml
```

Deployment validates installed discovery with the installed generators before
replacing them. Provider activation then validates and publishes new capabilities.
Retain the prior changed runtime files and model files until both activations
pass; on release failure restore that runtime and its matching catalog together.
The isolated acceptance starts disposable proxy/Codex homes, verifies successful
helper output with sources, native Codex activity, external tools and follow-up
context, and removes successful test state. Failed evidence is retained at the
printed path only until diagnosis completes. Restart Grace's desktop connection
after publication to load the new catalog.

## Modal inference

Modal uses stock LiteLLM's Responses-to-Chat-Completions bridge, configured with
`use_chat_completions_api: true` on each OpenAI-compatible route. No Modal SDK,
custom inference adapter, source patch, or additional server is installed.
The upstream is `https://inference.us-west.modal.direct/v1/chat/completions`.

`scripts/modal-models.json` owns the three existing endpoints and their picker
metadata: Modal DeepSeek V4.1 Flash, Modal GLM 5.3 Flash, and Modal Kimi K3.
Their `modal/<endpoint-id>` names, reasoning choices, image inputs, and 1,048,576
token context limits come from the frozen gateway's Modal model definitions.
These are explicit configuration, not fresh provider discovery. Change the
manifest and rerun Modal configuration when the endpoints change.

The unit reuses `/etc/credstore.encrypted/codex-gateway-modal` under its original
credential name, `modal-inference-token`. Deployment requires this existing
root-owned mode-0600 encrypted file before changing the installation. Systemd
decrypts it into the private service credential directory. `scripts/launch.py`
parses a combined `wk-....ws-...` token, `MODAL_PROXY_TOKEN`, or the retained
`WK_SECRET`/`WS_SECRET` pair without evaluating shell code, then exports only the
combined token as `MODAL_API_KEY` to LiteLLM. It never prints the key or writes a
plaintext copy. The encrypted file is neither renamed nor re-encrypted, so its
embedded credential name and the documented gateway recovery remain valid.
After loading credentials, the launcher runs `scripts/serve.py`, preserving the
subscription request customization, fast-mode forwarding, and reviewer behavior.

### Deploy and activate later

From a clean committed checkout on Grace, first update the launcher and service,
then preview and activate the Modal routes:

```sh
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --syntax-check
ansible-playbook -i deploy/inventory.yml deploy/modal.yml --syntax-check
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
ansible-playbook -i deploy/inventory.yml deploy/modal.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/modal.yml
```

The preview reads the checked-in manifest and installed Codex version. It does
not decrypt credentials, contact Modal, write configuration, or restart services.
Live activation checks the candidate catalog with stock Codex, installs routes,
restarts LiteLLM if needed, and tests every new or changed model for Responses
streaming, function-call continuation, and stock Codex shell execution and resumed
context. Those live tests consume Modal inference. The picker is published only
after validation succeeds. Restart Grace's connection through the owning desktop
after a picker change, then try a fresh Modal task. The selected default and all
subscription routes, metadata, and saved discovery stay intact.

Modal setup owns `modal/`; subscription refresh owns `chatgpt/`. Both reuse the
same activation lock and recovery directory to prevent concurrent changes.
Successful Modal activation saves `~/.local/state/litellm/modal-models.json`;
ordinary deployment checks and preserves both providers' saved snapshots.
Unchanged activation performs no inference or restart. A failed activation restores
the previous routes and picker. To recover an interrupted Modal activation:

```sh
ansible-playbook -i deploy/inventory.yml deploy/modal.yml -e refresh_action=rollback
```

Recovery refuses concurrent file edits and retains
`~/.local/state/litellm/model-refresh` only until recovery succeeds. Use the
playbook that started the transaction; rollback rejects a different namespace.

### Capability boundaries

Modal models use the existing named `litellm` provider with HTTP Responses and
full tool history and shared hosted search. No WebSocket support is advertised.
The old gateway's search behavior is adapted through LiteLLM's existing search
interception hooks. Subscription search is unchanged. Existing gateway tasks are
not relabeled or migrated, and encrypted
OpenAI compaction state is not converted for Modal. Begin with a fresh task under
the `litellm` provider. Local compaction, subagents, and long-running task behavior
still require deployment-time acceptance testing.

The offline tests below check the pinned LiteLLM async router with an in-memory
HTTP transport and synthetic credentials, including text streaming, reasoning
effort, namespaced functions, freeform tools, and tool-result continuation. They
also check stock Codex catalog parsing, preview, rollback, credential formats,
and preservation of subscription refresh. They do not establish live endpoint
availability or validate the retained secret. Installing and activating this
change is a separate operation.

### Repair installations missing the approval reviewer

An installation missing the reserved route rejects approval requests with
`Invalid model name passed in model=codex-auto-review`. From a clean committed
checkout containing this fix, run refresh **before** ordinary deployment: the
deployment snapshot check intentionally rejects the old, incomplete routing.

```sh
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
uv run --no-sync scripts/verify_review.py
```

The refresh uses this checkout's scripts and the existing installed environment;
it requires no reinstall or credential changes. Its preview should show
`test_reviewer: true`. Unchanged chat routes need no additional inference probes.
Refresh rolls back routing and metadata if reviewer validation fails; use the
documented refresh rollback command if the operation is interrupted.

Restart Grace's connection through the owning desktop if refresh reports a
catalog change. Then exercise one authorized, read-only command that requires
automatic approval, such as a host service-status check, and confirm an actual
review decision and command result. `verify_review.py` checks only route access,
streaming completion, and a JSON response; it does not prove Codex's native
approval lifecycle. The repository customization forwards `text.format`, but this
probe also explicitly prompts for JSON and does not itself assert schema enforcement;
see the separate subscription-backend evidence above.

If the broken reviewer prevents the repair command from being approved, perform
the repair through a user-approved host terminal. Do not disable or work around
approval controls from inside the agent task.

Development checks:

```sh
uv sync --locked
LITELLM_LOCAL_MODEL_COST_MAP=True uv run --no-sync -m unittest discover -s scripts -p 'test_*.py'
```

To run these checks without creating or synchronizing a worktree environment on
Grace, use the existing locked installation explicitly:

```sh
UV_PROJECT_ENVIRONMENT=/home/agent/.local/share/litellm/.venv \
  LITELLM_LOCAL_MODEL_COST_MAP=True \
  uv run --no-sync -m unittest discover -s scripts -p 'test_*.py'
```

These checks are offline. The startup test runs the actual shell/uv launcher and
proxy on a temporary loopback port against a local SSE fixture with synthetic
credentials. It exercises outgoing fields, exact/absent instructions, ordinary
requests, tools/continuation, backend errors, and the reserved reviewer verifier.
Existing tests also retain the reviewer routing, hidden metadata, and refresh
checks. They neither contact the subscription backend nor restart live services.
