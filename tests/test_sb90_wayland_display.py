"""SB-90: headed Chrome on Wayland without a DRM render node."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.window_mode import (
    HEADED_WAYLAND_NO_DISPLAY_ERROR,
    X11_OZONE_ARG,
    drm_render_node_available,
    headed_display_args,
    headed_display_error,
)


class DrmRenderNodeTests(unittest.TestCase):
    """Test render-node detection (a card device alone is not enough)."""

    def test_missing_dir_is_false(self) -> None:
        """Verify a missing dri directory is treated as no render node."""
        self.assertFalse(drm_render_node_available(Path("/no/such/dri-dir")))

    def test_card_only_is_false(self) -> None:
        """Verify a DRI card device without renderD* is not enough."""
        with tempfile.TemporaryDirectory() as tmp:
            dri = Path(tmp)
            (dri / "card1").touch()
            self.assertFalse(drm_render_node_available(dri))

    def test_render_node_is_true(self) -> None:
        """Verify a renderD* node is detected."""
        with tempfile.TemporaryDirectory() as tmp:
            dri = Path(tmp)
            (dri / "card1").touch()
            (dri / "renderD128").touch()
            self.assertTrue(drm_render_node_available(dri))


class HeadedDisplayArgsTests(unittest.TestCase):
    """Test when spawn should force X11 ozone."""

    def test_wayland_without_render_node_uses_x11_when_display_set(self) -> None:
        """Reproduce the hang setup: DISPLAY plus Wayland, no render node."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        for mode in ("normal", "offscreen", "minimized"):
            self.assertEqual(
                headed_display_args(
                    mode,
                    system="Linux",
                    env=env,
                    render_node_available=False,
                ),
                [X11_OZONE_ARG],
                msg=mode,
            )

    def test_working_wayland_is_left_alone(self) -> None:
        """Verify Chrome keeps Wayland when a render node exists."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(
            headed_display_args(
                "offscreen",
                system="Linux",
                env=env,
                render_node_available=True,
            ),
            [],
        )

    def test_headless_never_adds_ozone(self) -> None:
        """Verify headless launch args are unchanged."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(
            headed_display_args(
                "headless",
                system="Linux",
                env=env,
                render_node_available=False,
            ),
            [],
        )

    def test_no_display_does_not_add_ozone(self) -> None:
        """Verify X11 ozone is not added when DISPLAY is unset."""
        env = {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(
            headed_display_args(
                "normal",
                system="Linux",
                env=env,
                render_node_available=False,
            ),
            [],
        )

    def test_x11_only_does_not_need_the_flag(self) -> None:
        """Verify a plain X11 session does not get an extra ozone flag."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}
        self.assertEqual(
            headed_display_args(
                "normal",
                system="Linux",
                env=env,
                render_node_available=False,
            ),
            [],
        )

    def test_non_linux_is_unchanged(self) -> None:
        """Verify Windows and macOS do not get ozone args."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        for system in ("Windows", "Darwin"):
            self.assertEqual(
                headed_display_args(
                    "normal",
                    system=system,
                    env=env,
                    render_node_available=False,
                ),
                [],
                msg=system,
            )


class HeadedDisplayErrorTests(unittest.TestCase):
    """Test fail-fast when a headed window cannot open DevTools."""

    def test_wayland_only_without_render_node_fails_fast(self) -> None:
        """Reproduce the 30s hang: Wayland only, no render node, no DISPLAY."""
        env = {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}
        msg = headed_display_error(
            "normal",
            system="Linux",
            env=env,
            render_node_available=False,
        )
        self.assertEqual(msg, HEADED_WAYLAND_NO_DISPLAY_ERROR)
        self.assertIn("window=headless", msg)

    def test_display_set_is_not_an_error(self) -> None:
        """Verify DISPLAY plus missing render node is handled by the X11 flag, not an error."""
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertIsNone(
            headed_display_error(
                "offscreen",
                system="Linux",
                env=env,
                render_node_available=False,
            )
        )

    def test_headless_is_not_an_error(self) -> None:
        """Verify headless does not fail when Wayland is unusable."""
        env = {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertIsNone(
            headed_display_error(
                "headless",
                system="Linux",
                env=env,
                render_node_available=False,
            )
        )

    def test_working_wayland_only_is_not_an_error(self) -> None:
        """Verify Wayland without DISPLAY is fine when a render node exists."""
        env = {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertIsNone(
            headed_display_error(
                "normal",
                system="Linux",
                env=env,
                render_node_available=True,
            )
        )


class SpawnWiringTests(unittest.TestCase):
    """Test spawn_browser applies the fail-fast error and the X11 flag."""

    def test_spawn_raises_before_launch_when_display_unusable(self) -> None:
        """Verify spawn_browser raises immediately, without waiting on Chrome."""
        from stealth_browser_mcp.browser_manager import BrowserManager

        manager = BrowserManager()
        with patch(
            "stealth_browser_mcp.browser_manager.headed_display_error",
            return_value=HEADED_WAYLAND_NO_DISPLAY_ERROR,
        ), patch(
            "stealth_browser_mcp.browser_manager.launch_browser",
            new_callable=AsyncMock,
        ) as launch:
            with self.assertRaises(Exception) as ctx:
                asyncio.run(manager.spawn_browser(BrowserOptions(window="normal")))
        self.assertEqual(str(ctx.exception), HEADED_WAYLAND_NO_DISPLAY_ERROR)
        launch.assert_not_called()

    def test_spawn_adds_x11_ozone_arg(self) -> None:
        """Verify spawn_browser passes --ozone-platform=x11 through to Chrome."""
        from stealth_browser_mcp.browser_manager import BrowserManager

        captured: dict = {}

        async def fake_launch(config, timeout=30, args_modifier=None):
            captured["args"] = list(config.browser_args or [])
            raise RuntimeError("stop after capturing launch args")

        with tempfile.TemporaryDirectory() as tmp:
            prev_data = os.environ.get("STEALTH_BROWSER_MCP_DATA_DIR")
            prev_root = os.environ.get("STEALTH_MANAGED_PROFILES_ROOT")
            os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = tmp
            os.environ["STEALTH_MANAGED_PROFILES_ROOT"] = tmp
            profile = Path(tmp) / "p"
            profile.mkdir()
            try:
                manager = BrowserManager()
                with patch(
                    "stealth_browser_mcp.browser_manager.headed_display_error",
                    return_value=None,
                ), patch(
                    "stealth_browser_mcp.browser_manager.headed_display_args",
                    return_value=[X11_OZONE_ARG],
                ), patch(
                    "stealth_browser_mcp.browser_manager.launch_browser",
                    side_effect=fake_launch,
                ):
                    with self.assertRaises(Exception):
                        asyncio.run(
                            manager.spawn_browser(
                                BrowserOptions(
                                    window="offscreen",
                                    user_data_dir=str(profile),
                                )
                            )
                        )
            finally:
                if prev_data is None:
                    os.environ.pop("STEALTH_BROWSER_MCP_DATA_DIR", None)
                else:
                    os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = prev_data
                if prev_root is None:
                    os.environ.pop("STEALTH_MANAGED_PROFILES_ROOT", None)
                else:
                    os.environ["STEALTH_MANAGED_PROFILES_ROOT"] = prev_root
        self.assertIn(X11_OZONE_ARG, captured.get("args") or [])
