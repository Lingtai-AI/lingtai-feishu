"""LingTai Feishu MCP server.

Exposes a single omnibus ``feishu`` MCP tool that dispatches to
FeishuManager for all 9 actions (send, check, read, reply, search,
contacts, add_contact, remove_contact, accounts). Inbound Feishu events
flow into the host agent's inbox via LICC.

Configuration:
    LINGTAI_FEISHU_CONFIG  — path to a JSON config file (required).

Config schema (plaintext, no env-indirection):

    {
      "accounts": [
        {
          "alias": "myapp",
          "app_id": "cli_xxxxxxxx",
          "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
          "allowed_users": ["ou_xxxxx"]    // optional allow-list of open_ids
        }
      ]
    }

Env vars injected by the LingTai kernel for LICC:
    LINGTAI_AGENT_DIR — host agent's working directory.
    LINGTAI_MCP_NAME  — this MCP's registry name (typically "feishu").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .licc import push_inbox_event
from .manager import FeishuManager, SCHEMA, DESCRIPTION
from .service import FeishuService

log = logging.getLogger("lingtai_feishu")


_SERVER_INSTRUCTIONS = (
    "lingtai-feishu: Feishu/Lark message client. "
    "Configure via the LINGTAI_FEISHU_CONFIG env var pointing at a JSON file. "
    "Inbound messages flow into the host agent's inbox via LICC. "
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-feishu"
)


# ---------------------------------------------------------------------------
# LingTai MCP profile resources
# ---------------------------------------------------------------------------

_PROFILE_MIME = "application/vnd.lingtai.mcp-profile+json"
_MARKDOWN_SKILL_MIME = "text/markdown; profile=lingtai-skill"
_MARKDOWN_MIME = "text/markdown"
_JSON_MIME = "application/json"

_MANIFEST_URI = "lingtai://manifest"
_SKILL_URI = "lingtai://skills/feishu"
_CONFIG_DOC_URI = "lingtai://docs/configuration"
_TROUBLESHOOTING_DOC_URI = "lingtai://docs/troubleshooting"
_STATUS_URI = "lingtai://status"

_RESOURCE_INDEX = [
    {
        "uri": _MANIFEST_URI,
        "name": "LingTai MCP profile manifest",
        "mimeType": _PROFILE_MIME,
        "description": "Machine-readable LingTai profile for this Feishu MCP server.",
    },
    {
        "uri": _SKILL_URI,
        "name": "Feishu pointer skill",
        "mimeType": _MARKDOWN_SKILL_MIME,
        "description": "Thin agent-facing routing hint for Feishu MCP usage.",
    },
    {
        "uri": _CONFIG_DOC_URI,
        "name": "Feishu configuration guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Authoritative config fields, secrets, activation, and security notes.",
    },
    {
        "uri": _TROUBLESHOOTING_DOC_URI,
        "name": "Feishu troubleshooting guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Common setup/runtime failures and diagnostic steps.",
    },
    {
        "uri": _STATUS_URI,
        "name": "Feishu safe status",
        "mimeType": _JSON_MIME,
        "description": "Redacted runtime status derived from config and manager state.",
    },
]


def _package_version() -> str:
    try:
        return version("lingtai-feishu")
    except PackageNotFoundError:  # editable checkout without installation metadata
        return "0+local"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _canonical_resource_uri(uri: object) -> str:
    return str(uri).rstrip("/")


def _redact_app_id(app_id: object) -> str | None:
    """app_id (cli_xxx) is a non-secret identifier; return it as-is."""
    if not app_id:
        return None
    return str(app_id)


def _safe_status_payload(manager: FeishuManager | None) -> dict[str, Any]:
    """Return runtime status without exposing app secrets or raw config."""
    config_path_raw = os.environ.get("LINGTAI_FEISHU_CONFIG")
    config_path = None
    config_readable = False
    accounts: list[dict[str, Any]] = []
    notes: list[str] = []

    if config_path_raw:
        try:
            path = Path(config_path_raw).expanduser()
            if not path.is_absolute():
                base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
                path = base / path
            config_path = str(path)
            if path.is_file():
                config_readable = True
                cfg = json.loads(path.read_text(encoding="utf-8"))
                for account in cfg.get("accounts") or []:
                    allowed_users = account.get("allowed_users")
                    accounts.append({
                        "alias": account.get("alias"),
                        "app_id": _redact_app_id(account.get("app_id")),
                        "has_app_id": bool(account.get("app_id")),
                        "has_app_secret": bool(account.get("app_secret")),
                        "allowed_users_count": (
                            len(allowed_users) if isinstance(allowed_users, list) else None
                        ),
                    })
            else:
                notes.append("Feishu config path is set but the file is not readable.")
        except Exception as exc:  # status must never leak raw config or fail hard
            notes.append(f"Could not read Feishu config safely: {type(exc).__name__}: {exc}")
    else:
        notes.append("LINGTAI_FEISHU_CONFIG is not set.")

    service_started = False
    if manager is not None:
        try:
            service = getattr(manager, "_service", None)
            service_started = bool(getattr(service, "_running", False))
        except Exception:
            service_started = False

    status = "ok" if manager is not None else "degraded"
    return {
        "status": status,
        "manager_initialized": manager is not None,
        "service_started": service_started,
        "config_path_set": bool(config_path_raw),
        "config_path": config_path,
        "config_readable": config_readable,
        "accounts_count": len(accounts),
        "accounts": accounts,
        "notes": notes,
    }


def _profile_manifest(manager: FeishuManager | None) -> dict[str, Any]:
    return {
        "schema": "lingtai.mcp.profile.v1",
        "server": {
            "name": "lingtai-feishu",
            "registry_name": "feishu",
            "version": _package_version(),
            "summary": "Feishu/Lark Open API client with LICC inbox callback.",
            "homepage": "https://github.com/Lingtai-AI/lingtai-feishu",
        },
        "ownership": {
            "configuration": "This MCP owns Feishu config fields, Open API caveats, and diagnostics.",
            "human_ui": "LingTai TUI /mcp is the human-facing control panel and should render these resources generically.",
            "agent_interface": "Agents should use MCP tools/resources/prompts directly; LingTai skills are thin discovery pointers.",
        },
        "resources": _RESOURCE_INDEX,
        "tools": [
            {
                "name": "feishu",
                "description": "Omnibus Feishu tool for send/check/read/reply/search/delete/edit/contacts/accounts.",
                "actions": [
                    "send", "check", "read", "reply", "search", "delete", "edit",
                    "contacts", "add_contact", "remove_contact", "accounts",
                ],
            }
        ],
        "agent_entrypoints": {
            "skill": _SKILL_URI,
            "configuration": _CONFIG_DOC_URI,
            "troubleshooting": _TROUBLESHOOTING_DOC_URI,
            "status": _STATUS_URI,
        },
        "status": _safe_status_payload(manager),
    }


def _skill_markdown() -> str:
    return """---
