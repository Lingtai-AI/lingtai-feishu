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
