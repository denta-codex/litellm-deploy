# Gemini 4 Argon on Vertex AI

The sole Google picker entry is `vertex/gemini-4-argon`, routed to
`vertex_ai/gemini-4-argon` in project `fourth-gantry-511518-b0`, location `global`.
The existing default and all other provider routes are retained. Low, medium,
and high reasoning are advertised, with medium as default. The catalog uses
Code Mode, text/image input, and a unified 1,048,576-token context window.
Search uses the existing ChatGPT-backed shared-search adapter, including its
citation and retained-history handling. No Google Search grounding is enabled.

`litellm-vertex@fourth-gantry-511518-b0.iam.gserviceaccount.com` has the project
custom role `litellmVertexPredict`: only `aiplatform.endpoints.predict` and
`serviceusage.services.use`. This identity cannot manage IAM, billing, or cloud
resources. The Google IAM permission is project scoped; the exact model
allowlist is enforced by the route generator, not by a claimed per-model IAM
restriction. Credentials are created by `scripts/provision_vertex.py`, kept in
memory while being encrypted, and stored root-owned mode 0600 at
`/etc/credstore.encrypted/litellm-vertex-ai`. No plaintext key file is created.
The encrypted credential name is `vertex-ai-credentials`; systemd exposes it
only to the service through its credentials directory. The launcher validates
the dedicated identity, scrubs ambient Google credentials, and passes a file
path rather than the private key through the environment.

## Provision and activate

Run the following from committed source on Grace, using the repository's managed
`uv` and Ansible toolchains:

```sh
uv sync --locked
uv run --no-sync scripts/provision_vertex.py
uv run --no-sync scripts/configure_vertex.py preview
uv run --no-sync scripts/runtime_rollout.py prepare
ansible-playbook -i deploy/inventory.yml deploy/deploy.yml
# Run the existing provider acceptance checks against the upgraded service.
uv run --no-sync scripts/runtime_rollout.py finish
ansible-playbook -i deploy/inventory.yml deploy/vertex.yml
```

The runtime and provider activation are separate checkpoints. Runtime preparation
saves only changed managed scripts, dependency manifests, and systemd files,
plus the protected configuration used to detect concurrent changes. It never
copies a Python environment or credentials. If runtime deployment/acceptance
fails, run `uv run --no-sync scripts/runtime_rollout.py rollback` with this
checkout's tested environment; it restores the saved files, syncs the saved
lock, and restarts/checks the previous service. The recovery journal is retained
on failure and deleted on successful finish or recovery.

Vertex activation uses the existing shared `model-refresh` transaction. It
checks exact project/model inference with the dedicated identity, validates
stock Codex catalog parsing, activates the route, and tests streamed tools,
all advertised reasoning levels, images, shared search, and stock Codex resume.
Only then does it publish the picker and `vertex-models.json`. On validation
failure the playbook restores the pre-activation config/catalog and restarts
the previously accepted runtime; Bedrock is preserved.

Recover an interrupted Vertex activation with:

```sh
ansible-playbook -i deploy/inventory.yml deploy/vertex.yml -e refresh_action=rollback
```

After successful publication, reconnect Grace from the owning desktop to load
the picker. This does not change the selected default. Google credit eligibility
and actual credit application are billing-account matters; successful inference
alone does not prove that an invoice was paid by credits. Inspect Cloud Billing
credits and usage reports for account `012E84-0FCE2E-C5D866`.

## Runtime security review: October 27, 2026

The deployment pins LiteLLM 1.106.5. The source and exact Linux wheel were
reviewed before execution. The wheel is
`litellm-1.106.5-cp310-abi3-manylinux_2_28_x86_64.whl`, SHA-256
`9c8ebd30c4a5643207b8e9dade2680c540bbe17ca9543474d12147f6cd2f913f`.
It matches PyPI metadata. All 2,776 packaged Python files match the official
`v1.106.5` source tag byte for byte. No `.pth`, `sitecustomize`, or
`usercustomize` injection was present. The wheel includes
`litellm/rust_bridge/_native.abi3.so`; source comparison of Python files does not
prove reproducibility of that native binary or establish complete package safety.

The October 21 supply-chain incident affected the published 1.107.0 artifacts.
The vendor's canonical account explains why valid release signing alone did not
establish safety. We did not install that version. See:

- https://docs.litellm.ai/blog/security-incident-oct-21-2026-attack-chain
- https://github.com/BerriAI/litellm/releases/tag/v1.106.5
- https://github.com/BerriAI/litellm/compare/v1.106.4...v1.106.5

1.106.5 fixes GHSA-w4c7-34wj-hqm8, disclosed October 27: unauthenticated local
file disclosure through dynamic logging configuration on licensed Enterprise
proxies. Grace has no Enterprise license, SSO/JWT/team administration, or proxy
database configured, and binds to loopback. The exact patch narrows rejected
request logging and makes GCS logging use server-side credentials/configuration.
The upgrade also includes the fix for the salt-key cross-domain escalation that
affects the previously installed 1.102.1 branch.

- https://github.com/BerriAI/litellm/security/advisories/GHSA-w4c7-34wj-hqm8
- https://github.com/BerriAI/litellm/security/advisories/GHSA-7hp6-4w63-5g45

OSV query of all 127 registry packages in the candidate lock found only three
filelock advisories after updating aiohttp to 3.14.5, dnspython to 2.9.0, and
oauthlib to 4.0.0 within supported constraints. LiteLLM requires filelock <4;
we retained its supported filelock 3.32.7 rather than overriding dependencies.
The recorded exceptions are:

- GHSA-567q-5qvw-pf92 and GHSA-9vhv-jxww-xfm5 require Windows lock paths;
  Grace is Linux.
- GHSA-v4x8-3wr5-3wq8 requires filelock's SQLite-backed `ReadWriteLock` and
  an attacker-writable lock directory. No code outside filelock in the installed
  dependency tree invokes `ReadWriteLock`. Hugging Face uses FileLock and, when
  flock is unsupported, SoftFileLock. Neither is the affected SQLite class.

Reevaluate these exceptions if the host OS, dependencies, or locking callers
change. The full regression suite and live provider canary checks supplement
this review; neither static inspection nor an advisory scan is a safety proof.
