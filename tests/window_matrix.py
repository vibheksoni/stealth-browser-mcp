"""Window matrix test configuration and synchronization helpers (SB-75).

Provides test execution matrix helpers for headless and headful test runs:
- HEADFUL / HEADLESS boolean flags driven by STEALTH_TEST_HEADFUL.
- display_available() checking for DISPLAY / WAYLAND_DISPLAY on Linux.
- window_mode() returning "normal" when HEADFUL else "headless".
- skip_if_no_display(testcase) and requires_display_when_headful decorator.
- wait_until and wait_until_async polling helpers for condition-based waits.

ASCII only. Stdlib only.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys
import time
from typing import Any, Callable, Dict, Optional
import unittest

HEADFUL = os.environ.get("STEALTH_TEST_HEADFUL") == "1"
HEADLESS = not HEADFUL


def display_available(
    env: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
) -> bool:
    """Check if a graphical display environment is available.

    On Windows and macOS, True is always returned.
    On Linux and other POSIX platforms, True is returned only if DISPLAY
    or WAYLAND_DISPLAY is set and non-empty.
    """
    if env is None:
        env = os.environ
    if system is None:
        system = sys.platform

    sys_lower = system.lower()
    if sys_lower in ("win32", "windows", "darwin", "macos"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def window_mode() -> str:
    """Return 'normal' when HEADFUL else 'headless'."""
    return "normal" if HEADFUL else "headless"


def skip_if_no_display(testcase: Optional[Any] = None) -> None:
    """Skip testcase if running in headful mode without an available display."""
    if HEADFUL and not display_available():
        msg = "Headful test pass requires DISPLAY or WAYLAND_DISPLAY on Linux"
        if testcase is not None and hasattr(testcase, "skipTest"):
            testcase.skipTest(msg)
        raise unittest.SkipTest(msg)


def requires_display_when_headful(func_or_cls: Any) -> Any:
    """Decorator that skips when HEADFUL is set but no display is available."""
    if isinstance(func_or_cls, type) and issubclass(func_or_cls, unittest.TestCase):
        orig_setUp = getattr(func_or_cls, "setUp", None)

        def setUp(self):
            skip_if_no_display(self)
            if orig_setUp is not None:
                orig_setUp(self)

        func_or_cls.setUp = setUp
        return func_or_cls

    if asyncio.iscoroutinefunction(func_or_cls):
        @functools.wraps(func_or_cls)
        async def async_wrapper(*args, **kwargs):
            skip_if_no_display(args[0] if args else None)
            return await func_or_cls(*args, **kwargs)

        return async_wrapper
    else:
        @functools.wraps(func_or_cls)
        def sync_wrapper(*args, **kwargs):
            skip_if_no_display(args[0] if args else None)
            return func_or_cls(*args, **kwargs)

        return sync_wrapper


def wait_until(
    predicate: Callable[[], Any],
    timeout: float = 15.0,
    interval: float = 0.1,
    message: str = "Condition not met within timeout",
) -> Any:
    """Poll predicate synchronously until it returns a truthy value or timeout expires."""
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            val = predicate()
            if val:
                return val
        except Exception as err:
            last_err = err
        time.sleep(interval)

    # Final attempt
    try:
        val = predicate()
        if val:
            return val
    except Exception as err:
        last_err = err

    detail = f": {last_err}" if last_err else ""
    raise TimeoutError(f"{message} after {timeout:.2f}s{detail}")


async def wait_until_async(
    predicate: Callable[[], Any],
    timeout: float = 15.0,
    interval: float = 0.1,
    message: str = "Condition not met within timeout",
) -> Any:
    """Poll predicate asynchronously until it returns a truthy value or timeout expires."""
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            val = predicate()
            if asyncio.iscoroutine(val):
                val = await val
            if val:
                return val
        except Exception as err:
            last_err = err
        await asyncio.sleep(interval)

    # Final attempt
    try:
        val = predicate()
        if asyncio.iscoroutine(val):
            val = await val
        if val:
            return val
    except Exception as err:
        last_err = err

    detail = f": {last_err}" if last_err else ""
    raise TimeoutError(f"{message} after {timeout:.2f}s{detail}")
