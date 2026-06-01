from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lingtai_feishu.account import FeishuAccount  # noqa: E402
from lingtai_feishu.manager import FeishuManager  # noqa: E402
from lingtai_feishu.service import FeishuService  # noqa: E402


class FakeAccount:
    def __init__(self, alias: str = "main") -> None:
        self.alias = alias

    def public_identity(self) -> dict:
        return {
            "alias": self.alias,
            "app_id": "cli_public_app_id",
            "last_verified_at": "2026-06-01T08:00:00+00:00",
        }

    @property
    def allowed_users_count(self) -> int:
        return 2

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class FakeService:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._account = FakeAccount()
        self.default_account = self._account

    def list_accounts(self) -> list[str]:
        return [self._account.alias]

    def account_details(self) -> list[dict]:
        item = self._account.public_identity()
        item["allowed_users_count"] = self._account.allowed_users_count
        item["contact_count"] = 0
        item["config_source"] = ".secrets/feishu.json"
        return [item]

    def identity_path(self) -> Path:
        return self._root / "system" / "mcp_identities" / "feishu.json"


def test_feishu_account_public_identity_omits_secrets_and_user_ids(tmp_path: Path):
    acct = FeishuAccount(
        alias="main",
        app_id="cli_public_app_id",
        app_secret="SUPER_SECRET_APP_SECRET",
        allowed_users=["ou_should_not_leak", "ou_also_secret"],
        state_dir=tmp_path / "state",
    )
    acct._bot_info = {"app_id": "cli_public_app_id"}
    acct._last_verified_at = "2026-06-01T08:00:00+00:00"

    identity = acct.public_identity()
    assert identity == {
        "alias": "main",
        "app_id": "cli_public_app_id",
        "last_verified_at": "2026-06-01T08:00:00+00:00",
    }
    assert acct.allowed_users_count == 2
    rendered = json.dumps(identity, ensure_ascii=False)
    assert "SUPER_SECRET" not in rendered
    assert "ou_should_not_leak" not in rendered
    assert "ou_also_secret" not in rendered


def test_feishu_service_identity_payload_and_file_are_public(tmp_path: Path):
    svc = FeishuService(
        working_dir=tmp_path,
        accounts_config=[{
            "alias": "main",
            "app_id": "cli_public_app_id",
            "app_secret": "SUPER_SECRET_APP_SECRET",
            "allowed_users": ["ou_should_not_leak"],
        }],
        on_message=lambda *_: None,
        config_source=".secrets/feishu.json",
    )
    acct = svc.get_account("main")
    acct._bot_info = {"app_id": "cli_public_app_id"}
    acct._last_verified_at = "2026-06-01T08:00:00+00:00"

    contacts = tmp_path / "feishu" / "main" / "contacts.json"
    contacts.parent.mkdir(parents=True)
    contacts.write_text(json.dumps({"ou_hidden_contact": {"alias": "Jason"}}), encoding="utf-8")

    payload = svc.identity_payload()
    assert payload["schema"] == "lingtai.mcp.identity.v1"
    assert payload["mcp"] == "feishu"
    assert payload["last_verified_at"] == "2026-06-01T08:00:00+00:00"
    assert payload["accounts"] == [{
        "alias": "main",
        "app_id": "cli_public_app_id",
        "last_verified_at": "2026-06-01T08:00:00+00:00",
        "allowed_users_count": 1,
        "contact_count": 1,
        "config_source": ".secrets/feishu.json",
    }]

    path = svc.write_identity_file()
    assert path == tmp_path / "system" / "mcp_identities" / "feishu.json"
    on_disk = path.read_text(encoding="utf-8")
    assert "SUPER_SECRET" not in on_disk
    assert "ou_should_not_leak" not in on_disk
    assert "ou_hidden_contact" not in on_disk
    assert json.loads(on_disk)["accounts"][0]["app_id"] == "cli_public_app_id"


def test_feishu_accounts_action_returns_details_and_identity_path(tmp_path: Path):
    mgr = FeishuManager(
        service=FakeService(tmp_path),
        working_dir=tmp_path,
        on_inbound=lambda event: None,
    )

    result = mgr.handle({"action": "accounts"})
    assert result["status"] == "ok"
    assert result["accounts"] == ["main"]
    assert result["details"][0]["app_id"] == "cli_public_app_id"
    assert result["details"][0]["allowed_users_count"] == 2
    assert result["identity_path"].endswith("system/mcp_identities/feishu.json")
    rendered = json.dumps(result, ensure_ascii=False)
    assert "SUPER_SECRET" not in rendered
    assert "ou_should_not_leak" not in rendered
