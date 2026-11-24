"""SB-93 / review F28: saved sessions, network exports, and clone files are per session."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from stealth_browser_mcp.file_based_element_cloner import FileBasedElementCloner
from stealth_browser_mcp.mcp_sessions import set_current_mcp_session_id
from stealth_browser_mcp.path_policy import (
    PathPolicyError,
    read_managed_text,
    resolve_managed_path,
    session_workspace_dir,
    write_private_text,
)
from stealth_browser_mcp.session_state import default_session_path, write_session_file


class SessionFileIsolationTests(unittest.TestCase):
    """Live F28 exploit: B restore_session('sessions/session_<A>.json') loaded A's cookies."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_data = os.environ.get("STEALTH_BROWSER_MCP_DATA_DIR")
        os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = self.tmp.name
        self.data = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        if self.prev_data is None:
            os.environ.pop("STEALTH_BROWSER_MCP_DATA_DIR", None)
        else:
            os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = self.prev_data
        self.tmp.cleanup()

    def test_default_session_path_is_under_the_caller_workspace(self) -> None:
        with set_current_mcp_session_id("session-A"):
            path = default_session_path("inst-A")
        self.assertTrue(str(path).startswith(str(self.data / "clients")))
        self.assertIn("session-A", str(path))

    def test_session_b_cannot_read_session_a_saved_session(self) -> None:
        state = {
            "cookies": [{"name": "sid", "value": "A-ONLY-COOKIE", "domain": "example.test"}],
            "origins": [],
        }
        with set_current_mcp_session_id("session-A"):
            saved = write_session_file(default_session_path("inst-A"), state)
            relative = "sessions/session_inst-A.json"
            self.assertEqual(json.loads(read_managed_text(relative))["cookies"][0]["value"], "A-ONLY-COOKIE")

        with set_current_mcp_session_id("session-B"):
            # A relative path is resolved in B's workspace, not A's.
            b_relative = resolve_managed_path(relative)
            self.assertNotEqual(b_relative.resolve(), saved.resolve())
            with self.assertRaises((PathPolicyError, FileNotFoundError, OSError, ValueError)):
                read_managed_text(relative)
            with self.assertRaises(PathPolicyError):
                resolve_managed_path(saved)
            with self.assertRaises((PathPolicyError, FileNotFoundError, OSError, ValueError)):
                read_managed_text(saved)

    def test_session_b_cannot_import_session_a_network_export(self) -> None:
        with set_current_mcp_session_id("session-A"):
            path = write_private_text(
                "netA.json",
                json.dumps({"requests": [{"url": "https://example.test/?tok=A-URL-SECRET"}]}),
            )
        with set_current_mcp_session_id("session-B"):
            with self.assertRaises(PathPolicyError):
                read_managed_text("netA.json")
            with self.assertRaises(PathPolicyError):
                read_managed_text(path)

    def test_clone_list_and_cleanup_stay_in_the_caller_workspace(self) -> None:
        cloner = FileBasedElementCloner()
        with set_current_mcp_session_id("session-A"):
            a_dir = cloner.output_dir
            (a_dir / "clone_a.json").write_text(
                json.dumps({"_metadata": {"selector": "#secret-a"}, "html": "A"}),
                encoding="utf-8",
            )
            listed = cloner.list_clone_files()
            self.assertEqual(len(listed), 1)
            self.assertIn("secret-a", json.dumps(listed))

        with set_current_mcp_session_id("session-B"):
            self.assertEqual(cloner.list_clone_files(), [])
            deleted = cloner.cleanup_old_files(0)
            self.assertEqual(deleted, 0)

        with set_current_mcp_session_id("session-A"):
            self.assertEqual(len(cloner.list_clone_files()), 1)
            deleted = cloner.cleanup_old_files(0)
            self.assertEqual(deleted, 1)
            self.assertEqual(cloner.list_clone_files(), [])

    def test_stdio_without_session_still_uses_data_dir(self) -> None:
        with set_current_mcp_session_id(None):
            self.assertEqual(session_workspace_dir(), self.data)
            path = default_session_path("inst-X")
        self.assertEqual(path, self.data / "sessions" / "session_inst-X.json")
