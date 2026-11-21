"""Tests for raw-CDP download_path workspace confinement and per-session download dir (SB-97 / F37)."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from typing import Optional
from unittest import mock

from stealth_browser_mcp.cdp_policy import CdpPolicyError, prepare_cdp_command
from stealth_browser_mcp.downloads import get_session_download_dir
from stealth_browser_mcp.mcp_sessions import set_current_mcp_session_id
from stealth_browser_mcp.server import execute_cdp_command


@contextmanager
def active_session(session_id: Optional[str]):
    """Activate an MCP session for the duration of a test block."""
    with set_current_mcp_session_id(session_id):
        with mock.patch(
            "stealth_browser_mcp.mcp_sessions.get_current_mcp_session_id",
            return_value=session_id,
        ):
            yield


class DownloadWorkspaceConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sb97_test_")
        self.data_dir = Path(self.tmp) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.session_a = "session_alpha_1111"
        self.session_b = "session_bravo_2222"

        # Create A and B workspaces
        self.a_ws = self.data_dir / "clients" / self.session_a
        self.a_sessions = self.a_ws / "sessions"
        self.a_sessions.mkdir(parents=True, exist_ok=True)

        self.b_ws = self.data_dir / "clients" / self.session_b
        self.b_downloads = self.b_ws / "downloads"
        self.b_downloads.mkdir(parents=True, exist_ok=True)

        # Create shared directories under data_dir
        self.seeds_dir = self.data_dir / "seeds"
        self.seeds_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.data_dir / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.shared_sessions_dir = self.data_dir / "sessions"
        self.shared_sessions_dir.mkdir(parents=True, exist_ok=True)
        self.shared_downloads_dir = self.data_dir / "downloads"
        self.shared_downloads_dir.mkdir(parents=True, exist_ok=True)

        self.env = mock.patch.dict(
            os.environ,
            {
                "STEALTH_BROWSER_MCP_DATA_DIR": str(self.data_dir),
                "STEALTH_ALLOW_RAW_CDP": "1",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        if os.path.exists(self.tmp):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_b_cannot_set_download_path_to_session_a_workspace(self) -> None:
        with active_session(self.session_b):
            with self.assertRaises(CdpPolicyError):
                prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": str(self.a_sessions)},
                )
            with self.assertRaises(CdpPolicyError):
                prepare_cdp_command(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "download_path": str(self.a_sessions)},
                )
            with self.assertRaises(CdpPolicyError):
                prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(self.a_ws)},
                )

    def test_session_b_accepts_download_path_under_own_workspace(self) -> None:
        with active_session(self.session_b):
            params = prepare_cdp_command(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "download_path": str(self.b_downloads)},
            )
            self.assertEqual(
                Path(params["download_path"]).resolve(),
                self.b_downloads.resolve(),
            )

            # Relative path should resolve under B's workspace
            params_rel = prepare_cdp_command(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "download_path": "downloads"},
            )
            self.assertEqual(
                Path(params_rel["download_path"]).resolve(),
                (self.b_ws / "downloads").resolve(),
            )

    def test_session_cannot_download_into_shared_dirs(self) -> None:
        shared_targets = [
            self.seeds_dir,
            self.locks_dir,
            self.shared_sessions_dir,
            self.shared_downloads_dir,
        ]
        with active_session(self.session_b):
            for target in shared_targets:
                with self.subTest(target=str(target)):
                    with self.assertRaises(CdpPolicyError):
                        prepare_cdp_command(
                            "Page.setDownloadBehavior",
                            {"behavior": "allow", "download_path": str(target)},
                        )

    def test_path_escaping_data_dir_rejected(self) -> None:
        outside = Path(self.tmp) / "outside"
        outside.mkdir(parents=True, exist_ok=True)

        with active_session(self.session_b):
            with self.assertRaisesRegex(CdpPolicyError, "data directory"):
                prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": str(outside)},
                )

        with active_session(None):
            with self.assertRaisesRegex(CdpPolicyError, "data directory"):
                prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": str(outside)},
                )

    def test_get_session_download_dir_resolves_under_caller_workspace(self) -> None:
        with active_session(self.session_a):
            dl_a = get_session_download_dir("instance_a")
            self.assertTrue(dl_a.resolve().is_relative_to(self.a_ws.resolve()))
            self.assertFalse(dl_a.resolve().is_relative_to((self.data_dir / "downloads").resolve()))
            if os.name == "posix":
                self.assertEqual(dl_a.stat().st_mode & 0o777, 0o700)

        with active_session(self.session_b):
            dl_b = get_session_download_dir("instance_b")
            self.assertTrue(dl_b.resolve().is_relative_to(self.b_ws.resolve()))
            self.assertFalse(dl_b.resolve().is_relative_to((self.data_dir / "downloads").resolve()))
            self.assertNotEqual(dl_a.resolve(), dl_b.resolve())

    def test_get_session_download_dir_stdio_uses_data_dir(self) -> None:
        with active_session(None):
            dl_stdio = get_session_download_dir("instance_stdio")
            self.assertEqual(
                dl_stdio.resolve(),
                (self.data_dir / "downloads" / "instance_stdio").resolve(),
            )
            # stdio caller can use data_dir downloads in prepare_cdp_command
            params = prepare_cdp_command(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "download_path": str(self.shared_downloads_dir)},
            )
            self.assertEqual(
                Path(params["download_path"]).resolve(),
                self.shared_downloads_dir.resolve(),
            )

    def test_raw_cdp_disabled_by_default(self) -> None:
        with active_session(self.session_b):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
                with self.assertRaisesRegex(CdpPolicyError, "setDownloadBehavior"):
                    prepare_cdp_command(
                        "Page.setDownloadBehavior",
                        {"behavior": "allow", "download_path": str(self.b_downloads)},
                    )
                with self.assertRaisesRegex(CdpPolicyError, "setDownloadBehavior"):
                    prepare_cdp_command(
                        "Browser.setDownloadBehavior",
                        {"behavior": "allow", "download_path": str(self.b_downloads)},
                    )

    def test_execute_cdp_command_fn_enforces_workspace(self) -> None:
        fake_tab = mock.AsyncMock()
        fake_bm = mock.AsyncMock()
        fake_bm.get_tab.return_value = fake_tab

        with mock.patch("stealth_browser_mcp.server.browser_manager", fake_bm):
            with active_session(self.session_b):
                result = asyncio.run(
                    execute_cdp_command.fn(
                        instance_id="inst_b",
                        command="Page.setDownloadBehavior",
                        params={"behavior": "allow", "download_path": str(self.a_sessions)},
                    )
                )
                self.assertFalse(result["success"])
                self.assertIn("workspace", result["error"])


if __name__ == "__main__":
    unittest.main()
