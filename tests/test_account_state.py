import json

import pytest

import lingtai_feishu.account as account_module
from lingtai_feishu.account import FeishuAccount


def test_save_state_writes_json_atomically(tmp_path):
    acct = FeishuAccount(
        alias="test",
        app_id="app_id",
        app_secret="secret",
        allowed_users=None,
        state_dir=tmp_path,
    )
    acct._bot_info = {"app_id": "app_id", "name": "Test Bot"}

    acct._save_state()

    state_path = tmp_path / "state.json"
    assert state_path.read_text(encoding="utf-8").endswith("\n")
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "bot_info": {"app_id": "app_id", "name": "Test Bot"},
    }
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_save_state_preserves_existing_file_when_replace_fails(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"bot_info": {"app_id": "old"}}\n', encoding="utf-8")
    acct = FeishuAccount(
        alias="test",
        app_id="app_id",
        app_secret="secret",
        allowed_users=None,
        state_dir=tmp_path,
    )
    acct._bot_info = {"app_id": "new"}

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(account_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        acct._save_state()

    assert state_path.read_text(encoding="utf-8") == '{"bot_info": {"app_id": "old"}}\n'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []
