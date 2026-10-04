# Subscription voice on Grace — client contract v1

This document is the handoff and replacement guide; no chat history is required.
Deployment and observed acceptance results are recorded in [VOICE-ACCEPTANCE.md](VOICE-ACCEPTANCE.md).
The target is stock Codex **0.159.2**, LiteLLM **1.102.1**, voice model
`gpt-live-1-codex`, and initial voice `cove`. The adapter supports the single-user
Grace deployment, using its existing private gateway master credential. It does
not implement LiteLLM virtual-key permissions or separate voice spend accounting.

## Ownership and connections

```text
Client ── authenticated app-server control connection ──► stock Codex on Grace
Client ◄──────────────── WebRTC media ────────────────► OpenAI voice service
Codex ── loopback signaling/control ──► LiteLLM subscription adapter ──► OpenAI
Codex ── existing Responses route ────► LiteLLM text models and tools
```

The client owns WebRTC, microphone/speaker, audio buffering, mute, interruption,
reconnection and UI. Grace does not decode, transcribe or synthesize client audio.
Subscription credentials and the gateway key stay on Grace. The client needs
only its existing app-server connection credential, delivered through the
established secure provisioning flow, never copied from this document.

The existing remote endpoint is `wss://grace.taila198f.ts.net/codex/rpc`.
It forwards the stock app-server protocol. The client must additionally reach
the provider's WebRTC endpoints described by the SDP answer; media does not pass
through that Tailscale control endpoint. Do not expose port 4000 or add an audio
bridge on Grace to work around client networking.

## Stock client sequence

1. Open the authenticated control connection. `initialize` with a unique
   `clientInfo` name/version and `capabilities.experimentalApi: true`, then send
   the `initialized` notification. Create a thread or resume a selected thread.
2. Create a WebRTC peer connection with an outgoing audio track and incoming
   playback, plus the `oai-events` data channel. Complete local ICE gathering and
   create an SDP offer. The reference client uses file audio for reproducibility;
   the Dot supplies its microphone instead.
3. Call `thread/realtime/start` with `threadId`, `version: "v3"`,
   `outputModality: "audio"`, `voice: "cove"`, and
   `transport: {type: "webrtc", sdp: "<offer>"}`. Production clients normally
   retain startup context; the acceptance harness disables it for isolated tests.
   A successful RPC reply is only acknowledgement, not proof that voice started.
4. Process `thread/realtime/sdp` and apply its SDP answer. Wait for the peer
   connection and the data channel's `session.started` event before sending the
   user's speech. Receive and play media through WebRTC. Do not send this audio
   using `thread/realtime/appendAudio`.
5. Consume `thread/realtime/started`, transcript notifications, ordinary Codex
   turn/item events, and realtime errors/closure. Codex owns delegation and tools;
   do not independently execute or replay a voice delegation. `appendText` is
   available when explicitly needed, not as a replacement for microphone input.
6. On stop/mute, call `thread/realtime/stop` and close the peer connection. On
   interruption, stop stale playback promptly while continuing capture, and use
   the pinned provider's media/data-channel behavior. Verify actual interruption
   on the client; a transcript notification alone is not an audio-flush signal.
7. Treat control/media failure as the end of that voice session. Discard queued
   audio, restore the authenticated control connection, resume the thread if
   desired, and create a fresh peer/session after user activation. Never replay
   microphone buffers, tool calls, or approval responses after an uncertain loss.

Keep approvals governed by Codex. A client without approval UI should direct the
user to an existing client and leave the request pending. The file-based reference
client uses read-only sandboxing and approval policy `never`, so actions needing
approval fail instead of being silently approved.

## Adapter boundary and lifecycle

`scripts/live_voice.py` registers only these loopback routes in `scripts/serve.py`:

- `POST /v1/live`: authenticated Codex multipart or JSON SDP/session offer;
  subscription JSON call creation; HTTP 201 with SDP and an opaque `rtc_` handle.
- `WebSocket /v1/live/{handle}`: authenticated attachment to that call's upstream
  control connection, relaying protocol messages without changing their semantics.

The backend creation path is
`https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`.
Its control attachment is `wss://api.openai.com/v1/live/{upstream_call_id}`.
Call termination uses authenticated
`POST https://api.openai.com/v1/realtime/calls/{upstream_call_id}/hangup`.
A live probe on October 4, 2026 observed HTTP 200, connection closure, and HTTP 404
on subsequent attachment. Closing a socket alone is not counted as termination.

The registry allows 16 calls, including creation in flight, with 120 seconds to
attach an offer. Initial attachment failures retain the handle for stock Codex's
retries; concurrent duplicate attachments are rejected. Once active, a lost
sideband terminates the call rather than promising transparent reconnection.
Abandoned offers, cancellation after creation, explicit stop, disconnect, and
shutdown all use upstream hangup. Failed cleanup remains tracked for retry while
the service runs; shutdown logs an explicit unconfirmed-cleanup count if the
provider remains unavailable. A process crash cannot guarantee upstream cleanup.

