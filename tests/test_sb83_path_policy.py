"""SB-83 / review F7 and F12: confine file paths and block file: navigation."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stealth_browser_mcp.path_policy import (
    PathPolicyError,
    reject_blocked_url,
    resolve_managed_path,
    resolve_under_root,
    write_private_text,
)
from stealth_browser_mcp.server import export_debug_logs, navigate, new_tab, save_session, take_screenshot
from stealth_browser_mcp import session_state


class UrlSchemeTests(unittest.TestCase):
    def test_file_url_blocked_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            with self.assertRaisesRegex(PathPolicyError, "file"):
                reject_blocked_url("file:///etc/hostname")
            with self.assertRaisesRegex(PathPolicyError, "chrome"):
                reject_blocked_url("chrome://policy")
            with self.assertRaisesRegex(PathPolicyError, "devtools"):
                reject_blocked_url("devtools://devtools/bundled/inspector.html")
            self.assertEqual(reject_blocked_url("https://example.com/"), "https://example.com/")
            self.assertEqual(reject_blocked_url("about:blank"), "about:blank")

    def test_flag_allows_file_urls(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_FILE_URLS": "1"}):
            self.assertEqual(reject_blocked_url("file:///etc/hostname"), "file:///etc/hostname")

    def test_navigate_file_url_does_not_read_host_file(self) -> None:
        called = {"get_page_content": False, "navigate": False}

        async def fake_navigate(**kwargs):
            called["navigate"] = True
            return {"url": kwargs["url"]}

        async def fake_get_page_content(**kwargs):
            called["get_page_content"] = True
            return "secret"

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            with mock.patch("stealth_browser_mcp.server.browser_manager.navigate", fake_navigate):
                with mock.patch(
                    "stealth_browser_mcp.server.get_page_content",
                    fake_get_page_content,
                ):
                    with self.assertRaisesRegex(PathPolicyError, "file"):
                        asyncio.run(navigate.fn(instance_id="inst", url="file:///etc/hostname"))
        self.assertFalse(called["navigate"])
        self.assertFalse(called["get_page_content"])

    def test_new_tab_blocks_file_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            with self.assertRaisesRegex(Exception, "file"):
                asyncio.run(new_tab.fn(instance_id="inst", url="file:///etc/hostname"))


class PathConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "data"
        self.root.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {"STEALTH_BROWSER_MCP_DATA_DIR": str(self.root)},
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_outside_path_rejected(self) -> None:
        with self.assertRaisesRegex(PathPolicyError, "must stay under"):
            resolve_managed_path("/etc/hostname")

    def test_symlink_escape_rejected(self) -> None:
        link = self.root / "escape"
        link.symlink_to("/etc")
        with self.assertRaisesRegex(PathPolicyError, "must stay under"):
            resolve_managed_path(str(link / "hostname"))

    def test_path_under_data_dir_works_and_is_0600(self) -> None:
        dest = self.root / "exports" / "ok.json"
        written = write_private_text(dest, "{\"ok\": true}")
        self.assertTrue(written.is_file())
        self.assertEqual(written.parent.resolve(), (self.root / "exports").resolve())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)

    def test_save_session_rejects_outside_path(self) -> None:
        with self.assertRaises(Exception):
            session_state.write_session_file("/etc/stealth-session-pwn.json", {"cookies": []})
        self.assertFalse(Path("/etc/stealth-session-pwn.json").exists())

    def test_save_session_under_data_dir(self) -> None:
        dest = self.root / "sessions" / "mine.json"
        path = session_state.write_session_file(dest, {"cookies": []})
        self.assertTrue(path.is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_export_debug_logs_rejects_outside_path(self) -> None:
        with self.assertRaises(PathPolicyError):
            asyncio.run(export_debug_logs.fn(filename="/etc/stealth-debug-pwn.json"))
        self.assertFalse(Path("/etc/stealth-debug-pwn.json").exists())


if __name__ == "__main__":
    unittest.main()
