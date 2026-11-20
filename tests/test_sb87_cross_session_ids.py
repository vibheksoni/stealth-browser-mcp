"""SB-87 / review F18, F19, F20: request, seed, and element ids stay session-scoped."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from typing import Any, Dict, Optional
from unittest import mock

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.mcp_sessions import MCPSessionManager
from stealth_browser_mcp.models import NetworkRequest, NetworkResponse
from stealth_browser_mcp.network_interceptor import NetworkInterceptor
from stealth_browser_mcp.progressive_element_cloner import ProgressiveElementCloner


class FakeBrowserManager:
    def __init__(self) -> None:
        self._instances: Dict[str, dict] = {}
        self._session_infos: Dict[str, dict] = {}
        self.close_calls = []

    def add_instance(self, instance_id: str, session_id: Optional[str] = None, session_info: Optional[dict] = None) -> None:
        self._instances[instance_id] = {"session_id": session_id}
        if session_info:
            self._session_infos[instance_id] = session_info

    async def find_instance_id_for_session_or_instance(self, session_or_instance_id: str) -> Optional[str]:
        if session_or_instance_id in self._instances:
            return session_or_instance_id
        for instance_id, data in self._instances.items():
            if data.get("session_id") == session_or_instance_id:
                return instance_id
        if session_or_instance_id in self._session_infos:
            return session_or_instance_id
        return None

    async def get_session_info(self, instance_id: str) -> Optional[dict]:
        return self._session_infos.get(instance_id)

    async def get_instance(self, instance_id: str) -> Optional[dict]:
        return self._instances.get(instance_id)


def _request(request_id: str, instance_id: str, url: str) -> NetworkRequest:
    return NetworkRequest(
        request_id=request_id,
        instance_id=instance_id,
        url=url,
        method="GET",
        headers={"x-secret": "A-HEADER"},
        timestamp=datetime.now(),
        resource_type="document",
    )


class RequestDetailsIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Live F18 exploit: B get_request_details(A's request_id) leaked URL and instance_id."""

    def setUp(self) -> None:
        self.interceptor = NetworkInterceptor()
        self.sess = MCPSessionManager()
        self.sess.register_session("session-A", "inst-A")
        self.sess.register_session("session-B", "inst-B")
        self._real_interceptor = server.network_interceptor
        self._real_sm = server.mcp_session_manager
        server.network_interceptor = self.interceptor
        server.mcp_session_manager = self.sess
        mcp_sessions.mcp_session_manager = self.sess

    def tearDown(self) -> None:
        server.network_interceptor = self._real_interceptor
        server.mcp_session_manager = self._real_sm
        mcp_sessions.mcp_session_manager = self._real_sm

    def _seed(self) -> None:
        req = _request("AE4665deadbeef", "inst-A", "https://example.test/index.html?tok=A-URL-SECRET")
        self.interceptor._requests[req.request_id] = req
        self.interceptor._instance_requests["inst-A"] = [req.request_id]
        self.interceptor._responses[req.request_id] = NetworkResponse(
            request_id=req.request_id,
            status=200,
            headers={"set-cookie": "secret=A-ONLY"},
        )

    async def test_session_b_cannot_read_session_a_request_or_response(self) -> None:
        self._seed()
        with mock.patch(
            "stealth_browser_mcp.mcp_sessions.get_current_mcp_session_id",
            return_value="session-B",
        ):
            details = await server.get_request_details.fn("AE4665deadbeef")
            response = await server.get_response_details.fn("AE4665deadbeef")
        self.assertIsNone(details)
        self.assertIsNone(response)

    async def test_owner_can_read_own_request(self) -> None:
        self._seed()
        with mock.patch(
            "stealth_browser_mcp.mcp_sessions.get_current_mcp_session_id",
            return_value="session-A",
        ):
            details = await server.get_request_details.fn("AE4665deadbeef")
        self.assertIsNotNone(details)
        self.assertIn("A-URL-SECRET", details["url"])
        self.assertEqual(details["instance_id"], "inst-A")


class SaveToSeedIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Live F19 exploit: B save_to_seed(session_id=A, close_first=true) stole cookies and closed A."""

    def setUp(self) -> None:
        self.fake_bm = FakeBrowserManager()
        self.fake_bm.add_instance(
            "inst-A",
            session_id="seed-sess-A",
            session_info={"session_id": "seed-sess-A"},
        )
        self.sess = MCPSessionManager(browser_manager=self.fake_bm)
        self.sess.register_session("session-A", "inst-A")
        self.sess.register_session("session-B", "inst-B")
        self._real_bm = server.browser_manager
        self._real_sm = server.mcp_session_manager
        server.browser_manager = self.fake_bm
        server.mcp_session_manager = self.sess
        mcp_sessions.mcp_session_manager = self.sess

    def tearDown(self) -> None:
        server.browser_manager = self._real_bm
        server.mcp_session_manager = self._real_sm
        mcp_sessions.mcp_session_manager = self._real_sm

    async def test_session_b_cannot_save_or_close_session_a(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.mcp_sessions.get_current_mcp_session_id",
            return_value="session-B",
        ), mock.patch("stealth_browser_mcp.server.get_current_mcp_session_id", return_value="session-B"), mock.patch(
            "stealth_browser_mcp.server.profiles.session_exists", return_value=False
        ), mock.patch("stealth_browser_mcp.server.seed_save.save_to_seed") as save, mock.patch(
            "stealth_browser_mcp.server.seed_save.close_session_browser"
        ) as closer:
            with self.assertRaises(ToolError) as ctx:
                await server.save_to_seed.fn(session_id="inst-A", seed="bsteal2", close_first=True)
            self.assertIn("belongs to another session", str(ctx.exception))
            save.assert_not_called()
            closer.assert_not_called()

    async def test_owner_can_resolve_own_instance(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.mcp_sessions.get_current_mcp_session_id",
            return_value="session-A",
        ), mock.patch("stealth_browser_mcp.server.get_current_mcp_session_id", return_value="session-A"), mock.patch(
            "stealth_browser_mcp.server.profiles.session_exists", return_value=False
        ), mock.patch(
            "stealth_browser_mcp.server.seed_save.save_to_seed",
            return_value={"status": "ok"},
        ) as save:
            result = await server.save_to_seed.fn(session_id="inst-A", seed="kept", close_first=False)
        self.assertEqual(result["status"], "ok")
        save.assert_called_once()


class ElementStoreIsolationTests(unittest.TestCase):
    """Live F20 exploit: B list_stored_elements / expand_* / clear_* on A's element."""

    def setUp(self) -> None:
        self.cloner = ProgressiveElementCloner()
        self._store: Dict[str, Any] = {}

        def get_store():
            return self._store

        def save_store(data):
            self._store = dict(data)

        self.cloner._get_store = get_store  # type: ignore[method-assign]
        self.cloner._save_store = save_store  # type: ignore[method-assign]
        self._store["elem_aaa111"] = {
            "full_data": {
                "element": {"html": {"tagName": "DIV"}, "styles": {"color": "red"}},
                "children": [{}],
            },
            "url": "https://example.test/a",
            "selector": "#secret",
            "timestamp": 1,
            "owner_session_id": "session-A",
        }

    def test_session_b_cannot_list_expand_or_clear_session_a_element(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.progressive_element_cloner.get_current_mcp_session_id",
            return_value="session-B",
        ):
            listed = self.cloner.list_stored_elements()
            self.assertEqual(listed["stored_elements"], [])
            self.assertEqual(listed["total_count"], 0)
            expanded = self.cloner.expand_styles("elem_aaa111")
            self.assertIn("not found", expanded["error"])
            children = self.cloner.expand_children("elem_aaa111")
            self.assertIn("not found", children["error"])
            cleared = self.cloner.clear_stored_element("elem_aaa111")
            self.assertIn("not found", cleared["error"])
            self.cloner.clear_all_elements()
        self.assertIn("elem_aaa111", self._store)

    def test_owner_can_list_and_clear_own_element(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.progressive_element_cloner.get_current_mcp_session_id",
            return_value="session-A",
        ):
            listed = self.cloner.list_stored_elements()
            self.assertEqual(listed["total_count"], 1)
            self.assertEqual(listed["stored_elements"][0]["element_id"], "elem_aaa111")
            expanded = self.cloner.expand_styles("elem_aaa111")
            self.assertEqual(expanded["element_id"], "elem_aaa111")
            cleared = self.cloner.clear_stored_element("elem_aaa111")
            self.assertTrue(cleared["success"])
        self.assertNotIn("elem_aaa111", self._store)


if __name__ == "__main__":
    unittest.main()
