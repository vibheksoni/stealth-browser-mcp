"""SB-78: closing a browser must not leave it in nodriver's global registry (memory leak found by the soak)."""
import asyncio
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stealth_browser_mcp import nodriver_registry  # noqa: E402
from stealth_browser_mcp.browser_manager import BrowserManager  # noqa: E402


class FakeConn:
    handlers: dict = {}

    async def send(self, *_a: Any, **_k: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class FakeBrowser:
    def __init__(self) -> None:
        self.connection = FakeConn()
        self.targets: list = []
        self._process = None


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        from nodriver.core import util
        self.util = util

    def test_release_removes_and_reports(self) -> None:
        b = FakeBrowser()
        self.util.__registered__instances__.add(b)
        before = nodriver_registry.registered_count()
        self.assertTrue(nodriver_registry.release_browser(b))
        self.assertEqual(nodriver_registry.registered_count(), before - 1)
        self.assertFalse(nodriver_registry.release_browser(b))
        self.assertFalse(nodriver_registry.release_browser(None))

    def test_shutdown_and_disconnect_release_registry_entry(self) -> None:
        manager = BrowserManager()
        for method in ("_shutdown_browser", "_disconnect_browser"):
            b = FakeBrowser()
            self.util.__registered__instances__.add(b)
            asyncio.run(getattr(manager, method)(b))
            self.assertNotIn(b, self.util.__registered__instances__, method)

    def test_release_happens_even_if_shutdown_raises(self) -> None:
        manager = BrowserManager()
        b = FakeBrowser()
        self.util.__registered__instances__.add(b)

        async def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("boom")

        manager._shutdown_browser_impl = boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            asyncio.run(manager._shutdown_browser(b))
        self.assertNotIn(b, self.util.__registered__instances__)


if __name__ == "__main__":
    unittest.main()