Routine logs contain no credentials, SDP, audio or transcripts. HTTP failures
retain the upstream status and a validated error code with a safe message.
Missing/failed subscription refresh returns an authentication error; the adapter
never starts a device-login flow inside an HTTP request. Subscription storage and
refresh remain owned by the pinned LiteLLM authenticator.

The standalone `wss://api.openai.com/v1/live?model=...` audio route is deliberately
unsupported: it remained denied in our subscription tests. The accepted sideband
attaches to an already-created WebRTC call and is a different route. In a direct
WebRTC probe, changing the requested voice from `marin` to `cove` resolved a
misleading `Voice session access denied` error. Do not infer lack of subscription
voice access from that earlier result.

## Deployment and recovery

Use the repository's existing managed toolchain and encrypted credentials.
`deploy/voice.yml` owns this initial cutover. From committed source:

```sh
ansible-playbook -i deploy/inventory.yml deploy/voice.yml
ansible-playbook -i deploy/inventory.yml deploy/voice.yml -e voice_action=prepare
ansible-playbook -i deploy/inventory.yml deploy/voice.yml -e voice_action=verify
```

Preparation copies only `serve.py` and `live_voice.py`, sets the two root voice
base URLs to `http://127.0.0.1:4000/v1`, and sets `realtime.version = "v3"` and
`realtime.voice = "cove"`. The transaction preserves other Codex settings.
It restarts LiteLLM, not the Codex service that owns active chats. Validate config
pickup with a fresh/resumed thread; if the desktop retains old session settings,
reconnect Grace through the desktop before desktop acceptance. Do not restart an
app-server in the middle of its active work to conceal a failed configuration test.

One private journal, `~/.local/state/litellm/voice-rollout.json`, is the recovery
copy until acceptance. General deployment refuses to run while it is pending.
Rollback restores only the managed files/settings, refuses conflicting edits,
and restarts the gateway:

```sh
ansible-playbook -i deploy/inventory.yml deploy/voice.yml -e voice_action=rollback
```

After the remote reference client, regression checks and real desktop voice test
pass, finish with the recorded receipt and actual desktop confirmation:

```sh
ansible-playbook -i deploy/inventory.yml deploy/voice.yml -e voice_action=finish \
  -e voice_desktop_validated=true -e voice_receipt=/absolute/path/tool-receipt.json
```

Finishing removes recovery staging and writes the accepted commit/receipt digest
to `~/.local/state/litellm/voice-installed.json`. Future ordinary deployments use
the existing module copy/restart mechanism. No separate voice service remains.

## Reference client and acceptance

`scripts/voice_client.py` is a runnable, version-pinned WebRTC reference client.
It connects through the same authenticated remote WSS route as the Dot will use.
Its only nonportable default is the path to Grace's encrypted connection token;
on another host use `--token-env REMOTE_CODEX_TOKEN` with securely provisioned
credentials. It needs neither a LiteLLM key nor a subscription token.

```sh
uv run --script scripts/voice_client.py --input question.wav --output answer.wav \
  --receipt receipt.json --duration 60
```

The receipt contains transcripts, command results, Codex agent messages, and
their observed event order to help investigate tool-to-speech discrepancies;
it and audio outputs are
private artifacts, not routine server logs. Use synthetic speech for automated
tests. `--say 'Read exactly these words aloud: ...'` can generate a spoken fixture.
The real acceptance invocation must use `--input`, with no text prompt supplying
the question. `--require-tool --expect 'unpredictable test value'` requires a real
successful command, matching command output, and a matching spoken transcript.
Verify the command runs before the final spoken result; early acknowledgements
do not count as tool success. No microphone/speaker device test is implied by a
headless audio-file test.

Acceptance matrix: invalid credentials/offers/handles; retry/duplicate attachment;
failed refresh; two isolated concurrent calls; actual audio round trip; unknown
file-content tool result; interrupted playback; stop/control loss; abandoned
offer; gateway restart followed by a fresh call; and existing text/search/tool/
shell credential-isolation checks. Desktop acceptance must be an existing chat
on **Grace**, not a local Mac chat. Record the actual results with the deployed
commit and never turn an unperformed check into a pass.

## Replacement by upstream LiteLLM

The reference is [LiteLLM PR #40366](https://github.com/BerriAI/litellm/pull/40366),
studied at `966b047dd5ff7e67b6afe4156172c1e3d53ba0fa`; our code is not a vendored
copy. The protocol source is OpenAI Codex `rust-v0.159.2`, especially
`codex-api/src/endpoint/realtime_call.rs`, `realtime_websocket/methods.rs`, and
`app-server-protocol/src/protocol/v2/realtime.rs`.

To replace this module, pin a released LiteLLM version in an isolated environment,
configure its subscription signaling and sideband routes, and run the same
contract/lifecycle and real desktop/remote-client acceptance. A merged PR or
successful HTTP handshake is insufficient. Once it passes, remove `live_voice.py`,
its startup registration and deployment copy entry; retire adapter-specific tests
while preserving the reference client and end-to-end checks. Update configuration
only where upstream requires it, deploy with targeted recovery, and record the
replacement release and results here. Stock Codex and the client-facing protocol
remain the boundary; no Dot changes should be required solely to retire the adapter.
