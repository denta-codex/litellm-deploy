# Grace LiteLLM deployment

Stock LiteLLM 1.102.1, uv, and systemd on Grace, for stock Codex through a named
`litellm` Responses provider. Initial model: `chatgpt/gpt-5.4`. Edit the inventory
and redeploy to change models. This is a private, single-host deployment.

## Deploy

Use a clean committed checkout. Python 3.14 and uv use Grace's existing toolchain.

```sh
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --syntax-check
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
```

The proxy listens on `127.0.0.1:4000`. It runs with a locked uv environment and
no runtime dependency downloads. No database or Redis server is configured;
stock upstream may include their client libraries among its dependencies.
No virtual-key administration, middleware, source patches, or custom catalog.
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
```

`verify.py` sends small subscription requests. It checks unauthenticated rejection,
model configuration, streaming completion, and a function-call/result round trip.
It does not print credentials or conversation content. Routine updates need only
deploy.yml; restart the desktop connection if its configuration changes.

The locked `prisma==0.15.0` client dependency is included solely because stock
LiteLLM's database-free authentication error handler imports it unconditionally
(upstream issue https://github.com/BerriAI/litellm/issues/38978). No Prisma engine,
schema generation, database connection, or database server is configured.
