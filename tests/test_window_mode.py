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
from stealth_browser_mcp.js_values import to_python
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.window_mode import (
    DEFAULT_WINDOW_MODE,
    WINDOW_ENV,
    WINDOW_MODES,
    apply_window_mode,
    display_available,
    effective_window_mode,
    format_window_bounds,
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
        def __init__(self, bounds: Optional[Any] = None, should_fail: bool = False, clamp: bool = False):
            self.commands: List[Any] = []
            self.sent_messages: List[Dict[str, Any]] = []
            self.bounds = bounds if bounds is not None else cdp.browser.Bounds(
                left=-32000,
                top=-32000,
                width=1920,
                height=1080,
                window_state=cdp.browser.WindowState.NORMAL,
            )
            self.should_fail = should_fail
            self.clamp = clamp

        async def send(self, cmd: Any) -> Any:
            if self.should_fail:
                raise RuntimeError("CDP command failed")
            self.commands.append(cmd)
            method = None
            params = {}
            if hasattr(cmd, "__next__"):
                try:
                    msg = next(cmd)
                    if isinstance(msg, dict):
                        self.sent_messages.append(msg)
                        method = msg.get("method")
                        params = msg.get("params", {})
                except Exception:
                    pass
            elif isinstance(cmd, dict):
                self.sent_messages.append(cmd)
                method = cmd.get("method")
                params = cmd.get("params", {})
            if method == "Browser.getWindowForTarget":
                return (cdp.browser.WindowID(1), self.bounds)
            if method == "Browser.getWindowBounds":
                return self.bounds
            if method == "Browser.setWindowBounds":
                return None
            return (cdp.browser.WindowID(1), self.bounds)

    async def test_normal_mode_sends_nothing(self) -> None:
        """Verify normal mode executes no CDP commands."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "normal", 1280, 800)
        self.assertEqual(result, {"mode": "normal", "applied": []})
        self.assertEqual(len(tab.commands), 0)

    async def test_minimized_mode_steps(self) -> None:
        """Verify minimized mode sets normal first, bounds, then minimized, with bounds read back."""
        tab = self.FakeTab(bounds=cdp.browser.Bounds(
            left=0,
            top=0,
            width=1050,
            height=844,
            window_state=cdp.browser.WindowState.MINIMIZED,
        ))
        result = await apply_window_mode(tab, "minimized", 1280, 800)
        self.assertEqual(result["mode"], "minimized")
        self.assertIn("get_window_for_target", result["applied"])
        self.assertIn("set_window_bounds", result["applied"])
        self.assertIn("get_window_bounds", result["applied"])
        self.assertIn("set_focus_emulation_enabled", result["applied"])
        self.assertEqual(len(tab.commands), 6)
        self.assertIn("window_bounds", result)
        self.assertEqual(result["window_bounds"]["windowState"], "minimized")
        self.assertEqual(result["window_bounds"]["width"], 1050)
        self.assertEqual(result["window_bounds"]["height"], 844)

        # Assert order and values of setWindowBounds calls
        set_bounds_calls = [m for m in tab.sent_messages if m.get("method") == "Browser.setWindowBounds"]
        self.assertEqual(len(set_bounds_calls), 3)
        self.assertEqual(set_bounds_calls[0]["params"]["bounds"], {"windowState": "normal"})
        self.assertEqual(set_bounds_calls[1]["params"]["bounds"], {"left": 0, "top": 0, "width": 1280, "height": 800})
        self.assertEqual(set_bounds_calls[2]["params"]["bounds"], {"windowState": "minimized"})

    async def test_offscreen_mode_steps(self) -> None:
        """Verify offscreen mode sets normal first, then bounds at (-32000, -32000), then reads bounds."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "offscreen", 1920, 1080)
        self.assertEqual(result["mode"], "offscreen")
        self.assertIn("get_window_for_target", result["applied"])
        self.assertIn("set_window_bounds", result["applied"])
        self.assertIn("get_window_bounds", result["applied"])
        self.assertIn("set_focus_emulation_enabled", result["applied"])
        self.assertEqual(len(tab.commands), 5)
        self.assertIn("window_bounds", result)
        self.assertEqual(result["window_bounds"]["left"], -32000)
        self.assertEqual(result["window_bounds"]["top"], -32000)
        self.assertEqual(result["window_bounds"]["width"], 1920)
        self.assertEqual(result["window_bounds"]["height"], 1080)
        self.assertIsNone(result.get("window_note"))

        # Assert order and values of setWindowBounds calls
        set_bounds_calls = [m for m in tab.sent_messages if m.get("method") == "Browser.setWindowBounds"]
        self.assertEqual(len(set_bounds_calls), 2)
        self.assertEqual(set_bounds_calls[0]["params"]["bounds"], {"windowState": "normal"})
        self.assertEqual(
            set_bounds_calls[1]["params"]["bounds"],
            {"left": -32000, "top": -32000, "width": 1920, "height": 1080},
        )

    async def test_offscreen_clamped_reports_window_note(self) -> None:
        """Verify clamped offscreen positioning produces diagnostic window_note."""
        clamped_bounds = cdp.browser.Bounds(
            left=0,
            top=0,
            width=1469,
            height=832,
            window_state=cdp.browser.WindowState.NORMAL,
        )
        tab = self.FakeTab(bounds=clamped_bounds, clamp=True)
        result = await apply_window_mode(tab, "offscreen", 1920, 1080)
        self.assertEqual(result["mode"], "offscreen")
        self.assertIn("window_bounds", result)
        self.assertEqual(result["window_bounds"]["left"], 0)
        self.assertEqual(result["window_bounds"]["top"], 0)
        self.assertEqual(
            result["window_note"],
            "window clamped to 0,0; use window=minimized or headless if it is visible",
        )

    async def test_headless_mode_steps(self) -> None:
        """Verify headless mode sends focus emulation only."""
        tab = self.FakeTab()
        result = await apply_window_mode(tab, "headless", 1280, 800)
        self.assertEqual(result["mode"], "headless")
        self.assertEqual(result["applied"], ["set_focus_emulation_enabled"])
        self.assertEqual(len(tab.commands), 1)
        self.assertNotIn("window_bounds", result)

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


class TestFormatWindowBounds(unittest.TestCase):
    """Test format_window_bounds normalization."""

    def test_format_cdp_bounds_object(self) -> None:
        """Verify CDP Bounds instance is formatted correctly."""
        bounds = cdp.browser.Bounds(
            left=-32000,
            top=-32000,
            width=1920,
            height=1080,
            window_state=cdp.browser.WindowState.NORMAL,
        )
        formatted = format_window_bounds(bounds)
        self.assertEqual(formatted, {
            "left": -32000,
            "top": -32000,
            "width": 1920,
            "height": 1080,
            "windowState": "normal",
        })

    def test_format_dict_bounds(self) -> None:
        """Verify raw dictionary bounds are formatted correctly."""
        d = {"left": 0, "top": 0, "width": 1280, "height": 800, "windowState": "minimized"}
        self.assertEqual(format_window_bounds(d), d)

    def test_format_none_returns_none(self) -> None:
        """Verify None returns None."""
        self.assertIsNone(format_window_bounds(None))

    def test_format_tuple_bounds(self) -> None:
        """Verify (window_id, bounds) tuple returns formatted bounds."""
        bounds = cdp.browser.Bounds(left=10, top=20, width=800, height=600)
        self.assertEqual(
            format_window_bounds((1, bounds)),
            {"left": 10, "top": 20, "width": 800, "height": 600, "windowState": None},
        )


class TestSpawnBrowserWindowBoundsInResult(unittest.IsolatedAsyncioTestCase):
    """Test spawn_browser return dict includes window_bounds and window_note."""

    async def test_spawn_browser_returns_window_bounds_for_headful_modes(self) -> None:
        """Verify spawn_browser tool result includes window_bounds for headful instances."""
        from stealth_browser_mcp.models import BrowserInstance, BrowserState

        fake_instance = BrowserInstance(
            instance_id="fake-id",
            headless=False,
            window="offscreen",
            window_note="window clamped to 0,0; use window=minimized or headless if it is visible",
            window_bounds={"left": 0, "top": 0, "width": 1920, "height": 1080, "windowState": "normal"},
            state=BrowserState.READY,
        )

        with mock.patch.object(server.browser_manager, "spawn_browser", return_value=fake_instance):
            with mock.patch.object(server.browser_manager, "get_spawn_diagnostics", return_value={}):
                with mock.patch.object(server.browser_manager, "get_session_info", return_value=None):
                    result = await server.spawn_browser.fn(window="offscreen")
                    self.assertIn("window_bounds", result)
                    self.assertEqual(result["window_bounds"]["left"], 0)
                    self.assertEqual(result["window_bounds"]["windowState"], "normal")
                    self.assertIn("window_note", result)
                    self.assertEqual(
                        result["window_note"],
                        "window clamped to 0,0; use window=minimized or headless if it is visible",
                    )


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestGatedRealBrowserWindowModes(unittest.TestCase):
    """Gated integration tests with real browser instances across window modes."""

    def test_gated_real_browser_modes(self) -> None:
        """Spawn real browser instances in normal, offscreen, minimized, and headless modes."""
        async def _run() -> None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="stealth-gated-window-"))
            manager = BrowserManager()

            async def _eval_window_size(tab: Any) -> Dict[str, Any]:
                raw = await tab.evaluate(
                    "({width: window.outerWidth, height: window.outerHeight})",
                    return_by_value=True,
                )
                val = to_python(raw)
                w = None
                h = None
                if isinstance(val, dict):
                    w = val.get("width")
                    h = val.get("height")
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            k, v = item
                            if k == "width":
                                w = v
                            elif k == "height":
                                h = v
                if isinstance(w, dict) and "value" in w:
                    w = w["value"]
                if isinstance(h, dict) and "value" in h:
                    h = h["value"]
                if not isinstance(w, (int, float)):
                    raw_w = await tab.evaluate("window.outerWidth", return_by_value=True)
                    val_w = to_python(raw_w)
                    if isinstance(val_w, (int, float)):
                        w = val_w
                    elif isinstance(val_w, dict) and "value" in val_w:
                        w = val_w["value"]
                if not isinstance(h, (int, float)):
                    raw_h = await tab.evaluate("window.outerHeight", return_by_value=True)
                    val_h = to_python(raw_h)
                    if isinstance(val_h, (int, float)):
                        h = val_h
                    elif isinstance(val_h, dict) and "value" in val_h:
                        h = val_h["value"]
                return {"width": w, "height": h, "raw": raw}

            async def _eval_has_focus(tab: Any) -> bool:
                raw = await tab.evaluate("document.hasFocus()", return_by_value=True)
                val = to_python(raw)
                if isinstance(val, dict) and "value" in val:
                    val = val["value"]
                return bool(val)

            try:
                # 1. Normal mode: outer width within 100px of requested viewport width
                d_norm = tmp_dir / "norm"
                d_norm.mkdir(parents=True, exist_ok=True)
                opts_norm = BrowserOptions(
                    window="normal",
                    viewport_width=1280,
                    viewport_height=800,
                    user_data_dir=str(d_norm),
                    sandbox=False,
                )
                inst_norm = await manager.spawn_browser(opts_norm)
                self.assertIsNotNone(inst_norm.window_bounds)
                tab_norm = await manager.get_tab(inst_norm.instance_id)
                self.assertIsNotNone(tab_norm)
                res_norm = await _eval_window_size(tab_norm)
                outer_w_norm = res_norm.get("width")
                self.assertIsNotNone(outer_w_norm)
                self.assertLessEqual(abs(outer_w_norm - 1280), 100)
                await manager.close_instance(inst_norm.instance_id)

                # 2. Offscreen mode: outer width within 100px of requested viewport width
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
                self.assertIsNotNone(inst_off.window_bounds)
                tab_off = await manager.get_tab(inst_off.instance_id)
                self.assertIsNotNone(tab_off)
                res_off = await _eval_window_size(tab_off)
                outer_w_off = res_off.get("width")
                self.assertIsNotNone(outer_w_off)
                self.assertLessEqual(abs(outer_w_off - 1280), 100)

                has_focus_off = await _eval_has_focus(tab_off)
                self.assertTrue(has_focus_off)
                await manager.close_instance(inst_off.instance_id)

                # 3. Minimized mode: assert execute_script works
                d_min = tmp_dir / "min"
                d_min.mkdir(parents=True, exist_ok=True)
                opts_min = BrowserOptions(
                    window="minimized",
                    viewport_width=1280,
                    viewport_height=800,
                    user_data_dir=str(d_min),
                    sandbox=False,
                )
                inst_min = await manager.spawn_browser(opts_min)
                self.assertIsNotNone(inst_min.window_bounds)
                tab_min = await manager.get_tab(inst_min.instance_id)
                self.assertIsNotNone(tab_min)
                res_min = await _eval_window_size(tab_min)
                self.assertIsNotNone(res_min.get("width"))
                has_focus_min = await _eval_has_focus(tab_min)
                self.assertTrue(has_focus_min)
                await manager.close_instance(inst_min.instance_id)

                # 4. Headless mode: assert execute_script works
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
                res_head = await _eval_window_size(tab_head)
                self.assertIsNotNone(res_head.get("width"))
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
