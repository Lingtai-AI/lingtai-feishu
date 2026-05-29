import asyncio
import json
import os
import tempfile
from pathlib import Path
import unittest

import mcp.types as types

from lingtai_feishu import server as feishu_server


class LingTaiProfileResourcesTest(unittest.TestCase):
    def test_server_lists_profile_resources_without_manager(self):
        srv = feishu_server.build_server(None)
        handlers = getattr(srv, "request_handlers", {})
        list_handler = handlers[types.ListResourcesRequest]
        result = asyncio.run(list_handler(types.ListResourcesRequest()))
        uris = {str(resource.uri) for resource in result.root.resources}
        self.assertIn("lingtai://manifest", uris)
        self.assertIn("lingtai://skills/feishu", uris)
        self.assertIn("lingtai://docs/configuration", uris)
        self.assertIn("lingtai://docs/troubleshooting", uris)
        self.assertIn("lingtai://status", uris)

    def test_manifest_describes_agent_and_human_boundaries(self):
        manifest = feishu_server._profile_manifest(None)
        self.assertEqual(manifest["schema"], "lingtai.mcp.profile.v1")
        self.assertEqual(manifest["server"]["registry_name"], "feishu")
        self.assertEqual(manifest["server"]["name"], "lingtai-feishu")
        self.assertIn("/mcp", manifest["ownership"]["human_ui"])
        self.assertIn("MCP tools/resources/prompts", manifest["ownership"]["agent_interface"])
        self.assertEqual(manifest["agent_entrypoints"]["skill"], "lingtai://skills/feishu")
        self.assertEqual(manifest["status"]["status"], "degraded")

    def test_safe_status_redacts_app_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "feishu.json"
            cfg.write_text(json.dumps({
                "accounts": [{
                    "alias": "myapp",
                    "app_id": "cli_a1b2c3d4e5f6",
                    "app_secret": "SUPER_SECRET_VALUE_123456",
                    "allowed_users": ["ou_aaa", "ou_bbb"],
                }]
            }))
            old = os.environ.get("LINGTAI_FEISHU_CONFIG")
            try:
                os.environ["LINGTAI_FEISHU_CONFIG"] = str(cfg)
                status = feishu_server._safe_status_payload(None)
            finally:
                if old is None:
                    os.environ.pop("LINGTAI_FEISHU_CONFIG", None)
                else:
                    os.environ["LINGTAI_FEISHU_CONFIG"] = old

        self.assertTrue(status["config_readable"])
        self.assertEqual(status["accounts_count"], 1)
        account = status["accounts"][0]
        self.assertEqual(account["alias"], "myapp")
        # app_id is non-secret and may be shown; app_secret must never appear in full.
        self.assertEqual(account["app_id"], "cli_a1b2c3d4e5f6")
        self.assertTrue(account["has_app_secret"])
        self.assertNotIn("app_secret", account)
        self.assertEqual(account["allowed_users_count"], 2)
        # The full secret must not leak anywhere in the serialized status.
        self.assertNotIn("SUPER_SECRET_VALUE_123456", json.dumps(status))

    def test_resource_payloads_include_docs_and_json_status(self):
        payloads = feishu_server._resource_payloads(None)
        manifest_mime, manifest_text = payloads["lingtai://manifest"]
        self.assertEqual(manifest_mime, "application/vnd.lingtai.mcp-profile+json")
        self.assertEqual(json.loads(manifest_text)["server"]["name"], "lingtai-feishu")

        skill_mime, skill_text = payloads["lingtai://skills/feishu"]
        self.assertIn("profile=lingtai-skill", skill_mime)
        self.assertIn("thin", skill_text.lower())
        self.assertIn("lingtai://docs/configuration", skill_text)

        config_mime, config_text = payloads["lingtai://docs/configuration"]
        self.assertEqual(config_mime, "text/markdown")
        self.assertIn("LINGTAI_FEISHU_CONFIG", config_text)
        self.assertIn("app_id", config_text)
        self.assertIn("app_secret", config_text)

        troubleshooting_mime, troubleshooting_text = payloads["lingtai://docs/troubleshooting"]
        self.assertEqual(troubleshooting_mime, "text/markdown")
        self.assertIn("/mcp", troubleshooting_text)

    def test_read_resource_returns_text_and_rejects_unknown(self):
        srv = feishu_server.build_server(None)
        handlers = getattr(srv, "request_handlers", {})
        read_handler = handlers[types.ReadResourceRequest]
        req = types.ReadResourceRequest(
            params=types.ReadResourceRequestParams(uri="lingtai://manifest"),
        )
        result = asyncio.run(read_handler(req))
        contents = result.root.contents
        self.assertTrue(contents)
        self.assertIn("lingtai-feishu", contents[0].text)

        bad = types.ReadResourceRequest(
            params=types.ReadResourceRequestParams(uri="lingtai://nope"),
        )
        with self.assertRaises(Exception):
            asyncio.run(read_handler(bad))


if __name__ == "__main__":
    unittest.main()