name: feishu
summary: Thin routing hint for the lingtai-feishu MCP server.
---

# Feishu MCP pointer skill

This MCP is the authoritative source for Feishu/Lark Open API setup and runtime
behavior. Do not copy platform details into a LingTai skill. Instead:

1. Read `lingtai://manifest` to discover this server's LingTai profile.
2. Read `lingtai://docs/configuration` for config fields, secrets, and activation.
3. Read `lingtai://docs/troubleshooting` for setup/runtime failures.
4. Read `lingtai://status` for safe, redacted runtime status.
5. Use the `feishu` MCP tool for agent-facing operations.

Human-facing setup should be rendered by LingTai's `/mcp` control panel from
these resources; agents use MCP tools/resources/prompts directly.
"""


def _configuration_markdown() -> str:
    return """# lingtai-feishu configuration

`lingtai-feishu` is a Feishu/Lark Open API MCP server. It is configured via a
JSON file whose path is supplied in `LINGTAI_FEISHU_CONFIG`.

## Environment

- `LINGTAI_FEISHU_CONFIG` — path to the JSON config file. Relative paths are
  resolved against `LINGTAI_AGENT_DIR` when present.
- `LINGTAI_AGENT_DIR` — injected by LingTai; used for state, contacts, and LICC.
- `LINGTAI_MCP_NAME` — injected by LingTai; usually `feishu`.

## Config schema

```json
{
  "accounts": [
    {
      "alias": "myapp",
      "app_id": "cli_xxxxxxxx",
      "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
      "allowed_users": ["ou_xxxxx"]
    }
  ]
}
```

Required fields:

- `accounts` — non-empty list.
- `accounts[].app_id` — Feishu app ID (`cli_...`). Not secret, but pairs with the
  secret below.
- `accounts[].app_secret` — Feishu app secret. Keep it secret; do not print it in
  logs, chat, issues, or PRs.

Common optional fields:

- `alias` — account alias used by compound message IDs and the `account` tool
  argument. Defaults are handled by the manager if omitted.
- `allowed_users` — list of Feishu `open_id`s (`ou_...`) allowed to contact the
  app.

## Tool entrypoint

Use the `feishu` tool with actions: `send`, `check`, `read`, `reply`, `search`,
`delete`, `edit`, `contacts`, `add_contact`, `remove_contact`, and `accounts`.
Compound message IDs have the form `account_alias:chat_id:feishu_message_id`.

Voice messages received from Feishu are downloaded and transcribed locally with
faster-whisper when the `voice` extra is installed. For long-running responses,
`send` accepts `placeholder=true` to post an immediate placeholder that `edit`
can later replace.
"""


def _troubleshooting_markdown() -> str:
    return """# lingtai-feishu troubleshooting

## `LINGTAI_FEISHU_CONFIG env var not set`

Set `LINGTAI_FEISHU_CONFIG` to the config JSON path. Relative paths resolve
against `LINGTAI_AGENT_DIR`.

## `Feishu config not found`

Check the path in `LINGTAI_FEISHU_CONFIG`, file permissions, and whether the
agent was refreshed after config changes.

## `config must contain 'accounts' (list)`

The JSON must contain a non-empty `accounts` list.

## Invalid app credentials

