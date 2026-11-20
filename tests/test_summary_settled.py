"""Tests for page summary settling and history navigation decisions (SB-65).

ASCII only. No real browser or network calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import history_nav, page_summary, server
from stealth_browser_mcp.history_nav import decide_history_move
from stealth_browser_mcp.page_summary import (
    _read_location,
    _wait_for_ready,
    get_page_summary,
)


class FakeTabWithSend:
    """Fake tab with send dispatching on evaluated expressions."""

    def __init__(self, old_url: str = "https://old.example.com/stale", old_title: str = "Old Stale Title"):
        self.url = old_url
        self.title = old_title
        self.ready_poll_count = 0
        self.location_call_count = 0

    async def send(self, command):
        if isinstance(command, str):
            cmd_str = command
        elif hasattr(command, "__next__"):
            try:
                req = next(command)
                cmd_str = req.get("params", {}).get("expression", "") if isinstance(req, dict) else str(req)
            except Exception:
                cmd_str = str(command)
        else:
            cmd_str = str(command)

        if "document.readyState" in cmd_str:
            self.ready_poll_count += 1
            if self.ready_poll_count < 3:
                return SimpleNamespace(value="loading")
            return SimpleNamespace(value="complete")

        if "JSON.stringify" in cmd_str:
            self.location_call_count += 1
            payload = json.dumps({
                "u": "https://new.example.com/settled",
                "t": "New Settled Title",
            })
            return SimpleNamespace(value=payload)

        return SimpleNamespace(value=None)


class PageSummarySettledTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for settled page summary extraction."""

    async def test_page_summary_settled_reads_new_values_after_ready_polling(self):
        fake_cdp = SimpleNamespace(
            runtime=SimpleNamespace(
                evaluate=lambda expression, return_by_value=True: expression
            )
        )
        fake_uc = SimpleNamespace(cdp=fake_cdp)

        tab = FakeTabWithSend(
            old_url="https://old.example.com/stale",
            old_title="Old Stale Title",
        )

        with patch("stealth_browser_mcp.page_summary.uc", fake_uc), \
             patch("stealth_browser_mcp.page_summary.asyncio.sleep", new_callable=AsyncMock):
            summary = await get_page_summary(tab)

        self.assertEqual(summary["url"], "https://new.example.com/settled")
        self.assertEqual(summary["title"], "New Settled Title")
        self.assertEqual(tab.ready_poll_count, 3)
        self.assertEqual(tab.location_call_count, 1)

    async def test_fake_tab_without_send_returns_cached_values(self):
        fake_tab = SimpleNamespace(
            url="https://cached.example.com/page",
            title="Cached Title",
        )
        summary = await get_page_summary(fake_tab)
        self.assertEqual(summary["url"], "https://cached.example.com/page")
        self.assertEqual(summary["title"], "Cached Title")

        # Tab with target fallback
        fake_tab_target = SimpleNamespace(
            target=SimpleNamespace(
                url="https://target.example.com/page",
                title="Target Title",
            )
        )
        summary_target = await get_page_summary(fake_tab_target)
        self.assertEqual(summary_target["url"], "https://target.example.com/page")
        self.assertEqual(summary_target["title"], "Target Title")

    async def test_wait_for_ready_timeout_returns_last_state(self):
        fake_cdp = SimpleNamespace(
            runtime=SimpleNamespace(
                evaluate=lambda expression, return_by_value=True: expression
            )
        )
        fake_uc = SimpleNamespace(cdp=fake_cdp)

        class StuckTab:
            def __init__(self):
                self.calls = 0

            async def send(self, command):
                self.calls += 1
                return SimpleNamespace(value="interactive")

        stuck_tab = StuckTab()
        with patch("stealth_browser_mcp.page_summary.uc", fake_uc), \
             patch("stealth_browser_mcp.page_summary.asyncio.sleep", new_callable=AsyncMock):
            state = await _wait_for_ready(stuck_tab, timeout=0.5, interval=0.1)

        self.assertEqual(state, "interactive")
        self.assertGreaterEqual(stuck_tab.calls, 1)

    async def test_wait_for_ready_impossible_evaluation(self):
        # Tab with no send attribute
        state_no_send = await _wait_for_ready(SimpleNamespace())
        self.assertEqual(state_no_send, "")

        # Tab where send raises an exception
        failing_tab = SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("CDP connection disconnected"))
        )
        fake_cdp = SimpleNamespace(
            runtime=SimpleNamespace(
                evaluate=lambda expression, return_by_value=True: expression
            )
        )
        fake_uc = SimpleNamespace(cdp=fake_cdp)
        with patch("stealth_browser_mcp.page_summary.uc", fake_uc), \
             patch("stealth_browser_mcp.page_summary.asyncio.sleep", new_callable=AsyncMock):
            state_failing = await _wait_for_ready(failing_tab, timeout=0.2, interval=0.05)
        self.assertEqual(state_failing, "")

    async def test_read_location_failures(self):
        # Tab without send
        url, title = await _read_location(SimpleNamespace())
        self.assertEqual(url, "")
        self.assertEqual(title, "")

        # Tab with send raising
        failing_tab = SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("Evaluate failed"))
        )
        fake_cdp = SimpleNamespace(
            runtime=SimpleNamespace(
                evaluate=lambda expression, return_by_value=True: expression
            )
        )
        fake_uc = SimpleNamespace(cdp=fake_cdp)
        with patch("stealth_browser_mcp.page_summary.uc", fake_uc):
            url, title = await _read_location(failing_tab)
        self.assertEqual(url, "")
        self.assertEqual(title, "")

        # Tab returning invalid JSON
        bad_json_tab = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(value="invalid json string"))
        )
        with patch("stealth_browser_mcp.page_summary.uc", fake_uc):
            url, title = await _read_location(bad_json_tab)
        self.assertEqual(url, "")
        self.assertEqual(title, "")


