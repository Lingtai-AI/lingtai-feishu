"""Regression tests for issue #3.

`_send` left typing indicators orphaned when a p2p (open_id) send raised,
because `chat_id` was only resolved from the send result. These tests cover
the cleanup contract for `_send` on both failure and success paths, plus
the underlying `TypingIndicatorManager` fallback behavior.
"""
from __future__ import annotations

import logging
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
    """Minimal stand-in for FeishuAccount used by typing cleanup."""

    def __init__(
        self,
        alias: str = "acct1",
        *,
        send_result: dict | None = None,
        send_raises: Exception | None = None,
        reply_result: dict | None = None,
    ) -> None:
        self.alias = alias
        self._send_result = send_result or {}
        self._send_raises = send_raises
        self._reply_result = reply_result or {}
        self.sent_texts: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self.deleted_messages: list[str] = []

    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict:
        self.sent_texts.append((receive_id, receive_id_type, text))
        if self._send_raises is not None:
            raise self._send_raises
        return self._send_result

    def delete_message(self, message_id: str) -> bool:
        self.deleted_messages.append(message_id)
        return True

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append((message_id, text))
        return self._reply_result

    def add_reaction(self, message_id: str, reaction: str) -> bool:
        self.reactions.append((message_id, reaction))
        return True


class FakeService:
    """Minimal stand-in for FeishuService."""

    def __init__(self, account: FakeAccount) -> None:
        self._account = account
        self.default_account = account

    def get_account(self, alias: str) -> FakeAccount:
        if alias != self._account.alias:
            raise KeyError(alias)
        return self._account

    def list_accounts(self) -> list[str]:
        return [self._account.alias]


def _make_manager(tmp_path: Path, account: FakeAccount) -> FeishuManager:
    return FeishuManager(
        service=FakeService(account),
        working_dir=tmp_path,
        on_inbound=lambda payload: None,
    )


@pytest.fixture(autouse=True)
def _reset_typing_manager():
    """The module-level typing manager is shared global state."""
    manager_mod._typing_manager = TypingIndicatorManager()
    yield
    manager_mod._typing_manager = TypingIndicatorManager()


# --------------------------------------------------------------------
# TypingIndicatorManager unit tests for the new fallback
# --------------------------------------------------------------------


def test_stop_typing_by_receive_removes_matching_entry():
    tm = TypingIndicatorManager()
    acct = FakeAccount(send_result={"message_id": "om_typing_1"})
    chat_id = "oc_real_chat"
    open_id = "ou_user"

    msg_id = tm.start_typing(acct, chat_id, open_id, "open_id")
    assert msg_id == "om_typing_1"

    tm.stop_typing_by_receive(acct, open_id, "open_id")

    assert "om_typing_1" in acct.deleted_messages
    # Entry was popped from active table — a second call is a no-op.
    acct.deleted_messages.clear()
    tm.stop_typing_by_receive(acct, open_id, "open_id")
    assert acct.deleted_messages == []


def test_stop_typing_by_receive_leaves_unrelated_entries():
    tm = TypingIndicatorManager()
    acct = FakeAccount()

    # Two active typing indicators on the same account, different chats.
    tm._active_chats[("acct1", "oc_chat_A")] = {
        "message_id": "om_A",
        "receive_id": "ou_alice",
        "receive_id_type": "open_id",
    }
    tm._active_chats[("acct1", "oc_chat_B")] = {
        "message_id": "om_B",
        "receive_id": "ou_bob",
        "receive_id_type": "open_id",
    }

    tm.stop_typing_by_receive(acct, "ou_alice", "open_id")

    assert acct.deleted_messages == ["om_A"]
    assert ("acct1", "oc_chat_A") not in tm._active_chats
    assert ("acct1", "oc_chat_B") in tm._active_chats


def test_stop_typing_by_receive_is_best_effort_when_delete_fails():
    tm = TypingIndicatorManager()

    class BoomAccount(FakeAccount):
        def delete_message(self, message_id: str) -> bool:
            raise RuntimeError("network down")

    acct = BoomAccount()
    tm._active_chats[("acct1", "oc_chat_A")] = {
        "message_id": "om_A",
        "receive_id": "ou_alice",
        "receive_id_type": "open_id",
    }

    # Must not raise.
    tm.stop_typing_by_receive(acct, "ou_alice", "open_id")
    # Entry still popped despite delete failure.
    assert ("acct1", "oc_chat_A") not in tm._active_chats


def test_stop_typing_returns_true_when_entry_was_deleted():
    tm = TypingIndicatorManager()
    acct = FakeAccount()
    tm._active_chats[("acct1", "oc_chat_A")] = {
        "message_id": "om_A",
        "receive_id": "ou_alice",
        "receive_id_type": "open_id",
    }

    found = tm.stop_typing(acct, "oc_chat_A")

    assert found is True
    assert acct.deleted_messages == ["om_A"]
    assert ("acct1", "oc_chat_A") not in tm._active_chats


def test_stop_typing_returns_false_when_no_entry_exists():
    tm = TypingIndicatorManager()
    acct = FakeAccount()

    found = tm.stop_typing(acct, "oc_missing")

    assert found is False
    assert acct.deleted_messages == []


# --------------------------------------------------------------------
# _send cleanup behavior — the issue #3 regression
# --------------------------------------------------------------------


