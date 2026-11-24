"""F31: refresh_seed from a real browser requires a flag in HTTP mode."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stealth_browser_mcp import seed_tools, server


class RefreshSeedHttpFlagTests(unittest.IsolatedAsyncioTestCase):
    """Reproduce F31: a session with no flags can copy the owner's cookies."""

    def setUp(self) -> None:
        self._prev_http = server._HTTP_MODE
        self._prev_bind = server._HTTP_BIND_HOST
        server._HTTP_MODE = True
        server._HTTP_BIND_HOST = "127.0.0.1"

    def tearDown(self) -> None:
        server._HTTP_MODE = self._prev_http
        server._HTTP_BIND_HOST = self._prev_bind

    async def test_http_without_flag_does_not_copy_cookies(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_SEED_REFRESH", None)
            os.environ.pop("STEALTH_ALLOW_SEED_PASSWORDS", None)
            with mock.patch("stealth_browser_mcp.server.seed_tools.refresh_seed") as refresh:
                result = await server.refresh_seed.fn(
                    name="stolen",
                    source_browser="chrome",
                )
        self.assertEqual(result["status"], "error")
        self.assertIn("STEALTH_ALLOW_SEED_REFRESH", result["error"])
        refresh.assert_not_called()

    async def test_http_with_flag_on_loopback_is_allowed(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_SEED_REFRESH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.http_auth.request_appears_proxied",
                return_value=False,
            ), mock.patch(
                "stealth_browser_mcp.server.seed_tools.refresh_seed",
                return_value={"status": "ok", "copied": 1},
            ) as refresh:
                result = await server.refresh_seed.fn(
                    name="mine",
                    source_browser="chrome",
                )
        self.assertEqual(result["status"], "ok")
        refresh.assert_called_once()

    async def test_http_with_flag_is_refused_when_proxied(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_SEED_REFRESH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.http_auth.request_appears_proxied",
                return_value=True,
            ), mock.patch("stealth_browser_mcp.server.seed_tools.refresh_seed") as refresh:
                result = await server.refresh_seed.fn(
                    name="mine",
                    source_browser="chrome",
                )
        self.assertEqual(result["status"], "error")
        self.assertIn("proxied", result["error"].lower())
        refresh.assert_not_called()

    async def test_http_source_dir_outside_browser_roots_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_SEED_REFRESH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.http_auth.request_appears_proxied",
                return_value=False,
            ), mock.patch("stealth_browser_mcp.server.seed_tools.refresh_seed") as refresh:
                result = await server.refresh_seed.fn(
                    name="stolen",
                    source_browser="chrome",
                    source_dir="/tmp/not-a-browser-profile",
                )
        self.assertEqual(result["status"], "error")
        self.assertIn("known browser profile root", result["error"])
        refresh.assert_not_called()


class SeedSourceDirConfineTests(unittest.TestCase):
    def test_chrome_user_data_root_is_an_allowed_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            chrome = home / ".config" / "google-chrome"
            chrome.mkdir(parents=True)
            env = {"HOME": str(home)}
            self.assertTrue(
                seed_tools.is_allowed_seed_source_dir(
                    str(chrome),
                    platform="linux",
                    env=env,
                )
            )
            self.assertTrue(
                seed_tools.is_allowed_seed_source_dir(
                    str(chrome / "Default"),
                    platform="linux",
                    env=env,
                )
            )
            self.assertFalse(
                seed_tools.is_allowed_seed_source_dir(
                    str(Path(td) / "evil"),
                    platform="linux",
                    env=env,
                )
            )


if __name__ == "__main__":
    unittest.main()
