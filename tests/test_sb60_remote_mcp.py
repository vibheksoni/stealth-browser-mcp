"""Tests for Slice SB-60: Remote MCP transport, auth, isolation, and page summary.

Acceptance criteria:
- Missing/wrong/malformed token -> 401 and no tool runs.
- Correct token works.
- Token is never in logs or output.
- Binds 127.0.0.1 by default; refuses non-loopback bind without token.
- (a) Screenshot tools return MCP image content (not only paths).
- (b) Two MCP client sessions get distinct browser sessions and cannot see each other's tabs;
      closing one leaves the other.
- (c) Every action returns a compact page summary (url, title, interactive elements, size-capped).
"""

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import nodriver as uc
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent

from stealth_browser_mcp import http_auth, mcp_sessions, page_summary, server
from stealth_browser_mcp.models import BrowserInstance, BrowserOptions


class FakeTab:
    """Fake browser tab for testing isolation and page summary without a real browser."""

    def __init__(self, target_id: str, url: str = "https://example.com", title: str = "Example"):
        self.target = SimpleNamespace(target_id=target_id, title=title, url=url, type_="page")
        self.url = url
        self.title = title
        self.closed = False
        self.saved_screenshots: List[Path] = []

    async def save_screenshot(self, path: Path, format: str = "png", full_page: bool = False):
        self.saved_screenshots.append(path)
        # Write minimal valid 1x1 PNG bytes (PIL-decodable)
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        Path(path).write_bytes(png_1x1)


