"""SB-101: verify spawn honors window=headless vs window=offscreen when display is present.

Free of real browsers and network.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
src_dir = str(ROOT / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions


class HeadlessLaunchWiringTests(unittest.TestCase):
    """Test spawn_browser config and args for window=headless vs window=offscreen."""

    def _spawn_and_capture(self, options: BrowserOptions) -> dict:
        captured: dict = {}

        async def fake_launch(config, timeout=30, args_modifier=None):
            captured["headless"] = config.headless
            captured["browser_args"] = list(config.browser_args or [])
            raise RuntimeError("stop after capturing launch args")

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            effective_options = options.model_copy(
                update={"user_data_dir": options.user_data_dir or str(profile)}
            )
            env_overrides = {
                "STEALTH_BROWSER_MCP_DATA_DIR": tmp,
                "STEALTH_MANAGED_PROFILES_ROOT": tmp,
                "DISPLAY": ":0",
                "WAYLAND_DISPLAY": "",
            }
            with patch.dict(os.environ, env_overrides), \
                 patch("stealth_browser_mcp.browser_manager.headed_display_error", return_value=None), \
                 patch("stealth_browser_mcp.browser_manager.check_browser_executable", return_value="/tmp/fake-chrome"), \
                 patch.object(BrowserManager, "_resolve_headless_user_agent", new_callable=AsyncMock, return_value="FakeUserAgent/1.0"), \
                 patch("stealth_browser_mcp.browser_manager.launch_browser", side_effect=fake_launch):
                manager = BrowserManager()
                with self.assertRaises(Exception) as ctx:
                    asyncio.run(manager.spawn_browser(effective_options))
                self.assertIn("stop after capturing launch args", str(ctx.exception))

        return captured

    def test_spawn_window_headless(self) -> None:
        """Verify window=headless sets config.headless=True and omits offscreen position."""
        captured = self._spawn_and_capture(BrowserOptions(window="headless"))
        self.assertTrue(captured["headless"])
        self.assertNotIn("--window-position=-32000,-32000", captured["browser_args"])

    def test_spawn_window_offscreen(self) -> None:
        """Verify window=offscreen sets config.headless=False and includes offscreen position."""
        captured = self._spawn_and_capture(BrowserOptions(window="offscreen"))
        self.assertFalse(captured["headless"])
        self.assertIn("--window-position=-32000,-32000", captured["browser_args"])

    def test_spawn_headless_flag_true(self) -> None:
        """Verify headless=True flag sets config.headless=True and omits offscreen position."""
        captured = self._spawn_and_capture(BrowserOptions(headless=True))
        self.assertTrue(captured["headless"])
        self.assertNotIn("--window-position=-32000,-32000", captured["browser_args"])


if __name__ == "__main__":
    unittest.main()
