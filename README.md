# Grace LiteLLM deployment

LiteLLM 1.102.1 with a guarded service-tier fix, uv, and systemd on Grace, for stock
Codex through a named `litellm` Responses provider. Default model:
`chatgpt/gpt-6-astra`. Discover subscription models with the explicit refresh
command below. This is a private, single-host deployment.

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
No virtual-key administration or middleware. One version- and checksum-guarded
source patch preserves ChatGPT service tiers; see Fast mode below.
Codex uses a locally discovered model catalog to enable hosted Responses search;
see the compatibility note below.
Application files live under `~/.local/share/litellm`, configuration under
`~/.config/litellm`, and mutable state under `~/.local/state/litellm`.
The root-owned unit is `/etc/systemd/system/litellm.service`.

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

LiteLLM 1.102.1 accepts `service_tier` on Responses requests but removes it in
the ChatGPT adapter's outgoing allowlist. A request for `priority` therefore
silently uses the backend's default tier. `deploy.yml` runs
`scripts/patch_litellm.py` after environment synchronization to add just
`service_tier` to that allowlist. It does not force priority on ordinary requests
or change model discovery, authentication, or other providers.

The patch checks both the installed version and the complete adapter SHA-256,
accepts only the original or already-patched source, and writes atomically without
modifying uv's cached package. Unexpected versions or edits fail deployment.
Check mode reports whether a patch is needed without changing the environment.
An actual patch triggers the existing service restart handler; repeat deployments
are unchanged. No catalog refresh or desktop restart is required for this fix.

After deploying from a clean committed checkout, verify both tiers and the real
Codex search/tool flow. These probes consume subscription usage:

```sh
uv run --no-sync scripts/verify.py --service-tier priority
uv run --no-sync scripts/verify.py --service-tier default
uv run --no-sync scripts/verify_codex.py --service-tier priority
```

The API probes require the backend to report the requested tier on streaming,
function-call, and tool-result responses. A successful text response alone is
not evidence that fast mode worked. The backend can still return `default` for
an explicitly forwarded `priority` request; the priority probe deliberately fails
in that case. Do not treat a forwarding fix as proof of backend priority service.
The Codex probe enables its fast-mode feature and exercises an explicit tier
with native search and resumed tool execution. Both probes accept `--base-url`
to test an isolated proxy without changing the live listener. Also check the
desktop Fast toggle in a new task before declaring the rollout verified.

Rollback requires restoring the installed adapter as well as reverting the
deployment change; deploying an earlier checkout alone does not undo a package
edit. Before deploying that checkout, use the installed utility and restart:

```sh
uv run --no-sync --project "$HOME/.local/share/litellm" \
  "$HOME/.local/share/litellm/scripts/patch_litellm.py" --restore
sudo systemctl restart litellm.service
```

This restores only the verified upstream source and leaves no recovery copies.
Retire the patch and its deployment task when a locked upstream release preserves
`service_tier` and passes the same tier probes. Dependency upgrades deliberately
require reviewing this exception rather than carrying it forward silently.

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
approval lifecycle. Stock LiteLLM 1.102.1's ChatGPT adapter drops `text.format`,
so the probe does not assert upstream JSON-schema enforcement.

If the broken reviewer prevents the repair command from being approved, perform
the repair through a user-approved host terminal. Do not disable or work around
approval controls from inside the agent task.

Development checks:

```sh
uv run --no-sync scripts/test_patch_litellm.py
uv run --no-sync scripts/test_refresh_models.py
uv run --no-sync scripts/test_cutover.py
uv run --no-sync scripts/test_verify_review.py
```