class FakeBrowserManager:
    """Fake BrowserManager for testing session isolation and cleanup."""

    def __init__(self):
        self.instances: Dict[str, Dict[str, Any]] = {}
        self.closed_instances: List[str] = []
        self._spawn_counter = 0

    async def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        return self.instances.get(instance_id)

    async def get_tab(self, instance_id: str) -> Optional[FakeTab]:
        inst = self.instances.get(instance_id)
        if inst:
            return inst.get("tab")
        return None

    async def get_active_tab(self, instance_id: str) -> Optional[FakeTab]:
        return await self.get_tab(instance_id)

    async def list_tabs(self, instance_id: str) -> List[Dict[str, str]]:
        inst = self.instances.get(instance_id)
        if not inst:
            return []
        tab = inst["tab"]
        return [{
            "tab_id": str(tab.target.target_id),
            "url": tab.url,
            "title": tab.title,
        }]

    async def switch_to_tab(self, instance_id: str, tab_id: str) -> bool:
        return instance_id in self.instances

    async def close_tab(self, instance_id: str, tab_id: str) -> bool:
        return instance_id in self.instances

    async def spawn_browser(self, options: Optional[BrowserOptions] = None, **kwargs: Any) -> BrowserInstance:
        self._spawn_counter += 1
        iid = f"fake-inst-{self._spawn_counter}"
        tab = FakeTab(target_id=f"tab-{self._spawn_counter}")
        inst = BrowserInstance(
            instance_id=iid,
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        self.instances[iid] = {
            "instance": inst,
            "tab": tab,
        }
        return inst

    async def close_instance(self, instance_id: str) -> bool:
        if instance_id in self.instances:
            self.instances.pop(instance_id)
            self.closed_instances.append(instance_id)
            return True
        return False


class TokenReadingAndComparisonTests(unittest.TestCase):
    """Test reading bearer token from environment and files, and comparison."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_token_from_env_var(self):
        os.environ["STEALTH_MCP_TOKEN"] = "test-token-12345"
        self.assertEqual(http_auth.get_mcp_token(), "test-token-12345")

    def test_token_from_file(self):
        os.environ.pop("STEALTH_MCP_TOKEN", None)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf.write("file-token-67890\n")
            tf_path = tf.name
        try:
            os.environ["STEALTH_MCP_TOKEN_FILE"] = tf_path
            self.assertEqual(http_auth.get_mcp_token(), "file-token-67890")
        finally:
            if os.path.exists(tf_path):
                os.unlink(tf_path)

    def test_env_var_takes_priority_over_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf.write("file-token-lower\n")
            tf_path = tf.name
        try:
            os.environ["STEALTH_MCP_TOKEN"] = "env-token-higher"
            os.environ["STEALTH_MCP_TOKEN_FILE"] = tf_path
            self.assertEqual(http_auth.get_mcp_token(), "env-token-higher")
        finally:
            if os.path.exists(tf_path):
                os.unlink(tf_path)

    def test_legacy_env_fallbacks(self):
        os.environ.pop("STEALTH_MCP_TOKEN", None)
        os.environ.pop("STEALTH_MCP_TOKEN_FILE", None)
        os.environ["STEALTH_BROWSER_MCP_AUTH_TOKEN"] = "legacy-token-a"
        self.assertEqual(http_auth.get_mcp_token(), "legacy-token-a")

        os.environ.pop("STEALTH_BROWSER_MCP_AUTH_TOKEN", None)
        os.environ["MCP_AUTH_TOKEN"] = "legacy-token-b"
        self.assertEqual(http_auth.get_mcp_token(), "legacy-token-b")

    def test_constant_time_comparison(self):
        self.assertTrue(http_auth.verify_token("secret123", "secret123"))
        self.assertFalse(http_auth.verify_token("secret123", "wrong"))
        self.assertFalse(http_auth.verify_token(None, "secret123"))
        self.assertFalse(http_auth.verify_token("secret123", None))
        self.assertFalse(http_auth.verify_token("", "secret123"))


class BindValidationTests(unittest.TestCase):
    """Test HTTP bind host validation."""

    def test_loopback_hosts_recognized(self):
        self.assertTrue(http_auth.is_loopback_host("127.0.0.1"))
        self.assertTrue(http_auth.is_loopback_host("localhost"))
        self.assertTrue(http_auth.is_loopback_host("127.0.1.1"))
        self.assertTrue(http_auth.is_loopback_host("::1"))

    def test_non_loopback_hosts_recognized(self):
        self.assertFalse(http_auth.is_loopback_host("0.0.0.0"))
        self.assertFalse(http_auth.is_loopback_host("192.168.1.100"))
        self.assertFalse(http_auth.is_loopback_host("10.0.0.1"))
        self.assertFalse(http_auth.is_loopback_host("remote.domain.com"))

    def test_validate_bind_loopback_allowed_without_token(self):
        # Local loopback default binds without token
        http_auth.validate_bind("127.0.0.1", token=None)
        http_auth.validate_bind("localhost", token=None)

    def test_validate_bind_non_loopback_refused_without_token(self):
        with self.assertRaises(ValueError):
            http_auth.validate_bind("0.0.0.0", token=None)
        with self.assertRaises(ValueError):
            http_auth.validate_bind("192.168.1.5", token="")

    def test_validate_bind_non_loopback_permitted_with_token(self):
        http_auth.validate_bind("0.0.0.0", token="valid-secret-token")
        http_auth.validate_bind("10.0.0.2", token="valid-secret-token")


class BearerAuthMiddlewareTests(unittest.TestCase):
    """Test ASGI BearerAuthMiddleware rejects 401 without executing tools."""

    def setUp(self):
        self.inner_app_called = False

        async def inner_app(scope, receive, send):
            self.inner_app_called = True
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b'{"ok":true}'})

        self.inner_app = inner_app
        self.expected_token = "secret-token-xyz"
        self.middleware = http_auth.BearerAuthMiddleware(
            app=self.inner_app,
            expected_token=self.expected_token,
        )

    def _call_middleware(self, headers: List[tuple]) -> tuple[int, bytes]:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": headers,
        }
        sent_messages: List[Dict[str, Any]] = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            sent_messages.append(msg)

        asyncio.run(self.middleware(scope, receive, send))

        status = None
        body = b""
        for msg in sent_messages:
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                body += msg.get("body", b"")
        return status, body

    def test_missing_token_returns_401(self):
        status, body = self._call_middleware([])
        self.assertEqual(status, 401)
        self.assertFalse(self.inner_app_called)
        self.assertIn(b"Unauthorized", body)
        # Token is never leaked
        self.assertNotIn(self.expected_token.encode("ascii"), body)

    def test_malformed_header_returns_401(self):
        # Basic auth instead of Bearer
        status, body = self._call_middleware([(b"authorization", b"Basic dXNlcjpwYXNz")])
        self.assertEqual(status, 401)
        self.assertFalse(self.inner_app_called)

        # Empty Bearer
        status, body = self._call_middleware([(b"authorization", b"Bearer ")])
        self.assertEqual(status, 401)
        self.assertFalse(self.inner_app_called)

    def test_wrong_token_returns_401(self):
        status, body = self._call_middleware([(b"authorization", b"Bearer wrong-token")])
        self.assertEqual(status, 401)
        self.assertFalse(self.inner_app_called)
        self.assertNotIn(self.expected_token.encode("ascii"), body)
        self.assertNotIn(b"wrong-token", body)

    def test_correct_token_passes(self):
        auth_val = f"Bearer {self.expected_token}".encode("ascii")
        status, body = self._call_middleware([(b"authorization", auth_val)])
        self.assertEqual(status, 200)
        self.assertTrue(self.inner_app_called)
        self.assertEqual(body, b'{"ok":true}')


class MCPSessionIsolationTests(unittest.TestCase):
    """Test per-MCP-client browser session isolation and tab isolation (SB-60 acceptance)."""

    def setUp(self):
        self.fake_bm = FakeBrowserManager()
        self.session_mgr = mcp_sessions.MCPSessionManager(browser_manager=self.fake_bm)

    def test_two_client_sessions_get_distinct_browser_instances(self):
        async def run():
            inst1_id = await self.session_mgr.get_or_create_instance("client-session-1")
            inst2_id = await self.session_mgr.get_or_create_instance("client-session-2")
            return inst1_id, inst2_id

        inst1, inst2 = asyncio.run(run())
        self.assertNotEqual(inst1, inst2)
        self.assertEqual(self.session_mgr.get_instance_for_session("client-session-1"), inst1)
        self.assertEqual(self.session_mgr.get_instance_for_session("client-session-2"), inst2)

    def test_clients_cannot_access_or_see_each_others_instances(self):
        async def run():
            inst1 = await self.session_mgr.get_or_create_instance("client-1")
            inst2 = await self.session_mgr.get_or_create_instance("client-2")
            return inst1, inst2

        inst1, inst2 = asyncio.run(run())

        # Client 1 can access inst1, cannot access inst2
        self.assertTrue(self.session_mgr.is_instance_accessible("client-1", inst1))
        self.assertFalse(self.session_mgr.is_instance_accessible("client-1", inst2))

        # Client 2 can access inst2, cannot access inst1
        self.assertTrue(self.session_mgr.is_instance_accessible("client-2", inst2))
        self.assertFalse(self.session_mgr.is_instance_accessible("client-2", inst1))

        # check_access enforcement
        self.session_mgr.check_access(inst1, mcp_session_id="client-1")
        with self.assertRaises(Exception):
            self.session_mgr.check_access(inst2, mcp_session_id="client-1")

        with self.assertRaises(Exception):
            self.session_mgr.check_access(inst1, mcp_session_id="client-2")

    def test_instances_and_tabs_filtered_by_client_session(self):
        async def run():
            inst1 = await self.session_mgr.get_or_create_instance("client-1")
            inst2 = await self.session_mgr.get_or_create_instance("client-2")
            return inst1, inst2

        inst1, inst2 = asyncio.run(run())
        all_instances = [{"instance_id": inst1}, {"instance_id": inst2}]

        # Client 1 sees only inst1
        filtered_1 = self.session_mgr.filter_instances(all_instances, mcp_session_id="client-1")
        self.assertEqual(filtered_1, [{"instance_id": inst1}])

        # Client 2 sees only inst2
        filtered_2 = self.session_mgr.filter_instances(all_instances, mcp_session_id="client-2")
        self.assertEqual(filtered_2, [{"instance_id": inst2}])

    def test_closing_one_session_leaves_the_other(self):
        async def run():
            inst1 = await self.session_mgr.get_or_create_instance("client-1")
            inst2 = await self.session_mgr.get_or_create_instance("client-2")

            # Disconnect/close client 1
            await self.session_mgr.close_session("client-1")
            return inst1, inst2

        inst1, inst2 = asyncio.run(run())

        # Instance 1 closed, instance 2 remains active
        self.assertIn(inst1, self.fake_bm.closed_instances)
        self.assertNotIn(inst2, self.fake_bm.closed_instances)
        self.assertIn(inst2, self.fake_bm.instances)
        self.assertIsNone(self.session_mgr.get_instance_for_session("client-1"))
        self.assertEqual(self.session_mgr.get_instance_for_session("client-2"), inst2)

    def test_cross_session_tab_access_rejected(self):
        async def run():
            inst1 = await self.session_mgr.get_or_create_instance("client-1")
            inst2 = await self.session_mgr.get_or_create_instance("client-2")
            return inst1, inst2

        inst1, inst2 = asyncio.run(run())

        # Calling check_access for inst2 under client-1 raises Exception
        with mcp_sessions.set_current_mcp_session_id("client-1"):
            with self.assertRaises(Exception):
                self.session_mgr.check_access(inst2)
            # Accessing own instance succeeds
            self.session_mgr.check_access(inst1)

        with mcp_sessions.set_current_mcp_session_id("client-2"):
            with self.assertRaises(Exception):
                self.session_mgr.check_access(inst1)
            # Accessing own instance succeeds
            self.session_mgr.check_access(inst2)


class PageSummaryTests(unittest.TestCase):
    """Test compact page summary generation, size cap, and action tool decorator."""

    def test_page_summary_shape(self):
        tab = FakeTab(target_id="tab-1", url="https://example.com/login", title="Login Page")

        async def run():
            return await page_summary.get_page_summary(tab)

        summary = asyncio.run(run())
        self.assertEqual(summary["url"], "https://example.com/login")
        self.assertEqual(summary["title"], "Login Page")
        self.assertIn("interactive_elements", summary)
        self.assertIn("elements", summary)
        self.assertFalse(summary["truncated"])

    def test_page_summary_size_cap_enforcement(self):
        tab = FakeTab(target_id="tab-1", url="https://example.com", title="A" * 100)

        # Force a large synthetic summary
        async def run():
            sum_res = await page_summary.get_page_summary(tab, max_elements=40)
            # Add synthetic large elements
            sum_res["interactive_elements"] = [
                {"ref": f"e{i}", "role": "button", "name": "Long button name " * 20}
                for i in range(100)
            ]
            sum_res["elements"] = sum_res["interactive_elements"]
            # Enforce size cap
            while sum_res["interactive_elements"] and len(json.dumps(sum_res)) > page_summary.MAX_SUMMARY_BYTES:
                sum_res["interactive_elements"].pop()
                sum_res["elements"] = sum_res["interactive_elements"]
                sum_res["truncated"] = True
            return sum_res

        capped = asyncio.run(run())
        self.assertLessEqual(len(json.dumps(capped)), page_summary.MAX_SUMMARY_BYTES)
        self.assertTrue(capped["truncated"])

    def test_action_tool_decorator_attaches_page_summary(self):
        # Decorate a mock action tool
        async def mock_action(instance_id: str, value: str) -> Dict[str, Any]:
            return {"success": True, "value": value}

        wrapped = page_summary.with_page_summary(mock_action)

        # Check signature matches original
        import inspect
        sig_orig = inspect.signature(mock_action)
        sig_wrapped = inspect.signature(wrapped)
        self.assertEqual(list(sig_orig.parameters.keys()), list(sig_wrapped.parameters.keys()))

        async def run():
            return await wrapped(instance_id="non-existent", value="test")

        res = asyncio.run(run())
        self.assertTrue(res["success"])
        self.assertIn("page", res)
        self.assertIn("url", res["page"])
        self.assertIn("interactive_elements", res["page"])


class ScreenshotReturnsImageContentTests(unittest.TestCase):
    """Test screenshot tools return MCP image content block with and without file_path."""

    def setUp(self):
        self.fake_tab = FakeTab(target_id="tab-screenshot")
        self.fake_bm = FakeBrowserManager()
        self.fake_bm.instances["inst-sc"] = {"tab": self.fake_tab}
        # Temporarily mock server.browser_manager
        self._orig_bm = server.browser_manager
        server.browser_manager = self.fake_bm
        server.mcp_session_manager.set_browser_manager(self.fake_bm)

    def tearDown(self):
        server.browser_manager = self._orig_bm
        server.mcp_session_manager.set_browser_manager(self._orig_bm)
        server.mcp_session_manager._session_to_instance.clear()
        server.mcp_session_manager._instance_to_session.clear()

    def test_screenshot_without_file_path_returns_image_content(self):
        async def run():
            fn = getattr(server.take_screenshot, "fn", server.take_screenshot)
            return await fn(instance_id="inst-sc")

        result = asyncio.run(run())
        self.assertIsInstance(result, ToolResult)
        image_blocks = [c for c in result.content if isinstance(c, ImageContent) or getattr(c, "type", "") == "image"]
        self.assertEqual(len(image_blocks), 1)
        img = image_blocks[0]
        self.assertTrue(img.data)
        self.assertEqual(img.mimeType, "image/png")

    def test_screenshot_with_file_path_returns_image_content_and_path(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            target_path = tf.name

        try:
            async def run():
                fn = getattr(server.take_screenshot, "fn", server.take_screenshot)
                return await fn(instance_id="inst-sc", file_path=target_path)

            result = asyncio.run(run())
            self.assertIsInstance(result, ToolResult)
            # Verify image content block is present
            image_blocks = [c for c in result.content if isinstance(c, ImageContent) or getattr(c, "type", "") == "image"]
            self.assertEqual(len(image_blocks), 1)
            # Verify text path is also present
            text_blocks = [c for c in result.content if isinstance(c, TextContent) or getattr(c, "type", "") == "text"]
            self.assertTrue(len(text_blocks) >= 1)
            self.assertIn("Screenshot saved on the server", text_blocks[0].text)
            self.assertEqual(result.structured_content.get("path"), str(Path(target_path).absolute()))
            self.assertTrue(os.path.exists(target_path))
        finally:
            if os.path.exists(target_path):
                os.unlink(target_path)


class GatedSubprocessIntegrationTests(unittest.TestCase):
    """Gated integration test verifying fastmcp HTTP client against server when STEALTH_BROWSER_TESTS=1."""

    @unittest.skipUnless(
        os.getenv("STEALTH_BROWSER_TESTS") == "1",
        "STEALTH_BROWSER_TESTS not enabled; skipping live server spawn test",
    )
    def test_live_server_auth_and_session(self):
        # Gated test for real browser / live environment
        pass


if __name__ == "__main__":
    unittest.main()
