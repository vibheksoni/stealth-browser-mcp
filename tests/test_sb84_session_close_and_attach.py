"""SB-84 / review F8 and F10: close all session browsers; rate-limit attach_browser."""
from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from fastmcp.exceptions import ToolError
from starlette.types import Message, Receive, Scope, Send

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.mcp_sessions import MCPSessionManager, SessionLifecycleMiddleware


class FakeBrowserManager:
    def __init__(self) -> None:
        self.instances: Dict[str, Dict[str, Any]] = {}
        self.closed_instances: List[str] = []

    async def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        return self.instances.get(instance_id)

    async def close_instance(self, instance_id: str) -> bool:
        self.closed_instances.append(instance_id)
        self.instances.pop(instance_id, None)
        return True


class CloseAllSessionBrowsersTests(unittest.TestCase):
    def test_close_session_closes_all_instances_not_just_last(self) -> None:
        fake = FakeBrowserManager()
        mgr = MCPSessionManager(browser_manager=fake)
        mgr.register_session("session-A", "inst-1")
        mgr.register_session("session-A", "inst-2")
        mgr.register_session("session-A", "inst-3")
        self.assertEqual(mgr.get_instance_for_session("session-A"), "inst-3")
        self.assertEqual(sorted(mgr.list_instance_ids_for_session("session-A")), ["inst-1", "inst-2", "inst-3"])

        closed = asyncio.run(mgr.close_session("session-A"))
        self.assertTrue(closed)
        self.assertEqual(sorted(fake.closed_instances), ["inst-1", "inst-2", "inst-3"])
        self.assertIsNone(mgr.get_instance_for_session("session-A"))
        self.assertEqual(mgr.list_instance_ids_for_session("session-A"), [])

    def test_get_disconnect_does_not_close_instance(self) -> None:
        fake = FakeBrowserManager()
        mgr = MCPSessionManager(browser_manager=fake)
        mgr.register_session("session-A", "inst-1")
        mgr.register_session("session-A", "inst-2")

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = SessionLifecycleMiddleware(app, session_manager=mgr)

        async def run(method: str) -> None:
            receive_messages: List[Message] = [
                {"type": "http.request", "body": b""},
                {"type": "http.disconnect"},
            ]

            async def receive() -> Message:
                return receive_messages.pop(0)

            sent: List[Message] = []

            async def send(msg: Message) -> None:
                sent.append(msg)

            scope: Scope = {
                "type": "http",
                "method": method,
                "path": "/mcp/",
                "headers": [(b"mcp-session-id", b"session-A")],
            }
            await middleware(scope, receive, send)

        asyncio.run(run("GET"))
        self.assertEqual(fake.closed_instances, [])
        self.assertEqual(sorted(mgr.list_instance_ids_for_session("session-A")), ["inst-1", "inst-2"])

        asyncio.run(run("DELETE"))
        self.assertEqual(sorted(fake.closed_instances), ["inst-1", "inst-2"])


class AttachRateLimitAndBindTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        server.reset_attach_rate_limiter()
        self._http_mode = server._HTTP_MODE
        self._bind = server._HTTP_BIND_HOST
        server._HTTP_MODE = False
        server._HTTP_BIND_HOST = None

    def tearDown(self) -> None:
        server.reset_attach_rate_limiter()
        server._HTTP_MODE = self._http_mode
        server._HTTP_BIND_HOST = self._bind

    async def test_eleventh_failed_probe_is_rate_limited(self) -> None:
        with mock.patch.object(server, "_probe_version", return_value=False):
            for index in range(10):
                with self.assertRaises(ToolError) as ctx:
                    await server.attach_browser.fn(port=1, host="127.0.0.1")
                self.assertIn("Could not connect", str(ctx.exception))
            with self.assertRaises(ToolError) as ctx:
                await server.attach_browser.fn(port=1, host="127.0.0.1")
            self.assertIn("rate limited", str(ctx.exception))

    async def test_remote_attach_ignored_when_http_bound_off_loopback(self) -> None:
        server._HTTP_MODE = True
        server._HTTP_BIND_HOST = "0.0.0.0"
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_REMOTE_ATTACH": "1", "STEALTH_ALLOW_ATTACH": "1"}):
            with mock.patch.object(server, "_probe_version") as probe:
                with self.assertRaises(ToolError) as ctx:
                    await server.attach_browser.fn(port=9222, host="10.0.0.5")
                self.assertIn("STEALTH_ALLOW_REMOTE_ATTACH is ignored", str(ctx.exception))
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
