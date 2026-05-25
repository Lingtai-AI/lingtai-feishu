"""Voice/audio retention tests.

Successful Feishu voice transcription should not leave the downloaded raw
audio file in the agent working directory. The JSON payload and transcript
metadata are sufficient for later reads; retaining raw audio causes unbounded
PII/disk growth.
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
    alias = "acct1"

    def send_text(self, *_a, **_k) -> dict:
        return {"message_id": "om_typing_x"}

    def delete_message(self, *_a, **_k) -> bool:
        return True

    def add_reaction(self, *_a, **_k) -> None:
        pass

    def get_message_resource(
        self, message_id: str, file_key: str, resource_type: str,
    ) -> tuple[str, bytes]:
        assert message_id == "om_voice"
        assert file_key == "file_voice"
        assert resource_type == "file"
        return "voice_payload", b"raw audio bytes"


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


class _SenderId:
    open_id = "ou_jason"


class _Sender:
    sender_id = _SenderId()


class _Message:
    message_id = "om_voice"
    chat_id = "oc_chat"
    chat_type = "p2p"
    message_type = "audio"
    content = json.dumps({"file_key": "file_voice"})
    create_time = "1700000000000"
    parent_id = ""


class _Event:
    message = _Message()
    sender = _Sender()


class _Data:
    event = _Event()


def test_successful_voice_transcription_deletes_downloaded_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_mod,
        "_transcribe_voice",
        lambda audio_path: {
            "text": "hello from voice",
            "language": "en",
            "duration": 1.25,
            "segments": [{"start": 0, "end": 1.25, "text": "hello from voice"}],
        },
    )
    inbound: list[dict] = []
    fm = FeishuManager(
        service=FakeService(FakeAccount()),
        working_dir=tmp_path,
        on_inbound=lambda p: inbound.append(p),
    )

    fm.on_incoming("acct1", _Data())

    message_files = list((tmp_path / "feishu" / "acct1" / "inbox").glob("*/message.json"))
    assert len(message_files) == 1
    payload = json.loads(message_files[0].read_text(encoding="utf-8"))

    assert payload["text"] == "hello from voice"
    assert payload["voice_transcript"]["text"] == "hello from voice"
    assert payload["media"]["filename"] == "voice_payload.ogg"
    assert payload["media"]["path"] is None
    assert payload["media"]["retained"] is False
    assert not list(message_files[0].parent.glob("attachments/*"))
    assert inbound[0]["metadata"]["is_voice_transcript"] is True
