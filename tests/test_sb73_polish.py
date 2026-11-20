"""Unit tests for SB-73 polish items.

Covers:
1. History navigation start-of-history logic and pure decide_history_move.
2. HTTP transport path normalization without 307 redirect.
3. Page content size capping, boundary truncation, and max_chars parameter.
4. Clearer errors with actionable next-step hints.

ASCII only. Runs without real browsers or network.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from stealth_browser_mcp import content_cap, errors, history_nav, http_auth, server


class TestHistoryNav(unittest.TestCase):
    """Item 1: history navigation decision helpers."""

    def test_go_back_at_start_pages_refused(self):
        first_page_msg = history_nav.FIRST_PAGE_MESSAGE
        blank_tab_msg = history_nav.BLANK_PREV_PAGE_MESSAGE
        self.assertEqual(
            first_page_msg,
            "No previous page in history; this is the first page.",
        )
        self.assertEqual(
            blank_tab_msg,
            "No previous page in history (previous entry is a blank tab).",
        )

        # Instance just spawned and navigated once
        self.assertEqual(
            history_nav.decide_history_move("back", 1, ["newtab", "https://a"]),
            blank_tab_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("back", 1, ["about:blank", "https://a"]),
            blank_tab_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("back", 1, ["chrome://new-tab-page/", "https://a"]),
            blank_tab_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("back", 1, ["chrome://newtab", "https://a"]),
            blank_tab_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("back", 1, ["chrome-search://local-ntp", "https://a"]),
            blank_tab_msg,
        )

        # At the very first entry
        self.assertEqual(
            history_nav.decide_history_move("back", 0, ["https://a"]),
            first_page_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("back", 0, []),
            first_page_msg,
        )

    def test_go_back_with_real_history_allowed(self):
        # Two real pages, going back from second to first is allowed
        self.assertIsNone(
            history_nav.decide_history_move("back", 1, ["https://a", "https://b"])
        )
        # Three entries where index 1 is real page, going back from index 2 to index 1 is allowed
        self.assertIsNone(
            history_nav.decide_history_move(
                "back", 2, ["about:blank", "https://a", "https://b"]
            )
        )

    def test_go_forward_cases(self):
        next_msg = history_nav.NO_NEXT_PAGE_MESSAGE
        self.assertIn("No next page", next_msg)

        # Can move forward from index 0 to 1
        self.assertIsNone(
            history_nav.decide_history_move("forward", 0, ["https://a", "https://b"])
        )
        # Already at the end
        self.assertEqual(
            history_nav.decide_history_move("forward", 1, ["https://a", "https://b"]),
            next_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("forward", 0, ["https://a"]),
            next_msg,
        )
        self.assertEqual(
            history_nav.decide_history_move("forward", 0, []),
            next_msg,
        )


class TestHttpRedirect(unittest.TestCase):
    """Item 2: HTTP transport /mcp path redirect handling."""

    def test_mcp_path_rewrite_middleware_direct(self):
        calls = []

        async def dummy_app(scope, receive, send):
            calls.append((scope.get("path"), scope.get("raw_path")))

        middleware = http_auth.McpPathRewriteMiddleware(dummy_app)

        scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}
        asyncio.run(middleware(scope, None, None))
        self.assertEqual(calls[-1], ("/mcp/", b"/mcp/"))

        scope_slash = {"type": "http", "path": "/mcp/", "raw_path": b"/mcp/"}
        asyncio.run(middleware(scope_slash, None, None))
        self.assertEqual(calls[-1], ("/mcp/", b"/mcp/"))

        scope_other = {"type": "http", "path": "/other", "raw_path": b"/other"}
        asyncio.run(middleware(scope_other, None, None))
        self.assertEqual(calls[-1], ("/other", b"/other"))

    def test_http_app_paths_and_auth(self):
        token = "test-secret-token-12345"
        with patch.dict(os.environ, {"STEALTH_MCP_TOKEN": token}):
            middleware = server.create_http_middleware(token=token)
            app = server.mcp.http_app(middleware=middleware)

            with TestClient(app, follow_redirects=False) as client:
                # 1. Without token: both /mcp and /mcp/ return 401 Unauthorized
                res_no_auth_no_slash = client.post("/mcp")
                self.assertEqual(res_no_auth_no_slash.status_code, 401)

                res_no_auth_slash = client.post("/mcp/")
                self.assertEqual(res_no_auth_slash.status_code, 401)

                # 2. With token: neither path answers 307 redirect
                headers = {"Authorization": f"Bearer {token}"}

                res_auth_no_slash = client.post("/mcp", headers=headers)
                self.assertNotEqual(res_auth_no_slash.status_code, 307)
                self.assertNotIn(res_auth_no_slash.status_code, (301, 302, 307, 308))

                res_auth_slash = client.post("/mcp/", headers=headers)
                self.assertNotEqual(res_auth_slash.status_code, 307)
                self.assertNotIn(res_auth_slash.status_code, (301, 302, 307, 308))


class TestContentCap(unittest.TestCase):
    """Item 3: get_page_content size cap and boundary truncation."""

    def test_clamp_max_chars(self):
        self.assertEqual(content_cap.clamp_max_chars(0), 1000)
        self.assertEqual(content_cap.clamp_max_chars(-100), 1000)
        self.assertEqual(content_cap.clamp_max_chars(500), 1000)
        self.assertEqual(content_cap.clamp_max_chars(1000), 1000)
        self.assertEqual(content_cap.clamp_max_chars(40000), 40000)
        self.assertEqual(content_cap.clamp_max_chars(200000), 200000)
        self.assertEqual(content_cap.clamp_max_chars(999999), 200000)
        self.assertEqual(content_cap.clamp_max_chars("invalid"), 40000)

    def test_cap_content_not_truncated(self):
        short = "This is a short sample page."
        res = content_cap.cap_content(short, max_chars=40000)
        self.assertFalse(res["truncated"])
        self.assertEqual(res["original_chars"], len(short))
        self.assertEqual(res["content"], short)
        self.assertEqual(res["text"], short)
        self.assertNotIn("note", res)

    def test_cap_content_line_boundary(self):
        lines = [f"Line {i} content text." for i in range(10)]
        text = "\n".join(lines)
        limit = 35
        res = content_cap.cap_content(text, max_chars=limit)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["original_chars"], len(text))
        self.assertLessEqual(len(res["content"]), limit)
        self.assertTrue(res["content"].endswith(lines[0]))
        self.assertNotIn(lines[1], res["content"])
        self.assertIn("note", res)
        self.assertIn("selector", res["note"])
        self.assertIn("max_chars", res["note"])
        self.assertIn("get_accessibility_snapshot", res["note"])

    def test_cap_content_word_boundary(self):
        text = "wordone wordtwo wordthree wordfour wordfive"
        limit = 22
        res = content_cap.cap_content(text, max_chars=limit)
        self.assertTrue(res["truncated"])
        self.assertLessEqual(len(res["content"]), limit)
        self.assertEqual(res["content"], "wordone wordtwo")
        self.assertIn("note", res)

    def test_cap_content_unbroken_text(self):
        text = "a" * 100
        limit = 20
        res = content_cap.cap_content(text, max_chars=limit)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["content"]), limit)
        self.assertEqual(res["content"], "a" * limit)

    def test_get_page_content_tool_call(self):
        mock_tab = MagicMock()
        large_html = "<html><body>" + ("<p>Hello world paragraph</p>\n" * 2000) + "</body></html>"
        large_text = ("Hello world paragraph\n" * 2000).strip()

        async def fake_get_content():
            return large_html

        async def fake_evaluate(expr):
            if "innerText" in expr:
                return large_text
            if "location.href" in expr:
                return "https://example.com"
            if "title" in expr:
                return "Example"
            return ""

        mock_tab.get_content = fake_get_content
        mock_tab.evaluate = fake_evaluate

        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=mock_tab)):
            res = asyncio.run(
                server.get_page_content.fn(
                    instance_id="inst_test",
                    include_frames=False,
                    max_chars=2000,
                )
            )

        self.assertTrue(res["truncated"])
        self.assertLessEqual(len(res["html"]), 2000)
        self.assertLessEqual(len(res["text"]), 2000)
        self.assertIn("note", res)
        self.assertGreaterEqual(res["original_chars"], len(large_text))


class TestClearerErrors(unittest.TestCase):
    """Item 4: instance_not_found_error, tab errors, and server.py grep assertion."""

    def test_instance_not_found_error_properties(self):
        err = errors.instance_not_found_error("inst_123")
        self.assertIsInstance(err, ToolError)
        self.assertIsInstance(err, Exception)

        msg = str(err)
        self.assertTrue(msg.startswith("Instance not found: inst_123."))
        self.assertIn("list_instances", msg)
        self.assertIn("spawn_browser", msg)

    def test_tab_errors_properties(self):
        tab_err = errors.tab_not_found_error("tab_456")
        self.assertIsInstance(tab_err, ToolError)
        self.assertIn("Tab not found: tab_456", str(tab_err))
        self.assertIn("list_tabs", str(tab_err))
        self.assertIn("new_tab", str(tab_err))

        active_err = errors.no_active_tab_error("inst_789")
        self.assertIsInstance(active_err, ToolError)
        self.assertIn("No active tab found for instance 'inst_789'", str(active_err))
        self.assertIn("list_tabs", str(active_err))
        self.assertIn("new_tab", str(active_err))

    def test_no_bare_instance_not_found_exception_in_server(self):
        server_path = ROOT / "src" / "stealth_browser_mcp" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        # Grep assertion requested by prompt
        self.assertNotIn('raise Exception(f"Instance not found', source)
        self.assertNotIn("raise Exception(f'Instance not found", source)


if __name__ == "__main__":
    unittest.main()
