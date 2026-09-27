# Grace LiteLLM deployment

Stock LiteLLM 1.102.1, uv, and systemd on Grace, for stock Codex through a named
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
No virtual-key administration, middleware, or source patches.
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
```

`verify.py` sends small subscription requests. It checks unauthenticated rejection,
model configuration, streaming completion, and a function-call/result round trip.
It does not print credentials or conversation content. Routine updates need only
deploy.yml; restart the desktop connection if its configuration changes.

The locked `prisma==0.15.0` client dependency is included solely because stock
LiteLLM's database-free authentication error handler imports it unconditionally
(upstream issue https://github.com/BerriAI/litellm/issues/38978). No Prisma engine,
schema generation, database connection, or database server is configured.

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

`chatgpt/` is the refresh-owned model namespace. Each upstream selectable model
gets one `chatgpt/<slug>` route and one visible picker entry. Hidden native entries
retain metadata for existing tasks. Other provider routes, their settings, and
catalog entries are preserved. The selected default is not changed; removing it
from the discovered list aborts the refresh rather than substituting another model.
Old tasks can retain their native model selection; no task history is rewritten.

Preview shows added/removed model IDs and changed metadata fields, with before/after
lists. It performs discovery but does not install files, refresh OAuth tokens,
restart services, validate with inference, or print credentials/model instructions.
Live refresh checks the candidate catalog with stock Codex, restarts LiteLLM only
if routes changed, and tests streaming, function-call continuation, and stock Codex
search/tools/context for new or changed models (two concurrent probes maximum).
Search is tested only when the discovered model advertises it. Validation consumes
subscription usage. The picker is published only after every probe succeeds.

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
Modal entries do not advertise native search; see the Modal setup below.

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
full tool history. No WebSocket support or native hosted search is advertised.
The old gateway's ChatGPT-backed search bridge is not part of this setup; adding
search requires a separately configured search backend. Subscription search is
unchanged. Existing gateway tasks are not relabeled or migrated, and encrypted
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

Development checks:

```sh
uv run --no-sync scripts/test_refresh_models.py
uv run --no-sync scripts/test_cutover.py
uv run --no-sync scripts/test_modal.py
uv run --no-sync scripts/test_modal_bridge.py
```

To run these checks without creating or synchronizing a worktree environment on
Grace, use the existing locked installation explicitly:

```sh
uv run --no-sync --project /home/agent/.local/share/litellm scripts/test_modal.py
uv run --no-sync --project /home/agent/.local/share/litellm scripts/test_modal_bridge.py
uv run --no-sync --project /home/agent/.local/share/litellm scripts/test_refresh_models.py
```
