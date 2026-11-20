"""Allowlist and confinement for spawn_browser Chrome flags (SB-81 / review F3).

Caller-supplied browser_args are not passed through. A small documented
allowlist is accepted; known process-control and debug flags are always
rejected. user_data_dir must stay under the managed profiles root.
sandbox=false is denied unless the platform already forces the sandbox off.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Optional

from .paths import data_dir
from .platform_utils import sandbox_must_be_disabled

MANAGED_PROFILES_ROOT_ENV = "STEALTH_MANAGED_PROFILES_ROOT"

# Always rejected, even if later added to the allowlist.
ALWAYS_DENY_FLAGS = frozenset({
    "renderer-cmd-prefix",
    "utility-cmd-prefix",
    "gpu-launcher",
    "browser-subprocess-path",
    "load-extension",
    "load-extensions",
    "remote-debugging-address",
    "remote-debugging-port",
    "remote-debugging-pipe",
    "user-data-dir",
    "disable-web-security",
    "allow-file-access-from-files",
    "proxy-server",
    "proxy-pac-url",
    "proxy-auto-detect",
    "no-zygote",
    "no-sandbox",
    "disable-setuid-sandbox",
    "single-process",
    "no-or-process-sandbox",
})

# Flags a caller may set. True means a value is required; False means a bare flag.
ALLOWED_FLAGS = {
    "window-size": True,
    "window-position": True,
    "lang": True,
    "accept-lang": True,
    "user-agent": True,
    "force-device-scale-factor": True,
    "disable-features": True,
    "blink-settings": True,
    "mute-audio": False,
    "disable-gpu": False,
    "disable-dev-shm-usage": False,
    "no-first-run": False,
    "no-default-browser-check": False,
    "disable-default-apps": False,
    "disable-sync": False,
    "disable-component-update": False,
    "disable-hang-monitor": False,
    "disable-background-timer-throttling": False,
    "disable-backgrounding-occluded-windows": False,
    "disable-renderer-backgrounding": False,
    "hide-scrollbars": False,
    "disable-infobars": False,
    "disable-notifications": False,
    "disable-translate": False,
    "incognito": False,
    "start-maximized": False,
}

_PATHISH = re.compile(r"[\\/]|\.exe$|\.bat$|\.cmd$|\.sh$|\.ps1$", re.IGNORECASE)


class BrowserArgsPolicyError(ValueError):
    """Raised when spawn_browser options violate the launch policy."""


def managed_profiles_root() -> Path:
    """Return the directory that user_data_dir must stay under."""
    configured = os.getenv(MANAGED_PROFILES_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return data_dir().resolve()


def _flag_name_and_value(arg: str) -> tuple[str, Optional[str]]:
    if not isinstance(arg, str) or not arg.startswith("--") or len(arg) < 3:
        raise BrowserArgsPolicyError(
            "rejected browser_arg {!r}: flags must start with --".format(arg)
        )
    body = arg[2:]
    if body.startswith("-"):
        raise BrowserArgsPolicyError(
            "rejected browser_arg {!r}: unknown flag".format(arg)
        )
    if "=" in body:
        name, value = body.split("=", 1)
        return name.lower(), value
    return body.lower(), None


def validate_browser_args(args: Optional[Iterable[str]]) -> List[str]:
    """Return allowed args, or raise BrowserArgsPolicyError naming the bad flag."""
    validated: List[str] = []
    for raw in list(args or []):
        name, value = _flag_name_and_value(raw)
        if name in ALWAYS_DENY_FLAGS:
            raise BrowserArgsPolicyError(
                "rejected browser_arg --{}: this flag is never allowed".format(name)
            )
        if name not in ALLOWED_FLAGS:
            raise BrowserArgsPolicyError(
                "rejected browser_arg --{}: not in the spawn_browser allowlist".format(name)
            )
        needs_value = ALLOWED_FLAGS[name]
        if needs_value and (value is None or value == ""):
            raise BrowserArgsPolicyError(
                "rejected browser_arg --{}: a value is required".format(name)
            )
        if not needs_value and value not in (None, ""):
            raise BrowserArgsPolicyError(
                "rejected browser_arg --{}: this flag does not take a value".format(name)
            )
        if value and name not in {"user-agent", "blink-settings"} and _PATHISH.search(value):
            raise BrowserArgsPolicyError(
                "rejected browser_arg --{}: values may not contain a path".format(name)
            )
        validated.append(raw)
    return validated


def validate_user_data_dir(user_data_dir: Optional[str]) -> Optional[str]:
    """Require user_data_dir to resolve under the managed profiles root."""
    if user_data_dir is None or str(user_data_dir).strip() == "":
        return None
    root = managed_profiles_root()
    candidate = Path(str(user_data_dir)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        raise BrowserArgsPolicyError(
            "rejected user_data_dir {!r}: {}".format(user_data_dir, exc)
        ) from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise BrowserArgsPolicyError(
            "rejected user_data_dir {!r}: must stay under the managed profiles "
            "root ({})".format(user_data_dir, root_resolved)
        ) from exc
    # If the path exists, reject a direct symlink that escaped before resolve.
    if candidate.exists() and candidate.is_symlink():
        link_target = candidate.resolve()
        try:
            link_target.relative_to(root_resolved)
        except ValueError as exc:
            raise BrowserArgsPolicyError(
                "rejected user_data_dir {!r}: symlink escapes the managed "
                "profiles root".format(user_data_dir)
            ) from exc
    return str(resolved)


def validate_sandbox(sandbox: bool, http_mode: bool = True) -> bool:
    """Deny sandbox=false unless the platform already forces the sandbox off."""
    if sandbox is False and not sandbox_must_be_disabled():
        # Same tool serves HTTP and stdio, so the HTTP rule applies to both.
        raise BrowserArgsPolicyError(
            "sandbox=false is not allowed unless the platform requires it"
            + (" (HTTP caller)" if http_mode else "")
        )
    return sandbox
