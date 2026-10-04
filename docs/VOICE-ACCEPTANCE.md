# Grace voice acceptance — October 4, 2026

Installed application commit: `e31c9e8812076337de6de77986b067fc4afc5a97`.
Client contract: [VOICE.md](VOICE.md), version 1. Codex 0.159.2 and LiteLLM
1.102.1 remain pinned. The voice module runs inside the existing LiteLLM service
on port 4000; the temporary candidate on port 48769 was stopped.

## Observed results

| Gate | Evidence |
| --- | --- |
| Desktop microphone and playback | User reported that everything worked in Grace chat `01a10846-528a-7f11-aac7-aa862432e7fe`. Its recorded realtime tail contains spoken input, replies, and session end. |
| Desktop interruption and restart | Included in the hardware check requested of the user; the user reported everything worked. These were not independently instrumented. |
| Remote WebRTC and actual tools | Reference client used the existing authenticated remote WSS endpoint with no voice-base override. An audio-only request caused successful `cat /tmp/voice-proof.txt`; a fresh session spoke the actual unpredictable value. Receipt `production-retry.json` passed. |
| Concurrent isolation | Two simultaneous remote clients returned their respective distinct phrases with audible media and successful stop. Both receipts passed. |
| Abandonment, control loss, shutdown | Live probes using the installed module terminated each upstream call. Subsequent upstream sideband attachment returned HTTP 404 in all three cases. |
| Service restart and fresh sessions | The targeted production cutover restarted LiteLLM; subsequent remote and desktop sessions worked. An active call during a systemd restart was not separately tested; module shutdown with an attached call was tested directly. |
| Protocol/security and recovery | 17 focused offline tests passed, covering authorization, malformed/bounded offers, invalid handles, attachment retry/duplicates, capacity, expiry, cancellation, shutdown races, refresh failure, and targeted rollback. Ansible syntax and installed-file/config verification passed. |
| Existing functionality | Live authentication/catalog/streaming/function-result checks passed. Stock Codex native search, shell execution and resume/context checks passed. Running app-server validation confirmed the pinned executable, ChatGPT identity, and absence of the proxy key in shell tools. |

The full offline suite before the final shutdown-race additions passed 157 tests
with one existing optional skip; all 17 voice/rollout tests passed after those
additions.

## Observed limitation

The first production tool test **failed speech fidelity**: the shell successfully
returned the file contents, but the spoken answer incorrectly claimed the file
was missing. That failed receipt is retained as `production-tool.json`. A fresh
session using the same audio input returned and spoke the correct value, matching
the earlier isolated candidate result. This proves the deployed route supports
the full tool flow; it does not establish reliable factual speech on every turn.
The adapter relays control messages unchanged. The cause of the incorrect spoken
summary was not isolated, so do not attribute it conclusively to either Codex or
the voice model, or mark that first test as passing. Client UI should retain the
actual Codex tool result for inspection. Do not automatically replay failed tool
requests to mask this issue.

## Records and recovery

Private receipts, including both production attempts, concurrent-client checks,
and lifecycle results, are in
`/home/agent/.local/state/litellm/voice-acceptance/2026-10-04/`.
The accepted deployment record is
`/home/agent/.local/state/litellm/voice-installed.json`; it records the application
commit and passing receipt digest. The targeted rollback journal is removed by
the finish operation after desktop confirmation. No subscription credential is
part of these receipts. Temporary audio fixtures and candidate staging are
removed after acceptance; no separate experimental listener is retained.

Dot implementation remains separate. A future replacement must repeat the
contract and acceptance gates in VOICE.md and preserve the limitation above
until additional evidence resolves it.
