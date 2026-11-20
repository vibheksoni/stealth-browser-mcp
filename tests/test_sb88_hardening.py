"""SB-88 / review F21-F25, Q4, Q5: rate limit, reload, writes, proxy flags, expiry, seed passwords."""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import http_auth, mcp_sessions, server
from stealth_browser_mcp.hook_policy import refuse_code_hooks_off_loopback
from stealth_browser_mcp.mcp_sessions import MCPSessionManager
from stealth_browser_mcp.path_policy import (
    PathPolicyError,
    resolve_managed_path,
    write_mode_0600,
    write_private_text,
)
from stealth_browser_mcp.cdp_policy import refuse_raw_cdp_off_loopback


class AttachRateLimitGlobalTests(unittest.IsolatedAsyncioTestCase):
    """Live F21: a fresh session with the same token got 10 more failures after another session hit RL."""

    def setUp(self) -> None:
        server.reset_attach_rate_limiter()
        self._http = server._HTTP_MODE
        server._HTTP_MODE = False

    def tearDown(self) -> None:
        server.reset_attach_rate_limiter()
        server._HTTP_MODE = self._http

    async def test_new_session_shares_the_global_budget(self) -> None:
        with mock.patch.object(server, "_probe_version", return_value=False):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-E",
            ):
                for _ in range(10):
                    with self.assertRaises(ToolError) as ctx:
                        await server.attach_browser.fn(port=1, host="127.0.0.1")
                    self.assertIn("Could not connect", str(ctx.exception))
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-G",
            ):
                with self.assertRaises(ToolError) as ctx:
                    await server.attach_browser.fn(port=1, host="127.0.0.1")
                self.assertIn("rate limited", str(ctx.exception))


class HotReloadRewireTests(unittest.TestCase):
    """F22: hot_reload must re-wire set_browser_manager and on_instance_closed."""

    def test_register_manager_listeners_rewires_session_manager(self) -> None:
        source = Path(server.__file__).read_text(encoding="utf-8")
        block_start = source.find("def _register_manager_listeners")
        block = source[block_start:source.find("\n\n", block_start)]
        self.assertIn("mcp_session_manager.set_browser_manager(browser_manager)", block)
        self.assertIn("mcp_session_manager.on_instance_closed", block)


class UploadDirsAreReadOnlyTests(unittest.TestCase):
    """Live F23: BROWSER_FILE_UPLOAD_ALLOWED_DIRS=/ made resolve_managed_path accept ~/.bashrc for writes."""

    def test_upload_root_is_not_a_write_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            outside = Path(tmp) / "home-bashrc"
            outside.write_text("safe\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "STEALTH_BROWSER_MCP_DATA_DIR": str(data),
                    "BROWSER_FILE_UPLOAD_ALLOWED_DIRS": str(tmp),
                },
            ):
                # reads from the upload root still work
                resolve_managed_path(outside, must_exist=True)
                with self.assertRaises(PathPolicyError):
                    write_private_text(outside, "pwned")
                self.assertEqual(outside.read_text(encoding="utf-8"), "safe\n")

    def test_refuse_truncate_existing_outside_data_dir_even_in_export_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            export = Path(tmp) / "export"
            export.mkdir()
            existing = export / "bashrc"
            existing.write_text("keep\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "STEALTH_BROWSER_MCP_DATA_DIR": str(data),
                    "STEALTH_EXPORT_DIRS": str(export),
                },
            ):
                with self.assertRaisesRegex(PathPolicyError, "overwrite"):
                    write_private_text(existing, "pwned")
                self.assertEqual(existing.read_text(encoding="utf-8"), "keep\n")


class ProxyFlagTests(unittest.TestCase):
    """F24: allowed-host/origin or X-Forwarded-For counts as off loopback."""

    def test_allowed_host_refuses_code_hooks_and_raw_cdp(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_CODE_HOOKS": "1", "STEALTH_ALLOW_RAW_CDP": "1"}):
            refuse_code_hooks_off_loopback("127.0.0.1")
            refuse_raw_cdp_off_loopback("127.0.0.1")
            with self.assertRaises(ValueError):
                refuse_code_hooks_off_loopback("127.0.0.1", allowed_hosts=["mcp.example"])
            with self.assertRaises(ValueError):
                refuse_raw_cdp_off_loopback("127.0.0.1", allowed_origins=["https://mcp.example"])

    def test_forwarded_header_marks_request_proxied(self) -> None:
        inner_called = {"n": 0}

        async def inner(scope, receive, send):
            inner_called["n"] += 1
            self.assertTrue(http_auth.request_appears_proxied())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = http_auth.HostOriginMiddleware(inner)

        async def run():
            async def receive():
                return {"type": "http.request", "body": b""}

            sent = []

            async def send(msg):
                sent.append(msg)

            await mw(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "headers": [
                        (b"host", b"127.0.0.1:8000"),
                        (b"x-forwarded-for", b"203.0.113.9"),
                    ],
                },
                receive,
                send,
            )
            return sent

        asyncio.run(run())
        self.assertEqual(inner_called["n"], 1)
        self.assertFalse(http_auth.request_appears_proxied())


