"""Regression tests for issue #5.

`on_incoming` used to write a fresh `inbox/{uuid}/message.json` every
time the Lark WebSocket delivered an event. On reconnect, lark-oapi can
redeliver the same event, which silently duplicated messages to the
agent. These tests cover the in-memory dedupe keyed by
`(account_alias, feishu_message_id)`.
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
    """Minimal stand-in for FeishuAccount used by on_incoming."""

    def __init__(self, alias: str = "acct1") -> None:
        self.alias = alias
        self.sent_texts: list[tuple[str, str, str]] = []
        self.deleted_messages: list[str] = []
        self.reactions: list[tuple[str, str]] = []

    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict:
        self.sent_texts.append((receive_id, receive_id_type, text))
        return {"message_id": "om_typing_x"}

    def delete_message(self, message_id: str) -> bool:
        self.deleted_messages.append(message_id)
        return True

    def add_reaction(self, message_id: str, emoji: str) -> None:
        self.reactions.append((message_id, emoji))


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


class _SenderId:
    def __init__(self, open_id: str) -> None:
        self.open_id = open_id


class _Sender:
    def __init__(self, open_id: str) -> None:
        self.sender_id = _SenderId(open_id)


class _Message:
    def __init__(
        self,
        *,
        message_id: str,
        chat_id: str = "oc_chat",
        chat_type: str = "p2p",
        message_type: str = "text",
        content: str = '{"text": "hi"}',
        create_time: str = "1700000000000",
        parent_id: str = "",
    ) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.message_type = message_type
        self.content = content
        self.create_time = create_time
        self.parent_id = parent_id


class _Event:
    def __init__(self, message: _Message, sender: _Sender) -> None:
        self.message = message
        self.sender = sender


class _Data:
    def __init__(self, event: _Event) -> None:
        self.event = event


def _make_data(message_id: str, *, chat_id: str = "oc_chat",
               open_id: str = "ou_user") -> _Data:
    return _Data(_Event(_Message(message_id=message_id, chat_id=chat_id),
                        _Sender(open_id)))


def _make_manager(
    tmp_path: Path, account: FakeAccount, inbound_log: list[dict],
) -> FeishuManager:
    return FeishuManager(
        service=FakeService(account),
        working_dir=tmp_path,
        on_inbound=lambda payload: inbound_log.append(payload),
    )


def _inbox_dir(tmp_path: Path, alias: str) -> Path:
    return tmp_path / "feishu" / alias / "inbox"


def _inbox_message_count(tmp_path: Path, alias: str) -> int:
    inbox = _inbox_dir(tmp_path, alias)
    if not inbox.is_dir():
        return 0
    return sum(
        1 for d in inbox.iterdir()
        if d.is_dir() and (d / "message.json").is_file()
    )


@pytest.fixture(autouse=True)
def _reset_typing_manager():
    """The module-level typing manager is shared global state."""
    manager_mod._typing_manager = TypingIndicatorManager()
    yield
    manager_mod._typing_manager = TypingIndicatorManager()


# --------------------------------------------------------------------
# Dedupe behavior
# --------------------------------------------------------------------


def test_duplicate_message_id_writes_and_forwards_only_once(tmp_path: Path):
    acct = FakeAccount()
    inbound: list[dict] = []
    fm = _make_manager(tmp_path, acct, inbound)

    data = _make_data("om_dup_1")
    fm.on_incoming(acct.alias, data)
    fm.on_incoming(acct.alias, data)

    assert _inbox_message_count(tmp_path, acct.alias) == 1
    assert len(inbound) == 1
    assert inbound[0]["metadata"]["message_id"].endswith(":om_dup_1")


def test_distinct_message_ids_both_write_and_forward(tmp_path: Path):
    acct = FakeAccount()
    inbound: list[dict] = []
    fm = _make_manager(tmp_path, acct, inbound)

    fm.on_incoming(acct.alias, _make_data("om_a"))
    fm.on_incoming(acct.alias, _make_data("om_b"))

    assert _inbox_message_count(tmp_path, acct.alias) == 2
    assert len(inbound) == 2
    forwarded_ids = {p["metadata"]["message_id"] for p in inbound}
    assert any(i.endswith(":om_a") for i in forwarded_ids)
    assert any(i.endswith(":om_b") for i in forwarded_ids)


def test_empty_message_id_is_not_deduped(tmp_path: Path):
    """When Lark hands us an event with no message_id (shouldn't happen in
    practice, but we don't want to silently drop the second copy if it
    does), we treat each as distinct."""
    acct = FakeAccount()
    inbound: list[dict] = []
    fm = _make_manager(tmp_path, acct, inbound)

    fm.on_incoming(acct.alias, _make_data(""))
    fm.on_incoming(acct.alias, _make_data(""))

    assert _inbox_message_count(tmp_path, acct.alias) == 2
    assert len(inbound) == 2


def test_dedupe_is_scoped_per_account(tmp_path: Path):
    """The same message_id arriving on a different account is a different
    event and must not be deduped."""
    acct_a = FakeAccount(alias="acct_a")
    acct_b = FakeAccount(alias="acct_b")
    inbound: list[dict] = []

    class TwoAccountService:
        def __init__(self) -> None:
            self.default_account = acct_a

        def get_account(self, alias: str) -> FakeAccount:
            return {"acct_a": acct_a, "acct_b": acct_b}[alias]

        def list_accounts(self) -> list[str]:
            return ["acct_a", "acct_b"]

    fm = FeishuManager(
        service=TwoAccountService(),
        working_dir=tmp_path,
        on_inbound=lambda payload: inbound.append(payload),
    )

    data = _make_data("om_shared")
    fm.on_incoming("acct_a", data)
    fm.on_incoming("acct_b", data)

    assert _inbox_message_count(tmp_path, "acct_a") == 1
    assert _inbox_message_count(tmp_path, "acct_b") == 1
    assert len(inbound) == 2


def test_cache_eviction_allows_old_ids_after_limit(tmp_path: Path):
    """The cache is bounded; once an id falls out, a replay would be
    treated as new again. This protects against unbounded growth."""
    acct = FakeAccount()
    inbound: list[dict] = []
    fm = _make_manager(tmp_path, acct, inbound)

    # Shrink the limit so the test stays fast. We're testing the
    # eviction contract, not the production limit value.
    fm._dedupe_limit = 5

    first_id = "om_first"
    fm.on_incoming(acct.alias, _make_data(first_id))
    assert _inbox_message_count(tmp_path, acct.alias) == 1

    # Push enough distinct ids through to evict `first_id`.
    for i in range(fm._dedupe_limit + 2):
        fm.on_incoming(acct.alias, _make_data(f"om_filler_{i}"))

    # Replaying `first_id` after eviction should not be deduped.
    inbound.clear()
    fm.on_incoming(acct.alias, _make_data(first_id))

    assert len(inbound) == 1
    assert inbound[0]["metadata"]["message_id"].endswith(f":{first_id}")
    # Two inbox entries for `first_id` overall: the original and the replay.
    inbox_files = list(_inbox_dir(tmp_path, acct.alias).iterdir())
    first_id_writes = 0
    for d in inbox_files:
        msg_file = d / "message.json"
        if not msg_file.is_file():
            continue
        data = json.loads(msg_file.read_text(encoding="utf-8"))
        if data.get("feishu_message_id") == first_id:
            first_id_writes += 1
    assert first_id_writes == 2
