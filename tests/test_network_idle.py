import asyncio
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp.network_idle import (
    DEFAULT_IDLE_TIME,
    NetworkIdleTracker,
    wait_for_network_idle,
)
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp import server


class FakeNetworkTab:
    """Tab that records sent CDP commands, dispatches handlers, and evaluates expressions."""

    def __init__(self, url="https://example.com/test"):
        self.handlers = {}
        self.sent_commands = []
        self.url = url
        self.target = SimpleNamespace(target_id="fake-target-1")
        self.evaluations = []
        self._eval_responses = {}

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event_type, event):
        for handler in list(self.handlers.get(event_type, [])):
            handler(event)

    async def send(self, command):
        self.sent_commands.append(command)
        return None

    async def evaluate(self, expression):
        self.evaluations.append(expression)
        for pattern, response in self._eval_responses.items():
            if pattern in expression:
                if callable(response):
                    return response()
                return response
        if expression == "window.location.href":
            return self.url
        if expression == "document.title":
            return "Test Title"
        return True


class FakeBrowser:
    """Minimal fake browser holding a list of tabs."""

    def __init__(self, tab):
        self.tabs = [tab]
        self.main_tab = tab

    async def update_targets(self):
        return None

    async def get(self, url, new_tab=False, new_window=False):
        return self.main_tab


class FakeInstance(SimpleNamespace):
    """Fake browser instance for testing."""

    def update_activity(self):
        self.last_activity = datetime.now()


def create_manager_with_tab(tab, instance_id="test-inst"):
    manager = BrowserManager()
    browser = FakeBrowser(tab)
    manager._instances[instance_id] = {
        "browser": browser,
        "tab": tab,
        "instance": FakeInstance(
            instance_id=instance_id,
            state="ready",
            current_url=tab.url,
            title="Test",
            last_activity=datetime.now(),
        ),
        "options": BrowserOptions(),
        "navigation_count": 0,
        "init_scripts": [],
        "configured_tabs": {tab.target.target_id},
        "idle_timeout_seconds": 600,
    }
    return manager


class NetworkIdleTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_when_no_requests(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.05)
        start = time.monotonic()
        idle = await tracker.wait(timeout=1.0)
        elapsed = time.monotonic() - start
        self.assertTrue(idle)
        self.assertLess(elapsed, 0.3)
        await tracker.stop()

    async def test_delayed_request_simulation_returns_on_idle_not_2s(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.1)
        await tracker.start()

        # Schedule a simulated request starting at 0.05s and finishing at 0.15s
        loop = asyncio.get_running_loop()

        def start_request():
            tab.dispatch(
                uc.cdp.network.RequestWillBeSent,
                SimpleNamespace(
                    request_id="req-1",
                    request=SimpleNamespace(url="https://example.com/api/delayed-fetch"),
                ),
            )

        def finish_request():
            tab.dispatch(
                uc.cdp.network.LoadingFinished,
                SimpleNamespace(request_id="req-1"),
            )

        loop.call_later(0.05, start_request)
        loop.call_later(0.15, finish_request)

        start = time.monotonic()
        idle = await tracker.wait(timeout=2.0)
        elapsed = time.monotonic() - start

        self.assertTrue(idle)
        # Idle completes at request finish (0.15s) + idle_time (0.10s) = ~0.25s
        # Crucially: it returns on idle (< 0.8s), NOT at 2.0s!
        self.assertLess(elapsed, 0.8)
        self.assertGreaterEqual(elapsed, 0.2)
        await tracker.stop()

    async def test_concurrent_requests_all_must_finish(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.05)
        await tracker.start()
        loop = asyncio.get_running_loop()

        # Start req-1 and req-2
        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(request_id="req-1", request=SimpleNamespace(url="https://example.com/1")),
        )
        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(request_id="req-2", request=SimpleNamespace(url="https://example.com/2")),
        )
        self.assertEqual(len(tracker.in_flight), 2)

        # Finish req-1 at 0.05s
        loop.call_later(
            0.05,
            lambda: tab.dispatch(
                uc.cdp.network.LoadingFinished, SimpleNamespace(request_id="req-1")
            ),
        )
        # Finish req-2 at 0.15s
        loop.call_later(
            0.15,
            lambda: tab.dispatch(
                uc.cdp.network.LoadingFinished, SimpleNamespace(request_id="req-2")
            ),
        )

        start = time.monotonic()
        idle = await tracker.wait(timeout=2.0)
        elapsed = time.monotonic() - start

        self.assertTrue(idle)
        # Should resolve after req-2 finishes (0.15s + 0.05s = ~0.20s)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.8)
        await tracker.stop()

    async def test_new_request_resets_idle_timer(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.1)
        await tracker.start()
        loop = asyncio.get_running_loop()

        # req-1 starts and finishes at 0.02s
        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(request_id="req-1", request=SimpleNamespace(url="https://example.com/1")),
        )
        tab.dispatch(
            uc.cdp.network.LoadingFinished,
            SimpleNamespace(request_id="req-1"),
        )

        # At 0.06s (before 0.1s idle timer fires), req-2 arrives
        def start_req2():
            tab.dispatch(
                uc.cdp.network.RequestWillBeSent,
                SimpleNamespace(request_id="req-2", request=SimpleNamespace(url="https://example.com/2")),
            )

        def finish_req2():
            tab.dispatch(
                uc.cdp.network.LoadingFinished,
                SimpleNamespace(request_id="req-2"),
            )

        loop.call_later(0.06, start_req2)
        loop.call_later(0.16, finish_req2)

        start = time.monotonic()
        idle = await tracker.wait(timeout=2.0)
        elapsed = time.monotonic() - start

        self.assertTrue(idle)
        # Idle timer was reset when req-2 started, so it should finish after 0.16s + 0.10s = ~0.26s
        self.assertGreaterEqual(elapsed, 0.22)
        self.assertLess(elapsed, 0.8)
        await tracker.stop()

    async def test_loading_failed_decrements_in_flight(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.05)
        await tracker.start()

        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(request_id="req-fail", request=SimpleNamespace(url="https://example.com/fail")),
        )
        self.assertEqual(len(tracker.in_flight), 1)

        loop = asyncio.get_running_loop()
        loop.call_later(
            0.05,
            lambda: tab.dispatch(
                uc.cdp.network.LoadingFailed,
                SimpleNamespace(request_id="req-fail"),
            ),
        )

        idle = await tracker.wait(timeout=1.0)
        self.assertTrue(idle)
        self.assertEqual(len(tracker.in_flight), 0)
        await tracker.stop()

    async def test_internal_urls_ignored(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.05)
        await tracker.start()

        # chrome:// URL should be ignored
        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(
                request_id="internal-1",
                request=SimpleNamespace(url="chrome://resources/logo.png"),
            ),
        )
        self.assertEqual(len(tracker.in_flight), 0)

        idle = await tracker.wait(timeout=0.5)
        self.assertTrue(idle)
        await tracker.stop()

    async def test_timeout_when_requests_never_finish(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.05)
        await tracker.start()

        tab.dispatch(
            uc.cdp.network.RequestWillBeSent,
            SimpleNamespace(request_id="hanging", request=SimpleNamespace(url="https://example.com/hang")),
        )
        self.assertEqual(len(tracker.in_flight), 1)

        start = time.monotonic()
        idle = await tracker.wait(timeout=0.1)
        elapsed = time.monotonic() - start

        self.assertFalse(idle)
        self.assertLess(elapsed, 0.3)
        await tracker.stop()

    async def test_stop_unregisters_handlers(self):
        tab = FakeNetworkTab()
        tracker = NetworkIdleTracker(tab, idle_time=0.1)
        await tracker.start()
        self.assertTrue(len(tab.handlers.get(uc.cdp.network.RequestWillBeSent, [])) > 0)

        await tracker.stop()
        self.assertEqual(len(tab.handlers.get(uc.cdp.network.RequestWillBeSent, [])), 0)
        self.assertEqual(len(tab.handlers.get(uc.cdp.network.LoadingFinished, [])), 0)
        self.assertEqual(len(tab.handlers.get(uc.cdp.network.LoadingFailed, [])), 0)

    async def test_convenience_wait_for_network_idle(self):
        tab = FakeNetworkTab()
        start = time.monotonic()
        idle = await wait_for_network_idle(tab, idle_time=0.05, timeout=1.0)
        elapsed = time.monotonic() - start
        self.assertTrue(idle)
        self.assertLess(elapsed, 0.3)


class NavigationNetworkIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_networkidle_returns_on_idle(self):
        tab = FakeNetworkTab(url="https://example.com/page")
        manager = create_manager_with_tab(tab, "inst-1")

        # Simulate requests during navigation
        loop = asyncio.get_running_loop()

        def send_page_requests():
            tab.dispatch(
                uc.cdp.network.RequestWillBeSent,
                SimpleNamespace(
                    request_id="nav-doc",
                    request=SimpleNamespace(url="https://example.com/page"),
                ),
            )
            loop.call_later(
                0.05,
                lambda: tab.dispatch(
                    uc.cdp.network.LoadingFinished,
                    SimpleNamespace(request_id="nav-doc"),
                ),
            )

        loop.call_later(0.01, send_page_requests)

        start = time.monotonic()
        result = await manager.navigate(
            instance_id="inst-1",
            url="https://example.com/page",
            wait_until="networkidle",
            timeout=5000,
        )
        elapsed = time.monotonic() - start

        self.assertTrue(result["success"])
        self.assertEqual(result["url"], "https://example.com/page")
        # With 0.5s idle window and 0.05s request, it returns around ~0.55s-0.7s, well under 2.0s!
        self.assertLess(elapsed, 1.5)


class WaitForToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_networkidle(self):
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "inst-1")

        start = time.monotonic()
        result = await manager.wait_for(
            instance_id="inst-1",
            networkidle=True,
            timeout=2000,
        )
        elapsed = time.monotonic() - start

        self.assertTrue(result["success"])
        self.assertIn("networkidle", result["conditions"])
        self.assertLess(elapsed, 1.0)

    async def test_wait_for_url_match(self):
        tab = FakeNetworkTab(url="https://example.com/dashboard/settings")
        manager = create_manager_with_tab(tab, "inst-1")

        result = await manager.wait_for(
            instance_id="inst-1",
            url="dashboard",
            timeout=1000,
        )
        self.assertTrue(result["success"])
        self.assertIn("url:dashboard", result["conditions"])

    async def test_wait_for_text_on_page(self):
        tab = FakeNetworkTab(url="https://example.com")
        tab._eval_responses["Welcome"] = True
        manager = create_manager_with_tab(tab, "inst-1")

        result = await manager.wait_for(
            instance_id="inst-1",
            text="Welcome",
            timeout=1000,
        )
        self.assertTrue(result["success"])
        self.assertIn("text:Welcome", result["conditions"])

    async def test_wait_for_fallback_delay(self):
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "inst-1")

        start = time.monotonic()
        result = await manager.wait_for(
            instance_id="inst-1",
            timeout=100,
        )
        elapsed = time.monotonic() - start

        self.assertTrue(result["success"])
        self.assertIn("delay:100ms", result["conditions"])
        self.assertGreaterEqual(elapsed, 0.08)

    async def test_wait_for_timeout_raises(self):
        tab = FakeNetworkTab(url="https://example.com/home")
        manager = create_manager_with_tab(tab, "inst-1")

        with self.assertRaises(TimeoutError):
            await manager.wait_for(
                instance_id="inst-1",
                url="never-matching-url",
                timeout=100,
            )

    async def test_wait_for_timeout_no_raise_option(self):
        tab = FakeNetworkTab(url="https://example.com/home")
        manager = create_manager_with_tab(tab, "inst-1")

        result = await manager.wait_for(
            instance_id="inst-1",
            url="never-matching-url",
            timeout=100,
            raise_on_timeout=False,
        )
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    async def test_wait_for_selector_visible(self):
        from unittest.mock import patch
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "inst-1")

        with patch("stealth_browser_mcp.dom_handler.DOMHandler.wait_for_element", return_value=True) as mock_wait:
            result = await manager.wait_for(
                instance_id="inst-1",
                selector="#submit-btn",
                timeout=1000,
            )
            self.assertTrue(result["success"])
            self.assertIn("selector:#submit-btn", result["conditions"])
            mock_wait.assert_called_once()
            _, kwargs = mock_wait.call_args
            self.assertTrue(kwargs["visible"])

    async def test_wait_for_selector_present(self):
        from unittest.mock import patch
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "inst-1")

        with patch("stealth_browser_mcp.dom_handler.DOMHandler.wait_for_element", return_value=True) as mock_wait:
            result = await manager.wait_for(
                instance_id="inst-1",
                selector="#hidden-input",
                present=True,
                timeout=1000,
            )
            self.assertTrue(result["success"])
            self.assertIn("selector:#hidden-input", result["conditions"])
            mock_wait.assert_called_once()
            _, kwargs = mock_wait.call_args
            self.assertFalse(kwargs["visible"])

    async def test_wait_for_selector_timeout(self):
        from unittest.mock import patch
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "inst-1")

        with patch("stealth_browser_mcp.dom_handler.DOMHandler.wait_for_element", return_value=False):
            with self.assertRaises(TimeoutError):
                await manager.wait_for(
                    instance_id="inst-1",
                    selector="#missing",
                    timeout=50,
                )

    async def test_wait_for_server_tool_registered(self):
        # Test calling the MCP tool via server module
        tab = FakeNetworkTab(url="https://example.com")
        manager = create_manager_with_tab(tab, "tool-inst")

        # Temporarily wire manager to server.browser_manager
        orig_manager = server.browser_manager
        server.browser_manager = manager
        try:
            wait_fn = getattr(server.wait_for, "fn", server.wait_for)
            result = await wait_fn(
                instance_id="tool-inst",
                timeout=50,
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["instance_id"], "tool-inst")
        finally:
            server.browser_manager = orig_manager