Verify the `app_id` (`cli_...`) and `app_secret` in the Feishu Developer
console. Never paste the full `app_secret` into chat, logs, issues, or PRs.
Rotate the secret if it was exposed.

## Bot cannot message the human

Confirm the app has the required messaging scopes and event subscriptions, the
human is reachable via the configured `open_id`, and (if used) `allowed_users`
includes the sender's `open_id`. Long-running connections use a WebSocket; a
restart or `refresh` may be needed after scope changes.

## No inbound messages arrive

Check that the MCP process is active, the app's event subscription is enabled,
the WebSocket connection is healthy, and `allowed_users` includes the sender's
`open_id`. Read `lingtai://status` for redacted config/runtime state.

## Voice messages are not transcribed

Install the optional `voice` extra (`pip install lingtai-feishu[voice]`) to
enable local faster-whisper transcription.

## Agent-facing vs human-facing interface

`/mcp` is the human-facing TUI control panel. Agents should use this MCP's
resources and `feishu` tool directly.
"""


def _resource_payloads(manager: FeishuManager | None) -> dict[str, tuple[str, str]]:
    return {
        _MANIFEST_URI: (_PROFILE_MIME, _json_dumps(_profile_manifest(manager))),
        _SKILL_URI: (_MARKDOWN_SKILL_MIME, _skill_markdown()),
        _CONFIG_DOC_URI: (_MARKDOWN_MIME, _configuration_markdown()),
        _TROUBLESHOOTING_DOC_URI: (_MARKDOWN_MIME, _troubleshooting_markdown()),
        _STATUS_URI: (_JSON_MIME, _json_dumps(_safe_status_payload(manager))),
    }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read config from the path in LINGTAI_FEISHU_CONFIG.

    Path is resolved relative to LINGTAI_AGENT_DIR (or cwd as fallback)
    if not absolute. Plaintext only — no *_env indirection.
    """
    config_path_raw = os.environ.get("LINGTAI_FEISHU_CONFIG")
    if not config_path_raw:
        raise ValueError(
            "LINGTAI_FEISHU_CONFIG env var not set — point it at your "
            "Feishu config JSON file"
        )
    config_path = Path(config_path_raw).expanduser()
    if not config_path.is_absolute():
        base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
        config_path = base / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"Feishu config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def _accounts_from_config(cfg: dict) -> list[dict]:
    accounts = cfg.get("accounts")
    if not accounts:
        raise ValueError("config must contain 'accounts' (list)")
    return list(accounts)


# ---------------------------------------------------------------------------
# Manager construction
# ---------------------------------------------------------------------------

def build_manager() -> tuple[FeishuManager, Path]:
    """Construct manager + service from env + config."""
    cfg = load_config()
    accounts = _accounts_from_config(cfg)

    agent_dir_raw = os.environ.get("LINGTAI_AGENT_DIR")
    working_dir = Path(agent_dir_raw) if agent_dir_raw else Path.cwd()
    working_dir.mkdir(parents=True, exist_ok=True)

    def _on_inbound(event: dict) -> None:
        push_inbox_event(
            sender=event["from"],
            subject=event["subject"],
            body=event["body"],
            metadata=event.get("metadata"),
            wake=event.get("wake", True),
        )

    mgr_ref: list[FeishuManager | None] = [None]

    svc = FeishuService(
        working_dir=working_dir,
        accounts_config=accounts,
        on_message=lambda alias, ctx: mgr_ref[0].on_incoming(alias, ctx),
    )

    mgr = FeishuManager(
        service=svc,
        working_dir=working_dir,
        on_inbound=_on_inbound,
    )
    mgr_ref[0] = mgr
    return mgr, working_dir


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def build_server(manager: FeishuManager | None) -> Server:
    server: Server = Server("lingtai-feishu", instructions=_SERVER_INSTRUCTIONS)

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=item["uri"],
                name=item["name"],
                description=item["description"],
                mimeType=item["mimeType"],
            )
            for item in _RESOURCE_INDEX
        ]

    @server.read_resource()
    async def _read_resource(uri: object) -> str:
        resource_uri = _canonical_resource_uri(uri)
        try:
            _mime, text = _resource_payloads(manager)[resource_uri]
        except KeyError as exc:
            raise ValueError(f"unknown resource: {resource_uri}") from exc
        return text

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="feishu",
                description=DESCRIPTION,
                inputSchema=SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "feishu":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            result = {
                "status": "error",
                "error": (
                    "Feishu manager not initialized — server boot failed. "
                    "Check stderr for the underlying exception (most often "
                    "missing LINGTAI_FEISHU_CONFIG or invalid app credentials)."
                ),
            }
        else:
            try:
                result = await asyncio.to_thread(manager.handle, arguments)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
        return [types.TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False),
        )]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Run the MCP server over stdio. Eagerly starts the WebSocket clients
    so inbound messages flow before the host expects them."""
    manager: FeishuManager | None = None
    service_started = False
    try:
        manager, _wd = build_manager()
        manager._service.start()
        service_started = True
        log.info("Feishu listener running")
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None

    server = build_server(manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None and service_started:
            try:
                manager._service.stop()
            except Exception:
                pass
