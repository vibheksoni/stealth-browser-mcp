"""Policy for server-side Python hook compilation (SB-80 / review F2).

create_dynamic_hook exec()s caller Python. That is not a sandbox. The tool is
off unless STEALTH_ALLOW_CODE_HOOKS=1, and that flag is refused when HTTP is
bound off loopback. Declarative create_simple_dynamic_hook stays available.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from .http_auth import is_loopback_host

CODE_HOOKS_ENV = "STEALTH_ALLOW_CODE_HOOKS"


def code_hooks_enabled() -> bool:
    """True only when STEALTH_ALLOW_CODE_HOOKS is an explicit truthy value."""
    value = os.getenv(CODE_HOOKS_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def code_hooks_disabled_result() -> Dict[str, Any]:
    """Standard error payload when caller Python hooks are disabled."""
    return {
        "success": False,
        "error": "code_hooks_disabled",
        "message": (
            "create_dynamic_hook is disabled because it runs caller Python on the "
            "server. Use create_simple_dynamic_hook for declarative actions, or set "
            "STEALTH_ALLOW_CODE_HOOKS=1 (allowed only when HTTP is bound to loopback)."
        ),
    }


def refuse_code_hooks_off_loopback(
    bind_host: str,
    allowed_hosts=(),
    allowed_origins=(),
) -> None:
    """Raise ValueError if code hooks are enabled on an exposed HTTP bind."""
    if not code_hooks_enabled():
        return
    exposed = (not is_loopback_host(bind_host)) or bool(allowed_hosts) or bool(allowed_origins)
    if exposed:
        raise ValueError(
            "STEALTH_ALLOW_CODE_HOOKS=1 is not allowed when HTTP is bound off loopback "
            "or when --allowed-host / --allowed-origin is set (a reverse proxy in front "
            "of a loopback bind counts as off loopback)."
        )


def code_hooks_available() -> bool:
    """Like code_hooks_enabled, but false for a proxied request (X-Forwarded-For)."""
    if not code_hooks_enabled():
        return False
    from .http_auth import request_appears_proxied
    return not request_appears_proxied()
