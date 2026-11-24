"""Gated browser integration tests for agent guide input recipes on local SPA (SB-68).

Ungated unit tests verify recipe definitions and ASCII validity.
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
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import agent_guide, server
from stealth_browser_mcp.input_formats import InputFormatError
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
from stealth_browser_mcp.process_cleanup import process_cleanup
from tests.browser_harness.server import FixtureServer

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


class TestAgentGuideGatedUngated(unittest.TestCase):
    """Ungated tests ensuring runnable recipes exist and are well-formed."""

    def test_runnable_recipes_exist(self) -> None:
        """Verify at least 5 recipes are marked runnable with selector and expected state."""
        runnable = [r for r in agent_guide.INPUT_RECIPES if r.get("runnable")]
        self.assertGreaterEqual(len(runnable), 5)
        for r in runnable:
            self.assertIn("selector", r["args_example"])
            self.assertIn("state_key", r)
            self.assertIn("expected_value", r)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestAgentGuideGated(unittest.IsolatedAsyncioTestCase):
    """Gated real-browser integration suite for agent guide runnable recipes (SB-68)."""

    async def asyncSetUp(self) -> None:
        """Spawn headless browser with isolated temp profile and navigate to fixture page."""
        self.server = FixtureServer().start()
        self.temp_dir = tempfile.mkdtemp(prefix="stealth_guide_prim_")
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
                process_cleanup._kill_process_by_pid(pid, "test_agent_guide")
        except Exception:
            pass
        _safe_cleanup_dir(self.temp_dir)
        if hasattr(self, "server") and self.server:
            self.server.stop()

    async def get_state(self) -> Dict[str, Any]:
        """Read full application state from window.__state() as a parsed dict."""
        raw = await self.tab.evaluate("window.__state()")
        return json.loads(raw)

    async def test_execute_all_runnable_recipes(self) -> None:
        """Execute every runnable recipe against the mock SPA and assert read-back matches."""
        runnable = [r for r in agent_guide.INPUT_RECIPES if r.get("runnable")]
        self.assertGreaterEqual(len(runnable), 5)

        for recipe in runnable:
            tool_name = recipe["tool"]
            tool_obj = getattr(server, tool_name)
            args = dict(recipe["args_example"])

            # Call tool with instance_id and arguments
            await tool_obj.fn(self.instance_id, **args)

            # Read back state from mock application
            state = await self.get_state()
            state_key = recipe["state_key"]
            expected = recipe["expected_value"]
            actual = state["state"].get(state_key)

            self.assertEqual(
                actual,
                expected,
                f"Recipe '{recipe['id']}' failed: expected state['{state_key}'] == {expected!r}, got {actual!r}",
            )

    async def test_number_recipe_negative_case(self) -> None:
        """Verify fill on number input rejects non-numeric text with InputFormatError."""
        with self.assertRaises(InputFormatError) as cm:
            await server.fill.fn(
                self.instance_id,
                selector="#hours",
                value="not_numeric",
            )
        self.assertIn("numeric value", str(cm.exception))

    async def test_chips_interaction_proven_pattern(self) -> None:
        """Verify chips proven pattern: type prefix, wait for suggestion, click, remove."""
        await server.type_text.fn(
            self.instance_id,
            selector="#label-input",
            text="back",
            clear_first=True,
        )
        await server.wait_for_element.fn(
            self.instance_id,
            selector="#label-list li",
            timeout=3000,
        )
        await server.click_element.fn(self.instance_id, selector="#label-list li")
        state_1 = await self.get_state()
        self.assertIn("backend", state_1["state"]["labels"])

        # Remove chip
        await server.click_element.fn(
            self.instance_id,
            selector='span.chip[data-label="backend"] button.chip-x',
        )
        state_2 = await self.get_state()
        self.assertNotIn("backend", state_2["state"]["labels"])


if __name__ == "__main__":
    unittest.main()
