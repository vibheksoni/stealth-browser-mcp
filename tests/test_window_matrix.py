"""Unit tests for window_matrix helper module (SB-75).

Tests:
- HEADFUL and HEADLESS parsing from env via importlib.reload.
- window_mode() return value according to HEADFUL.
- display_available() logic across Linux, Windows, macOS, and env vars.
- skip_if_no_display() and requires_display_when_headful decorator behavior.
- wait_until() and wait_until_async() condition checking and timeout handling.

ASCII only. Free of real browsers and network.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from unittest import mock

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

try:
    import window_matrix
except ImportError:
    from tests import window_matrix


class TestWindowMatrixEnvParsing(unittest.TestCase):
    """Test HEADFUL / HEADLESS env parsing and window_mode."""

    def tearDown(self) -> None:
        os.environ.pop("STEALTH_TEST_HEADFUL", None)
        importlib.reload(window_matrix)

    def test_default_is_headless(self) -> None:
        os.environ.pop("STEALTH_TEST_HEADFUL", None)
        importlib.reload(window_matrix)
        self.assertFalse(window_matrix.HEADFUL)
        self.assertTrue(window_matrix.HEADLESS)
        self.assertEqual(window_matrix.window_mode(), "headless")

    def test_headful_when_env_is_one(self) -> None:
        os.environ["STEALTH_TEST_HEADFUL"] = "1"
        importlib.reload(window_matrix)
        self.assertTrue(window_matrix.HEADFUL)
        self.assertFalse(window_matrix.HEADLESS)
        self.assertEqual(window_matrix.window_mode(), "normal")

    def test_headless_when_env_is_zero_or_other(self) -> None:
        for val in ("0", "false", "no", "yes", "2", ""):
            with self.subTest(val=val):
                os.environ["STEALTH_TEST_HEADFUL"] = val
                importlib.reload(window_matrix)
                self.assertFalse(window_matrix.HEADFUL)
                self.assertTrue(window_matrix.HEADLESS)
                self.assertEqual(window_matrix.window_mode(), "headless")


class TestDisplayAvailable(unittest.TestCase):
    """Test display_available with patched env and platform."""

    def test_windows_always_available(self) -> None:
        self.assertTrue(window_matrix.display_available(env={}, system="win32"))
        self.assertTrue(window_matrix.display_available(env={}, system="Windows"))

    def test_macos_always_available(self) -> None:
        self.assertTrue(window_matrix.display_available(env={}, system="darwin"))
        self.assertTrue(window_matrix.display_available(env={}, system="Darwin"))

    def test_linux_with_display(self) -> None:
        self.assertTrue(
            window_matrix.display_available(env={"DISPLAY": ":0"}, system="linux")
        )
        self.assertTrue(
            window_matrix.display_available(
                env={"WAYLAND_DISPLAY": "wayland-0"}, system="linux"
            )
        )
        self.assertTrue(
            window_matrix.display_available(
                env={"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0"}, system="linux"
            )
        )

    def test_linux_without_display(self) -> None:
        self.assertFalse(window_matrix.display_available(env={}, system="linux"))
        self.assertFalse(
            window_matrix.display_available(
                env={"DISPLAY": "", "WAYLAND_DISPLAY": ""}, system="linux"
            )
        )


class TestSkipIfNoDisplay(unittest.TestCase):
    """Test skip_if_no_display and requires_display_when_headful decorator."""

    def test_no_skip_when_headless(self) -> None:
        with mock.patch.object(window_matrix, "HEADFUL", False):
            # Must not raise SkipTest even if no display is available
            with mock.patch.object(window_matrix, "display_available", return_value=False):
                window_matrix.skip_if_no_display(self)

    def test_no_skip_when_headful_with_display(self) -> None:
        with mock.patch.object(window_matrix, "HEADFUL", True):
            with mock.patch.object(window_matrix, "display_available", return_value=True):
                window_matrix.skip_if_no_display(self)

    def test_skips_when_headful_and_no_display(self) -> None:
        with mock.patch.object(window_matrix, "HEADFUL", True):
            with mock.patch.object(window_matrix, "display_available", return_value=False):
                with self.assertRaises(unittest.SkipTest) as ctx:
                    window_matrix.skip_if_no_display(self)
                self.assertIn("requires DISPLAY or WAYLAND_DISPLAY", str(ctx.exception))

    def test_decorator_on_sync_function(self) -> None:
        called = []

        @window_matrix.requires_display_when_headful
        def my_test(testcase=None):
            called.append(True)

        with mock.patch.object(window_matrix, "HEADFUL", True):
            with mock.patch.object(window_matrix, "display_available", return_value=False):
                with self.assertRaises(unittest.SkipTest):
                    my_test(self)
                self.assertEqual(called, [])

            with mock.patch.object(window_matrix, "display_available", return_value=True):
                my_test(self)
                self.assertEqual(called, [True])

    def test_decorator_on_async_function(self) -> None:
        called = []

        @window_matrix.requires_display_when_headful
        async def my_async_test(testcase=None):
            called.append(True)

        with mock.patch.object(window_matrix, "HEADFUL", True):
            with mock.patch.object(window_matrix, "display_available", return_value=False):
                with self.assertRaises(unittest.SkipTest):
                    asyncio.run(my_async_test(self))
                self.assertEqual(called, [])

            with mock.patch.object(window_matrix, "display_available", return_value=True):
                asyncio.run(my_async_test(self))
                self.assertEqual(called, [True])


class TestWaitUntil(unittest.TestCase):
    """Test wait_until and wait_until_async polling helpers."""

    def test_wait_until_sync_success(self) -> None:
        attempts = 0

        def predicate():
            nonlocal attempts
            attempts += 1
            return attempts >= 3

        result = window_matrix.wait_until(predicate, timeout=1.0, interval=0.01)
        self.assertTrue(result)
        self.assertGreaterEqual(attempts, 3)

    def test_wait_until_sync_timeout(self) -> None:
        def predicate():
            return False

        with self.assertRaises(TimeoutError) as ctx:
            window_matrix.wait_until(
                predicate,
                timeout=0.1,
                interval=0.02,
                message="Resource not ready",
            )
        self.assertIn("Resource not ready", str(ctx.exception))

    def test_wait_until_sync_exception_suppression_until_timeout(self) -> None:
        attempts = 0

        def predicate():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ValueError("transient failure")
            return "recovered"

        res = window_matrix.wait_until(predicate, timeout=1.0, interval=0.01)
        self.assertEqual(res, "recovered")

    def test_wait_until_async_success_with_sync_callable(self) -> None:
        attempts = 0

        def predicate():
            nonlocal attempts
            attempts += 1
            return attempts >= 2

        async def run():
            return await window_matrix.wait_until_async(
                predicate, timeout=1.0, interval=0.01
            )

        res = asyncio.run(run())
        self.assertTrue(res)

    def test_wait_until_async_success_with_coroutine(self) -> None:
        attempts = 0

        async def predicate():
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0.001)
            return "coro_success" if attempts >= 2 else None

        async def run():
            return await window_matrix.wait_until_async(
                predicate, timeout=1.0, interval=0.01
            )

        res = asyncio.run(run())
        self.assertEqual(res, "coro_success")

    def test_wait_until_async_timeout(self) -> None:
        async def predicate():
            return None

        async def run():
            await window_matrix.wait_until_async(
                predicate,
                timeout=0.08,
                interval=0.02,
                message="Async condition failed",
            )

        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(run())
        self.assertIn("Async condition failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
