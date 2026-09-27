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
No virtual-key administration, middleware, or installed dependency source edits.
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

The API probes require the backend to report the requested tier on streaming,
function-call, and tool-result responses. Successful text alone is not proof of
priority service. On September 27, 2026, isolated and direct backend probes
reported `default` for explicit `priority` requests. A native Codex ChatGPT control
using the built-in provider, with LiteLLM bypassed, also completed with `priority`
on the wire and `default` in the response. The isolated shared adapter preserved
priority on every observed Codex search/tool/resume request. Forwarding is verified;
backend priority delivery remains unverified, and the strict probe deliberately
fails on a mismatch. The adapter does not relabel the returned tier.

The Codex probe enables its fast-mode feature and exercises an explicit tier with
native search and resumed tool execution. Both probes accept `--base-url` for an
isolated proxy. Check the desktop Fast toggle before declaring fast mode verified.
Rollback uses the previous committed adapter and ordinary deployment; there is no
installed-package edit to restore. Remove this forwarding override when a pinned
upstream release preserves the tier and passes the same checks.

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
Native-search support is not inferred for future Modal inference endpoints.

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

These checks are offline. The startup test runs the actual shell/uv launcher and
proxy on a temporary loopback port against a local SSE fixture with synthetic
credentials. It exercises outgoing fields, exact/absent instructions, ordinary
requests, tools/continuation, backend errors, and the reserved reviewer verifier.
Existing tests also retain the reviewer routing, hidden metadata, and refresh
checks. They neither contact the subscription backend nor restart live services.
