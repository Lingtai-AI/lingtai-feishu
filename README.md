# lingtai-feishu

LingTai Feishu/Lark MCP server — Open API client with multi-account support and LICC inbox callback.

This is the canonical setup, configuration, and troubleshooting doc for the `lingtai-feishu` MCP. It is fetched by LingTai agents (or anyone else) when they need to install or configure this server.

> **MCP / LICC contract spec:** see the `lingtai-anatomy` skill, `reference/mcp-protocol.md`, for the canonical specification of the catalog → registry → activation chain, environment-variable injection, and the LICC v1 inbox callback protocol. The reference client implementation is `src/lingtai_feishu/licc.py` in this repo (vendored verbatim into all first-party LingTai MCP repos — copy it if you're writing your own).

## Tools

One omnibus MCP tool: `feishu(action=...)`. Actions: `send`, `check`, `read`, `reply`, `search`, `contacts`, `add_contact`, `remove_contact`, `accounts`. Compound message IDs: `account_alias:chat_id:feishu_message_id`.

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

## Troubleshooting

- **`LINGTAI_FEISHU_CONFIG env var not set`** — your `init.json` `mcp.feishu.env` entry is missing the `LINGTAI_FEISHU_CONFIG` key.
- **`Feishu config not found`** — the path resolves but no file exists. Relative paths are resolved against `LINGTAI_AGENT_DIR`.
- **`coroutine 'Client._connect' was never awaited` warning** — usually means invalid `app_id`/`app_secret`. The lark SDK fails the WebSocket handshake; tool calls still work for actions that don't require live connection (like `accounts`, `check` of cached state).
- **Server boots but no inbound messages** — your app needs `im:message` and `im:message:send_as_bot` scopes in the Feishu Open Platform console. After enabling, re-publish the version.
- **MCP server failed to start** — usually the `command` path in `init.json` doesn't have `lingtai_feishu` installed. Confirm with `<command> -m lingtai_feishu --help` from a shell.
- **Tool calls return `Feishu manager not initialized`** — server boot failed (most often a malformed config). Check stderr.

## License

MIT.
