"""SB-92 / review F26, F29, F32: attach aliases, generic denials, owner reattach."""
from __future__ import annotations

import os
import unittest
from typing import Any, Dict, Optional
from unittest import mock

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.http_auth import (
    canonical_connect_host,
    connect_host_kind,
    is_loopback_connect_host,
    is_loopback_host,
)
from stealth_browser_mcp.mcp_sessions import MCPSessionManager


class FakeInstance:
    def __init__(self, instance_id: str, state: str = "ready") -> None:
        self.instance_id = instance_id
        self.state = state


class LiveShapedBrowserManager:
    """get_instance returns a dict, like BrowserManager does in production (F32)."""

    def __init__(self) -> None:
        self._instances: Dict[str, dict] = {}
        self._debug_endpoints: Dict[tuple, str] = {}
        self.attach_calls = []

    def _endpoint_key(self, host: Optional[str], port: Optional[int]):
        return BrowserManager._endpoint_key(host, port)

    def lookup_instance_by_debug_endpoint(self, host: str, port: int) -> Optional[str]:
        key = self._endpoint_key(host, port)
        return self._debug_endpoints.get(key) if key else None

    def register(self, instance_id: str, host: str, port: int) -> None:
        inst = FakeInstance(instance_id)
        self._instances[instance_id] = {
            "instance": inst,
            "debug_host": host,
            "debug_port": port,
        }
        key = self._endpoint_key(host, port)
        if key:
            self._debug_endpoints[key] = instance_id

    async def get_instance(self, instance_id: str) -> Optional[dict]:
        return self._instances.get(instance_id)

    async def attach_browser(self, port: int = 9222, host: str = "127.0.0.1", window: str = "keep") -> FakeInstance:
        self.attach_calls.append({"port": port, "host": host, "window": window})
        instance_id = f"hijacked-{port}"
        self.register(instance_id, host, port)
        return FakeInstance(instance_id)


class ConnectHostTests(unittest.TestCase):
    """Hosts the F26 exploit used must canonicalize to the loopback attach key."""

    def test_exploit_aliases_share_loopback_key(self) -> None:
        base = BrowserManager._endpoint_key("127.0.0.1", 59519)
        for host in ("127.1", "0.0.0.0", "2130706433", "localhost.", "LOCALHOST", "::ffff:127.0.0.1"):
            self.assertEqual(
                BrowserManager._endpoint_key(host, 59519),
                base,
                msg=host,
            )
            self.assertTrue(is_loopback_connect_host(host), msg=host)

    def test_bind_check_still_treats_0_0_0_0_as_non_loopback(self) -> None:
        """0.0.0.0 is a connect alias for loopback, but a bind to it is off loopback."""
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("::"))
        self.assertTrue(is_loopback_connect_host("0.0.0.0"))

    def test_remote_host_stays_distinct(self) -> None:
        self.assertNotEqual(
            BrowserManager._endpoint_key("127.0.0.1", 9222),
            BrowserManager._endpoint_key("10.0.0.5", 9222),
        )
        self.assertEqual(connect_host_kind("10.0.0.5"), "public")
        self.assertEqual(canonical_connect_host("10.0.0.5"), "10.0.0.5")


class AttachAliasIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Live F26 exploit: B attach_browser(port=A, host='127.1') took over A's Chrome."""

    def setUp(self) -> None:
        server.reset_attach_rate_limiter()
        self._http_mode = server._HTTP_MODE
        self._bind = server._HTTP_BIND_HOST
        self._real_bm = server.browser_manager
        self._real_sm = server.mcp_session_manager
        self.fake_bm = LiveShapedBrowserManager()
        self.sess = MCPSessionManager(browser_manager=self.fake_bm)
        server.browser_manager = self.fake_bm
        server.mcp_session_manager = self.sess
        mcp_sessions.mcp_session_manager = self.sess
        server._HTTP_MODE = True
        server._HTTP_BIND_HOST = "127.0.0.1"
        self.fake_bm.register("inst-A", "127.0.0.1", 59519)
        self.sess.register_session("session-A", "inst-A")

    def tearDown(self) -> None:
        server.reset_attach_rate_limiter()
        server._HTTP_MODE = self._http_mode
        server._HTTP_BIND_HOST = self._bind
        server.browser_manager = self._real_bm
        server.mcp_session_manager = self._real_sm
        mcp_sessions.mcp_session_manager = self._real_sm

    async def _deny(self, host: str) -> str:
        with mock.patch.dict(
            os.environ,
            {"STEALTH_ALLOW_ATTACH": "1", "STEALTH_ALLOW_REMOTE_ATTACH": "1"},
        ):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-B",
            ):
                with mock.patch.object(server, "_probe_version") as probe:
                    with self.assertRaises(ToolError) as ctx:
                        await server.attach_browser.fn(port=59519, host=host)
                    probe.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("not available for attach", message)
        self.assertNotIn("inst-A", message)
        self.assertEqual(self.fake_bm.attach_calls, [])
        return message

    async def test_session_b_cannot_hijack_via_127_1(self) -> None:
        await self._deny("127.1")

    async def test_session_b_cannot_hijack_via_0_0_0_0(self) -> None:
        await self._deny("0.0.0.0")

    async def test_session_b_cannot_hijack_via_decimal_loopback(self) -> None:
        await self._deny("2130706433")

    async def test_session_b_cannot_hijack_via_localhost_dot(self) -> None:
        await self._deny("localhost.")

    async def test_owner_reattach_with_live_shaped_dict(self) -> None:
        """F32: get_instance returns a dict; owner reattach must not crash."""
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-A",
            ):
                with mock.patch.object(server, "_probe_version") as probe:
                    result = await server.attach_browser.fn(port=59519, host="127.0.0.1")
        self.assertEqual(result["instance_id"], "inst-A")
        self.assertTrue(result.get("already_owned"))
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.fake_bm.attach_calls, [])
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
