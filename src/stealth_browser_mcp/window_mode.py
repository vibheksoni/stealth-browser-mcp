"""Window mode resolution and focus management for stealth browser.

ASCII only.
"""

from __future__ import annotations

import asyncio
import os
import platform
from typing import Any, Dict, List, Mapping, Optional, Tuple

import nodriver as uc
from nodriver import cdp


WINDOW_MODES = ("normal", "minimized", "offscreen", "headless")
DEFAULT_WINDOW_MODE = "offscreen"
WINDOW_ENV = "STEALTH_WINDOW_MODE"


def normalize_window_mode(value: Any) -> Optional[str]:
    """
    Normalize window mode string.

    Args:
        value: Mode string or None.

    Returns:
        Optional[str]: Normalized mode in WINDOW_MODES, or None for empty/None input.

    Raises:
        ValueError: If value is not a valid mode.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        lowered = stripped.lower()
        if lowered in WINDOW_MODES:
            return lowered
    raise ValueError(
        f"invalid window mode {value!r}; use one of: normal, minimized, offscreen, headless"
    )


def resolve_window_mode(
    window: Optional[str] = None,
    headless: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Resolve effective window mode based on precedence:
    1) explicit window argument
    2) headless=True -> "headless"
    3) env STEALTH_WINDOW_MODE
    4) DEFAULT_WINDOW_MODE

    Args:
        window: Explicit window argument.
        headless: Headless flag.
        env: Optional environment mapping.

    Returns:
        str: Resolved window mode.

    Raises:
        ValueError: If window or STEALTH_WINDOW_MODE is invalid.
    """
    norm_window = normalize_window_mode(window)
    if norm_window is not None:
        return norm_window

    if headless:
        return "headless"

    env_map = os.environ if env is None else env
    raw_env = env_map.get(WINDOW_ENV)
    if raw_env is not None and raw_env != "":
        try:
            norm_env = normalize_window_mode(raw_env)
        except ValueError:
            raise ValueError(
                f"invalid {WINDOW_ENV} window mode {raw_env!r}; use one of: normal, minimized, offscreen, headless"
            )
        if norm_env is not None:
            return norm_env

    return DEFAULT_WINDOW_MODE


def display_available(
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """
    Check if a GUI display is available.

    Args:
        system: Optional OS name (Windows, Darwin, Linux, etc.).
        env: Optional environment mapping.

    Returns:
        bool: True if display is available.
    """
    sys_name = system if system is not None else platform.system()
    if sys_name in ("Windows", "Darwin"):
        return True
    env_map = os.environ if env is None else env
    display = env_map.get("DISPLAY")
    wayland = env_map.get("WAYLAND_DISPLAY")
    return bool((display and display.strip()) or (wayland and wayland.strip()))


def effective_window_mode(
    mode: str,
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Optional[str]]:
    """
    Determine effective window mode, falling back to headless if display is missing.

    Args:
        mode: Requested window mode.
        system: Optional OS name.
        env: Optional environment mapping.

    Returns:
        Tuple[str, Optional[str]]: (effective_mode, diagnostic_note)
    """
    if mode != "headless" and not display_available(system=system, env=env):
        return ("headless", "No display available (DISPLAY/WAYLAND_DISPLAY unset); using headless.")
    return (mode, None)


def window_launch_args(mode: str, width: int = 1280, height: int = 800) -> List[str]:
    """
    Get browser launch arguments for the window mode.

    Args:
        mode: Window mode.
        width: Viewport width.
        height: Viewport height.

    Returns:
        List[str]: Launch arguments.
    """
    if mode == "minimized":
        return ["--start-minimized"]
    if mode == "offscreen":
        return ["--window-position=-32000,-32000", f"--window-size={width},{height}"]
    return []


async def apply_window_mode(
    tab: Any,
    mode: str,
    width: int = 1280,
    height: int = 800,
) -> Dict[str, Any]:
    """
    Apply window positioning and focus emulation via CDP. Never raises.

    Args:
        tab: Target tab connection.
        mode: Window mode.
        width: Viewport width.
        height: Viewport height.

    Returns:
        Dict[str, Any]: Dict with mode and applied step names.
    """
    norm_mode = mode.lower().strip() if isinstance(mode, str) else str(mode)
    if norm_mode == "normal":
        return {"mode": "normal", "applied": []}

    applied: List[str] = []

    if norm_mode in ("minimized", "offscreen"):
        window_id = None
        try:
            res = await asyncio.wait_for(
                tab.send(cdp.browser.get_window_for_target()),
                timeout=3.0,
            )
            if isinstance(res, (tuple, list)) and len(res) >= 1:
                window_id = res[0]
            applied.append("get_window_for_target")
        except Exception:
            pass

        if window_id is not None:
            try:
                if norm_mode == "minimized":
                    bounds = cdp.browser.Bounds(
                        window_state=cdp.browser.WindowState.MINIMIZED,
                    )
                else:
                    bounds = cdp.browser.Bounds(
                        left=-32000,
                        top=-32000,
                        width=width,
                        height=height,
                        window_state=cdp.browser.WindowState.NORMAL,
                    )
                await asyncio.wait_for(
                    tab.send(cdp.browser.set_window_bounds(window_id, bounds)),
                    timeout=3.0,
                )
                applied.append("set_window_bounds")
            except Exception:
                pass

    if norm_mode in ("minimized", "offscreen", "headless"):
        try:
            await asyncio.wait_for(
                tab.send(cdp.emulation.set_focus_emulation_enabled(True)),
                timeout=3.0,
            )
            applied.append("set_focus_emulation_enabled")
        except Exception:
            pass

    return {"mode": norm_mode, "applied": applied}


async def open_background_tab(
    browser: Any,
    url: str = "about:blank",
    new_window: bool = False,
) -> Any:
    """
    Open a new tab in the background without stealing focus.

    Args:
        browser: nodriver Browser instance.
        url: Initial URL.
        new_window: Whether to create in a new window.

    Returns:
        Tab: The newly opened Tab instance.
    """
    conn = getattr(browser, "connection", None)
    send_fn = conn.send if conn is not None and hasattr(conn, "send") else getattr(browser, "send", None)
    if send_fn is None or not callable(send_fn):
        if hasattr(browser, "get"):
            if new_window:
                try:
                    return await browser.get(url, new_window=True)
                except TypeError:
                    pass
            try:
                return await browser.get(url, new_tab=True)
            except TypeError:
                return await browser.get(url)
        raise RuntimeError("Browser has no connection or send method")

    cmd = cdp.target.create_target(
        url,
        new_window=new_window,
        background=True,
        enable_begin_frame_control=True,
    )
    target_id = await send_fn(cmd)

    for attempt in range(21):
        if hasattr(browser, "update_targets"):
            try:
                await browser.update_targets()
            except Exception:
                pass
        for item in getattr(browser, "targets", []):
            item_type = getattr(item, "type_", None) or getattr(getattr(item, "target", None), "type_", None)
            item_tid = getattr(item, "target_id", None) or getattr(getattr(item, "target", None), "target_id", None)
            if (item_type == "page" or item_type is None) and (item_tid == target_id or str(item_tid) == str(target_id)):
                item._browser = browser
                return item
        if attempt < 20:
            await asyncio.sleep(0.05)

    raise RuntimeError(f"Target {target_id} not found in browser targets")
