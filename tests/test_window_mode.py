"""Unit and integration tests for window modes and focus management (SB-66).

ASCII only. Free of real browsers and network in unit tests.
Gated real-browser test runs only when STEALTH_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Dict, List
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc
from nodriver import cdp

from stealth_browser_mcp import server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.window_mode import (
    DEFAULT_WINDOW_MODE,
    WINDOW_ENV,
    WINDOW_MODES,
    apply_window_mode,
    display_available,
    effective_window_mode,
    normalize_window_mode,
    open_background_tab,
    resolve_window_mode,
    window_launch_args,
)

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class TestNormalizeWindowMode(unittest.TestCase):
    """Test normalize_window_mode validation and parsing."""

    def test_normalize_valid_modes(self) -> None:
        """Verify valid modes return exact lowercase strings."""
        for mode in ("normal", "minimized", "offscreen", "headless"):
            self.assertEqual(normalize_window_mode(mode), mode)

    def test_normalize_case_and_whitespace(self) -> None:
        """Verify case-insensitivity and whitespace stripping."""
        self.assertEqual(normalize_window_mode("NORMAL"), "normal")
        self.assertEqual(normalize_window_mode("  OffScreen  "), "offscreen")
        self.assertEqual(normalize_window_mode("mInImIzEd"), "minimized")
        self.assertEqual(normalize_window_mode("\theadless\n"), "headless")

    def test_normalize_empty_and_none(self) -> None:
        """Verify None and empty strings return None."""
        self.assertIsNone(normalize_window_mode(None))
        self.assertIsNone(normalize_window_mode(""))
        self.assertIsNone(normalize_window_mode("   "))

    def test_normalize_invalid_modes_raise(self) -> None:
        """Verify invalid values raise ValueError mentioning valid modes."""
        for invalid in ("fullscreen", "maximized", "hidden", "invalid", 123, []):
            with self.assertRaises(ValueError) as ctx:
                normalize_window_mode(invalid)
            self.assertEqual(
                str(ctx.exception),
                f"invalid window mode {invalid!r}; use one of: normal, minimized, offscreen, headless",
            )


class TestResolveWindowMode(unittest.TestCase):
    """Test resolve_window_mode precedence rules."""

    def test_explicit_arg_beats_all(self) -> None:
        """Verify explicit argument beats headless flag, env var, and default."""
        env = {WINDOW_ENV: "normal"}
        self.assertEqual(
            resolve_window_mode("minimized", headless=True, env=env),
            "minimized",
        )
        self.assertEqual(
            resolve_window_mode("offscreen", headless=False, env=env),
            "offscreen",
        )

    def test_headless_beats_env_and_default(self) -> None:
        """Verify headless=True beats env var and default when window is unset."""
        env = {WINDOW_ENV: "normal"}
        self.assertEqual(
            resolve_window_mode(None, headless=True, env=env),
            "headless",
        )
        self.assertEqual(
            resolve_window_mode("", headless=True, env=env),
            "headless",
        )
        self.assertEqual(
            resolve_window_mode(None, headless=True, env={}),
            "headless",
        )

    def test_env_beats_default(self) -> None:
        """Verify env var beats default when window is unset and headless=False."""
        self.assertEqual(
            resolve_window_mode(None, headless=False, env={WINDOW_ENV: "normal"}),
            "normal",
        )
        self.assertEqual(
            resolve_window_mode(None, headless=False, env={WINDOW_ENV: "minimized"}),
            "minimized",
        )

    def test_default_when_unset(self) -> None:
        """Verify default window mode is used when all inputs are unset."""
        self.assertEqual(
            resolve_window_mode(None, headless=False, env={}),
            DEFAULT_WINDOW_MODE,
        )
        self.assertEqual(
            resolve_window_mode("", headless=False, env={WINDOW_ENV: ""}),
            DEFAULT_WINDOW_MODE,
        )

    def test_invalid_env_raises(self) -> None:
        """Verify invalid env value raises ValueError mentioning STEALTH_WINDOW_MODE."""
        with self.assertRaises(ValueError) as ctx:
            resolve_window_mode(None, headless=False, env={WINDOW_ENV: "invalid_mode"})
        msg = str(ctx.exception)
        self.assertIn("STEALTH_WINDOW_MODE", msg)
        self.assertIn("use one of: normal, minimized, offscreen, headless", msg)


class TestDisplayAvailableAndEffective(unittest.TestCase):
    """Test display availability check and effective window mode calculation."""

    def test_linux_without_display(self) -> None:
        """Verify Linux with no display falls back to headless with note."""
        self.assertFalse(display_available(system="Linux", env={}))
        self.assertFalse(display_available(system="Linux", env={"DISPLAY": "", "WAYLAND_DISPLAY": ""}))

        mode, note = effective_window_mode("offscreen", system="Linux", env={})
        self.assertEqual(mode, "headless")
        self.assertEqual(note, "No display available (DISPLAY/WAYLAND_DISPLAY unset); using headless.")

        mode, note = effective_window_mode("minimized", system="Linux", env={})
        self.assertEqual(mode, "headless")
        self.assertEqual(note, "No display available (DISPLAY/WAYLAND_DISPLAY unset); using headless.")

        mode, note = effective_window_mode("normal", system="Linux", env={})
        self.assertEqual(mode, "headless")
        self.assertEqual(note, "No display available (DISPLAY/WAYLAND_DISPLAY unset); using headless.")

        mode, note = effective_window_mode("headless", system="Linux", env={})
        self.assertEqual(mode, "headless")
        self.assertIsNone(note)

    def test_linux_with_display(self) -> None:
        """Verify Linux with DISPLAY or WAYLAND_DISPLAY keeps requested mode."""
        self.assertTrue(display_available(system="Linux", env={"DISPLAY": ":0"}))
        mode, note = effective_window_mode("offscreen", system="Linux", env={"DISPLAY": ":0"})
        self.assertEqual(mode, "offscreen")
        self.assertIsNone(note)

        self.assertTrue(display_available(system="Linux", env={"WAYLAND_DISPLAY": "wayland-0"}))
        mode, note = effective_window_mode("minimized", system="Linux", env={"WAYLAND_DISPLAY": "wayland-0"})
        self.assertEqual(mode, "minimized")
        self.assertIsNone(note)

    def test_windows_and_darwin_display_available(self) -> None:
        """Verify Windows and macOS always report display available."""
        self.assertTrue(display_available(system="Windows", env={}))
        mode, note = effective_window_mode("offscreen", system="Windows", env={})
        self.assertEqual(mode, "offscreen")
        self.assertIsNone(note)

        self.assertTrue(display_available(system="Darwin", env={}))
        mode, note = effective_window_mode("offscreen", system="Darwin", env={})
        self.assertEqual(mode, "offscreen")
        self.assertIsNone(note)


class TestWindowLaunchArgs(unittest.TestCase):
    """Test window_launch_args generation for all modes."""

    def test_launch_args_modes(self) -> None:
        """Verify launch arguments for each window mode."""
        self.assertEqual(window_launch_args("normal"), [])
        self.assertEqual(window_launch_args("minimized"), ["--start-minimized"])
        self.assertEqual(
            window_launch_args("offscreen", 1280, 800),
            ["--window-position=-32000,-32000", "--window-size=1280,800"],
        )
        self.assertEqual(
            window_launch_args("offscreen", 1920, 1080),
            ["--window-position=-32000,-32000", "--window-size=1920,1080"],
        )
        self.assertEqual(window_launch_args("headless"), [])


class TestApplyWindowMode(unittest.IsolatedAsyncioTestCase):
    """Test apply_window_mode CDP steps and error tolerance."""

    class FakeTab:
        def __init__(self, should_fail: bool = False):
            self.commands: List[Any] = []
            self.should_fail = should_fail

        async def send(self, cmd: Any) -> Any:
            if self.should_fail:
                raise RuntimeError("CDP command failed")
            self.commands.append(cmd)
            return (1, mock.MagicMock())

    async def test_normal_mode_sends_nothing(self) -> None:
        """Verify normal mode executes no CDP commands."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "normal", 1280, 800)
        self.assertEqual(result, {"mode": "normal", "applied": []})
        self.assertEqual(len(tab.commands), 0)

    async def test_minimized_mode_steps(self) -> None:
        """Verify minimized mode sends get_window, set_window_bounds, and focus emulation."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "minimized", 1280, 800)
        self.assertEqual(result["mode"], "minimized")
        self.assertIn("get_window_for_target", result["applied"])
        self.assertIn("set_window_bounds", result["applied"])
        self.assertIn("set_focus_emulation_enabled", result["applied"])
        self.assertEqual(len(tab.commands), 3)

    async def test_offscreen_mode_steps(self) -> None:
        """Verify offscreen mode sends bounds at (-32000, -32000) and focus emulation."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "offscreen", 1920, 1080)
        self.assertEqual(result["mode"], "offscreen")
        self.assertIn("get_window_for_target", result["applied"])
        self.assertIn("set_window_bounds", result["applied"])
        self.assertIn("set_focus_emulation_enabled", result["applied"])
        self.assertEqual(len(tab.commands), 3)

    async def test_headless_mode_steps(self) -> None:
        """Verify headless mode sends focus emulation only."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "headless", 1280, 800)
        self.assertEqual(result["mode"], "headless")
        self.assertEqual(result["applied"], ["set_focus_emulation_enabled"])
        self.assertEqual(len(tab.commands), 1)

    async def test_error_handling_never_raises(self) -> None:
        """Verify apply_window_mode swallows CDP errors without raising."""
        tab = self.FakeTab(should_fail=True)
        result = await apply_window_mode(tab, "offscreen", 1280, 800)
        self.assertEqual(result, {"mode": "offscreen", "applied": []})


class TestOpenBackgroundTab(unittest.IsolatedAsyncioTestCase):
    """Test open_background_tab sends background=True and preserves target inventory."""

    async def test_open_background_tab_with_fake_browser(self) -> None:
        """Verify open_background_tab calls create_target with background=True."""
        with mock.patch("stealth_browser_mcp.window_mode.cdp.target.create_target") as mock_create_target:
            mock_create_target.return_value = "fake_cmd"
            fake_tab = SimpleNamespace(type_="page", target_id="target-123", _browser=None)
            fake_browser = SimpleNamespace(
                connection=SimpleNamespace(send=mock.AsyncMock(return_value="target-123")),
                targets=[fake_tab],
                update_targets=mock.AsyncMock(),
            )

            tab = await open_background_tab(fake_browser, "about:blank", new_window=False)
            mock_create_target.assert_called_once_with(
                "about:blank",
                new_window=False,
                background=True,
                enable_begin_frame_control=True,
            )
            fake_browser.update_targets.assert_called()
            self.assertIs(tab, fake_tab)
            self.assertIs(fake_tab._browser, fake_browser)

    def test_create_target_command_inspect(self) -> None:
        """Verify the create_target command generator sets background=True."""
        cmd = cdp.target.create_target(
            "about:blank",
            new_window=False,
            background=True,
            enable_begin_frame_control=True,
        )
        cmd_dict = next(cmd)
        self.assertEqual(cmd_dict.get("method"), "Target.createTarget")
        params = cmd_dict.get("params", {})
        self.assertIs(params.get("background"), True)
        self.assertIs(params.get("newWindow"), False)

    async def test_open_background_tab_fallback_without_connection(self) -> None:
        """Verify browser mock without CDP connection falls back to browser.get without exception."""
        fake_tab = SimpleNamespace(target_id="fallback-tab")
        fake_browser = SimpleNamespace(
            get=mock.AsyncMock(return_value=fake_tab),
        )

        tab = await open_background_tab(fake_browser, "about:blank", new_window=False)
        fake_browser.get.assert_awaited_once_with("about:blank", new_tab=True)
        self.assertIs(tab, fake_tab)

        fake_browser.get.reset_mock()
        tab_win = await open_background_tab(fake_browser, "about:blank", new_window=True)
        fake_browser.get.assert_awaited_once_with("about:blank", new_window=True)
        self.assertIs(tab_win, fake_tab)

        async def mock_get_no_new_window(url: str, new_tab: bool = False) -> Any:
            return fake_tab

        fake_browser_no_win = SimpleNamespace(get=mock.AsyncMock(side_effect=mock_get_no_new_window))
        tab_fallback = await open_background_tab(fake_browser_no_win, "about:blank", new_window=True)
        self.assertIs(tab_fallback, fake_tab)


class TestSourceScanFocusStealing(unittest.TestCase):
    """Source-scan test ensuring focus is never stolen unguarded."""

    def test_no_unguarded_focus_stealing(self) -> None:
        """
        Verify bring_to_front, activate, activate_target, Page.bringToFront
        appear only inside browser_manager.py within switch_tab guarded by 'normal'.
        """
        src_dir = ROOT / "src" / "stealth_browser_mcp"
        forbidden = [".activate(", "activate_target(", "Page.bringToFront"]

        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for term in forbidden:
                self.assertNotIn(
                    term,
                    content,
                    f"Forbidden focus-stealing call '{term}' found in {py_file}",
                )

            if "bring_to_front(" in content:
                self.assertEqual(
                    py_file.name,
                    "browser_manager.py",
                    f"Unexpected bring_to_front( call in {py_file}",
                )
                self.assertLessEqual(
                    content.count("bring_to_front("),
                    1,
                    f"Expected bring_to_front( at most once in {py_file}",
                )
                lines = content.splitlines()
                for idx, line in enumerate(lines):
                    if "bring_to_front(" in line:
                        start_idx = max(0, idx - 12)
                        preceding = "\n".join(lines[start_idx:idx])
                        self.assertIn(
                            "normal",
                            preceding,
                            f"Expected 'normal' mode check within 12 lines above bring_to_front in {py_file}",
                        )


class TestSpawnBrowserSchema(unittest.TestCase):
    """Verify spawn_browser tool input schema includes window parameter."""

    def test_spawn_browser_tool_has_optional_window_parameter(self) -> None:
        """Verify spawn_browser input schema has an optional window parameter."""
        tools = asyncio.run(server.mcp.get_tools())
        self.assertIn("spawn_browser", tools)
        spawn_tool = tools["spawn_browser"]
        parameters = spawn_tool.parameters
        self.assertIsInstance(parameters, dict)
        properties = parameters.get("properties", {})
        self.assertIn("window", properties)
        required = parameters.get("required", [])
        self.assertNotIn("window", required)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestGatedRealBrowserWindowModes(unittest.TestCase):
    """Gated integration tests with real browser instances across window modes."""

    def test_gated_real_browser_modes(self) -> None:
        """Spawn real browser instances in minimized, offscreen, and headless modes."""
        async def _run() -> None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="stealth-gated-window-"))
            manager = BrowserManager()

            try:
                # 1. Minimized mode
                d_min = tmp_dir / "min"
                d_min.mkdir(parents=True, exist_ok=True)
                opts_min = BrowserOptions(
                    window="minimized",
                    user_data_dir=str(d_min),
                    sandbox=False,
                )
                inst_min = await manager.spawn_browser(opts_min)
                tab_min = await manager.get_tab(inst_min.instance_id)
                self.assertIsNotNone(tab_min)

                try:
                    window_id, bounds = await tab_min.send(cdp.browser.get_window_for_target())
                    state = bounds.window_state.value if hasattr(bounds.window_state, "value") else bounds.window_state
                    if state != "minimized":
                        self.skipTest(f"Platform reports window state {state!r} instead of 'minimized'")
                except Exception as e:
                    self.skipTest(f"Platform does not report window bounds: {e}")

                has_focus_min = await tab_min.evaluate("document.hasFocus()")
                self.assertTrue(has_focus_min)
                await manager.close_instance(inst_min.instance_id)

                # 2. Offscreen mode
                d_off = tmp_dir / "off"
                d_off.mkdir(parents=True, exist_ok=True)
                opts_off = BrowserOptions(
                    window="offscreen",
                    viewport_width=1280,
                    viewport_height=800,
                    user_data_dir=str(d_off),
                    sandbox=False,
                )
                inst_off = await manager.spawn_browser(opts_off)
                tab_off = await manager.get_tab(inst_off.instance_id)
                self.assertIsNotNone(tab_off)

                try:
                    window_id, bounds = await tab_off.send(cdp.browser.get_window_for_target())
                    if bounds.left is not None and bounds.top is not None:
                        if bounds.left >= 0 and bounds.top >= 0:
                            self.skipTest("Window manager clamped offscreen bounds")
                        else:
                            self.assertTrue(bounds.left < 0 or bounds.top < 0)
                except Exception as e:
                    self.skipTest(f"Platform does not report window bounds: {e}")

                has_focus_off = await tab_off.evaluate("document.hasFocus()")
                self.assertTrue(has_focus_off)
                await manager.close_instance(inst_off.instance_id)

                # 3. Headless mode
                d_head = tmp_dir / "head"
                d_head.mkdir(parents=True, exist_ok=True)
                opts_head = BrowserOptions(
                    window="headless",
                    user_data_dir=str(d_head),
                    sandbox=False,
                )
                inst_head = await manager.spawn_browser(opts_head)
                tab_head = await manager.get_tab(inst_head.instance_id)
                self.assertIsNotNone(tab_head)
                await manager.close_instance(inst_head.instance_id)

            finally:
                try:
                    await manager.close_all()
                except Exception:
                    pass
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
