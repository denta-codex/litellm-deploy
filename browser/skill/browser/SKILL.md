---
name: browser
description: "Interact with websites through the server-side browser: inspect rendered pages, click, fill forms, test web apps, or use an explicitly requested saved login profile. Use hosted web search for research and document retrieval."
---

# Browser

Use the `browser` MCP tools for requested website interaction. For research, start with hosted web search. If research requires opening an interactive browser, ask first unless website interaction is already authorized.

Discover browser tools through code mode when needed: filter `ALL_TOOLS` for names starting with `mcp__browser__`. Their descriptions include callable signatures. Do not load unrelated tool catalogs.

The browser runs headlessly on Grace. The user does not need a desktop app or live viewer. Use accessibility snapshots and their current element references for ordinary interaction; use screenshots and coordinate actions for visual interfaces. When using code mode, forward image content with `image()` so you can see it; printing an image’s JSON or file path is not visual inspection.

Ordinary browser actions start an isolated session. It retains tabs and cookies across follow-up turns until closed or idle for 15 minutes. To use a saved login, call `browser_session` with `action: "open", mode: "saved", profile: "name"` only when the user requests that profile. Use `action: "status"` to inspect selection and `action: "close"` to release it. Saved profiles preserve website storage, not open tabs; isolated sessions lose storage when closed. Opening a saved profile does not import personal-browser credentials or log in automatically. A profile in use by another conversation must be released there before it can be used here.

After navigation, a session reset, or profile switching, inspect fresh page state; do not reuse old element references. If an action is interrupted, check its outcome before continuing. Never automatically repeat an uncertain submission, purchase, send, or other mutation. A reset cannot tell you whether the website completed that action.

Keep approvals within the user’s request. Do not obtain account credentials or send messages merely because browser tools are available. If login requires human interaction that these tools cannot complete, explain the specific blocker; manual takeover is not provided.

Screenshots and automatic downloads are temporary session artifacts. Copy user-requested deliverables to an authorized workspace location before closing the session. Do not expose cookies or credentials in chat. Use `browser_session` close when the task no longer needs browser state.
