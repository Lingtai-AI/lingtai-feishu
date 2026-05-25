"""Tests for Feishu conversation preview guidance.

Mirrors the Telegram conversation-preview improvement: the body of the
on_inbound notification should include a guidance header so the
receiving agent treats older lines as background and replies only to
the latest unresponded incoming message.
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

    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict:
        return {"message_id": "om_typing_x"}

    def delete_message(self, message_id: str) -> bool:
        return True

    def add_reaction(self, message_id: str, emoji: str) -> None:
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
    root: Path, account: str, folder: str, dirname: str, data: dict
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


def test_conversation_preview_includes_guidance_header(tmp_path: Path):
    account = "acct1"
    chat_id = "oc_chat"
    current_id = f"{account}:{chat_id}:om_3"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_id}:om_1",
        "chat_id": chat_id,
        "date": "2026-05-25T05:30:00Z",
        "from_open_id": "ou_jason",
        "text": "If this looks right, we can discuss whether to open a PR later.",
    })
    _write_message(tmp_path, account, "sent", "2", {
        "id": f"{account}:{chat_id}:om_2",
        "chat_id": chat_id,
        "date": "2026-05-25T05:31:00Z",
        "text": "If you explicitly approve, I can open a PR.",
    })
    _write_message(tmp_path, account, "inbox", "3", {
        "id": current_id,
        "chat_id": chat_id,
        "date": "2026-05-25T05:32:00Z",
        "from_open_id": "ou_jason",
        "text": "去写pr吧",
    })

    preview = _manager(tmp_path)._build_conversation_preview(
        account, chat_id, current_id,
    )

    assert preview.startswith(
        "**How to read this Feishu conversation preview (high attention)**"
    )
    assert (
        "The newest unresponded incoming message(s) are the message(s) to handle"
        in preview
    )
    assert "Older lines are background only" in preview
    assert "must not be treated as new approval or a new instruction" in preview
    assert f"**Conversation — last 3 messages (chat {chat_id})**" in preview
    assert "If you explicitly approve, I can open a PR." in preview
    assert f"#{current_id}" in preview
    assert "去写pr吧" in preview
    assert "**[NEW]**" not in preview


def test_conversation_preview_truncation_preserves_guidance_and_latest(tmp_path: Path):
    account = "acct1"
    chat_id = "oc_chat"
    old_text = "old background " * 900
    current_id = f"{account}:{chat_id}:om_2"

    _write_message(tmp_path, account, "inbox", "1", {
        "id": f"{account}:{chat_id}:om_1",
        "chat_id": chat_id,
        "date": "2026-05-25T05:30:00Z",
        "from_open_id": "ou_jason",
        "text": old_text,
    })
    _write_message(tmp_path, account, "inbox", "2", {
        "id": current_id,
        "chat_id": chat_id,
        "date": "2026-05-25T05:31:00Z",
        "from_open_id": "ou_jason",
        "text": "latest message survives truncation",
    })

    preview = _manager(tmp_path)._build_conversation_preview(
        account, chat_id, current_id,
    )

    assert len(preview) <= 10000
    assert preview.startswith(
        "**How to read this Feishu conversation preview (high attention)**"
    )
    assert "latest message survives truncation" in preview


def test_on_inbound_body_uses_conversation_preview(tmp_path: Path):
    """End-to-end: incoming events should propagate the guidance header
    through the on_inbound payload body."""
    acct = FakeAccount()
    inbound: list[dict] = []
    fm = FeishuManager(
        service=FakeService(acct),
        working_dir=tmp_path,
        on_inbound=lambda p: inbound.append(p),
    )

    class _SenderId:
        def __init__(self, open_id: str) -> None:
            self.open_id = open_id

    class _Sender:
        def __init__(self, open_id: str) -> None:
            self.sender_id = _SenderId(open_id)

    class _Message:
        def __init__(self, message_id: str, text: str) -> None:
            self.message_id = message_id
            self.chat_id = "oc_chat"
            self.chat_type = "p2p"
            self.message_type = "text"
            self.content = json.dumps({"text": text})
            self.create_time = "1700000000000"
            self.parent_id = ""

    class _Event:
        def __init__(self, message: _Message, sender: _Sender) -> None:
            self.message = message
            self.sender = sender

    class _Data:
        def __init__(self, event: _Event) -> None:
            self.event = event

    fm.on_incoming(
        acct.alias,
        _Data(_Event(_Message("om_real", "go write the PR"), _Sender("ou_jason"))),
    )

    assert len(inbound) == 1
    body = inbound[0]["body"]
    assert body.startswith(
        "**How to read this Feishu conversation preview (high attention)**"
    )
    assert "go write the PR" in body
