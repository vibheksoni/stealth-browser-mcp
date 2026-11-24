"""SB-91 / review F27: browser:// resources must not leak another session's cookies or URL."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest import mock

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.mcp_sessions import MCPSessionManager, set_current_mcp_session_id
from stealth_browser_mcp.models import NetworkRequest


class FakeTab:
    def __init__(self, cookies: List[Dict[str, Any]]) -> None:
        self._cookies = cookies


class FakeBrowserManager:
    def __init__(self) -> None:
        self.tabs: Dict[str, FakeTab] = {}
        self.states: Dict[str, Any] = {}

    async def get_tab(self, instance_id: str) -> Optional[FakeTab]:
        return self.tabs.get(instance_id)

    async def get_page_state(self, instance_id: str) -> Optional[Any]:
        return self.states.get(instance_id)


class FakeState:
    def __init__(self, url: str, cookies: List[Dict[str, Any]], console_logs: List[str]) -> None:
        self.url = url
        self.cookies = cookies
        self.console_logs = console_logs


class FakeInterceptor:
    def __init__(self) -> None:
        self.requests: Dict[str, List[NetworkRequest]] = {}

    async def get_cookies(self, tab: FakeTab) -> List[Dict[str, Any]]:
        return list(tab._cookies)

    async def list_requests(self, instance_id: str) -> List[NetworkRequest]:
        return list(self.requests.get(instance_id, []))


class ResourceIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Live F27 exploit: B read_resource('browser://<A>/cookies') returned A's cookie."""

    def setUp(self) -> None:
        self.sess = MCPSessionManager()
        self.sess.register_session("session-A", "inst-A")
        self.sess.register_session("session-B", "inst-B")
        self.bm = FakeBrowserManager()
        secret_cookie = [{"name": "sid", "value": "A-ONLY-COOKIE", "domain": "example.test"}]
        self.bm.tabs["inst-A"] = FakeTab(secret_cookie)
        self.bm.tabs["inst-B"] = FakeTab([{"name": "sid", "value": "B-COOKIE"}])
        self.bm.states["inst-A"] = FakeState(
            "https://example.test/index.html?tok=A-URL-SECRET",
            secret_cookie,
            ["A-CONSOLE"],
        )
        self.bm.states["inst-B"] = FakeState("https://b.test/", [{"name": "sid", "value": "B-COOKIE"}], ["B-CONSOLE"])
        self.interceptor = FakeInterceptor()
        self.interceptor.requests["inst-A"] = [
            NetworkRequest(
                request_id="req-A",
                instance_id="inst-A",
                url="https://example.test/index.html?tok=A-URL-SECRET",
                method="GET",
                headers={"cookie": "sid=A-ONLY-COOKIE"},
                timestamp=datetime.now(),
                resource_type="document",
            )
        ]
        self._real_bm = server.browser_manager
        self._real_sm = server.mcp_session_manager
        self._real_ni = server.network_interceptor
        self._http = server._HTTP_MODE
        server.browser_manager = self.bm
        server.mcp_session_manager = self.sess
        server.network_interceptor = self.interceptor
        mcp_sessions.mcp_session_manager = self.sess
        server._HTTP_MODE = True

    def tearDown(self) -> None:
        server.browser_manager = self._real_bm
        server.mcp_session_manager = self._real_sm
        server.network_interceptor = self._real_ni
        mcp_sessions.mcp_session_manager = self._real_sm
        server._HTTP_MODE = self._http

    def _not_found(self, raw: str) -> None:
        data = json.loads(raw)
        self.assertEqual(data, {"error": "Instance not found"})
        self.assertNotIn("A-ONLY-COOKIE", raw)
        self.assertNotIn("A-URL-SECRET", raw)
        self.assertNotIn("inst-A", raw)

    async def test_session_b_cannot_read_session_a_cookies_resource(self) -> None:
        with set_current_mcp_session_id("session-B"):
            raw = await server.get_cookies_resource.fn("inst-A")
        self._not_found(raw)

    async def test_session_b_cannot_read_session_a_state_resource(self) -> None:
        with set_current_mcp_session_id("session-B"):
            raw = await server.get_browser_state_resource.fn("inst-A")
        self._not_found(raw)

    async def test_session_b_cannot_read_session_a_network_resource(self) -> None:
        with set_current_mcp_session_id("session-B"):
            raw = await server.get_network_resource.fn("inst-A")
        self._not_found(raw)

    async def test_session_b_cannot_read_session_a_console_resource(self) -> None:
        with set_current_mcp_session_id("session-B"):
            raw = await server.get_console_resource.fn("inst-A")
        self._not_found(raw)

    async def test_owner_can_read_own_cookies_resource(self) -> None:
        with set_current_mcp_session_id("session-A"):
            raw = await server.get_cookies_resource.fn("inst-A")
        data = json.loads(raw)
        self.assertEqual(data[0]["value"], "A-ONLY-COOKIE")

    async def test_http_without_session_is_denied(self) -> None:
        with set_current_mcp_session_id(None):
            raw = await server.get_cookies_resource.fn("inst-A")
        self._not_found(raw)
