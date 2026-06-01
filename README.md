# lingtai-feishu

LingTai Feishu/Lark MCP server — Open API client with multi-account support and LICC inbox callback.

This is the canonical setup, configuration, and troubleshooting doc for the `lingtai-feishu` MCP. It is fetched by LingTai agents (or anyone else) when they need to install or configure this server.

> **MCP / LICC contract spec:** see the `lingtai-anatomy` skill, `reference/mcp-protocol.md`, for the canonical specification of the catalog → registry → activation chain, environment-variable injection, and the LICC v1 inbox callback protocol. The reference client implementation is `src/lingtai_feishu/licc.py` in this repo (vendored verbatim into all first-party LingTai MCP repos — copy it if you're writing your own).

## LingTai profile resources

`lingtai-feishu` is self-describing for LingTai-aware clients. In addition to
the `feishu` tool, the MCP server publishes ordinary MCP resources:

| Resource URI | Purpose |
|---|---|
| `lingtai://manifest` | Machine-readable LingTai MCP profile: server metadata, ownership boundaries, resource index, agent entrypoints, and safe status. |
| `lingtai://skills/feishu` | Thin agent-facing pointer skill. It routes agents to this MCP's own resources instead of copying Feishu details into LingTai skills. |
| `lingtai://docs/configuration` | Authoritative config schema, environment variables, security notes, and tool entrypoint summary. |
| `lingtai://docs/troubleshooting` | Common Feishu setup/runtime failures and diagnostic steps. |
| `lingtai://status` | Redacted runtime status derived from config and manager state. App secrets are never returned. |
| `lingtai://onboarding/feishu` | Agent-facing browser/HTML onboarding recipe: how to obtain/enter Feishu `app_id`/`app_secret` from the Developer Console, generate and open a local setup-checklist page, verify via `lingtai://status`, and redact secrets. Feishu has no QR/scan login. |
| `lingtai://onboarding/html-template` | Self-contained, secret-free static HTML setup-checklist page with a `{{SETUP}}` placeholder. An agent fills in non-secret setup context, writes it to a local file, and opens it in a browser. No scripts, no external assets, no credentials. |

Onboarding is MCP-owned: an agent reads `lingtai://onboarding/feishu`, and (if a
human wants a browser checklist to follow) reads
`lingtai://onboarding/html-template`, fills the `{{SETUP}}` placeholder with
non-secret setup context, writes the page to disk, and opens it. The page is a
*checklist* — credentials are still entered into the config JSON, never into the
generated HTML.

Boundary: `/mcp` is LingTai's human-facing TUI control panel and should render
these resources generically. Agents should use MCP tools/resources/prompts
directly; LingTai skills should remain thin discovery pointers.

## Tools

One omnibus MCP tool: `feishu(action=...)`. Actions: `send`, `check`, `read`, `reply`, `search`, `delete`, `edit`, `contacts`, `add_contact`, `remove_contact`, `accounts`. Compound message IDs: `account_alias:chat_id:feishu_message_id`.

### Voice Messages

Audio/voice messages received from Feishu are automatically:
1. Downloaded via the Feishu file resource API
2. Transcribed locally using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (no external API key needed)
3. Delivered as text in the LICC inbox event

Voice transcription is part of the default install; `faster-whisper` is a required dependency rather than an optional runtime install. The transcript replaces the empty text in the payload, and metadata includes `is_voice_transcript: true`, `voice_duration`, and `voice_transcript` (with language, segments, etc.).

### Rich Feedback

- **Seen reaction**: When a message is received, the bot adds an "OK" emoji reaction (✅) to acknowledge receipt.
- **Done reaction**: After sending a reply, the bot adds a "THUMBSUP" emoji reaction (👍) to the original message.
- **Typing indicator**: A temporary "⏳ ..." message is sent immediately on message receipt and automatically deleted when the agent's response is sent.
- **Placeholder mode**: Send a placeholder message immediately and edit it later with the final content. Use `feishu(action="send", placeholder=true, ...)` to get back a compound message_id, then `feishu(action="edit", message_id=..., text=<final>)` to update it.

## Public MCP identity metadata

On successful startup, `lingtai-feishu` writes a non-secret identity document to:

```text
<agent_dir>/system/mcp_identities/feishu.json
```

This file maps local account aliases to non-secret Feishu app identity fields such as `app_id`, `last_verified_at`, `allowed_users_count`, and `contact_count`. It never contains `app_secret`, message contents, individual allowed-user/open IDs, chat IDs, webhook secrets, or encryption keys. Agents can use it to answer questions like “which local LingTai agent owns this Feishu app?” without inspecting `.secrets/feishu.json` or mining logs.

`feishu(action="accounts")` also returns the same non-secret `details` list plus `identity_path` for live inspection.

## Inbound messages (LICC)

Inbound Feishu events flow into the host agent's inbox via the LingTai Inbox Callback Contract. Each new message is delivered as a LICC event with:

- `from` — contact's display name (or open_id if no contact saved).
- `subject` — `"feishu message from <name> via <account_alias>"`.
- `body` — a ~300 char preview of the message text.
- `metadata.message_id` — compound ID for `reply`.
- `metadata.account` — which configured app received it.
- `metadata.chat_id`, `metadata.chat_type`, `metadata.from_open_id` — routing keys.

## Install

```bash
# Into the LingTai agent's venv (typically ~/.lingtai-tui/runtime/venv/)
pip install git+https://github.com/Lingtai-AI/lingtai-feishu.git
```

After install, `python -m lingtai_feishu` (or the `lingtai-feishu` script) starts the MCP server over stdio.

`lark-oapi` (Feishu's official Python SDK) is a hard dependency.

## Configure

The server reads its app config from a JSON file pointed at by `LINGTAI_FEISHU_CONFIG`. Recommended path: `.secrets/feishu.json` inside the agent's working directory. Plaintext only — this MCP does not support `*_env` indirection.

### Config schema

```json
{
  "accounts": [
    {
      "alias": "myapp",
      "app_id": "cli_xxxxxxxxxxxx",
      "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "allowed_users": ["ou_xxxxxxxxxxxxxxxxxxxxxxxx"]
    }
  ]
}
```

- `alias` — human-friendly name; used as the `account` parameter in tool calls.
- `app_id` / `app_secret` — issued by [Feishu Open Platform](https://open.feishu.cn/app) when you create a custom app.
- `allowed_users` — optional allow-list of user `open_id`s. When set, messages from other senders are silently ignored. Omit to accept any sender within your app's scope.

### Activation in LingTai

```json
{
  "addons": ["feishu"],
  "mcp": {
    "feishu": {
      "type": "stdio",
      "command": "/path/to/your/python",
      "args": ["-m", "lingtai_feishu"],
      "env": {
        "LINGTAI_FEISHU_CONFIG": ".secrets/feishu.json"
      }
    }
  }
}
```

Then run `system(action="refresh")` from the agent. The MCP subprocess starts, the per-account Feishu WebSocket clients connect, and the omnibus `feishu` tool becomes available.

## Cleanup / Footprint

This addon persists state under the agent's working directory. It does **not** delete its own data automatically. Cleanup is an explicit, human-approved operation.

### What this addon leaves behind

Everything lives under `<agent_dir>/feishu/`:

- `feishu/{account}/inbox/{uuid}/message.json` — every inbound LICC message.
- `feishu/{account}/inbox/{uuid}/attachments/` — downloaded audio/voice/other resources (can grow large).
- `feishu/{account}/sent/{uuid}/message.json` — every outbound reply / send, including placeholders.
- `feishu/{account}/contacts.json` — saved open_id → display-name map.
- `feishu/{account}/read.json` — list of compound message IDs already marked read.
- `feishu/{account}/state.json` — per-account runtime state (cursors, etc.).

### What must never be deleted blindly

- `contacts.json`, `read.json`, `state.json` — losing these breaks read-tracking, contact aliases, and resume behavior. Treat as durable.
- `sent/` — these are records of what the agent itself sent. Deleting them loses outbound history.
- Any `message.json` referenced by an unresolved thread the agent is still working on.

Attachments under `inbox/*/attachments/` are the usual growth driver and the safest cleanup target — but still only after a dry-run report and explicit user consent.

### Read-only footprint check

Run the audit script. It is pure stdlib, never deletes anything, and appends one JSONL record to `<agent_dir>/logs/cleanup.jsonl` per run:

```bash
python3 scripts/footprint_audit.py <agent_dir>
python3 scripts/footprint_audit.py <agent_dir> --json
python3 scripts/footprint_audit.py <agent_dir> --max-paths 50
```

The report counts accounts, inbox/sent `message.json` files, attachment files, voice/audio candidates (`.ogg .opus .mp3 .wav .m4a .oga .webm .aac .flac`), total attachment bytes, and lists the largest attachment paths.

### Cleanup protocol

1. Run `footprint_audit.py` to produce a dry-run report.
2. Show the report to the user.
3. Get **explicit** consent before any destructive deletion. Default to keeping data.
4. After deletion (done by a separate, human-driven tool — not this addon), append a follow-up record to `<agent_dir>/logs/cleanup.jsonl` with `human_approved: true` and what was actually removed.

### Audit cadence

- Run the audit when an agent has been running for a long time, when the working directory feels large, when troubleshooting disk usage, or roughly once a week for active agents.
- Agents reading this README for setup, troubleshooting, or long-running operation should consider running the footprint audit themselves and **ask the user before any cleanup** if the footprint is large, stale, or contains privacy-sensitive content (voice/audio under `attachments/`, contact lists).

## Troubleshooting

- **`LINGTAI_FEISHU_CONFIG env var not set`** — your `init.json` `mcp.feishu.env` entry is missing the `LINGTAI_FEISHU_CONFIG` key.
- **`Feishu config not found`** — the path resolves but no file exists. Relative paths are resolved against `LINGTAI_AGENT_DIR`.
- **`coroutine 'Client._connect' was never awaited` warning** — usually means invalid `app_id`/`app_secret`. The lark SDK fails the WebSocket handshake; tool calls still work for actions that don't require live connection (like `accounts`, `check` of cached state).
- **Server boots but no inbound messages** — your app needs `im:message` and `im:message:send_as_bot` scopes in the Feishu Open Platform console. After enabling, re-publish the version.
- **Voice messages arrive but aren't transcribed** — `faster-whisper` is installed with `lingtai-feishu`; if it is missing, reinstall/upgrade `lingtai-feishu` in the runtime venv. Also check that your app has the `im:resource` scope for downloading message resources.
- **Reactions fail silently** — your app needs `im:message.reactions:write` scope. Check logs for "Failed to add" warnings.
- **Edit/delete actions return errors** — your app needs `im:message:patch` (edit) and/or `im:message:delete` scopes respectively.
- **MCP server failed to start** — usually the `command` path in `init.json` doesn't have `lingtai_feishu` installed. Confirm with `<command> -m lingtai_feishu --help` from a shell.
- **Tool calls return `Feishu manager not initialized`** — server boot failed (most often a malformed config). Check stderr.

## License

Apache-2.0.
