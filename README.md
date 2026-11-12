# LiteLLM deployment for stock Codex

> **Personal project:** This is my personal setup. I'm sharing the source in the
> hope that it benefits others. I am not accepting outside pull requests at this time.

Pinned LiteLLM with a small repository-owned ChatGPT request customization, uv,
and systemd, serving stock Codex through a named `litellm` Responses provider.
The ChatGPT subscription catalog is discovered by an explicit refresh; every other
model is ordinary LiteLLM configuration.

## How a machine is configured

The repository is identical on every machine. Everything that differs between
machines lives in one file outside the repository:

```text
~/.config/litellm-deploy/config.yml
```

Start from [`deploy/config.example.yml`](deploy/config.example.yml). It holds the
account and home directory, the mise binary, the pinned Codex version, the default
model, the proxy-credential mode, a few switches (`forward_agent`,
`compaction_fallback`, `voice`, `local_inference`, `browser`), the encrypted
provider credentials to load, and the `models:` list. Unknown keys are rejected.
Ansible reads it by default; pass `-e config=PATH` to use another file. The
scripts take `--config PATH`.

Run every playbook **locally, as the configured user, on the machine being
configured**, from a clean committed checkout. The inventory is `localhost` only.
Each playbook first checks that the running account and home match the
configuration. There is no remote-controller deployment.

Every machine runs the same pieces:

- the loopback LiteLLM gateway on `127.0.0.1:4000`;
- the pinned Codex app server;
- the Remote Codex forwarder, whose connection checks the runtime gates require.
  Stock SSH clients are checked as well.

Paths:

- application files: `~/.local/share/litellm`;
- generated configuration: `~/.config/litellm`;
- mutable state: `~/.local/state/litellm`;
- the root-owned unit: `/etc/systemd/system/litellm.service`.

The proxy runs from a locked uv environment with no runtime dependency downloads.
No database or Redis server is configured.

### Providers are LiteLLM configuration

Modal, Bedrock, Vertex, Claude, and local models are not deployment features. Each
is one `models:` entry whose `litellm:` block supplies `litellm_params`, plus
whatever credential that provider needs. Documented defaults fill gaps in the
pinned runtime's provider metadata; explicit output budgets still win. See
[`docs/FABLE-NATIVE.md`](docs/FABLE-NATIVE.md) for Fable's adaptive-thinking and
output-budget defaults. Each entry has:

| Key | Purpose |
| --- | --- |
| `name` | The route and the Codex slug, as `provider/model`. `chatgpt/` is reserved for the discovered subscription catalog. |
| `litellm` | LiteLLM `litellm_params`, exactly as LiteLLM documents them. Use `os.environ/VAR` for secrets. |
| `model_info` | Optional LiteLLM `model_info`. `catalog_pricing` selects the price row shown in Codex. |
| `codex` | Codex catalog fields. `display_name`, `context_window`, `supported_reasoning_levels`, and `default_reasoning_level` are required. |
| `verify` | Optional `{args, validators, timeout}` used before the model is published. Validators are scripts or commands with `{model}` and `{catalog}` placeholders. |

To add, change, or remove a model:

1. Edit `models:`.
2. If the model needs a new credential, add it under `credentials:` and rerun
   `deploy.yml` so the service loads it.
3. Run `refresh-models.yml`.

Refresh validates new and changed entries before publishing them.

### Credentials

The gateway key (`litellm-proxy-key`) is always loaded. Each additional
`credentials:` entry names a systemd-encrypted file, which must be root-owned with
mode 0600. Create one without writing plaintext to disk, for example:

```sh
read -rs SECRET && printf %s "$SECRET" | \
  sudo systemd-creds encrypt --name=modal-inference-token - /etc/credstore.encrypted/modal-inference-token
```

The `name` in the configuration must match the name embedded in the credential.
Each entry's `export` tells `scripts/launch.py` how to turn the decrypted file into
environment variables:

- `{env: VAR}`: the whole file is one token.
- `{path_env: [VAR, ...]}`: the file's path, for SDKs that read a file.
- `{json: {field: VAR, ...}}`: string fields of a JSON document.
- `{modal_token: VAR}`: a Modal combined `wk-…ws-…` inference token. The launcher
  also accepts `MODAL_PROXY_TOKEN=` or a `WK_SECRET`/`WS_SECRET` pair and exports
  only the combined token.

Deployment writes the non-secret export map to `~/.config/litellm/credentials.json`.
The launcher parses each credential in memory and never evaluates shell code. It
removes ambient provider credentials (AWS, Google, Modal, and similar variables)
from its environment before exporting the configured ones. A malformed credential
fails startup with a generic message that does not echo its contents.

Never copy encrypted credentials or saved ChatGPT tokens between machines. Each
machine encrypts with its own host key.

## Install and upgrade

Use the account's existing mise installation and this repository's pinned Ansible
and uv. Application Python must be 3.14, and deployment checks for it before
writing any files. The inventory's `/usr/bin/python3` is only Ansible's target
interpreter.

