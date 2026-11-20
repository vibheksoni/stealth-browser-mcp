"""Allowlist and URL/path checks for execute_cdp_command (SB-85 / review F16).

Page.navigate and other CDP methods previously bypassed the F7 scheme block
and write confinement. Default methods are allowlisted. Navigation URLs still
go through reject_blocked_url. Page.setDownloadBehavior and
Browser.setDownloadBehavior stay off the allowlist; with STEALTH_ALLOW_RAW_CDP=1
a download_path is still forced under data_dir(). Anything else requires
STEALTH_ALLOW_RAW_CDP=1, which is refused when HTTP is bound off loopback.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .http_auth import is_loopback_host
from .path_policy import PathPolicyError, reject_blocked_url
from .paths import data_dir

RAW_CDP_ENV = "STEALTH_ALLOW_RAW_CDP"

# Canonical keys are "{domain}.{snake_method}".
ALLOWED_CDP_METHODS = frozenset({
    "runtime.evaluate",
    "runtime.call_function_on",
    "runtime.add_binding",
    "runtime.remove_binding",
    "runtime.compile_script",
    "runtime.run_script",
    "runtime.await_promise",
    "runtime.get_properties",
    "runtime.get_exception_details",
    "runtime.global_lexical_scope_names",
    "runtime.query_objects",
    "runtime.release_object",
    "runtime.release_object_group",
    "runtime.terminate_execution",
    "runtime.set_async_call_stack_depth",
    "runtime.set_custom_object_formatter_enabled",
    "runtime.set_max_call_stack_size_to_capture",
    "runtime.run_if_waiting_for_debugger",
    "runtime.discard_console_entries",
    "runtime.get_heap_usage",
    "runtime.get_isolate_id",
    "runtime.enable",
    "runtime.disable",
    "page.enable",
    "page.disable",
    "page.add_script_to_evaluate_on_new_document",
    "page.remove_script_to_evaluate_on_new_document",
    "page.reload",
    "page.navigate",
    "page.stop_loading",
    "page.get_frame_tree",
    "page.get_layout_metrics",
    "page.capture_screenshot",
    "page.handle_javascript_dialog",
    "page.get_navigation_history",
    "page.navigate_to_history_entry",
    "page.bring_to_front",
    "page.set_lifecycle_events_enabled",
    "page.create_isolated_world",
})

NAVIGATE_METHODS = frozenset({"page.navigate"})
DOWNLOAD_METHODS = frozenset({
    "page.set_download_behavior",
    "browser.set_download_behavior",
})


class CdpPolicyError(ValueError):
    """Raised when a CDP method or its parameters are not allowed."""


def raw_cdp_enabled() -> bool:
    """True only when STEALTH_ALLOW_RAW_CDP is an explicit truthy value."""
    value = os.getenv(RAW_CDP_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def refuse_raw_cdp_off_loopback(bind_host: str) -> None:
    """Raise ValueError if raw CDP is enabled on a non-loopback HTTP bind."""
    if raw_cdp_enabled() and not is_loopback_host(bind_host):
        raise ValueError(
            "STEALTH_ALLOW_RAW_CDP=1 is not allowed when HTTP is bound to a "
            "non-loopback host ({}).".format(bind_host)
        )


def _to_snake(name: str) -> str:
    parts = []
    for index, char in enumerate(name.replace("-", "_")):
        if char.isupper() and index and name[index - 1] != "_":
            parts.append("_")
        parts.append(char.lower())
    return "".join(parts).replace("__", "_")


def canonicalize_cdp_command(command: str) -> str:
    """Return a canonical '{domain}.{snake_method}' key for a caller command name."""
    if command is None or not str(command).strip():
        raise CdpPolicyError("CDP command is required")
    raw = str(command).strip()
    domain = None
    method = raw
    if "." in raw:
        domain_part, method = raw.split(".", 1)
        domain = domain_part.strip().lower()
        method = method.strip()
    snake = _to_snake(method)
    if domain:
        return "{}.{}".format(domain, snake)
    for candidate_domain in ("runtime", "page", "browser"):
        key = "{}.{}".format(candidate_domain, snake)
        if key in ALLOWED_CDP_METHODS or key in DOWNLOAD_METHODS or key in NAVIGATE_METHODS:
            return key
    return "runtime.{}".format(snake)


def _param(params: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in params and params[name] is not None:
            return params[name]
    lowered = {str(key).lower(): key for key in params}
    for name in names:
        actual = lowered.get(name.lower())
        if actual is not None:
            return params[actual]
    return None


def _set_param(params: Dict[str, Any], value: Any, *names: str) -> None:
    for name in names:
        if name in params:
            params[name] = value
            return
    lowered = {str(key).lower(): key for key in params}
    for name in names:
        actual = lowered.get(name.lower())
        if actual is not None:
            params[actual] = value
            return
    params[names[0]] = value


def _confine_download_path(path: str) -> str:
    raw = Path(path).expanduser()
    try:
        resolved = raw.resolve()
        root = data_dir().resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CdpPolicyError(
            "download_path must be under the data directory ({}).".format(data_dir())
        ) from exc
    return str(resolved)


def prepare_cdp_command(command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return sanitized params, or raise CdpPolicyError / PathPolicyError."""
    prepared = dict(params or {})
    key = canonicalize_cdp_command(command)
    allowed = key in ALLOWED_CDP_METHODS
    if not allowed and not raw_cdp_enabled():
        raise CdpPolicyError(
            "CDP method '{}' is not on the allowlist. Set STEALTH_ALLOW_RAW_CDP=1 "
            "to use methods outside the allowlist (allowed only when HTTP is bound "
            "to loopback).".format(command)
        )
    if key in NAVIGATE_METHODS:
        url = _param(prepared, "url", "URL")
        if url is None:
            raise CdpPolicyError("Page.navigate requires a url parameter")
        try:
            reject_blocked_url(str(url))
        except PathPolicyError as exc:
            raise CdpPolicyError(str(exc)) from exc
    if key in DOWNLOAD_METHODS:
        if not raw_cdp_enabled():
            raise CdpPolicyError(
                "Page.setDownloadBehavior is blocked because it can write downloads "
                "outside the data directory. Set STEALTH_ALLOW_RAW_CDP=1 and pass a "
                "download_path under the data directory."
            )
        behavior = str(_param(prepared, "behavior") or "").strip().lower()
        path = _param(prepared, "download_path", "downloadPath")
        if behavior in {"allow", "allowandname", "allow_and_name"}:
            if not path:
                raise CdpPolicyError(
                    "setDownloadBehavior with behavior={!r} requires download_path "
                    "under the data directory.".format(behavior)
                )
            confined = _confine_download_path(str(path))
            _set_param(prepared, confined, "download_path", "downloadPath")
        elif path:
            confined = _confine_download_path(str(path))
            _set_param(prepared, confined, "download_path", "downloadPath")
    return prepared
