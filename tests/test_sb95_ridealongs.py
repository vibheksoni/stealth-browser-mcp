"""SB-95 / review F29(b), F30, F33, F34, F35 ride-alongs and leftovers."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stealth_browser_mcp import debug_logger as debug_logger_mod
from stealth_browser_mcp import http_auth
from stealth_browser_mcp.debug_logger import DebugLogger
from stealth_browser_mcp.path_policy import PathPolicyError
from stealth_browser_mcp.server import export_debug_logs, get_debug_view


class DebugLogIsolationTests(unittest.TestCase):
    """F29(b): B's get_debug_view must not include A's instance id or script."""

    def setUp(self) -> None:
        self.logger = DebugLogger()
        self.logger.enable()

    def test_session_b_cannot_read_session_a_debug_secret(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.debug_logger._current_session_id",
            return_value="session-A",
        ):
            self.logger.log_info("cdp", "Runtime.evaluate", "A-SCRIPT-SECRET")
            self.logger.log_error(
                "browser",
                "attach",
                RuntimeError("Instance inst-AAAA-SECRET failed"),
            )
        with mock.patch(
            "stealth_browser_mcp.debug_logger._current_session_id",
            return_value="session-B",
        ):
            view = self.logger.get_debug_view_paginated()
        blob = json.dumps(view)
        self.assertNotIn("A-SCRIPT-SECRET", blob)
        self.assertNotIn("inst-AAAA-SECRET", blob)
        self.assertNotIn("session-A", blob)
        self.assertEqual(view["summary"]["total_info"], 0)
        self.assertEqual(view["summary"]["total_errors"], 0)

    def test_owner_session_still_sees_its_own_logs(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.debug_logger._current_session_id",
            return_value="session-A",
        ):
            self.logger.log_info("cdp", "Runtime.evaluate", "A-SCRIPT-SECRET")
            view = self.logger.get_debug_view_paginated()
        blob = json.dumps(view)
        self.assertIn("A-SCRIPT-SECRET", blob)
        self.assertEqual(view["summary"]["total_info"], 1)

    def test_stdio_without_session_sees_untagged_logs(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.debug_logger._current_session_id",
            return_value=None,
        ):
            self.logger.log_info("server", "startup", "boot")
            view = self.logger.get_debug_view_paginated()
        self.assertEqual(view["summary"]["total_info"], 1)
        self.assertIn("boot", json.dumps(view))


class GetDebugViewToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_filters_by_calling_session(self) -> None:
        logger = debug_logger_mod.debug_logger
        prev = logger._enabled
        logger.enable()
        try:
            with mock.patch(
                "stealth_browser_mcp.debug_logger._current_session_id",
                return_value="session-A",
            ):
                logger.log_info("cdp", "Runtime.evaluate", "A-SCRIPT-SECRET")
            with mock.patch(
                "stealth_browser_mcp.debug_logger._current_session_id",
                return_value="session-B",
            ):
                view = await get_debug_view.fn()
            self.assertNotIn("A-SCRIPT-SECRET", json.dumps(view))
        finally:
            logger._enabled = prev
            logger._info.clear()
            logger._errors.clear()
            logger._warnings.clear()


class ProxyPerRequestTests(unittest.TestCase):
    """F30 / F24 leftover: proxy detection is per request and includes more headers."""

    def test_via_and_x_real_ip_count_as_proxied(self) -> None:
        self.assertTrue(http_auth.headers_appear_proxied([(b"via", b"1.1 nginx")]))
        self.assertTrue(http_auth.headers_appear_proxied([(b"x-real-ip", b"203.0.113.9")]))
        self.assertTrue(http_auth.headers_appear_proxied([(b"x-forwarded-host", b"mcp.example")]))
        self.assertTrue(http_auth.headers_appear_proxied([(b"x-forwarded-proto", b"https")]))
        self.assertFalse(http_auth.headers_appear_proxied([(b"host", b"127.0.0.1:8000")]))

    def test_live_request_headers_override_stale_contextvar(self) -> None:
        token = http_auth._REQUEST_FORWARDED.set(False)
        try:
            class FakeRequest:
                def __init__(self, headers):
                    self.headers = headers

            with mock.patch(
                "fastmcp.server.dependencies.get_http_request",
                return_value=FakeRequest({"X-Forwarded-For": "203.0.113.9"}),
            ):
                self.assertTrue(http_auth.request_appears_proxied())
            with mock.patch(
                "fastmcp.server.dependencies.get_http_request",
                return_value=FakeRequest({"Via": "1.1 nginx"}),
            ):
                self.assertTrue(http_auth.request_appears_proxied())
            with mock.patch(
                "fastmcp.server.dependencies.get_http_request",
                return_value=FakeRequest({"Host": "127.0.0.1:8000"}),
            ):
                self.assertFalse(http_auth.request_appears_proxied())
        finally:
            http_auth._REQUEST_FORWARDED.reset(token)


class ExportDebugLogsWriteRootTests(unittest.IsolatedAsyncioTestCase):
    """F33 / F23 leftover: write roots only; do not truncate an upload-dir file."""

    async def test_export_debug_logs_does_not_truncate_upload_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            up = Path(tmp) / "up"
            up.mkdir()
            victim = up / "victim.json"
            victim.write_text("KEEP-ME", encoding="utf-8")
            victim.chmod(0o644)
            exp = Path(tmp) / "exp"
            exp.mkdir()
            env = {
                "STEALTH_BROWSER_MCP_DATA_DIR": str(root),
                "BROWSER_FILE_UPLOAD_ALLOWED_DIRS": str(up),
                "STEALTH_EXPORT_DIRS": str(exp),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(PathPolicyError):
                    await export_debug_logs.fn(filename=str(victim), format="json")
                self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP-ME")
                dest = Path(await export_debug_logs.fn(filename=str(exp / "debug.json"), format="json"))
                self.assertEqual(dest.parent.resolve(), exp.resolve())
                self.assertTrue(dest.is_file())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)


class BadTokenImportTests(unittest.TestCase):
    """F34 / F25(a): a world-readable token file must not crash import, including stdio."""

    def test_import_server_with_world_readable_token_file_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("secret-token-value\n", encoding="utf-8")
            token_path.chmod(0o644)
            env = os.environ.copy()
            env["STEALTH_MCP_TOKEN_FILE"] = str(token_path)
            env.pop("STEALTH_MCP_TOKEN", None)
            env.pop("STEALTH_BROWSER_MCP_AUTH_TOKEN", None)
            env.pop("MCP_AUTH_TOKEN", None)
            src = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import stealth_browser_mcp.server as s; "
                    "print('IMPORTED'); "
                    "print('TOKEN_ERROR' if s._TOKEN_CONFIG_ERROR is not None else 'TOKEN_OK')",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("IMPORTED", proc.stdout)
            # Unix mode bits (0644) are a token-config error on POSIX. Windows
            # file modes are not POSIX permission bits, so the same chmod does
            # not raise, and import must still succeed without crashing.
            if os.name == "posix":
                self.assertIn("TOKEN_ERROR", proc.stdout)
            else:
                self.assertIn("TOKEN_OK", proc.stdout)


class UnsupportedProtocolTests(unittest.IsolatedAsyncioTestCase):
    """F35: non-websocket unknown scopes return 403 instead of recursing."""

    async def test_reject_unsupported_protocol_sends_403(self) -> None:
        sent = []

        async def send(msg):
            sent.append(msg)

        await http_auth._reject_unsupported_protocol({"type": "other"}, send)
        starts = [msg for msg in sent if msg.get("type") == "http.response.start"]
        self.assertTrue(starts)
        self.assertEqual(starts[0]["status"], 403)


if __name__ == "__main__":
    unittest.main()