class HistoryNavigationDecisionTests(unittest.TestCase):
    """Unit tests for decide_history_move pure helper."""

    def test_first_page_refused(self):
        msg = decide_history_move("back", 0, ["https://example.com"])
        self.assertEqual(msg, "No previous page in history; this is the first page.")

        # Negative index or empty list
        self.assertEqual(
            decide_history_move("back", -1, ["https://example.com"]),
            "No previous page in history; this is the first page.",
        )
        self.assertEqual(
            decide_history_move("back", 0, []),
            "No previous page in history; this is the first page.",
        )

    def test_blank_previous_entry_refused(self):
        msg_blank = decide_history_move("back", 1, ["about:blank", "https://example.com"])
        self.assertEqual(
            msg_blank,
            "No previous page in history (previous entry is a blank tab).",
        )

        msg_newtab = decide_history_move("back", 1, ["chrome://newtab", "https://example.com"])
        self.assertEqual(
            msg_newtab,
            "No previous page in history (previous entry is a blank tab).",
        )

        msg_newtab_slash = decide_history_move("back", 1, ["chrome://newtab/", "https://example.com"])
        self.assertEqual(
            msg_newtab_slash,
            "No previous page in history (previous entry is a blank tab).",
        )

    def test_normal_navigation_allowed(self):
        # Back from 2nd page to 1st page
        self.assertIsNone(
            decide_history_move("back", 1, ["https://page1.com", "https://page2.com"])
        )
        # Forward from 1st page to 2nd page
        self.assertIsNone(
            decide_history_move("forward", 0, ["https://page1.com", "https://page2.com"])
        )

    def test_forward_at_end_refused(self):
        msg = decide_history_move("forward", 1, ["https://page1.com", "https://page2.com"])
        self.assertEqual(msg, "No next page in history.")

        # Single page in history
        self.assertEqual(
            decide_history_move("forward", 0, ["https://single.com"]),
            "No next page in history.",
        )
        # Empty urls
        self.assertEqual(
            decide_history_move("forward", 0, []),
            "No next page in history.",
        )


class GoBackGoForwardServerToolTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for go_back and go_forward tools in server.py."""

    async def test_go_back_refused_on_first_page(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(return_value=(0, [SimpleNamespace(url="https://page1.com")])),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_back.fn(instance_id="inst_1")

        self.assertFalse(res["success"])
        self.assertEqual(res["message"], "No previous page in history; this is the first page.")
        fake_tab.back.assert_not_called()

    async def test_go_back_refused_on_blank_previous_page(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(
                return_value=(
                    1,
                    [
                        SimpleNamespace(url="chrome://newtab/"),
                        SimpleNamespace(url="https://page1.com"),
                    ],
                )
            ),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_back.fn(instance_id="inst_1")

        self.assertFalse(res["success"])
        self.assertEqual(
            res["message"],
            "No previous page in history (previous entry is a blank tab).",
        )
        fake_tab.back.assert_not_called()

    async def test_go_back_allowed_normal(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(
                return_value=(
                    1,
                    [
                        SimpleNamespace(url="https://page1.com"),
                        SimpleNamespace(url="https://page2.com"),
                    ],
                )
            ),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_back.fn(instance_id="inst_1")

        self.assertTrue(res["success"])
        fake_tab.back.assert_called_once()

    async def test_go_back_fallback_when_history_unavailable(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(side_effect=RuntimeError("CDP history failed")),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_back.fn(instance_id="inst_1")

        self.assertTrue(res["success"])
        fake_tab.back.assert_called_once()

    async def test_go_forward_refused_at_end(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(
                return_value=(
                    1,
                    [
                        SimpleNamespace(url="https://page1.com"),
                        SimpleNamespace(url="https://page2.com"),
                    ],
                )
            ),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_forward.fn(instance_id="inst_1")

        self.assertFalse(res["success"])
        self.assertEqual(res["message"], "No next page in history.")
        fake_tab.forward.assert_not_called()

    async def test_go_forward_allowed_normal(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(
                return_value=(
                    0,
                    [
                        SimpleNamespace(url="https://page1.com"),
                        SimpleNamespace(url="https://page2.com"),
                    ],
                )
            ),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_forward.fn(instance_id="inst_1")

        self.assertTrue(res["success"])
        fake_tab.forward.assert_called_once()

    async def test_go_forward_fallback_when_history_unavailable(self):
        fake_tab = SimpleNamespace(
            back=AsyncMock(),
            forward=AsyncMock(),
            send=AsyncMock(side_effect=RuntimeError("CDP history failed")),
        )
        with patch.object(server.browser_manager, "get_tab", AsyncMock(return_value=fake_tab)):
            res = await server.go_forward.fn(instance_id="inst_1")

        self.assertTrue(res["success"])
        fake_tab.forward.assert_called_once()


if __name__ == "__main__":
    unittest.main()