def test_send_failure_with_open_id_cleans_orphan_typing(tmp_path: Path):
    """The bug: send_text raises for an open_id recipient, and the typing
    indicator started under the real chat_id is left behind."""
    boom = RuntimeError("Feishu send_text failed")
    acct = FakeAccount(send_raises=boom)
    fm = _make_manager(tmp_path, acct)

    # Simulate the typing indicator started by on_incoming, keyed by the
    # real chat_id (which _send doesn't know up front).
    open_id = "ou_user"
    real_chat_id = "oc_real_chat"
    manager_mod._typing_manager._active_chats[(acct.alias, real_chat_id)] = {
        "message_id": "om_typing_orphan",
        "receive_id": open_id,
        "receive_id_type": "open_id",
    }

    result = fm.handle({
        "action": "send",
        "receive_id": open_id,
        "receive_id_type": "open_id",
        "text": "hello",
    })

    assert "error" in result
    assert "om_typing_orphan" in acct.deleted_messages
    assert (acct.alias, real_chat_id) not in (
        manager_mod._typing_manager._active_chats
    )


def test_send_failure_does_not_delete_unrelated_typing(tmp_path: Path):
    """A failed open_id send must not nuke other active typing indicators."""
    boom = RuntimeError("send blew up")
    acct = FakeAccount(send_raises=boom)
    fm = _make_manager(tmp_path, acct)

    target_open_id = "ou_target"
    target_chat_id = "oc_target_chat"
    other_open_id = "ou_other"
    other_chat_id = "oc_other_chat"

    manager_mod._typing_manager._active_chats[(acct.alias, target_chat_id)] = {
        "message_id": "om_target",
        "receive_id": target_open_id,
        "receive_id_type": "open_id",
    }
    manager_mod._typing_manager._active_chats[(acct.alias, other_chat_id)] = {
        "message_id": "om_other",
        "receive_id": other_open_id,
        "receive_id_type": "open_id",
    }

    fm.handle({
        "action": "send",
        "receive_id": target_open_id,
        "receive_id_type": "open_id",
        "text": "ping",
    })

    assert acct.deleted_messages == ["om_target"]
    assert (acct.alias, target_chat_id) not in (
        manager_mod._typing_manager._active_chats
    )
    assert (acct.alias, other_chat_id) in (
        manager_mod._typing_manager._active_chats
    )


def test_send_success_with_open_id_still_cleans_by_returned_chat_id(
    tmp_path: Path,
):
    """The success path must continue to clean via the chat_id returned
    by the Feishu API (the original, working behavior)."""
    real_chat_id = "oc_real_chat"
    acct = FakeAccount(send_result={
        "message_id": "om_sent",
        "chat_id": real_chat_id,
    })
    fm = _make_manager(tmp_path, acct)

    open_id = "ou_user"
    manager_mod._typing_manager._active_chats[(acct.alias, real_chat_id)] = {
        "message_id": "om_typing",
        "receive_id": open_id,
        "receive_id_type": "open_id",
    }

    result = fm.handle({
        "action": "send",
        "receive_id": open_id,
        "receive_id_type": "open_id",
        "text": "hello",
    })

    assert result["status"] == "sent"
    assert "om_typing" in acct.deleted_messages
    assert (acct.alias, real_chat_id) not in (
        manager_mod._typing_manager._active_chats
    )


def test_send_failure_no_active_typing_does_not_raise(tmp_path: Path):
    """If nothing was started, the fallback is a no-op (the duplicate-send
    guard or other failure path can hit this)."""
    acct = FakeAccount(send_raises=RuntimeError("boom"))
    fm = _make_manager(tmp_path, acct)

    result = fm.handle({
        "action": "send",
        "receive_id": "ou_user",
        "receive_id_type": "open_id",
        "text": "hello",
    })

    assert "error" in result
    assert acct.deleted_messages == []


# --------------------------------------------------------------------
# _reply cleanup behavior — issue #7 regression
# --------------------------------------------------------------------


def test_reply_with_empty_chat_id_skips_typing_cleanup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    acct = FakeAccount(reply_result={
        "message_id": "om_reply",
        "chat_id": "oc_returned_chat",
    })
    fm = _make_manager(tmp_path, acct)
    manager_mod._typing_manager._active_chats[(acct.alias, "oc_returned_chat")] = {
        "message_id": "om_typing",
        "receive_id": "oc_returned_chat",
        "receive_id_type": "chat_id",
    }

    with caplog.at_level(logging.DEBUG, logger="lingtai_feishu.manager"):
        result = fm.handle({
            "action": "reply",
            "message_id": f"{acct.alias}::om_original",
            "text": "hello",
        })

    assert result == {
        "status": "sent",
        "message_id": f"{acct.alias}:oc_returned_chat:om_reply",
    }
    assert acct.replies == [("om_original", "hello")]
    assert acct.deleted_messages == []
    assert (acct.alias, "oc_returned_chat") in (
        manager_mod._typing_manager._active_chats
    )
    assert "Skipping reply typing cleanup with no chat_id" in caplog.text


def test_reply_logs_when_no_typing_indicator_found(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    acct = FakeAccount(reply_result={
        "message_id": "om_reply",
        "chat_id": "oc_chat_A",
    })
    fm = _make_manager(tmp_path, acct)

    with caplog.at_level(logging.DEBUG, logger="lingtai_feishu.manager"):
        result = fm.handle({
            "action": "reply",
            "message_id": f"{acct.alias}:oc_chat_A:om_original",
            "text": "hello",
        })

    assert result == {
        "status": "sent",
        "message_id": f"{acct.alias}:oc_chat_A:om_reply",
    }
    assert acct.deleted_messages == []
    assert "No reply typing indicator found for acct1:oc_chat_A:om_original" in (
        caplog.text
    )
