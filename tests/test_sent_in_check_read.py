"""Post-molt visibility: sent messages must appear in _check and _read.

Without this, an agent that resumes after a molt sees only inbound
messages and may re-send a reply it already delivered.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lingtai_feishu import manager as manager_mod  # noqa: E402
from lingtai_feishu.manager import (  # noqa: E402
    FeishuManager,
    TypingIndicatorManager,
)


class FakeAccount:
    def __init__(self, alias: str = "acct1") -> None:
        self.alias = alias

    def send_text(self, *_a, **_k) -> dict:
        return {"message_id": "om_x"}

    def delete_message(self, *_a, **_k) -> bool:
        return True

    def add_reaction(self, *_a, **_k) -> None:
        pass


class FakeService:
    def __init__(self, account: FakeAccount) -> None:
        self._account = account
        self.default_account = account

    def get_account(self, alias: str) -> FakeAccount:
        if alias != self._account.alias:
            raise KeyError(alias)
        return self._account

    def list_accounts(self) -> list[str]:
        return [self._account.alias]


@pytest.fixture(autouse=True)
def _reset_typing_manager():
    manager_mod._typing_manager = TypingIndicatorManager()
    yield
    manager_mod._typing_manager = TypingIndicatorManager()


def _write_message(
    root: Path, account: str, folder: str, dirname: str, data: dict,
) -> None:
    path = root / "feishu" / account / folder / dirname / "message.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _manager(root: Path) -> FeishuManager:
    return FeishuManager(
        service=FakeService(FakeAccount()),
        working_dir=root,
        on_inbound=lambda event: None,
    )


def test_check_counts_sent_messages_in_conversation_total(tmp_path: Path):
    account = "acct1"
    chat_id = "oc_chat"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_id}:om_1",
        "chat_id": chat_id,
        "chat_type": "p2p",
        "from_open_id": "ou_jason",
        "text": "hello",
        "date": "2026-05-25T05:30:00Z",
    })
    _write_message(tmp_path, account, "sent", "2", {
        "id": f"{account}:{chat_id}:om_2",
        "chat_id": chat_id,
        "to": {"receive_id": "ou_jason", "receive_id_type": "open_id"},
        "text": "hi back",
        "sent_at": "2026-05-25T05:31:00Z",
        "date": "2026-05-25T05:31:00Z",
        "status": "sent",
    })

    fm = _manager(tmp_path)
    result = fm._check({"account": account})

    assert result["status"] == "ok"
    assert result["total"] == 2
    convs = {c["chat_id"]: c for c in result["conversations"]}
    assert chat_id in convs
    assert convs[chat_id]["total"] == 2


def test_read_returns_sent_messages_with_direction(tmp_path: Path):
    account = "acct1"
    chat_id = "oc_chat"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_id}:om_1",
        "chat_id": chat_id,
        "chat_type": "p2p",
        "from_open_id": "ou_jason",
        "text": "please write the PR",
        "date": "2026-05-25T05:30:00Z",
    })
    _write_message(tmp_path, account, "sent", "2", {
        "id": f"{account}:{chat_id}:om_2",
        "chat_id": chat_id,
        "to": {"receive_id": "ou_jason", "receive_id_type": "open_id"},
        "text": "on it",
        "sent_at": "2026-05-25T05:31:00Z",
        "date": "2026-05-25T05:31:00Z",
        "status": "sent",
    })

    fm = _manager(tmp_path)
    result = fm._read({"account": account, "chat_id": chat_id, "limit": 10})

    assert result["status"] == "ok"
    msgs = result["messages"]
    assert len(msgs) == 2

    by_id = {m["id"]: m for m in msgs}
    incoming_id = f"{account}:{chat_id}:om_1"
    outgoing_id = f"{account}:{chat_id}:om_2"
    assert by_id[incoming_id]["_direction"] == "incoming"
    assert by_id[outgoing_id]["_direction"] == "outgoing"
    assert by_id[outgoing_id]["text"] == "on it"


def test_read_filters_sent_messages_by_chat_id(tmp_path: Path):
    """Sent records for a different chat must not leak in."""
    account = "acct1"
    chat_a = "oc_a"
    chat_b = "oc_b"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_a}:om_1",
        "chat_id": chat_a,
        "chat_type": "p2p",
        "from_open_id": "ou_jason",
        "text": "in A",
        "date": "2026-05-25T05:30:00Z",
    })
    _write_message(tmp_path, account, "sent", "2", {
        "id": f"{account}:{chat_b}:om_2",
        "chat_id": chat_b,
        "to": {"receive_id": "ou_other", "receive_id_type": "open_id"},
        "text": "out in B",
        "sent_at": "2026-05-25T05:31:00Z",
        "date": "2026-05-25T05:31:00Z",
        "status": "sent",
    })

    fm = _manager(tmp_path)
    result = fm._read({"account": account, "chat_id": chat_a, "limit": 10})
    assert [m["chat_id"] for m in result["messages"]] == [chat_a]


def test_legacy_sent_at_only_records_are_sorted_and_marked_outgoing(tmp_path: Path):
    """Older sent records may have `sent_at` but no `date`/`to` fields."""
    account = "acct1"
    chat_id = "oc_chat"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_id}:om_1",
        "chat_id": chat_id,
        "chat_type": "p2p",
        "from_open_id": "ou_jason",
        "text": "incoming first",
        "date": "2026-05-25T05:30:00Z",
    })
    _write_message(tmp_path, account, "sent", "2", {
        "id": f"{account}:{chat_id}:om_2",
        "chat_id": chat_id,
        "text": "legacy outgoing latest",
        "sent_at": "2026-05-25T05:31:00Z",
        "status": "sent",
    })

    fm = _manager(tmp_path)
    result = fm._read({"account": account, "chat_id": chat_id, "limit": 10})

    assert [m["text"] for m in result["messages"]] == [
        "legacy outgoing latest",
        "incoming first",
    ]
    assert result["messages"][0]["_direction"] == "outgoing"