```sh
mise trust && mise install
mise exec -- uv python install 3.14
install -d ~/.config/litellm-deploy
cp deploy/config.example.yml ~/.config/litellm-deploy/config.yml   # then edit it
```

Each step below is an explicit transaction with its own recovery. Commands are
run with `mise exec --`, which is omitted here for brevity.

1. **Stage the pinned Codex binary.** This installs it beside existing versions.
   It leaves login, configuration, launchers, and the running app server unchanged:

   ```sh
   ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=install
   ```

2. **Prepare runtime recovery, deploy the gateway, then finish:**

   ```sh
   uv sync --locked
   uv run --no-sync scripts/runtime_rollout.py prepare --source "$PWD"
   ansible-playbook -i deploy/inventory.yml deploy/deploy.yml --check --diff
   ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
   uv run --no-sync scripts/runtime_rollout.py finish --source "$PWD"
   ```

   If the deployment prints `MODEL REFRESH REQUIRED`, the new release changes
   generated routes or picker entries. After `finish`, run step 4.

3. **Log in to ChatGPT for LiteLLM** on a new machine. See
   [ChatGPT login](#chatgpt-login) below.

4. **Publish models.** This discovers the subscription catalog, validates the
   configured models, and writes `~/.config/litellm/models.json` and the Codex
   catalog:

   ```sh
   ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml --check --diff
   ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml
   ```

5. **Switch the Codex runtime** using apply, then restart, verify, and finish. See
   [Codex runtime](#codex-runtime).

Every script takes `--config PATH` when the configuration is not in the default
location. Step 1 stages Codex side by side; the steps above do not select or
restart the app server until step 5.
Ordinary deployment changes neither the Codex catalog nor its configuration.

Check mode cannot sync an absent environment or provision credentials. On a fresh
machine, the preview covers paths, templates, and prerequisites, not live readiness.

### First rollout from the per-provider layout

Earlier releases installed per-provider scripts, model manifests, and systemd
drop-ins. These are listed in [`deploy/retired.yml`](deploy/retired.yml).
`deploy.yml` removes them, and `runtime_rollout.py` journals the removal so
rollback restores them.

Until `models.json` exists, `deploy.yml` skips its installed-snapshot check,
because the old generators cannot read the new configuration. Run
`refresh-models.yml` (step 4) immediately after the first new deployment.

Both exceptions have the same removal criterion: once every machine has published
`models.json` from this layout, remove `retired.yml`, its two readers, and the
`models.json` condition. Afterward, the old state files
(`~/.local/state/litellm/{bedrock,claude,modal,local,vertex}-models.json`,
`bedrock-runtime-installed.json`, and `vertex-installed.json`) are unused and
can be deleted.

### Recovery

- **Gateway runtime.** Before acceptance, run
  `uv run --no-sync scripts/runtime_rollout.py rollback --source "$PWD"`.
  - The rollback restores only changed application, service, and retired files,
    then re-syncs the prior lock.
  - After a failed first installation, it stops and disables the new service and
    removes the files it created.
  - Encrypted credentials are kept for a retry.
  - The journal at `~/.local/state/litellm/runtime-upgrade.json` is removed on
    finish or rollback.
  - Rollback stops, rather than overwriting, if files were edited concurrently.
- **Models.** Run `ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml -e refresh_action=rollback`.
  It recovers an interrupted refresh from `~/.local/state/litellm/model-refresh`.
- **Codex runtime.** Run `codex-runtime.yml -e codex_runtime_action=rollback`, as
  described below.

Ordinary deployment refuses to run while a model refresh or voice transaction is
unfinished.

## ChatGPT login

Complete a separate ChatGPT device login with the subscription account:

```sh
CHATGPT_TOKEN_DIR="$HOME/.local/state/litellm/chatgpt" \
  uv run --no-sync --project "$HOME/.local/share/litellm" \
  "$HOME/.local/share/litellm/scripts/login.py"
```

The login prints only the temporary device authorization code, never tokens.
Stock LiteLLM OAuth token files are private (a 0700 directory with 0600 files),
but this deployment does not encrypt them. Codex's own login is not changed.

## Encrypted proxy key

Provisioning generates one random key and encrypts it without writing a plaintext
file. A system credential is always created at
`/etc/credstore.encrypted/litellm-proxy-key`. With `credential_mode: user`, an
account-scoped copy is also created at `~/.config/litellm/proxy-key.cred`.

- **Encryption key.** Systemd's host key is used. Disk encryption is still needed
  against theft of the whole disk, including that key.
- **Who can read it.** Root or a compromised live account can access the running
  credential.
- **Partial pairs.** An incomplete pair fails closed rather than generating a
  replacement that could desynchronize clients.
- **How the gateway reads it.** Systemd `LoadCredentialEncrypted` supplies the
  credential, and the launcher sets LiteLLM's master key from it.
- **How Codex reads it.** In `user` mode, Codex decrypts it with
  `systemd-creds decrypt --user`, with no sudo. In `system` mode, validation
  commands decrypt it through noninteractive sudo into process memory.

The key never appears in `.codex/.env`, the unit, `config.toml`, or Git.

## Codex runtime

The desktop's Fast controls require the machine's genuine ChatGPT identity.

### Configuration requirements

`~/.codex/config.toml` is user-managed. The runtime deployment validates that its
`litellm` provider has:

- `requires_openai_auth = true`;
- `env_key = "LITELLM_PROXY_KEY"`;
- the local endpoint;
- standard or Fast speed;
- shell snapshots disabled;
- proxy-key exclusion.

When something is missing, it reports the exact correction. It never rewrites,
snapshots, or restores the file.

### What the runtime deployment owns

- `codex-app-server.service`;
- the Desktop attach setting;
- the credential-loading launcher;
- the `~/.local/bin/codex` entry point;
- the pinned Codex binary selection.

The launcher decrypts the proxy credential into the process environment, rejects
empty or invalid credentials, and runs the configured exact Codex version.
Environment exclusion keeps the key out of shell tools. **Shell snapshots must be
disabled**, because snapshots could restore the startup key after exclusion. With
`forward_agent: true`, the unit forwards the SSH agent into the app server.

The unit restarts at most 3 times in 300 seconds, so a crash loop leaves it in the
failed state instead of retrying forever. After reading
`journalctl -u codex-app-server`, recover with
`sudo systemctl reset-failed codex-app-server.service` and then
`sudo systemctl start codex-app-server.service`.

### Runtime actions

```sh
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml                                     # preview
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=apply
# Restart the app server from the owning desktop (or with systemctl).
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=verify
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=finish -e codex_desktop_validated=true
```

- **`apply`**
  - Validates `config.toml` and installs the exact binary if needed.
  - Tests the candidate with the installed forwarder under its real systemd
    sandbox, using an isolated Codex home and a synthetic credential.
  - Tests an isolated candidate restart.
  - Only then stages the launcher, unit, and attach setting.
- **`verify`**
  - Requires a replaced app-server socket.
  - Checks that the live process uses the pinned binary and the proxy credential.
  - Runs a real shell turn to prove the key is absent from tools.
  - Checks the configured default model's streaming, tool continuation, native search and resume,
    and the reserved reviewer route.
- **`finish`**
  - Reruns a live turn.
  - Runs the forwarder's authenticated local connection check and the SSH proxy-client check.
  - Records the accepted manifest.

The forwarder checks live in `~/.local/libexec/remote-codex-checks/`. Deploy Remote
Codex first. A missing check or a failed gate stops the operation and keeps the
recovery state. Nothing is retried or rolled back automatically.

To install updated checking utilities without staging or restarting Codex, run:

```sh
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=apply --tags runtime-checks
```

### Recovery state and rollback

One private transaction at `~/.local/state/litellm/codex-runtime.json` records:

- the repository-owned runtime files;
- the prior version selection;
- the pre-restart socket identity.

It never contains `config.toml`. Rollback refuses managed files that differ from
the staged revision, restores the previous runtime as a whole, and then requires
a restart:

```sh
ansible-playbook -i deploy/inventory.yml deploy/codex-runtime.yml -e codex_runtime_action=rollback
```

After acceptance, the installed manifest becomes the baseline for later updates.
An update aborts on any intervening managed-file edit instead of overwriting it.
Previous binaries stay installed; rollback changes the selection rather than
deleting shared mise state.

Before changing the provider on a machine whose history must be preserved, run
`scripts/verify_codex_connection.py baseline --home HOME --codex-binary BINARY`
with `uv run --no-project`. It stores only hashes of native authentication,
project and thread IDs, and saved session files. When pinned Codex compresses
older rollouts, it hashes the decompressed content. Remove the baseline after
acceptance or after a verified recovery.

After each runtime update, repeat the desktop acceptance:

- the desktop reconnects to the intended host/model with projects and history intact;
- for a model advertising Fast, a Fast-on turn sends `priority`, including after a restart;
- for a model advertising Fast, a Fast-off turn omits the field;
- the shell sees no proxy key.

Automated staging and live shell checks require the selected model's configured
`service_tier` (or `default` when omitted). Advertising Fast does not imply the
account has access to its optional paid tier. A configured `priority` tier still
has to pass; failures are never silently downgraded. To validate both advertised
tiers explicitly, run `verify_fast_app_server.py --all-tiers` with the managed
environment. Report unavailable Fast separately, and do not claim Fast desktop
acceptance when its account limit or usage credits prevent a successful turn.
Models without a priority tier, such as a configured Vertex model, need no
subscription-model test.

## Refresh models

```sh
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/refresh-models.yml
```

Refresh is the only way the published model set changes. There is no timer or
background synchronizer.

- **Subscription discovery.** Refresh fetches the account's current Codex catalog
  using LiteLLM's ChatGPT login and the pinned Codex version.
  - Each selectable upstream chat model gets one `chatgpt/<slug>` route and one
    visible picker entry.
  - Hidden native entries keep their metadata for existing tasks.
  - The reserved `codex-auto-review` name routes to `chatgpt/codex-auto-review`
    and stays hidden. Discovery that omits the reviewer is rejected.
- **Configured models.** Every `models:` entry becomes a router deployment and a
  catalog row. A configured `priority` controls its position in the picker.
- **Default model.** It is never changed. If it disappears from discovery, the
  refresh aborts rather than substituting another model.
- **Upgrade actions.** Upgrade actions on routed entries point to the matching
  selectable `chatgpt/` route. When that target is missing, the action is cleared
  and reported in `disabled_upgrades`.
- **Older catalogs.** An empty `available_access_programs` gains the empty `cyber`
  list that current Codex parsers require. This grants no access program.

**Preview** performs discovery and prints:

- added and removed model IDs;
- changed metadata;
- which models would be probed.

It installs nothing, refreshes no OAuth tokens, restarts nothing, and prints no
credentials.

**A live refresh** proceeds in this order:

1. Checks the candidate catalog with stock Codex.
2. Restarts LiteLLM only if routes changed.
3. Tests each new or changed model, two at a time: streaming, function-call
   continuation, and stock Codex search, tools, and context. Search is tested only
   when the model advertises it.
4. Runs the entry's `verify.validators`, if any.
5. Publishes the picker only after everything passes.

A Codex version change revalidates every model and the reviewer. Validation
consumes usage. Unchanged refreshes perform no inference and no restart.

**Failure handling.** A failure restores the previous configuration, catalog, and
discovery, restarts the restored routes, and checks liveness. Recovery state is
kept in `~/.local/state/litellm/model-refresh` only while work is unfinished.
Recovery refuses to overwrite concurrent edits; in that case, inspect the
directory and reconcile before retrying.

**Transient provider errors.** If a validation check fails with a provider
unavailability or throttling error (HTTP 503, 529, or 429, `ServiceUnavailable`,
or Bedrock's "unable to process"), refresh waits 15 seconds and runs that whole
check once more. Only the second run decides the result. The retry is labeled in
`validation.log` and in the `transient_retries` result. Any other failure, or a
second failure, fails the refresh. This applies only to acceptance validation;
model routes themselves do not retry inference.

**After a catalog change,** restart the Codex app server so it loads the new
picker.

An expired ChatGPT login is reported. Refresh does not start an interactive login.

Ordinary deployment checks the installed snapshot against the configuration. It
never fetches anything or changes timestamps. If generated files are missing or
inconsistent, it stops and asks you to run refresh.

That preflight uses the previously installed scripts. After installing, deployment
checks again with the new scripts. If the new release changes generated routes or
picker entries, it prints `MODEL REFRESH REQUIRED`. Run refresh after
`runtime_rollout.py finish`, not before the deployment, because validation needs
the new code running.

### Recommended picker order

Stock Codex sorts the picker by ascending catalog `priority`. Subscription
`chatgpt/` rows start at 1. To recommend a configured model, give it a negative
`codex.priority`. While any recommended row is listed, refresh adds a blank,
unrouted `separator` row at priority -1 below the recommended rows.

Codex has no native divider, so the separator is selectable. Choosing it returns
LiteLLM's unknown-model error. Pricing enrichment and compaction fallback skip it.
Stock Codex treats the first row as the picker default, but the configured
`model` is unchanged.

### Catalog pricing

`~/.config/litellm/codex-models.json` embeds versioned `remote_codex`
pricing and provenance beside its stock `models` array. Remote Codex clients read
the file through stock `config/read` and `fs/readFile`; there is no separate
pricing service.

- **OpenAI rates** come from OpenAI's official pricing document, including tiers,
  context variants, and cache rates.
- **Other rates** use exact models.dev identities, selected by
  `model_info.catalog_pricing`:
  - `{provider, model}` names the pricing identity;
  - `aliases` lists recorded runtime IDs;
  - `zero_rate: true` marks a deliberately free local route.
- **Unknown models.** An endpoint name is never guessed to be a model ID; unknown
  rates stay unavailable.
- **Not invoices.** These figures are API-equivalent estimates, not hosting
  invoices.

**Source failures.** If a pricing source fails, only previously validated prices
for the same identity are kept, marked stale with their original timestamp. If a
source succeeds but no longer publishes a price, that price becomes unknown.

**Pricing-only changes** set `enrichment_changed` and `catalog_document_changed`.
They publish without inference probes or restarts.

### Native web-search compatibility

In the pinned Codex, models that use Responses Lite suppress hosted web search,
and its standalone search uses `/v1/alpha/search`, which this deployment does not
serve. Generated entries therefore set `use_responses_lite=false`. All other
upstream metadata is retained.

Incomplete catalogs are rejected rather than filled with invented defaults.
`model_catalog_json` points at `~/.config/litellm/codex-models.json`, which Codex
loads at startup.

## ChatGPT Responses customization

`scripts/chatgpt_responses.py` subclasses the pinned upstream adapter. It changes
only request instructions and field preservation.

- **Instructions.** Explicit client instructions, including an empty string, are
  passed through unchanged. When instructions are absent, upstream's fallback is
  used.
- **Restored fields.** After upstream transformation, it restores:
  - `text` (`format` and `verbosity`);
  - `parallel_tool_calls`;
  - `prompt_cache_key`;
  - `service_tier`.
- **Left to upstream.** Authentication, token refresh, headers, transport,
  streaming, parsing, and errors. Upstream still forces `store=false`, streaming,
  and encrypted reasoning.

`scripts/start-litellm` runs `scripts/serve.py` through the locked environment.
`serve.py` replaces the `litellm.ChatGPTResponsesAPIConfig` export before invoking
the upstream CLI. The server runs with one worker. Running the stock `litellm`
command directly bypasses this customization.

Changes to runtime files restart the service; utility-only updates do not. The
launcher's optional fourth argument selects a port for isolated tests.

The override fails startup on any LiteLLM version other than the pinned one. Before
any upgrade, revalidate the dispatch, transformation, and launcher tests.

Live subscription probes (account- and model-specific, not guarantees) found:

| Field | Evidence and limits |
| --- | --- |
| `text.format: json_schema` | Schema-required enums were honored despite conflicting instructions. Invalid schemas were rejected with `invalid_json_schema`. |
| `text.format: json_object` | Accepted when the input mentions JSON. It is not schema enforcement. |
| `text.verbosity` | `low` was accepted and echoed. Invalid values were rejected. Output-length effects were not measured. |
| `parallel_tool_calls` | `false` and `true` produced one and two calls respectively. |
| `prompt_cache_key` | Forwarded, but the backend ignored the body value. A `session_id` header becomes the cache key. |

The locked `prisma` client is included only because stock LiteLLM's
authentication error handler imports it unconditionally
([BerriAI/litellm#38978](https://github.com/BerriAI/litellm/issues/38978)). No
database is configured.

## Fast mode

The customization preserves an explicitly supplied `service_tier`, which pinned
LiteLLM otherwise drops. Ordinary requests do not gain a tier. These probes consume
subscription usage:

```sh
uv run --no-sync scripts/verify.py --service-tier priority
uv run --no-sync scripts/verify.py --service-tier default
uv run --no-sync scripts/verify_codex.py --service-tier priority
```

The subscription backend can report `default` after a `priority` request, so the
returned tier is not an acceptance gate. See the
[Codex contributor's explanation](https://github.com/openai/codex/issues/14204#issuecomment-4033184620).
Offline tests check forwarding strictly. Check the desktop Fast toggle before
declaring Fast mode verified. Remove the override once a pinned upstream release
preserves the tier.

## Compaction recovery

`compaction_fallback: true` (the default) enables the compaction-only recovery
wrapper through `COMPACTION_FALLBACK_ENABLED` in the unit. Setting it to `false`
and redeploying restores ordinary dispatch.

**Which requests enter recovery.** Only requests marked `request_kind=compaction`
enter recovery. Codex carries the marker in `client_metadata["x-codex-turn-metadata"]`;
the legacy header is also accepted. Ordinary turns and `codex-auto-review` are
unaffected.

**Candidate order:**

1. The requested compactor.
2. After an availability failure, models from other backends, in picker priority
   order.
3. Remaining models on the original backend.

Candidates are the published catalog (`COMPACTION_MODEL_CATALOG`) intersected with
the active router deployments. Hidden, inactive, unauthorized, and unsuitable
routes are excluded. Each attempt has 120 seconds and no nested retries.
Cancellation closes the active stream.

**Handling summaries:**

- Summaries are buffered with SSE keepalives, so failed partial summaries never
  reach Codex.
- Compatible models reuse native history. Other backends receive quoted, readable
  history with tool execution disabled.
- Opaque checkpoints are not translated across backends. Unknown history items
  are rejected.
- ChatGPT compaction that includes earlier native search calls still declares the
  search tool, but disables execution.

On success, Codex persists its usual checkpoint. On exhaustion, the original error
and the outcome list are returned. Structured `compaction_recovery` logs identify
the actual compactor and size decisions, without transcripts or credentials.

```sh
(cd scripts && uv run --no-sync --project .. -m unittest test_compaction_fallback)
CODEX_COMPACTION_TEST_BINARY=/path/to/codex uv run --no-sync scripts/test_proxy_startup.py
uv run --no-sync scripts/verify_compaction.py            # live, synthetic history
```

Native OpenAI chats that bypass LiteLLM do not use this path.

## Shared search

Some providers have no hosted search. For them, `shared_search.py` extends
LiteLLM's `WebSearchInterceptionLogger`. LiteLLM's agentic loop performs the model
continuations. The extension supplies a subscription-backed search helper,
preserves mixed client and search tool batches, and emits standard Responses
search events to stock Codex. Native ChatGPT search keeps its own path.

**Helper model.** The helper is `chatgpt/gpt-6-luna` at low effort. Set
`SHARED_SEARCH_MODEL` to choose another configured ChatGPT route. ChatGPT
credentials never reach the other providers.

**Limits:**

- Every requested search runs, and parallel searches are not capped.
- Each Responses request allows five search rounds. Results in the fifth round
  tell the model that search is closed; any later search returns immediately as
  a failed "not run" result.
- `max_agentic_loops` is seven, which is the absolute stop.
- Each query has 60 seconds.
- Results keep up to 4,000 characters and eight sources.

**Context versus consumption.** Standard Responses usage describes the latest
model request, not the sum of internal ToolSearch or web-search requests. This
keeps stock Codex's context meter and automatic compaction accurate. Its cumulative
usage display consequently excludes hidden requests and is not a complete
subscription-consumption total.

Worker and completed-search journals add `usage_accounting` version 2:
`latest_request_usage` is the context snapshot; `consumed_usage` sums distinct
model-request receipts, including cache-read and cache-write details. Claude
journals also retain the SDK's aggregate query usage. Search-helper usage remains
separately attributed in the existing helper records. Completed response replays
do not create consumption receipts or repeat tools.

Inspect the ten newest accounting records without inference or conversation text:

```sh
uv run --no-sync scripts/verify_claude.py --usage-report
```

The report also counts legacy records. Old cached replies remain immutable until
normal eviction or expiry. An existing Codex thread can compact once from a
previously stored inflated count; deployment does not rewrite conversation history.

**Journals.** Private journals in `~/.local/state/litellm/search` hold search
calls, findings, and replays, scoped to the authenticated caller. Completed
records are pruned after 30 days or 100 MiB per caller. Treat them as conversation
data. `SHARED_SEARCH_STATE` overrides the directory for testing.

```sh
(cd scripts && uv run --no-sync --project .. -m unittest test_shared_search)
uv run --no-sync scripts/verify_shared_search.py --isolated --all-efforts
```

Configured models opt in by listing `verify_shared_search.py` among their
validators and by advertising search in their `codex` block.

## Provider notes

Each provider below is only a `models:` entry plus a credential. See
`deploy/config.example.yml` for one entry of each kind.

### Modal

Modal endpoints are OpenAI-compatible routes:

- `model: openai/<endpoint>`;
- `api_base: https://inference.<region>.modal.direct/v1`;
- `api_key: os.environ/MODAL_API_KEY`;
- `use_chat_completions_api: true`.

Stock LiteLLM bridges Responses to Chat Completions. No Modal SDK or custom adapter
is required. Load the token with a `{modal_token: MODAL_API_KEY}` credential.

For Moonshot models identified by `model_info.catalog_pricing.provider:
moonshotai`, the catalog ships `kimi-instructions.md` by default. An explicit
`codex.base_instructions` or `base_instructions_file` remains authoritative.
Kimi K3.1 Pro's complete `reasoning_content` is replayed with the original
assistant message and tool calls through LiteLLM's native Chat bridge; no opaque
state cache or separate session database is needed. Raw reasoning is replay
state, not a provider-authored summary, so reasoning-summary catalog flags stay
disabled. The pinned bridge keeps terminal items consistent with their streamed
IDs, and shared search/compaction use the same typed event renderer.

Kimi's configured reasoning effort remains unchanged. Its low/high/max levels
are advertised only when declared by the per-machine configuration.

Endpoint names are not model IDs. Set `model_info.catalog_pricing` explicitly to
show prices. Begin with a fresh task: encrypted OpenAI compaction state is not
converted for Modal.

### AWS Bedrock

Use LiteLLM's Bedrock parameters (`model: bedrock/...` and `aws_region_name`),
with a `{json: {...}}` credential exporting `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, and `AWS_REGION_NAME`. The launcher disables ambient AWS
credential sources.

- **Responses mode.** Models served by Bedrock's native Responses API set
  `mode: responses` in `model_info` and `store: false` in `litellm`, so history is
  replayed by the client.
  - Bedrock's Responses authorization needs a separate `bedrock:InvokeModel`
    statement on the account's `project/default`, constrained by `ArnEquals` on
    `bedrock:ModelArn`. See
    [AWS's Responses contract](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html).
  - This deployment never changes IAM, so an administrator must reconcile that
    policy.
  - Do not grant the runtime identity stored-response management, IAM
    administration, or wildcard model access.
- **Forced tool selection.** Some profiles reject it. Add
  `verify: {args: [--auto-tool-choice]}`.
- **Tool-name length.** Bedrock caps tool names at 64 characters. Code Mode keeps
  connected-app tool names within that limit.
- **Effort levels.** Advertise only the levels the pinned adapter accepts.
- **Validators.** Use `verify_capabilities.py`, `verify_shared_search.py`, and
  `verify_codex_image.py`. The image check needs a correct visual answer for both
  an attachment and a `view_image` tool result, and the expected answer never
  appears in the prompt.

### Google Vertex AI

Use `model: vertex_ai/<model>` with `vertex_project` and `vertex_location`. Load a
restricted service-account key with a
`{path_env: [VERTEX_CREDENTIALS_PATH, GOOGLE_APPLICATION_CREDENTIALS]}` credential.
The SDK reads the decrypted file from systemd's private credential directory. No
key is copied elsewhere. Validate with `verify_capabilities.py` and
`verify_shared_search.py`.

### Claude subscription adapter

The adapter supports Opus, Sonnet, and Haiku 5.5; each configured
`claudesdk/<model>` route runs Claude through the Claude Agent SDK, using the
account's existing `claude auth login`. To renew a login, run that command as the
configured user and retry. No Claude token appears in LiteLLM YAML, systemd, or
Git.

Each machine keeps its own login under `~/.claude`. Never copy that login between
machines: doing so would share one rotating refresh token. On a new machine, log
in with `~/.local/share/litellm/scripts/claude-cli auth login` before publishing
its Claude route.

**Worker isolation.** The `claude-cli` wrapper starts the SDK's binary with a
minimal environment. Workers get:

- an empty private working directory;
- no settings sources;
- only the declared MCP server.

Every command and edit still runs through Codex and its approvals.

**Catalog settings.** Set `codex.base_instructions_file: claude-instructions.md`,
`tool_mode: direct`, and `supports_search_tool: false`:

- Claude calls Codex tools directly under their real names.
- `tool_search` is off because the adapter cannot relay it. Hosted search still
  works through shared search.
- Claude's own `ToolSearch` loads Codex tools on demand instead. Only
  `exec_command`, `shell_command`, `write_stdin`, `apply_patch`, `update_plan`
  and `view_image` stay in context. The CLI runs each search itself and never
  sends it to Codex, and `claude-cli` sets `ENABLE_TOOL_SEARCH=true`.

**Supported features:**

- streaming and non-streaming Responses;
- inline images;
- JSON schema output;
- summarized reasoning;
- namespaced functions and freeform patches.

**Not supported:**

- Image URLs are not fetched.
- Sampling, token-budget, verbosity, and server-side retrieval controls are
  rejected.

**Fast mode.** `service_tier: "priority"` selects the worker-local
`fastMode: true`. This requires Claude usage credits.

- Fast is strict: every message must report `usage.speed: "fast"`, or the request
  fails.
- The adapter never retries at standard speed. Claude's runtime can still retry
  internally before the adapter notices.
- Speed changes apply on the next user turn.

```sh
uv run --no-sync scripts/verify_claude.py --isolated --fast-only   # consumes credits
```

**Sessions.**

- **Identifying a session.** Clients send `thread-id` or `x-claude-chat-id` with
  the full history. Session identity also includes the caller, the model, and the
  context window.
- **Concurrency.** Requests for one session are serialized, and chats run
  independently.
- **Partial tool results.** These return the outstanding calls with their
  original IDs.
- **Retries.** The last 16 responses per worker can be replayed.
- **Rebuilding.** Forks, compaction, configuration changes, and lost workers
  rebuild from readable history. Unknown tool execution is never replayed.
- **Journals.** Private journals under `~/.local/state/litellm/claude/journals`
  are conversation data.
- **Worker cap.** At most 20 idle workers (`MAX_WORKERS`, about 110 MiB each) stay
  alive, with least-recently-used eviction. Workers do not expire on a timer.

Deployment restarts the proxy, which ends in-memory workers, so deploy while
Claude is idle. The isolated acceptance suite (`verify_claude.py --isolated`)
starts a disposable proxy and Codex home and covers:

- efforts, images, and schemas;
- tool continuations, forks, and compaction;
- switching providers;
- cancellation;
- crash recovery.

### Local inference

With `local_inference: true`, `deploy/local.yml` installs the checksum-pinned
llama.cpp CUDA build and the model recorded in `scripts/local-model.json`. It uses
the existing driver and installs no CUDA toolkit. It then deploys the gateway and
runs the shared refresh to publish the configured `local/...` entry.

**Router behavior.** The `llama-local.service` router listens on `127.0.0.1:18743`.

- It starts with the weights unloaded and loads them on the first request.
- It unloads after 300 idle seconds.
- Monitoring endpoints do not wake it.
- It runs one fixed 16K slot with 512-token batches and a 4 GiB RAM prompt cache.
  llama.cpp fits GPU offload to available VRAM at each load, leaving its default
  1 GiB margin and keeping any remaining layers on the CPU. It does not shrink
  the advertised context to fit.
- A cold start can take five minutes.

**Chat template.** The GGUF's built-in chat template rejects a mid-conversation
system message. llama.cpp therefore loads
`deploy/files/qwen3.8-27b-chat-template.jinja`, which changes only that check.
Deployment stops if the embedded template no longer matches `upstream_sha256`.

- **Regenerating the override:** run
  `uv run --no-project scripts/gguf_template.py <model.gguf> --extract <path>`,
  reapply the one-line change, and update `sha256`.
- **Removing it:** delete the override once llama.cpp #27367 lands.

```sh
ansible-playbook -i deploy/inventory.yml deploy/local.yml --check --diff
ansible-playbook -i deploy/inventory.yml deploy/local.yml
ansible-playbook -i deploy/inventory.yml deploy/local.yml -e refresh_action=rollback
uv run --no-sync scripts/verify_local.py --proxy --lifecycle
```

- **Stopping it.** Stopping `llama-local.service` releases its memory and keeps
  requests from loading the model. Nothing detects games or other GPU users.
- **Restart limit.** Systemd restarts are bounded to three in five minutes.
- **The lifecycle probe** measures cold and warm latency, idle unload, and memory
  release. Run it when no other local client is active.

## Subscription voice

With `voice: true`, LiteLLM serves a narrow stock-Codex WebRTC signaling and
control adapter. Client audio travels directly over WebRTC; the server runs no
audio bridge. `deploy/voice.yml` manages it with these actions:

- `preview`
- `prepare`
- `verify`
- `rollback`
- `finish` (requires `-e voice_desktop_validated=true`)
- `waive`

See [docs/VOICE.md](docs/VOICE.md) for the contract, reference client,
verification, and migration to upstream support. See
[docs/VOICE-ACCEPTANCE.md](docs/VOICE-ACCEPTANCE.md) for acceptance.

## Browser capability

With `browser: {node: /path/to/node}`, `deploy/browser.yml` installs the `browser/`
package: a conversation-scoped stdio MCP wrapper around Playwright MCP, plus a
progressively loaded `browser` skill. It connects directly to stock Codex. LiteLLM
still carries only model traffic. Chromium starts only on a browser operation.

**Sessions and profiles:**

- `browser_session` accepts `status`, `open`, or `close`.
- Default browsing is isolated and in memory.
- `open` with `mode: "saved"` and a profile name uses persistent storage under
  `~/.local/state/codex-browser/profiles/`.
- Each profile is locked to one conversation at a time.
- Browsers close after 15 idle minutes.
- Browser subprocesses do not inherit provider credentials.
- No arbitrary Node-execution tool is exposed.

```sh
ansible-playbook -i deploy/inventory.yml deploy/browser.yml                                         # preview
ansible-playbook -i deploy/inventory.yml deploy/browser.yml -e codex_browser_action=stage
ansible-playbook -i deploy/inventory.yml deploy/browser.yml -e codex_browser_action=prepare
ansible-playbook -i deploy/inventory.yml deploy/browser.yml -e codex_browser_action=activate     # restarts the app server
ansible-playbook -i deploy/inventory.yml deploy/browser.yml -e codex_browser_action=verify
ansible-playbook -i deploy/inventory.yml deploy/browser.yml -e codex_browser_action=finish -e codex_browser_validated=true
```

The actions:

- **stage** installs a content-addressed release under
  `~/.local/share/codex-browser/` and runs its fixtures.
- **prepare** registers the skill and MCP without restarting.
- **activate** adds the `40-browser.conf` drop-in and restarts.
- **rollback** restores the previous release, or removes the registration.
- **finish** removes the superseded release.

Development checks:

```sh
npm --prefix browser ci --ignore-scripts --no-audit --no-fund
npm --prefix browser test
node browser/scripts/verify-codex.mjs
```

## Validation

```sh
uv run --no-sync scripts/verify.py           # auth rejection, streaming, tool round trip
uv run --no-sync scripts/verify_codex.py     # stock Codex search and tools
uv run --no-sync scripts/verify_review.py    # reserved reviewer route
```

Offline tests run one file at a time against the locked environment:

```sh
(cd scripts && LITELLM_LOCAL_MODEL_COST_MAP=True uv run --no-sync --project .. -m unittest test_refresh_models)
```

The startup test runs the real launcher and proxy on a temporary loopback port,
using a local SSE fixture and synthetic credentials. Tests that need an installed
Codex binary or Ansible take its path from an environment variable and skip
explicitly without it:

| Variable | Used by |
| --- | --- |
| `CODEX_ERROR_TEST_BINARY` | `test_responses_errors.py` |
| `CODEX_COMPACTION_TEST_BINARY` | `test_proxy_startup.py` |
| `ANSIBLE_TEST_BINARY` | `test_ansible_targets.py` |

### Missing approval reviewer

An installation without the reserved route rejects approvals with
`Invalid model name passed in model=codex-auto-review`. Run `refresh-models.yml`
before `deploy.yml`, because deployment's snapshot check rejects the incomplete
routing. Then run `verify_review.py`. If the broken reviewer blocks approval of
the repair itself, run it from a user-approved terminal. Do not work around
approval controls.

### Streaming provider failures

The Responses endpoint translates LiteLLM's bare SSE error objects into terminal
`response.failed` events. Provider messages and codes are retained, and HTTP 429
becomes `rate_limit_exceeded`, so Codex reports the actual failure instead of a
generic disconnect.
