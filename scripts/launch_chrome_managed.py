#!/usr/bin/env python3
"""Launch Chrome with remote debugging on a dedicated profile for managed accounts.

Stdlib only. Cross-platform (Linux, macOS, Windows).
ASCII only.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def is_default_profile_dir(
    path: Union[str, Path],
    platform: Optional[str] = None,
) -> bool:
    """Check whether a path is the platform default Chrome user data directory.

    Args:
        path: Directory path to check.
        platform: Platform name ('linux', 'darwin', 'win32', etc.). Defaults to sys.platform.

    Returns:
        bool: True if path matches or resides inside the default Chrome profile directory.
    """
    if not path:
        return False
    plat = (platform if platform is not None else sys.platform).lower()
    raw = str(path).strip()
    norm = os.path.normpath(raw).replace("\\", "/").lower()

    if "linux" in plat:
        linux_defaults = [
            ".config/google-chrome",
            ".config/chromium",
            ".config/google-chrome-beta",
            ".config/google-chrome-unstable",
        ]
        for d in linux_defaults:
            if norm == d or norm.endswith("/" + d) or f"/{d}/" in norm + "/":
                return True
        return False

    if "darwin" in plat or "mac" in plat:
        mac_defaults = [
            "library/application support/google/chrome",
            "library/application support/chromium",
        ]
        for d in mac_defaults:
            if norm == d or norm.endswith("/" + d) or f"/{d}/" in norm + "/":
                return True
        return False

    if "win" in plat:
        win_defaults = [
            "appdata/local/google/chrome/user data",
            "appdata/local/chromium/user data",
            "%localappdata%/google/chrome/user data",
            "%localappdata%/chromium/user data",
        ]
        for d in win_defaults:
            if norm == d or norm.endswith("/" + d) or f"/{d}/" in norm + "/":
                return True
        return False

    return False


def find_chrome(candidates: Optional[List[str]] = None) -> Optional[str]:
    """Find a Chrome executable from candidate paths or platform defaults.

    Args:
        candidates: Optional list of file paths to probe in order.

    Returns:
        Optional[str]: Path to the first existing executable, or None.
    """
    if candidates is not None:
        for path in candidates:
            if not path:
                continue
            p = os.path.expanduser(str(path))
            if os.path.isfile(p):
                return p
        return None

    env_override = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
    if env_override and os.path.isfile(env_override):
        return env_override

    system = platform.system().lower()
    default_candidates: List[str] = []

    if system == "windows":
        default_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Chromium\Application\chromium.exe",
        ]
    elif system == "darwin":
        default_candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        default_candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/local/bin/chrome",
        ]

    for path in default_candidates:
        if path and os.path.isfile(path):
            return path

    for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]:
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return which_path

    return None


def build_chrome_args(
    user_data_dir: str,
    port: int = 9222,
    window: str = "normal",
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Build Chrome launch arguments for remote debugging in a dedicated profile.

    Args:
        user_data_dir: Path to the dedicated user data directory.
        port: Remote debugging port number.
        window: Window mode ('normal', 'minimized', or 'offscreen').
        extra_args: Optional additional arguments to append.

    Returns:
        List[str]: List of command line arguments.
    """
    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    mode = window.lower().strip() if window else "normal"
    if mode == "offscreen":
        args.append("--window-position=-32000,-32000")
    elif mode == "minimized":
        args.append("--start-minimized")
    elif mode == "normal":
        pass
    else:
        raise ValueError(
            f"Invalid window mode: {window!r}. Must be 'offscreen', 'minimized', or 'normal'."
        )

    if extra_args:
        args.extend(extra_args)

    return args


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point to launch Chrome with remote debugging on a dedicated profile."""
    parser = argparse.ArgumentParser(
        description="Launch Chrome with remote debugging on a dedicated profile for managed accounts."
    )
    parser.add_argument(
        "--chrome",
        dest="chrome_path",
        default=None,
        help="Path to Chrome executable (default: auto-detected).",
    )
    parser.add_argument(
        "--user-data-dir",
        dest="user_data_dir",
        default=os.path.expanduser("~/.stealth-mcp/managed-profile"),
        help="Path to dedicated profile directory (default: ~/.stealth-mcp/managed-profile).",
    )
    parser.add_argument(
        "--port",
        dest="port",
        type=int,
        default=9222,
        help="Remote debugging port (default: 9222).",
    )
    parser.add_argument(
        "--window",
        dest="window",
        choices=["offscreen", "minimized", "normal"],
        default="normal",
        help="Window mode: offscreen, minimized, or normal (default: normal).",
    )
    args = parser.parse_args(argv)

    if is_default_profile_dir(args.user_data_dir):
        sys.stderr.write(
            f"Error: Refusing to use default Chrome profile directory '{args.user_data_dir}'.\n"
            "Chrome 136+ ignores remote debugging flags on the default user data directory.\n"
            "Please use a dedicated non-default directory (e.g. ~/.stealth-mcp/managed-profile).\n"
            "See docs/MANAGED_PROFILES.md for details.\n"
        )
        return 1

    chrome_exe = args.chrome_path or find_chrome()
    if not chrome_exe or not os.path.isfile(chrome_exe):
        sys.stderr.write(
            "Error: Could not find Chrome executable. Please specify with --chrome /path/to/chrome\n"
        )
        return 1

    target_profile_dir = os.path.expanduser(args.user_data_dir)
    os.makedirs(target_profile_dir, exist_ok=True)

    chrome_args = build_chrome_args(
        user_data_dir=target_profile_dir,
        port=args.port,
        window=args.window,
    )

    full_cmd = [chrome_exe, *chrome_args]
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(full_cmd, **kwargs)
    except Exception as e:
        sys.stderr.write(f"Error launching Chrome: {e}\n")
        return 1

    deadline = time.monotonic() + 15.0
    url = f"http://127.0.0.1:{args.port}/json/version"
    ready = False

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stealth-browser-mcp"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)

    if not ready:
        sys.stderr.write(
            f"Error: Chrome started (PID {proc.pid}) but did not respond on port {args.port} within 15 seconds.\n"
            "Check that enterprise policies permit remote debugging and port is not blocked.\n"
        )
        return 1

    print(f"Chrome started on port {args.port} (PID {proc.pid}).")
    print(f"Next step: call attach_browser(port={args.port})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
