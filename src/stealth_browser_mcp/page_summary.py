"""Compact page summary generation and action tool decoration (SB-60, SB-63).

Returns a size-capped page summary containing:
- url: Current page URL
- title: Current page title
- elements: Up to ~25 interactive elements with ref, role, and name
  using the existing a11y_snapshot extraction.

Also provides with_page_summary decorator applied centrally to all action tools
so every action result returns a 'page' summary field. Action tools accept an optional
keyword-only include_summary parameter (default True); when set to False, the page
summary computation is skipped and results are returned without the 'page' key.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import nodriver as uc
except Exception:
    uc = None

from .history_nav import decide_history_move

try:
    from .mcp_sessions import get_current_mcp_session_id, mcp_session_manager
except Exception:
    try:
        from stealth_browser_mcp.mcp_sessions import get_current_mcp_session_id, mcp_session_manager
    except Exception:
        get_current_mcp_session_id = None
        mcp_session_manager = None

MAX_SUMMARY_ELEMENTS = 25
MAX_NAME_CHARS = 100
MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 500
MAX_SUMMARY_BYTES = 8192

EMPTY_NAME_ALLOWED_ROLES = {
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "slider",
    "spinbutton",
    "textarea",
    "input",
}

ACTION_TOOL_NAMES = {
    "navigate",
    "go_back",
    "go_forward",
    "reload_page",
    "wait_for",
    "wait_for_element",
    "click_element",
    "type_text",
    "fill",
    "fill_element",
    "paste_text",
    "press_key",
    "select_option",
    "choose_option",
    "scroll_page",
    "execute_script",
    "file_upload",
    "drop_files",
}


async def _wait_for_ready(
    tab: Any,
    timeout: float = 3.0,
    interval: float = 0.1,
) -> str:
    """Poll document.readyState until complete or timeout.

    Args:
        tab: Browser tab instance.
        timeout (float): Max seconds to wait (default 3.0).
        interval (float): Sleep interval between polls (default 0.1).

    Returns:
        str: Last readyState string or '' if evaluation is impossible.
    """
    if not hasattr(tab, "send") or not callable(getattr(tab, "send")):
        return ""

    global uc
    if uc is None:
        try:
            import nodriver as uc
        except Exception:
            pass
    if uc is None:
        return ""

    last_state = ""
    start_time = time.monotonic()
    max_iterations = max(1, int(timeout / interval) + 1) if interval > 0 else 1
    iterations = 0

    while True:
        try:
            cmd = uc.cdp.runtime.evaluate(
                expression="document.readyState",
                return_by_value=True,
            )
            res = await asyncio.wait_for(tab.send(cmd), timeout=1.0)
            remote = res[0] if isinstance(res, tuple) and len(res) > 0 else res
            val = getattr(remote, "value", None)
            if val is None and isinstance(remote, dict):
                val = remote.get("value")
            if isinstance(remote, str):
                val = remote
            if val is not None:
                last_state = str(val)
                if last_state == "complete":
                    return last_state
        except Exception:
            pass

        iterations += 1
        elapsed = time.monotonic() - start_time
        if elapsed >= timeout or iterations >= max_iterations:
            break

        await asyncio.sleep(interval)

    return last_state


async def _read_location(tab: Any) -> Tuple[str, str]:
    """Read settled (url, title) using a single evaluate expression.

    Args:
        tab: Browser tab instance.

    Returns:
        Tuple[str, str]: (url, title) or ('', '') on any failure.
    """
    if not hasattr(tab, "send") or not callable(getattr(tab, "send")):
        return ("", "")

    global uc
    if uc is None:
        try:
            import nodriver as uc
        except Exception:
            pass
    if uc is None:
        return ("", "")

    try:
        cmd = uc.cdp.runtime.evaluate(
            expression="JSON.stringify({u: location.href, t: document.title})",
            return_by_value=True,
        )
        res = await asyncio.wait_for(tab.send(cmd), timeout=1.0)
        remote = res[0] if isinstance(res, tuple) and len(res) > 0 else res
        val = getattr(remote, "value", None)
        if val is None and isinstance(remote, dict):
            val = remote.get("value")
        if isinstance(remote, str):
            val = remote

        if isinstance(val, str):
            data = json.loads(val)
        elif isinstance(val, dict):
            data = val
        else:
            return ("", "")

        if isinstance(data, dict):
            url = str(data.get("u") or "")
            title = str(data.get("t") or "")
            return (url, title)
    except Exception:
        pass

    return ("", "")


async def get_page_summary(
    tab: Any,
    instance_id: Optional[str] = None,
    max_elements: int = MAX_SUMMARY_ELEMENTS,
) -> Dict[str, Any]:
    """Extract a compact, size-capped page summary from a browser tab.

    Args:
        tab: Browser tab instance.
        instance_id (Optional[str]): Browser instance ID.
        max_elements (int): Maximum interactive elements to include (default 25).

    Returns:
        Dict[str, Any]: Compact summary with url, title, elements,
            and truncated fields within size limits.
    """
    if tab is None:
        return {
            "url": "",
            "title": "",
            "elements": [],
            "truncated": False,
        }

    url = ""
    title = ""

    # If tab has a callable send, wait for ready and read live location first
    if hasattr(tab, "send") and callable(getattr(tab, "send")):
        await _wait_for_ready(tab)
        url, title = await _read_location(tab)

    # Fall back to cached tab.url/tab.title (and tab.target.url/title) only when empty
    if not url:
        if hasattr(tab, "url") and tab.url:
            url = str(tab.url)
        elif hasattr(tab, "target") and hasattr(tab.target, "url") and tab.target.url:
            url = str(tab.target.url)

    if not title:
        if hasattr(tab, "title") and tab.title:
            title = str(tab.title)
        elif hasattr(tab, "target") and hasattr(tab.target, "title") and tab.target.title:
            title = str(tab.target.title)

    # Attempt to extract accessibility snapshot
    snapshot: Optional[Dict[str, Any]] = None
    try:
        from . import a11y_snapshot
        snapshot = await asyncio.wait_for(
            a11y_snapshot.get_accessibility_snapshot(
                tab,
                instance_id=instance_id,
                interactive_only=True,
                max_nodes=100,
            ),
            timeout=3.0,
        )
    except Exception:
        try:
            from . import a11y_snapshot
            snapshot = a11y_snapshot.a11y_manager.get_snapshot(
                instance_id=instance_id,
                tab=tab,
            )
        except Exception:
            pass

    elements: List[Dict[str, str]] = []
    if snapshot:
        ref_source = snapshot.get("ref_details") or snapshot.get("refs") or {}
        for ref_id, details in ref_source.items():
            if len(elements) >= max_elements:
                break
            if isinstance(details, dict):
                role = str(details.get("role") or "element").lower()
                raw_name = str(details.get("name") or "")
                name = " ".join(raw_name.split())[:MAX_NAME_CHARS]
            else:
                role = "element"
                name = ""
            if not name and role not in EMPTY_NAME_ALLOWED_ROLES:
                continue
            elements.append({
                "ref": str(ref_id),
                "role": role,
                "name": name,
            })

    summary: Dict[str, Any] = {
        "url": str(url or "")[:MAX_URL_CHARS],
        "title": str(title or "")[:MAX_TITLE_CHARS],
        "elements": elements,
        "truncated": False,
    }

    # Size cap enforcement (keep within MAX_SUMMARY_BYTES)
    while elements and len(json.dumps(summary)) > MAX_SUMMARY_BYTES:
        elements.pop()
        summary["elements"] = elements
        summary["truncated"] = True

    return summary


def with_page_summary(func: Callable) -> Callable:
    """Decorator applied centrally to action tools to attach page summary to results.

    Synthesizes a wrapper matching the original function parameter signature
    plus an optional keyword-only include_summary parameter to ensure full
    compatibility with FastMCP's schema generation.
    """
    sig = inspect.signature(func)
    params = list(sig.parameters.values())

    param_strs: List[str] = []
    arg_names: List[str] = []
    kw_only_seen = False
    for p in params:
        if p.kind == inspect.Parameter.KEYWORD_ONLY and not kw_only_seen:
            param_strs.append("*")
            kw_only_seen = True
        if p.default is not inspect.Parameter.empty:
            param_strs.append(f"{p.name}=None")
        else:
            param_strs.append(p.name)
        arg_names.append(f"{p.name}={p.name}")

    if not kw_only_seen:
        param_strs.append("*")
    param_strs.append("include_summary=True")

    params_code = ", ".join(param_strs)
    args_code = ", ".join(arg_names)

    async def _pre_process(call_locals: Dict[str, Any]) -> None:
        instance_id = call_locals.get("instance_id") or call_locals.get("instance")
        if instance_id:
            mgr = mcp_session_manager
            if mgr is None:
                try:
                    from .mcp_sessions import mcp_session_manager as mgr
                except Exception:
                    try:
                        from stealth_browser_mcp.mcp_sessions import mcp_session_manager as mgr
                    except Exception:
                        mgr = None
            if mgr is not None:
                mgr.check_access(instance_id)

    async def _post_process(res: Any, call_locals: Dict[str, Any]) -> Any:
        raw_include_summary = call_locals.get("include_summary", True)
        if raw_include_summary is False or (
            isinstance(raw_include_summary, str)
            and raw_include_summary.strip().lower() in {"false", "0"}
        ):
            include_summary = False
        else:
            include_summary = True

        if not include_summary:
            if isinstance(res, dict):
                return res
            elif isinstance(res, bool):
                return {"success": res}
            else:
                try:
                    from fastmcp.tools.tool import ToolResult
                except Exception:
                    ToolResult = None
                if ToolResult is not None and isinstance(res, ToolResult):
                    return res
                return {"result": res, "success": bool(res)}

        instance_id = call_locals.get("instance_id") or call_locals.get("instance")
        if not instance_id:
            try:
                get_sid = get_current_mcp_session_id
                mgr = mcp_session_manager
                if get_sid is None or mgr is None:
                    try:
                        from .mcp_sessions import get_current_mcp_session_id as get_sid, mcp_session_manager as mgr
                    except Exception:
                        try:
                            from stealth_browser_mcp.mcp_sessions import get_current_mcp_session_id as get_sid, mcp_session_manager as mgr
                        except Exception:
                            get_sid = None
                            mgr = None
                if get_sid is not None and mgr is not None:
                    sid = get_sid()
                    if sid:
                        instance_id = mgr.get_instance_for_session(sid)
            except Exception:
                pass

        summary = {
            "url": "",
            "title": "",
            "elements": [],
            "truncated": False,
        }
        if instance_id:
            try:
                try:
                    from .server import browser_manager
                except Exception:
                    from stealth_browser_mcp.server import browser_manager
                tab = await browser_manager.get_tab(instance_id)
                if tab:
                    summary = await get_page_summary(tab, instance_id=instance_id)
            except Exception:
                pass

        # Attach 'page' summary field
        if isinstance(res, dict):
            res["page"] = summary
            return res
        elif isinstance(res, bool):
            return {"success": res, "page": summary}
        else:
            try:
                from fastmcp.tools.tool import ToolResult
            except Exception:
                ToolResult = None
            if ToolResult is not None and isinstance(res, ToolResult):
                if res.structured_content is not None and isinstance(res.structured_content, dict):
                    res.structured_content["page"] = summary
                else:
                    res.structured_content = {"page": summary}
                return res
            return {"result": res, "success": bool(res), "page": summary}

    scope = dict(func.__globals__) if hasattr(func, "__globals__") else {}
    scope["_func"] = func
    scope["_pre_process"] = _pre_process
    scope["_post_process"] = _post_process
    scope["mcp_session_manager"] = mcp_session_manager
    scope["get_current_mcp_session_id"] = get_current_mcp_session_id
    code = (
        f"async def wrapper({params_code}):\n"
        f"    await _pre_process(locals())\n"
        f"    res = await _func({args_code})\n"
        f"    return await _post_process(res, locals())\n"
    )
    exec(code, scope)
    wrapper = scope["wrapper"]
    functools.update_wrapper(wrapper, func)
    wrapper.__defaults__ = func.__defaults__
    wrapper.__kwdefaults__ = {**(func.__kwdefaults__ or {}), "include_summary": True}
    wrapper.__annotations__ = dict(getattr(wrapper, "__annotations__", {}))
    wrapper.__annotations__["return"] = Dict[str, Any]
    wrapper.__annotations__["include_summary"] = bool

    new_params = list(params)
    new_params.append(
        inspect.Parameter(
            "include_summary",
            inspect.Parameter.KEYWORD_ONLY,
            default=True,
            annotation=bool,
        )
    )
    wrapper.__signature__ = sig.replace(
        parameters=new_params,
        return_annotation=Dict[str, Any],
    )
    return wrapper