@unittest.skipUnless(
    os.getenv("STEALTH_BROWSER_TESTS") == "1",
    "Real browser tests require STEALTH_BROWSER_TESTS=1",
)
class RealBrowserNetworkIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_delayed_fetch_returns_on_idle_not_2s(self):
        """Gated test verifying delayed fetch returns on idle in a real browser."""
        manager = BrowserManager()
        options = BrowserOptions(headless=True)
        instance = await manager.spawn_browser(options)
        try:
            # Inline delayed-fetch fixture page
            html_content = (
                "<html><body><div id='status'>Waiting</div><script>"
                "setTimeout(() => {"
                "  const el = document.getElementById('status');"
                "  el.textContent = 'Fetching';"
                "  fetch('data:text/plain,delayed_content').catch(() => {}).then(() => {"
                "    el.textContent = 'Done';"
                "  });"
                "}, 200);"
                "</script></body></html>"
            )
            url = f"data:text/html,{html_content}"

            start = time.monotonic()
            result = await manager.navigate(
                instance_id=instance.instance_id,
                url=url,
                wait_until="networkidle",
                timeout=10000,
            )
            elapsed = time.monotonic() - start

            self.assertTrue(result["success"])
            # The delayed fetch happens around ~200ms and resolves, plus ~500ms idle window
            # It should complete cleanly and well under 5s
            self.assertLess(elapsed, 4.0)

            # Test wait_for tool on the instance
            wait_res = await manager.wait_for(
                instance_id=instance.instance_id,
                selector="#status",
                text="Done",
                timeout=5000,
            )
            self.assertTrue(wait_res["success"])
        finally:
            await manager.close_instance(instance.instance_id)


if __name__ == "__main__":
    unittest.main()
