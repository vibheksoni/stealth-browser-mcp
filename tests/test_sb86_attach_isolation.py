"""SB-86 / review F17 + Q3: HTTP attach is opt-in and cannot hijack another session's Chrome."""
from __future__ import annotations

import os
import unittest
from typing import Dict, Optional
from unittest import mock

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.mcp_sessions import MCPSessionManager


class FakeInstance:
    def __init__(self, instance_id: str, state: str = "ready") -> None:
        self.instance_id = instance_id
        self.state = state


class FakeBrowserManager:
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
        self._instances[instance_id] = {"debug_host": host, "debug_port": port}
        key = self._endpoint_key(host, port)
        if key:
            self._debug_endpoints[key] = instance_id

    async def get_instance(self, instance_id: str) -> Optional[FakeInstance]:
        if instance_id in self._instances:
            return FakeInstance(instance_id)
        return None

    async def attach_browser(self, port: int = 9222, host: str = "127.0.0.1", window: str = "keep") -> FakeInstance:
        self.attach_calls.append({"port": port, "host": host, "window": window})
        instance_id = f"attached-{port}"
        self.register(instance_id, host, port)
        return FakeInstance(instance_id)


class AttachIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        server.reset_attach_rate_limiter()
        self._http_mode = server._HTTP_MODE
        self._bind = server._HTTP_BIND_HOST
        self._real_bm = server.browser_manager
        self._real_sm = server.mcp_session_manager
        self.fake_bm = FakeBrowserManager()
        self.sess = MCPSessionManager(browser_manager=self.fake_bm)
        server.browser_manager = self.fake_bm
        server.mcp_session_manager = self.sess
        mcp_sessions.mcp_session_manager = self.sess
        server._HTTP_MODE = True
        server._HTTP_BIND_HOST = "127.0.0.1"

    def tearDown(self) -> None:
        server.reset_attach_rate_limiter()
        server._HTTP_MODE = self._http_mode
        server._HTTP_BIND_HOST = self._bind
        server.browser_manager = self._real_bm
        server.mcp_session_manager = self._real_sm
        mcp_sessions.mcp_session_manager = self._real_sm

    async def test_http_attach_requires_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_ATTACH", None)
            with mock.patch.object(server, "_probe_version") as probe:
                with self.assertRaisesRegex(ToolError, "STEALTH_ALLOW_ATTACH"):
                    await server.attach_browser.fn(port=9222, host="127.0.0.1")
                probe.assert_not_called()
        self.assertEqual(self.fake_bm.attach_calls, [])

    async def test_session_b_cannot_attach_to_session_a_port(self) -> None:
        """Live F17 exploit: B attach_browser on A's DevTools port and read cookies."""
        self.fake_bm.register("inst-A", "127.0.0.1", 59519)
        self.sess.register_session("session-A", "inst-A")
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-B",
            ):
                with mock.patch.object(server, "_probe_version") as probe:
                    with self.assertRaises(ToolError) as ctx:
                        await server.attach_browser.fn(port=59519, host="127.0.0.1")
                    self.assertIn("belongs to another session", str(ctx.exception))
                    probe.assert_not_called()
        self.assertEqual(self.fake_bm.attach_calls, [])

    async def test_loopback_aliases_still_match_owned_port(self) -> None:
        self.fake_bm.register("inst-A", "127.0.0.1", 36179)
        self.sess.register_session("session-A", "inst-A")
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-B",
            ):
                with self.assertRaises(ToolError) as ctx:
                    await server.attach_browser.fn(port=36179, host="localhost")
                self.assertIn("belongs to another session", str(ctx.exception))

    async def test_owner_reattach_returns_existing_instance(self) -> None:
        self.fake_bm.register("inst-A", "127.0.0.1", 59519)
        self.sess.register_session("session-A", "inst-A")
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-A",
            ):
                with mock.patch.object(server, "_probe_version") as probe:
                    result = await server.attach_browser.fn(port=59519, host="127.0.0.1")
        self.assertEqual(result["instance_id"], "inst-A")
        self.assertTrue(result.get("already_owned"))
        self.assertEqual(self.fake_bm.attach_calls, [])
        probe.assert_not_called()

    async def test_external_unowned_port_is_allowed_when_flag_set(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch(
                "stealth_browser_mcp.server.get_current_mcp_session_id",
                return_value="session-B",
            ):
                with mock.patch.object(server, "_probe_version", return_value=True):
                    result = await server.attach_browser.fn(port=9222, host="127.0.0.1")
        self.assertEqual(result["instance_id"], "attached-9222")
        self.assertEqual(self.fake_bm.attach_calls, [{"port": 9222, "host": "127.0.0.1", "window": "keep"}])

    async def test_stdio_mode_does_not_need_the_flag(self) -> None:
        server._HTTP_MODE = False
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_ATTACH", None)
            with mock.patch.object(server, "_probe_version", return_value=True):
                result = await server.attach_browser.fn(port=9222, host="127.0.0.1")
        self.assertEqual(result["instance_id"], "attached-9222")


class EndpointKeyTests(unittest.TestCase):
    def test_loopback_aliases_share_a_key(self) -> None:
        self.assertEqual(
            BrowserManager._endpoint_key("127.0.0.1", 9222),
            BrowserManager._endpoint_key("localhost", 9222),
        )
        self.assertEqual(
            BrowserManager._endpoint_key("127.0.0.1", 9222),
            BrowserManager._endpoint_key("::1", 9222),
        )
        self.assertNotEqual(
            BrowserManager._endpoint_key("127.0.0.1", 9222),
            BrowserManager._endpoint_key("10.0.0.5", 9222),
        )


if __name__ == "__main__":
    unittest.main()
