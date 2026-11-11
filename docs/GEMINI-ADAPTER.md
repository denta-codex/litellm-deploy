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

Replay uses the same key after a restart; no tool cache or conversation database
is required. Corruption, cross-caller replay, missing batch items, or edited
model-visible content fail before inference. Model switching retains public
history but does not send foreign opaque state. Older chats without carriers
remain readable; already-lost signatures cannot be recovered.

Compaction deliberately establishes a new readable checkpoint. Retained original
assistant batches keep their own carriers. Removed batches and their signatures
must not be transplanted onto rewritten checkpoint text. Subsequent Gemini output
produces fresh native state.

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

Use the existing locked environment, with `scripts` on `PYTHONPATH`:

```sh
uv run --no-sync -m unittest discover -s scripts -p 'test_*.py'
```

`test_gemini_native.py` exercises the actual pinned Router/Vertex/Responses bridge
with synthetic credentials and mocked HTTP. It covers JSON/streaming fidelity,
ordinary/custom and parallel/sequential tools, cache flushes, key reuse, content
binding, and caller isolation. The retained research probe describes the original
unadapted bridge; it is not a runtime acceptance test.

Live deployment, real-provider acceptance, and model-quality comparisons require
separate approval. Protocol fidelity and early feedback do not prove that the
provider's computation becomes faster or that Gemini matches another model.
