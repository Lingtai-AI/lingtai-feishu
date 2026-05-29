"""Tests for the MCP-owned HTML/browser onboarding resources.

These complement the LingTai profile resources (manifest/skill/config/
troubleshooting/status). They give an agent everything it needs to walk a
human through obtaining and entering Feishu/Lark *app credentials* and to
generate and open a local HTML+browser onboarding/checklist page — without
touching the LingTai TUI.

Feishu authenticates with app credentials (``app_id``/``app_secret``) issued
by the Feishu/Lark Developer Console. There is no QR/scan login flow, so the
onboarding recipe and template are credential- and verification-oriented, not
QR-oriented. The generated page never carries credentials.
"""
import asyncio
import unittest

import mcp.types as types

from lingtai_feishu import server as feishu_server


_ONBOARDING_DOC_URI = "lingtai://onboarding/feishu"
_ONBOARDING_TEMPLATE_URI = "lingtai://onboarding/html-template"


class OnboardingResourcesTest(unittest.TestCase):
    def test_onboarding_resources_are_listed(self):
        srv = feishu_server.build_server(None)
        handlers = getattr(srv, "request_handlers", {})
        list_handler = handlers[types.ListResourcesRequest]
        result = asyncio.run(list_handler(types.ListResourcesRequest()))
        uris = {str(resource.uri) for resource in result.root.resources}
        self.assertIn(_ONBOARDING_DOC_URI, uris)
        self.assertIn(_ONBOARDING_TEMPLATE_URI, uris)

    def test_manifest_indexes_onboarding_and_exposes_entrypoints(self):
        manifest = feishu_server._profile_manifest(None)
        index_uris = {item["uri"] for item in manifest["resources"]}
        self.assertIn(_ONBOARDING_DOC_URI, index_uris)
        self.assertIn(_ONBOARDING_TEMPLATE_URI, index_uris)
        self.assertEqual(
            manifest["agent_entrypoints"]["onboarding"], _ONBOARDING_DOC_URI,
        )
        self.assertEqual(
            manifest["agent_entrypoints"]["onboarding_html_template"],
            _ONBOARDING_TEMPLATE_URI,
        )

    def test_onboarding_doc_documents_credential_setup_and_verification(self):
        payloads = feishu_server._resource_payloads(None)
        mime, text = payloads[_ONBOARDING_DOC_URI]
        self.assertEqual(mime, "text/markdown")
        # Points at the HTML template it drives.
        self.assertIn("lingtai://onboarding/html-template", text)
        self.assertIn(".html", text)
        # Feishu is credential-based: app_id / app_secret from the console.
        self.assertIn("app_id", text)
        self.assertIn("app_secret", text)
        self.assertIn("Developer Console", text)
        # Verification path uses the package's own status resource.
        self.assertIn("lingtai://status", text)
        # Points agents back at the authoritative resources.
        self.assertIn("lingtai://docs/configuration", text)
        self.assertIn("lingtai://docs/troubleshooting", text)

    def test_onboarding_doc_states_feishu_has_no_qr_login(self):
        _mime, text = feishu_server._resource_payloads(None)[_ONBOARDING_DOC_URI]
        lower = text.lower()
        # Feishu has no QR/scan login; the recipe must say so explicitly rather
        # than inventing a scan flow. It must mention QR only to deny it.
        self.assertIn("no qr", lower)
        # And it must never instruct the human to scan a code.
        self.assertNotIn("scan the qr", lower)
        self.assertNotIn("scan this qr", lower)

    def test_onboarding_doc_warns_against_sharing_secrets(self):
        _mime, text = feishu_server._resource_payloads(None)[_ONBOARDING_DOC_URI]
        lower = text.lower()
        self.assertIn("app_secret", lower)
        self.assertTrue(
            "never" in lower or "do not" in lower or "redact" in lower,
            "onboarding doc must warn against exposing secrets",
        )
        # Must explicitly forbid baking credentials into the generated page.
        self.assertIn("redact", lower)

    def test_html_template_is_self_contained_static_html(self):
        _mime, html_text = feishu_server._resource_payloads(None)[_ONBOARDING_TEMPLATE_URI]
        self.assertIn("<!doctype html>", html_text.lower())
        self.assertIn("</html>", html_text.lower())
        # Self-contained: no external assets and no scripts.
        self.assertNotIn("<script", html_text.lower())
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)

    def test_html_template_is_served_as_html_mime(self):
        mime, _html = feishu_server._resource_payloads(None)[_ONBOARDING_TEMPLATE_URI]
        self.assertEqual(mime, "text/html")

    def test_html_template_has_placeholder_and_setup_guidance(self):
        _mime, html_text = feishu_server._resource_payloads(None)[_ONBOARDING_TEMPLATE_URI]
        # A placeholder the agent fills in with non-secret setup context.
        self.assertIn("{{SETUP}}", html_text)
        lower = html_text.lower()
        # Credential-oriented setup page, not a QR login page.
        self.assertIn("app_id", lower)
        self.assertIn("app_secret", lower)
        self.assertNotIn("scan", lower)

    def test_html_template_carries_no_secrets(self):
        _mime, html_text = feishu_server._resource_payloads(None)[_ONBOARDING_TEMPLATE_URI]
        # The template is static; it must never embed credential material.
        # A realistic secret value must never be present.
        lower = html_text.lower()
        self.assertNotIn("app_secret:", lower)
        # No placeholder-substituted secret slot — the page is secret-free.
        self.assertNotIn("{{app_secret}}", lower)
        self.assertNotIn("{{secret}}", lower)

    def test_html_template_warns_secrets_dont_belong_in_the_page(self):
        _mime, html_text = feishu_server._resource_payloads(None)[_ONBOARDING_TEMPLATE_URI]
        lower = html_text.lower()
        # The page itself must remind the human not to paste secrets into it.
        self.assertIn("app_secret", lower)
        self.assertTrue(
            "do not" in lower or "never" in lower,
            "the page must warn the human not to put secrets in it",
        )

    def test_read_resource_returns_onboarding_doc_and_template(self):
        srv = feishu_server.build_server(None)
        handlers = getattr(srv, "request_handlers", {})
        read_handler = handlers[types.ReadResourceRequest]
        for uri, needle in (
            (_ONBOARDING_DOC_URI, "app_secret"),
            (_ONBOARDING_TEMPLATE_URI, "{{SETUP}}"),
        ):
            req = types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(uri=uri),
            )
            result = asyncio.run(read_handler(req))
            contents = result.root.contents
            self.assertTrue(contents)
            self.assertIn(needle, contents[0].text)

    def test_status_and_config_docs_remain_secret_safe(self):
        """The existing profile docs must not start leaking secrets."""
        payloads = feishu_server._resource_payloads(None)
        # Status JSON never carries a secret slot.
        status_mime, status_text = payloads["lingtai://status"]
        self.assertEqual(status_mime, "application/json")
        self.assertNotIn("app_secret", status_text)
        # Configuration doc mentions app_secret but tells you to keep it secret.
        _cfg_mime, cfg_text = payloads["lingtai://docs/configuration"]
        lower = cfg_text.lower()
        self.assertIn("app_secret", lower)
        self.assertTrue("secret" in lower and ("do not" in lower or "never" in lower))


if __name__ == "__main__":
    unittest.main()
