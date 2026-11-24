"""Tests for HTTP client disconnect isolation (SB-62).

Ensures that in HTTP mode, one client disconnecting does not trigger global cleanup
(close_all or process_cleanup), while stdio mode retains per-process cleanup upon session exit.
Also verifies idempotence of shutdown_all and proper filtering of ClosedResourceError noise.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from fastmcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import server
from stealth_browser_mcp.server import _ClosedResourceNoiseFilter


class HttpDisconnectIsolationTests(unittest.TestCase):
    def test_http_mode_disconnect_does_not_close_all(self):
        fake = MagicMock()
        fake.start_idle_reaper = AsyncMock()
        fake.stop_idle_reaper = AsyncMock()
        fake.close_all = AsyncMock()

        cleanup_mock = MagicMock()

        with patch.object(server, "browser_manager", fake), \
             patch.object(server.process_cleanup, "_cleanup_all_tracked", cleanup_mock), \
             patch.object(server, "_HTTP_MODE", True):

            async def run():
                async with Client(server.mcp) as a:
                    async with Client(server.mcp) as b:
                        await a.ping()
                        await b.ping()

                    self.assertEqual(fake.close_all.call_count, 0)
                    cleanup_mock.assert_not_called()
                    await a.ping()

                self.assertEqual(fake.close_all.call_count, 0)
                cleanup_mock.assert_not_called()

            asyncio.run(run())

    def test_stdio_mode_session_end_still_closes_all(self):
        fake = MagicMock()
        fake.start_idle_reaper = AsyncMock()
        fake.stop_idle_reaper = AsyncMock()
        fake.close_all = AsyncMock()

        cleanup_mock = MagicMock()

        with patch.object(server, "browser_manager", fake), \
             patch.object(server.process_cleanup, "_cleanup_all_tracked", cleanup_mock), \
             patch.object(server, "_HTTP_MODE", False):

            async def run():
                async with Client(server.mcp) as client:
                    await client.ping()

            asyncio.run(run())

            self.assertEqual(fake.close_all.call_count, 1)
            cleanup_mock.assert_called_once()

    def test_shutdown_all_is_idempotent(self):
        fake = MagicMock()
        fake.start_idle_reaper = AsyncMock()
        fake.stop_idle_reaper = AsyncMock()
        fake.close_all = AsyncMock()

        with patch.object(server, "browser_manager", fake), \
             patch.object(server.process_cleanup, "_cleanup_all_tracked", MagicMock()):
            asyncio.run(server.shutdown_all())
            asyncio.run(server.shutdown_all())

        self.assertEqual(fake.close_all.call_count, 2)

    def test_run_shutdown_sync(self):
        fake = MagicMock()
        fake.start_idle_reaper = AsyncMock()
        fake.stop_idle_reaper = AsyncMock()
        fake.close_all = AsyncMock()

        cleanup_mock = MagicMock()

        with patch.object(server, "browser_manager", fake), \
             patch.object(server.process_cleanup, "_cleanup_all_tracked", cleanup_mock):
            server.run_shutdown_sync()

        self.assertEqual(fake.close_all.call_count, 1)
        self.assertGreaterEqual(cleanup_mock.call_count, 1)

    def test_closed_resource_filter(self):
        filter_ = _ClosedResourceNoiseFilter()

        record_closed = logging.LogRecord(
            name="mcp.server.streamable_http",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Client disconnected",
            args=(),
            exc_info=(anyio.ClosedResourceError, anyio.ClosedResourceError(), None),
        )
        self.assertFalse(filter_.filter(record_closed))

        record_plain = logging.LogRecord(
            name="mcp.server.streamable_http",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Something broke",
            args=(),
            exc_info=(ValueError, ValueError("boom"), None),
        )
        self.assertTrue(filter_.filter(record_plain))


if __name__ == "__main__":
    unittest.main()