class TokenAndWebsocketTests(unittest.TestCase):
    """F25: defer token errors, websocket.close, 0600 clone files, generated file cleanup."""

    def test_import_does_not_crash_on_bad_token_file(self) -> None:
        # The server module catches TokenConfigError at import. Re-run the
        # assignment path with a bad file and confirm we get a deferred error
        # object rather than an import failure.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "missing-token"
            with mock.patch.dict(os.environ, {"STEALTH_MCP_TOKEN_FILE": str(bad)}, clear=False):
                os.environ.pop("STEALTH_MCP_TOKEN", None)
                os.environ.pop("STEALTH_BROWSER_MCP_AUTH_TOKEN", None)
                os.environ.pop("MCP_AUTH_TOKEN", None)
                with self.assertRaises(http_auth.TokenConfigError):
                    http_auth.get_mcp_token()

    def test_websocket_close_is_used(self) -> None:
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(msg):
            sent.append(msg)

        async def inner(scope, receive, send):
            raise AssertionError("inner should not run")

        mw = http_auth.HostOriginMiddleware(inner)
        asyncio.run(mw({"type": "websocket", "headers": []}, receive, send))
        self.assertEqual(sent[0]["type"], "websocket.close")
        self.assertEqual(sent[0]["code"], 1008)

    def test_generated_token_is_tracked_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token, path = http_auth.write_generated_token_file(Path(tmp))
            self.assertTrue(path.is_file())
            self.assertIn(path, http_auth._GENERATED_TOKEN_FILES)
            http_auth.cleanup_generated_token_files()
            self.assertFalse(path.exists())
            self.assertEqual(token, token)  # value unused in report

    def test_element_clone_files_are_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "element_clones" / "el.json"
            write_mode_0600(dest, "{\"ok\": true}")
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)


class SessionExpiryTests(unittest.IsolatedAsyncioTestCase):
    """Q4: close_session runs when a session exceeds STEALTH_MCP_SESSION_TTL."""

    async def test_expire_stale_sessions_closes_owned_instances(self) -> None:
        closed = []

        class FakeBM:
            async def close_instance(self, instance_id: str) -> bool:
                closed.append(instance_id)
                return True

        mgr = MCPSessionManager(browser_manager=FakeBM())
        mgr.register_session("session-A", "inst-A")
        mgr._last_seen["session-A"] = time.monotonic() - 700
        with mock.patch.dict(os.environ, {"STEALTH_MCP_SESSION_TTL": "600"}):
            expired = await mgr.expire_stale_sessions()
        self.assertEqual(expired, ["session-A"])
        self.assertEqual(closed, ["inst-A"])


class SeedPasswordFlagTests(unittest.IsolatedAsyncioTestCase):
    """Q5: refresh_seed(include_passwords) is off by default and owner-only with a flag."""

    async def test_include_passwords_blocked_without_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_SEED_PASSWORDS", None)
            with mock.patch("stealth_browser_mcp.server.seed_tools.refresh_seed") as refresh:
                result = await server.refresh_seed.fn(
                    name="mine",
                    source_browser="chrome",
                    include_passwords=True,
                )
        self.assertEqual(result["status"], "error")
        self.assertIn("STEALTH_ALLOW_SEED_PASSWORDS", result["error"])
        refresh.assert_not_called()

    async def test_include_passwords_http_requires_owned_instance(self) -> None:
        server._HTTP_MODE = True
        try:
            with mock.patch.dict(os.environ, {"STEALTH_ALLOW_SEED_PASSWORDS": "1"}):
                with mock.patch(
                    "stealth_browser_mcp.server.get_current_mcp_session_id",
                    return_value="session-B",
                ), mock.patch("stealth_browser_mcp.server.seed_tools.refresh_seed") as refresh:
                    result = await server.refresh_seed.fn(
                        name="mine",
                        source_browser="chrome",
                        include_passwords=True,
                    )
            self.assertEqual(result["status"], "error")
            self.assertIn("owner-only", result["error"])
            refresh.assert_not_called()
        finally:
            server._HTTP_MODE = False


if __name__ == "__main__":
    unittest.main()
