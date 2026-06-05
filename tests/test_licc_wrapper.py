from __future__ import annotations

import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "lingtai_feishu" / "licc.py"
SPEC = importlib.util.spec_from_file_location("lingtai_feishu_licc_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
licc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(licc)


def test_push_inbox_event_delegates_to_kernel_client(monkeypatch):
    calls = []

    def fake_kernel_push(sender, subject, body, *, metadata=None, wake=True):
        calls.append((sender, subject, body, metadata, wake))
        return True

    monkeypatch.setattr(licc, "_kernel_push_inbox_event", fake_kernel_push)

    assert (
        licc.push_inbox_event(
            "sender",
            "subject",
            "body",
            metadata={"kind": "test"},
            wake=False,
        )
        is True
    )
    assert calls == [("sender", "subject", "body", {"kind": "test"}, False)]


def test_fallback_push_inbox_event_writes_licc_event(monkeypatch, tmp_path):
    monkeypatch.setattr(licc, "_kernel_push_inbox_event", None)
    monkeypatch.setenv("LINGTAI_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("LINGTAI_MCP_NAME", "test-mcp")

    assert (
        licc.push_inbox_event(
            "sender",
            "subject",
            "body",
            metadata={"kind": "fallback"},
            wake=False,
        )
        is True
    )

    files = list((tmp_path / ".mcp_inbox" / "test-mcp").glob("*.json"))
    assert len(files) == 1
    event = json.loads(files[0].read_text(encoding="utf-8"))
    assert event["licc_version"] == 1
    assert event["from"] == "sender"
    assert event["subject"] == "subject"
    assert event["body"] == "body"
    assert event["metadata"] == {"kind": "fallback"}
    assert event["wake"] is False
    assert isinstance(event["received_at"], str)


def test_fallback_push_inbox_event_returns_false_without_host_env(monkeypatch):
    monkeypatch.setattr(licc, "_kernel_push_inbox_event", None)
    monkeypatch.delenv("LINGTAI_AGENT_DIR", raising=False)
    monkeypatch.delenv("LINGTAI_MCP_NAME", raising=False)

    assert licc.push_inbox_event("sender", "subject", "body") is False
