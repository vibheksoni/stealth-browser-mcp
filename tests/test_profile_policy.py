"""Unit tests for managed Chrome profile policy parser and classifier (SB-70).

ASCII only. Free of real browsers and network in unit tests.
Gated real-browser test runs only when STEALTH_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client

from stealth_browser_mcp import server
from stealth_browser_mcp.profile_policy import (
    KNOWN_POLICIES,
    classify_policies,
    parse_policy_rows,
)


class TestParsePolicyRows(unittest.TestCase):
    """Test parse_policy_rows pure function."""

    def test_empty_and_invalid_input(self) -> None:
        self.assertEqual(parse_policy_rows([]), [])
        self.assertEqual(parse_policy_rows(None), [])
        self.assertEqual(parse_policy_rows("not a list"), [])
        self.assertEqual(parse_policy_rows([None, 123, "string", {}]), [])
        self.assertEqual(parse_policy_rows([{"name": ""}, {"name": "   "}]), [])

    def test_value_normalizations(self) -> None:
        rows = [
            {"name": "BoolTrue", "value": "true"},
            {"name": "BoolFalse", "value": "False"},
            {"name": "IntVal", "value": "2"},
            {"name": "ZeroVal", "value": "0"},
            {"name": "JsonList", "value": '["alpha", "beta"]'},
            {"name": "JsonDict", "value": '{"key": "value"}'},
            {"name": "PlainStr", "value": "plain text"},
            {"name": "ActualBool", "value": False},
            {"name": "ActualInt", "value": 42},
        ]
        parsed = parse_policy_rows(rows)
        by_name = {r["name"]: r["value"] for r in parsed}

        self.assertIs(by_name["BoolTrue"], True)
        self.assertIs(by_name["BoolFalse"], False)
        self.assertEqual(by_name["IntVal"], 2)
        self.assertEqual(by_name["ZeroVal"], 0)
        self.assertEqual(by_name["JsonList"], ["alpha", "beta"])
        self.assertEqual(by_name["JsonDict"], {"key": "value"})
        self.assertEqual(by_name["PlainStr"], "plain text")
        self.assertIs(by_name["ActualBool"], False)
        self.assertEqual(by_name["ActualInt"], 42)

    def test_field_trimming_and_defaults(self) -> None:
        rows = [
            {
                "name": "  RemoteDebuggingAllowed  ",
                "value": "false",
                "source": "  platform  ",
                "level": " mandatory ",
                "status": " ok ",
                "scope": " machine ",
            },
            {
                "name": "PartialRow",
                "value": None,
            },
        ]
        parsed = parse_policy_rows(rows)
        self.assertEqual(len(parsed), 2)

        self.assertEqual(parsed[0]["name"], "RemoteDebuggingAllowed")
        self.assertIs(parsed[0]["value"], False)
        self.assertEqual(parsed[0]["source"], "platform")
        self.assertEqual(parsed[0]["level"], "mandatory")
        self.assertEqual(parsed[0]["status"], "ok")
        self.assertEqual(parsed[0]["scope"], "machine")

        self.assertEqual(parsed[1]["name"], "PartialRow")
        self.assertIsNone(parsed[1]["value"])
        self.assertEqual(parsed[1]["source"], "")
        self.assertEqual(parsed[1]["level"], "")
        self.assertEqual(parsed[1]["status"], "")
        self.assertEqual(parsed[1]["scope"], "")


class TestClassifyPolicies(unittest.TestCase):
    """Test classify_policies pure function."""

    def test_unmanaged_empty_rows(self) -> None:
        for empty_val in ([], None, (), ""):
            res = classify_policies(empty_val)
            self.assertEqual(
                res,
                {
                    "managed": False,
                    "blockers": [],
                    "warnings": [],
                    "notes": [],
                },
            )

    def test_unknown_policies_ignored(self) -> None:
        rows = [
            {"name": "CustomUnknownPolicy1", "value": "some_value"},
            {"name": "UnrecognizedSetting", "value": 123},
        ]
        res = classify_policies(rows)
        self.assertTrue(res["managed"])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["notes"], [])

    def test_blockers_covered(self) -> None:
        # RemoteDebuggingAllowed=false is a blocker
        for val in (False, "false", "False", 0, "0"):
            res = classify_policies([{"name": "RemoteDebuggingAllowed", "value": val}])
            self.assertTrue(res["managed"])
            self.assertEqual(len(res["blockers"]), 1)
            item = res["blockers"][0]
            self.assertEqual(item["policy"], "RemoteDebuggingAllowed")
            self.assertIn("remote debugging", item["effect"].lower())
            self.assertIn("what_to_do", item)
            self.assertTrue(len(item["what_to_do"]) > 0)

        # RemoteDebuggingAllowed=true is NOT a blocker
        res_allowed = classify_policies([{"name": "RemoteDebuggingAllowed", "value": True}])
        self.assertTrue(res_allowed["managed"])
        self.assertEqual(res_allowed["blockers"], [])

    def test_warnings_covered(self) -> None:
        warning_cases = [
            ("DeveloperToolsAvailability", 2),
            ("DeveloperToolsAvailability", "2"),
            ("ExtensionInstallForcelist", ["ext-id-1", "ext-id-2"]),
            ("ExtensionInstallBlocklist", ["*"]),
            ("ExtensionSettings", {"*": {"installation_mode": "blocked"}}),
            ("URLBlocklist", ["*"]),
            ("URLAllowlist", ["https://app.corp.example.com"]),
            ("IncognitoModeAvailability", 1),
            ("IncognitoModeAvailability", "1"),
            ("DownloadRestrictions", 1),
            ("DownloadRestrictions", "2"),
            ("DeviceTrustConnectorEnabled", True),
            ("DeviceTrustConnectorEnabled", "true"),
        ]

        for policy_name, val in warning_cases:
            with self.subTest(policy=policy_name, value=val):
                res = classify_policies([{"name": policy_name, "value": val}])
                self.assertTrue(res["managed"])
                warn_policies = [item["policy"] for item in res["warnings"]]
                self.assertIn(policy_name, warn_policies)
                match = next(item for item in res["warnings"] if item["policy"] == policy_name)
                self.assertIn("effect", match)
                self.assertIn("what_to_do", match)
                self.assertTrue(len(match["effect"]) > 0)
                self.assertTrue(len(match["what_to_do"]) > 0)

        # Non-warning values should not trigger warnings
        safe_cases = [
            ("DeveloperToolsAvailability", 0),
            ("DeveloperToolsAvailability", 1),
            ("IncognitoModeAvailability", 0),
            ("DownloadRestrictions", 0),
            ("DeviceTrustConnectorEnabled", False),
        ]
        for policy_name, val in safe_cases:
            with self.subTest(policy=policy_name, safe_value=val):
                res = classify_policies([{"name": policy_name, "value": val}])
                warn_policies = [item["policy"] for item in res["warnings"]]
                self.assertNotIn(policy_name, warn_policies)

    def test_notes_covered(self) -> None:
        note_cases = [
            ("BrowserSignin", 1),
            ("ForceBrowserSignin", True),
            ("CommandLineFlagSecurityWarningsEnabled", True),
            ("SafeBrowsingProtectionLevel", 1),
            ("SafeBrowsingEnabled", True),
            ("SafeBrowsingDeepScanningEnabled", True),
            ("PasswordManagerEnabled", False),
            ("SyncDisabled", True),
            ("CloudManagementEnrollmentToken", "fake-token-guid"),
            ("MachineLevelUserCloudPolicyEnrollmentToken", "fake-token-guid"),
        ]

        for policy_name, val in note_cases:
            with self.subTest(policy=policy_name, value=val):
                res = classify_policies([{"name": policy_name, "value": val}])
                self.assertTrue(res["managed"])
                note_policies = [item["policy"] for item in res["notes"]]
                self.assertIn(policy_name, note_policies)
                match = next(item for item in res["notes"] if item["policy"] == policy_name)
                self.assertIn("effect", match)
                self.assertIn("what_to_do", match)
                self.assertTrue(len(match["effect"]) > 0)
                self.assertTrue(len(match["what_to_do"]) > 0)

    def test_deterministic_ordering(self) -> None:
        # Feed synthetic rows in reverse/shuffled order
        rows = [
            {"name": "URLBlocklist", "value": ["*"]},
            {"name": "DeveloperToolsAvailability", "value": 2},
            {"name": "ExtensionSettings", "value": {"a": "b"}},
            {"name": "DeviceTrustConnectorEnabled", "value": True},
            {"name": "ExtensionInstallForcelist", "value": ["x"]},
            {"name": "IncognitoModeAvailability", "value": 1},
            {"name": "DownloadRestrictions", "value": 3},
            {"name": "URLAllowlist", "value": ["example.com"]},
            {"name": "ExtensionInstallBlocklist", "value": ["*"]},
            {"name": "RemoteDebuggingAllowed", "value": False},
            {"name": "SyncDisabled", "value": True},
            {"name": "BrowserSignin", "value": 1},
            {"name": "SafeBrowsingProtectionLevel", "value": 1},
            {"name": "ForceBrowserSignin", "value": True},
        ]
        res = classify_policies(rows)

        blocker_names = [item["policy"] for item in res["blockers"]]
        warning_names = [item["policy"] for item in res["warnings"]]
        note_names = [item["policy"] for item in res["notes"]]

        self.assertEqual(blocker_names, sorted(blocker_names))
        self.assertEqual(warning_names, sorted(warning_names))
        self.assertEqual(note_names, sorted(note_names))

    def test_item_schema_complete(self) -> None:
        rows = [
            {"name": "RemoteDebuggingAllowed", "value": False},
            {"name": "DeveloperToolsAvailability", "value": 2},
            {"name": "BrowserSignin", "value": 1},
        ]
        res = classify_policies(rows)
        for cat in ("blockers", "warnings", "notes"):
            for item in res[cat]:
                self.assertEqual(set(item.keys()), {"policy", "effect", "what_to_do"})
                self.assertIsInstance(item["policy"], str)
                self.assertIsInstance(item["effect"], str)
                self.assertIsInstance(item["what_to_do"], str)
                self.assertTrue(len(item["policy"]) > 0)
                self.assertTrue(len(item["effect"]) > 0)
                self.assertTrue(len(item["what_to_do"]) > 0)


class TestProfilePolicyTool(unittest.TestCase):
    """Test profile_policy_report tool registration, schema, and execution."""

    def test_tool_registered_in_section(self) -> None:
        self.assertIn(
            "profile_policy_report",
            server.SECTION_TOOLS.get("browser-management", []),
        )

    def test_tool_description_length_and_sentences(self) -> None:
        async def run():
            return await server.mcp.get_tools()

        tools = asyncio.run(run())
        self.assertIn("profile_policy_report", tools)
        tool = tools["profile_policy_report"]

        desc = tool.description or ""
        self.assertTrue(len(desc) > 0, "Tool description must not be empty")
        self.assertTrue(
            len(desc) <= 300,
            f"Tool description must be <= 300 chars, got {len(desc)}",
        )

        func = getattr(server.profile_policy_report, "fn", server.profile_policy_report)
        doc = (func.__doc__ or "").strip()
        sentences = [s.strip() for s in doc.replace("\n", " ").split(".") if s.strip()]
        self.assertTrue(
            len(sentences) <= 2,
            f"Docstring must have at most 2 sentences, got {len(sentences)}",
        )

    def test_tool_execution_missing_instance_raises(self) -> None:
        with mock.patch.object(server.browser_manager, "get_browser", return_value=None):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.profile_policy_report.fn("missing_inst"))
            self.assertIn("Instance not found", str(ctx.exception))

    def test_tool_execution_unmanaged_page(self) -> None:
        mock_tab = mock.AsyncMock()
        mock_browser = mock.AsyncMock()

        with mock.patch.object(server.browser_manager, "get_browser", return_value=mock_browser), \
             mock.patch.object(server, "open_background_tab", return_value=mock_tab), \
             mock.patch.object(server.dom_handler, "execute_script", return_value=[]):

            report = asyncio.run(server.profile_policy_report.fn("test_inst"))

            self.assertFalse(report["managed"])
            self.assertEqual(report["policy_count"], 0)
            self.assertEqual(report["raw_policy_count"], 0)
            self.assertEqual(report["blockers"], [])
            self.assertEqual(report["warnings"], [])
            self.assertEqual(report["notes"], [])
            mock_tab.close.assert_awaited_once()

    def test_tool_execution_managed_page(self) -> None:
        mock_tab = mock.AsyncMock()
        mock_browser = mock.AsyncMock()
        synthetic_rows = [
            {
                "name": "RemoteDebuggingAllowed",
                "value": "false",
                "source": "platform",
                "scope": "machine",
                "level": "mandatory",
                "status": "ok",
            },
            {
                "name": "DeveloperToolsAvailability",
                "value": "2",
                "source": "platform",
                "scope": "machine",
                "level": "mandatory",
                "status": "ok",
            },
        ]

        with mock.patch.object(server.browser_manager, "get_browser", return_value=mock_browser), \
             mock.patch.object(server, "open_background_tab", return_value=mock_tab), \
             mock.patch.object(server.dom_handler, "execute_script", return_value=synthetic_rows):

            report = asyncio.run(server.profile_policy_report.fn("test_inst"))

            self.assertTrue(report["managed"])
            self.assertEqual(report["policy_count"], 2)
            self.assertEqual(report["raw_policy_count"], 2)
            self.assertEqual(len(report["blockers"]), 1)
            self.assertEqual(report["blockers"][0]["policy"], "RemoteDebuggingAllowed")
            self.assertEqual(len(report["warnings"]), 1)
            self.assertEqual(report["warnings"][0]["policy"], "DeveloperToolsAvailability")
            mock_tab.close.assert_awaited_once()

    def test_tool_execution_closes_tab_on_exception(self) -> None:
        mock_tab = mock.AsyncMock()
        mock_browser = mock.AsyncMock()

        with mock.patch.object(server.browser_manager, "get_browser", return_value=mock_browser), \
             mock.patch.object(server, "open_background_tab", return_value=mock_tab), \
             mock.patch.object(server.dom_handler, "execute_script", side_effect=RuntimeError("CDP fail")):

            with self.assertRaises(Exception) as ctx:
                asyncio.run(server.profile_policy_report.fn("test_inst"))

            self.assertIn("Failed to inspect profile policies", str(ctx.exception))
            mock_tab.close.assert_awaited_once()


@unittest.skipUnless(os.getenv("STEALTH_BROWSER_TESTS") == "1", "Requires real browser")
class TestGatedRealBrowserProfilePolicy(unittest.TestCase):
    """Gated integration test using a real browser instance."""

    def test_real_browser_policy_report(self) -> None:
        async def run():
            inst = await server.spawn_browser.fn(window="offscreen")
            inst_id = inst["instance_id"]
            try:
                report = await server.profile_policy_report.fn(inst_id)
                self.assertIn("managed", report)
                self.assertIn("blockers", report)
                self.assertIn("warnings", report)
                self.assertIn("notes", report)
                self.assertIn("policy_count", report)
                self.assertIsInstance(report["managed"], bool)
            finally:
                await server.close_instance.fn(inst_id)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
