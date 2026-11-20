"""Tests for response caps and include_summary (SB-63).

ASCII only. Free of browsers and network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Dict
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client, FastMCP
from stealth_browser_mcp import a11y_snapshot, page_summary, server
from stealth_browser_mcp.a11y_snapshot import cap_snapshot_response
from stealth_browser_mcp.page_summary import EMPTY_NAME_ALLOWED_ROLES, with_page_summary


class FakeTab:
    """Fake tab for testing get_page_summary."""

    def __init__(self, url: str = "https://example.com/test", title: str = "Test Page"):
        self.url = url
        self.title = title


class TestCapSnapshotResponse(unittest.TestCase):
    """Unit tests for a11y_snapshot.cap_snapshot_response."""

    def test_cap_snapshot_response_filtering_and_keys(self) -> None:
        """Verify response contains only matching refs, no nodes/json/text, json size < 20000."""
        # 40-line text: lines 1-30 have (ref=eNN), lines 31-40 have no refs
        lines = [f"button {i} (ref=e{i})" for i in range(1, 31)]
        lines.extend([f"plain text line {i}" for i in range(31, 41)])
        text = "\n".join(lines)

        # 230 refs: e1 to e230
        all_refs = {
            f"e{i}": {"role": "button", "name": f"Button {i}", "backend_node_id": 100 + i}
            for i in range(1, 231)
        }

        raw_result = {
            "tree": text,
            "text": text,
            "nodes": {"large_nodes_tree": "x" * 50000},
            "json": {"large_json_tree": "y" * 50000},
            "node_count": 40,
            "truncated": False,
            "refs": all_refs,
        }

        # 1. include_json=False (default)
        response = cap_snapshot_response(raw_result, max_chars=3000, include_json=False)

        # Keys check: exactly the 30 refs whose IDs appear in the text
        self.assertEqual(len(response["refs"]), 30)
        expected_ref_keys = {f"e{i}" for i in range(1, 31)}
        self.assertEqual(set(response["refs"].keys()), expected_ref_keys)

        # No "nodes", "json", or "text" keys
        self.assertNotIn("nodes", response)
        self.assertNotIn("json", response)
        self.assertNotIn("text", response)

        # Expected keys present
        self.assertEqual(response["tree"], text)
        self.assertEqual(response["node_count"], 40)
        self.assertFalse(response["truncated"])

        # JSON size < 20000
        json_str = json.dumps(response, default=str)
        self.assertLess(len(json_str), 20000)

    def test_cap_snapshot_response_with_include_json(self) -> None:
        """Verify include_json=True includes nodes and preserves it when under threshold."""
        text = "button (ref=e1)\nbutton (ref=e2)"
        raw_result = {
            "tree": text,
            "text": text,
            "nodes": {"role": "RootWebArea", "children": []},
            "json": {"role": "RootWebArea", "children": []},
            "node_count": 2,
            "truncated": False,
            "refs": {
                "e1": {"role": "button", "name": "B1"},
                "e2": {"role": "button", "name": "B2"},
            },
        }

        response = cap_snapshot_response(raw_result, max_chars=5000, include_json=True)
        self.assertIn("nodes", response)
        self.assertEqual(response["nodes"], {"role": "RootWebArea", "children": []})
        self.assertNotIn("json", response)
        self.assertNotIn("text", response)
        self.assertFalse(response["truncated"])

    def test_cap_snapshot_response_hard_cap_shrinks_and_truncates(self) -> None:
        """Verify hard-cap case with max_chars=100 and 400 long refs shrinks and sets truncated."""
        # 400 long refs
        refs = {
            f"e{i}": {"role": "button", "name": "Very long button name content " * 10}
            for i in range(1, 401)
        }
        lines = [f"button (ref=e{i})" for i in range(1, 401)]
        text = "\n".join(lines)

        raw_result = {
            "tree": text,
            "text": text,
            "node_count": 400,
            "truncated": False,
            "refs": refs,
        }

        response = cap_snapshot_response(raw_result, max_chars=100, include_json=False)

        # max_chars=100 -> threshold = max(300, 20000) = 20000
        threshold = max(3 * 100, 20000)
        json_len = len(json.dumps(response, default=str))
        self.assertLessEqual(json_len, threshold)
        self.assertTrue(response["truncated"])
        self.assertLess(len(response["refs"]), 400)
        self.assertGreater(len(response["refs"]), 0)

    def test_cap_snapshot_response_drops_nodes_if_still_exceeds(self) -> None:
        """Verify include_json drops nodes if response still exceeds threshold."""
        text = "button (ref=e1)"
        raw_result = {
            "tree": text,
            "nodes": {"huge": "a" * 25000},
            "node_count": 1,
            "truncated": False,
            "refs": {"e1": {"role": "button", "name": "B1"}},
        }

        response = cap_snapshot_response(raw_result, max_chars=100, include_json=True)
        self.assertNotIn("nodes", response)
        self.assertTrue(response["truncated"])

    def test_server_tool_get_accessibility_snapshot_caps_response(self) -> None:
        """Verify server.get_accessibility_snapshot applies cap_snapshot_response."""
        text = "button (ref=e1)\nbutton (ref=e2)"
        fake_tab = FakeTab()

        async def _run() -> None:
            with patch.object(server.browser_manager, "get_tab", return_value=fake_tab), \
                 patch.object(server.dom_handler, "get_accessibility_snapshot", return_value={
                     "tree": text,
                     "text": text,
                     "nodes": {"huge": "n" * 50000},
                     "json": {"huge": "j" * 50000},
                     "node_count": 2,
                     "truncated": False,
                     "refs": {
                         "e1": {"role": "button", "name": "B1"},
                         "e2": {"role": "button", "name": "B2"},
                         "e99": {"role": "button", "name": "Unreferenced"},
                     },
                 }):
                fn = getattr(server.get_accessibility_snapshot, "fn", server.get_accessibility_snapshot)
                resp = await fn(instance_id="fake-id", max_chars=5000, include_json=False)
                self.assertNotIn("nodes", resp)
                self.assertNotIn("json", resp)
                self.assertNotIn("text", resp)
                self.assertEqual(set(resp["refs"].keys()), {"e1", "e2"})

                # And with include_json=True
                resp_json = await fn(instance_id="fake-id", max_chars=50000, include_json=True)
                self.assertIn("nodes", resp_json)

        asyncio.run(_run())


class TestPageSummary(unittest.TestCase):
    """Unit tests for page_summary.get_page_summary."""

    def test_summary_filtering_and_cap(self) -> None:
        """Verify summary returns at most 25 elements, skips empty-name non-inputs, no interactive_elements."""
        tab = FakeTab()

        # 60 refs: 20 with empty names (some textbox/input roles), 40 with non-empty names
        fake_ref_details: Dict[str, Any] = {}
        # 5 empty textboxes (allowed)
        for i in range(1, 6):
            fake_ref_details[f"e{i}"] = {"role": "textbox", "name": ""}
        # 5 empty inputs (allowed)
        for i in range(6, 11):
            fake_ref_details[f"e{i}"] = {"role": "input", "name": "   "}
        # 10 empty buttons (must be skipped)
        for i in range(11, 21):
            fake_ref_details[f"e{i}"] = {"role": "button", "name": ""}
        # 40 buttons with names (allowed)
        for i in range(21, 61):
            fake_ref_details[f"e{i}"] = {"role": "button", "name": f"Button {i}"}

        async def fake_get_a11y_snapshot(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"ref_details": fake_ref_details}

        with patch("stealth_browser_mcp.a11y_snapshot.get_accessibility_snapshot", fake_get_a11y_snapshot):
            summary = asyncio.run(page_summary.get_page_summary(tab))

        # At most 25 elements
        self.assertEqual(len(summary["elements"]), 25)

        # "interactive_elements" key must be absent
        self.assertNotIn("interactive_elements", summary)
        self.assertIn("elements", summary)

        # Verify no element has an empty cleaned name unless role is in EMPTY_NAME_ALLOWED_ROLES
        for el in summary["elements"]:
            if not el["name"]:
                self.assertIn(
                    el["role"],
                    EMPTY_NAME_ALLOWED_ROLES,
                    f"Element with empty name had unexpected role: {el}",
                )

        # Verify the 10 empty-name buttons (e11-e20) were skipped
        element_refs = {el["ref"] for el in summary["elements"]}
        for i in range(11, 21):
            self.assertNotIn(f"e{i}", element_refs)


class TestIncludeSummaryFastMCP(unittest.TestCase):
    """Unit tests for include_summary flag through real FastMCP client."""

    def test_include_summary_through_real_client(self) -> None:
        """Verify include_summary behavior and input schema through real FastMCP client."""
        async def _run() -> None:
            m = FastMCP("test_include_summary")

            async def click(instance_id: str, selector: str = "a") -> bool:
                return True

            wrapped = with_page_summary(click)
            m.tool(wrapped)

            async with Client(m) as c:
                # 1. include_summary=False -> no "page" key, result.data == {"success": True}
                res_false = await c.call_tool(
                    "click",
                    {"instance_id": "i", "include_summary": False},
                    raise_on_error=False,
                )
                self.assertFalse(res_false.is_error)
                self.assertEqual(res_false.data, {"success": True})

                # 2. include_summary=True -> "page" present
                res_true = await c.call_tool(
                    "click",
                    {"instance_id": "i", "include_summary": True},
                    raise_on_error=False,
                )
                self.assertFalse(res_true.is_error)
                self.assertIsInstance(res_true.data, dict)
                self.assertTrue(res_true.data.get("success"))
                self.assertIn("page", res_true.data)

                # 3. include_summary omitted -> defaults to True ("page" present)
                res_omitted = await c.call_tool(
                    "click",
                    {"instance_id": "i"},
                    raise_on_error=False,
                )
                self.assertFalse(res_omitted.is_error)
                self.assertIsInstance(res_omitted.data, dict)
                self.assertTrue(res_omitted.data.get("success"))
                self.assertIn("page", res_omitted.data)

                # 4. Check tool input schema
                tools = {t.name: t for t in await c.list_tools()}
                self.assertIn("click", tools)
                tool = tools["click"]
                schema = tool.inputSchema
                props = schema.get("properties", {})
                self.assertIn("include_summary", props)
                self.assertEqual(props["include_summary"].get("type"), "boolean")
                required = schema.get("required", [])
                self.assertNotIn("include_summary", required)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
