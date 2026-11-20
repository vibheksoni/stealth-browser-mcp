"""Gated browser integration tests for low-level input primitives on local SPA (SB-67).

Ungated unit tests verify the fixture page structure and ASCII validity.
Real-browser tests are gated by STEALTH_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.input_formats import InputFormatError
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
from stealth_browser_mcp.process_cleanup import process_cleanup
from tests.browser_harness.server import PAGES_DIR, FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    """Safely remove a temporary directory with retry and ignore_errors fallback."""
    if not dir_path or not os.path.exists(dir_path):
        return
    for _ in range(10):
        try:
            shutil.rmtree(dir_path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


class TestProjectToolFixtureUngated(unittest.TestCase):
    """Ungated unit tests verifying project_tool.html fixture structure and serving."""

    def test_fixture_file_exists_and_is_ascii(self) -> None:
        """Verify project_tool.html exists and is 100% valid ASCII."""
        fixture_path = PAGES_DIR / "project_tool.html"
        self.assertTrue(fixture_path.is_file(), f"Missing fixture file: {fixture_path}")
        content_bytes = fixture_path.read_bytes()
        try:
            content_text = content_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail(f"Non-ASCII character in project_tool.html: {exc}")

        # Check required element ids and attributes
        expected_tokens = [
            'id="title"',
            'id="desc-plain"',
            'id="desc-rich"',
            'contenteditable="true"',
            'id="rich-bold"',
            'id="hours"',
            'id="priority"',
            'id="status-dd"',
            'id="status-list"',
            'id="label-input"',
            'id="label-list"',
            'id="chips"',
            'id="save"',
            'id="status-msg"',
            'window.__saved',
            'window.__state',
        ]
        for token in expected_tokens:
            self.assertIn(token, content_text, f"Missing token in project_tool.html: {token}")

    def test_server_serves_project_tool_page(self) -> None:
        """Verify FixtureServer serves project_tool.html on loopback."""
        with FixtureServer() as srv:
            url = srv.url_for("project_tool.html")
            with urllib.request.urlopen(url, timeout=5.0) as res:
                self.assertEqual(res.status, 200)
                body = res.read().decode("ascii")
                self.assertIn("Project Task Tool", body)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestInputPrimitivesGated(unittest.IsolatedAsyncioTestCase):
    """Gated real-browser integration suite for input primitives (SB-67)."""

    async def asyncSetUp(self) -> None:
        """Spawn headless browser with isolated temp profile and navigate to fixture page."""
        self.server = FixtureServer().start()
        self.temp_dir = tempfile.mkdtemp(prefix="stealth_input_prim_")
        self.manager = server.browser_manager
        options = BrowserOptions(
            headless=True,
            user_data_dir=self.temp_dir,
            sandbox=False if sandbox_must_be_disabled() else True,
        )
        self.instance = await self.manager.spawn_browser(options)
        self.instance_id = self.instance.instance_id
        await self.manager.navigate(
            self.instance_id,
            self.server.url_for("project_tool.html#/new"),
            wait_until="load",
            timeout=15000,
        )
        self.tab = await self.manager.get_tab(self.instance_id)

    async def asyncTearDown(self) -> None:
        """Clean up browser instance, processes, temporary profile, and fixture server."""
        if hasattr(self, "instance") and self.instance:
            try:
                await self.manager.close_instance(self.instance.instance_id)
            except Exception:
                pass
        try:
            for pid in process_cleanup._get_browser_pids_for_profile(self.temp_dir):
                process_cleanup._kill_process_by_pid(pid, "test_input_primitives")
        except Exception:
            pass
        _safe_cleanup_dir(self.temp_dir)
        if hasattr(self, "server") and self.server:
            self.server.stop()

    async def get_state(self) -> Dict[str, Any]:
        """Read full application state from window.__state() as a parsed dict."""
        raw = await self.tab.evaluate("window.__state()")
        return json.loads(raw)

    async def test_01_title_typed_and_filled(self) -> None:
        """Verify #title with type_text and fill (strategy auto) with unicode and emoji."""
        unicode_text = "Caf\u00e9 plan - r\u00e9sum\u00e9 \U0001f680"
        typed = await server.type_text.fn(
            self.instance_id,
            selector="#title",
            text=unicode_text,
            clear_first=True,
        )
        self.assertTrue(typed)
        state_typed = await self.get_state()
        self.assertEqual(state_typed["state"]["title"], unicode_text)

        fill_text = "Updated Caf\u00e9 \U0001f389"
        fill_res = await server.fill.fn(
            self.instance_id,
            selector="#title",
            value=fill_text,
            strategy="auto",
        )
        self.assertIsNotNone(fill_res)
        state_filled = await self.get_state()
        self.assertEqual(state_filled["state"]["title"], fill_text)

    async def test_02_multiline_description_plain(self) -> None:
        """Verify multi-line text into #desc-plain via fill preserves exact newlines."""
        multiline_value = "Line one\n\nLine three"
        await server.fill.fn(
            self.instance_id,
            selector="#desc-plain",
            value=multiline_value,
        )
        state = await self.get_state()
        self.assertEqual(state["state"]["descPlain"], multiline_value)

    async def test_03_rich_text_editor(self) -> None:
        """Verify rich text editor #desc-rich with type_text, paste_text, and fill."""
        # (a) type_text with parse_newlines=True
        text_a = "Line one\nLine two\n\nLine four"
        await server.type_text.fn(
            self.instance_id,
            selector="#desc-rich",
            text=text_a,
            parse_newlines=True,
            clear_first=True,
        )
        state_a = await self.get_state()
        self.assertEqual(
            state_a["state"]["rich"],
            ["Line one", "Line two", "", "Line four"],
        )

        # (b) paste_text of a 4-line string
        text_b = "Paragraph 1\nParagraph 2\nParagraph 3\nParagraph 4"
        await server.paste_text.fn(
            self.instance_id,
            selector="#desc-rich",
            text=text_b,
            clear_first=True,
        )
        state_b = await self.get_state()
        self.assertEqual(
            state_b["state"]["rich"],
            ["Paragraph 1", "Paragraph 2", "Paragraph 3", "Paragraph 4"],
        )

        # (c) fill with the same multi-line text
        await server.fill.fn(
            self.instance_id,
            selector="#desc-rich",
            value=text_b,
        )
        state_c = await self.get_state()
        self.assertEqual(
            state_c["state"]["rich"],
            ["Paragraph 1", "Paragraph 2", "Paragraph 3", "Paragraph 4"],
        )

    async def test_04_number_field_hours(self) -> None:
        """Verify #hours with fill sets, overwrites, and rejects non-numeric input."""
        await server.fill.fn(self.instance_id, selector="#hours", value="12.5")
        state_1 = await self.get_state()
        self.assertEqual(state_1["state"]["hours"], "12.5")

        await server.fill.fn(self.instance_id, selector="#hours", value="0.25")
        state_2 = await self.get_state()
        self.assertEqual(state_2["state"]["hours"], "0.25")

        with self.assertRaises(InputFormatError) as cm:
            await server.fill.fn(self.instance_id, selector="#hours", value="abc")
        self.assertIn("numeric value", str(cm.exception))
        state_3 = await self.get_state()
        self.assertEqual(state_3["state"]["hours"], "0.25")

    async def test_05_native_select_priority(self) -> None:
        """Verify native select #priority via select_option and choose_option."""
        await server.select_option.fn(self.instance_id, selector="#priority", value="high")
        state_1 = await self.get_state()
        self.assertEqual(state_1["state"]["priority"], "high")

        await server.choose_option.fn(self.instance_id, selector="#priority", option_text="medium")
        state_2 = await self.get_state()
        self.assertEqual(state_2["state"]["priority"], "medium")

    async def test_06_custom_dropdown_status(self) -> None:
        """Verify custom dropdown #status-dd via click_element and choose_option."""
        await server.click_element.fn(self.instance_id, selector="#status-dd")
        await server.click_element.fn(self.instance_id, selector="li[data-value=doing]")
        state_1 = await self.get_state()
        self.assertEqual(state_1["state"]["status"], "doing")

        is_open = await self.tab.evaluate(
            "document.getElementById('status-list').style.display !== 'none'"
        )
        self.assertFalse(is_open)

        await server.choose_option.fn(self.instance_id, selector="#status-dd", option_text="done")
        state_2 = await self.get_state()
        self.assertEqual(state_2["state"]["status"], "done")

    async def test_07_label_chips(self) -> None:
        """Verify label chips input, debounce suggestions, keyboard Enter, and removal."""
        # 1. Type "back", wait for suggestion listbox, click first suggestion
        await server.type_text.fn(self.instance_id, selector="#label-input", text="back", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.click_element.fn(self.instance_id, selector="#label-list li")
        state_1 = await self.get_state()
        self.assertEqual(state_1["state"]["labels"], ["backend"])

        # 2. Type "fro" + Enter
        await server.type_text.fn(self.instance_id, selector="#label-input", text="fro", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.press_key.fn(self.instance_id, key="Enter", selector="#label-input")
        state_2 = await self.get_state()
        self.assertEqual(state_2["state"]["labels"], ["backend", "frontend"])

        # 3. Type "urgent" and click
        await server.type_text.fn(self.instance_id, selector="#label-input", text="urgent", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.click_element.fn(self.instance_id, selector="#label-list li")
        state_3 = await self.get_state()
        self.assertEqual(state_3["state"]["labels"], ["backend", "frontend", "urgent"])

        # 4. Remove "frontend" through its x button
        await server.click_element.fn(
            self.instance_id,
            selector='span.chip[data-label="frontend"] button.chip-x',
        )
        state_4 = await self.get_state()
        self.assertEqual(state_4["state"]["labels"], ["backend", "urgent"])

        # 5. Typing a duplicate is ignored
        await server.type_text.fn(self.instance_id, selector="#label-input", text="urgent", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.click_element.fn(self.instance_id, selector="#label-list li")
        state_5 = await self.get_state()
        self.assertEqual(state_5["state"]["labels"], ["backend", "urgent"])

    async def test_08_full_task_flow_and_isolation(self) -> None:
        """Verify full task creation flow, saving, list view, form reset, and isolation."""
        # --- Task 1 ---
        await server.type_text.fn(self.instance_id, selector="#title", text="Task 1 Title", clear_first=True)
        await server.fill.fn(self.instance_id, selector="#desc-plain", value="Plain description 1")
        await server.fill.fn(self.instance_id, selector="#desc-rich", value="Rich line 1\nRich line 2")
        await server.fill.fn(self.instance_id, selector="#hours", value="4.5")
        await server.select_option.fn(self.instance_id, selector="#priority", value="high")
        await server.click_element.fn(self.instance_id, selector="#status-dd")
        await server.click_element.fn(self.instance_id, selector="li[data-value=doing]")
        await server.type_text.fn(self.instance_id, selector="#label-input", text="docs", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.click_element.fn(self.instance_id, selector="#label-list li")

        # Save task 1
        await server.click_element.fn(self.instance_id, selector="#save")

        state_1 = await self.get_state()
        self.assertEqual(state_1["route"], "#/list")
        self.assertEqual(len(state_1["saved"]), 1)
        saved_task = state_1["saved"][0]
        self.assertEqual(saved_task["title"], "Task 1 Title")
        self.assertEqual(saved_task["descPlain"], "Plain description 1")
        self.assertEqual(saved_task["hours"], "4.5")
        self.assertEqual(saved_task["priority"], "high")
        self.assertEqual(saved_task["status"], "doing")
        self.assertEqual(saved_task["labels"], ["docs"])

        task_count_1 = await self.tab.evaluate("document.querySelectorAll('li.task').length")
        self.assertEqual(task_count_1, 1)

        # Form reset checks
        self.assertEqual(state_1["state"]["title"], "")
        self.assertEqual(state_1["state"]["descPlain"], "")
        self.assertEqual(state_1["state"]["hours"], "")
        self.assertEqual(state_1["state"]["status"], "none")
        self.assertEqual(state_1["state"]["labels"], [])

        # --- Task 2: navigate back to #/new and check isolation ---
        await self.tab.evaluate("location.hash = '#/new'")
        await asyncio.sleep(0.1)

        leftover_chips = await self.tab.evaluate("document.querySelectorAll('#chips .chip').length")
        self.assertEqual(leftover_chips, 0)

        await server.type_text.fn(self.instance_id, selector="#title", text="Task 2 Title", clear_first=True)
        await server.type_text.fn(self.instance_id, selector="#label-input", text="infra", clear_first=True)
        await server.wait_for_element.fn(self.instance_id, selector="#label-list li", timeout=3000)
        await server.click_element.fn(self.instance_id, selector="#label-list li")

        # Save task 2
        await server.click_element.fn(self.instance_id, selector="#save")

        state_2 = await self.get_state()
        self.assertEqual(len(state_2["saved"]), 2)
        self.assertEqual(state_2["saved"][1]["title"], "Task 2 Title")
        self.assertEqual(state_2["saved"][1]["labels"], ["infra"])

        task_count_2 = await self.tab.evaluate("document.querySelectorAll('li.task').length")
        self.assertEqual(task_count_2, 2)

    async def test_09_background_safety(self) -> None:
        """Verify input tool calls do not require document.hasFocus() and never raise."""
        # Observe document.hasFocus() without asserting True
        has_focus = await self.tab.evaluate("document.hasFocus()")
        self.assertIn(has_focus, (True, False))

        # Perform tool calls and verify they never raise
        res_type = await server.type_text.fn(
            self.instance_id,
            selector="#title",
            text="Background Safety Title",
            clear_first=True,
        )
        self.assertTrue(res_type.get("success", False) if isinstance(res_type, dict) else res_type)

        res_fill = await server.fill.fn(
            self.instance_id,
            selector="#desc-plain",
            value="Background line 1\nBackground line 2",
        )
        self.assertIsNotNone(res_fill)

        res_key = await server.press_key.fn(
            self.instance_id,
            key="Tab",
            selector="#title",
        )
        self.assertIsNotNone(res_key)

        state = await self.get_state()
        self.assertEqual(state["state"]["title"], "Background Safety Title")
        self.assertEqual(state["state"]["descPlain"], "Background line 1\nBackground line 2")


if __name__ == "__main__":
    unittest.main()
