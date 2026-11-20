"""Default instance_id resolution so tools work without an explicit instance (SB-64).

An MCP session maps to exactly one browser. These helpers let the server treat
instance_id as optional: a middleware injects the session's only browser when a
call omits it, and the tool schemas advertise instance_id as optional.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, List, Optional

from fastmcp.server.middleware import Middleware
from fastmcp.exceptions import ToolError

from .mcp_sessions import get_current_mcp_session_id, mcp_session_manager

INSTANCE_DEFAULT_NOTE = " If instance_id is omitted, the session's only browser is used."


def pick_default_instance(
    session_instance: Optional[str],
    open_instance_ids: List[str],
) -> str:
    """Choose the instance_id for a single-browser session.

    Args:
        session_instance: Instance registered for the current MCP session.
        open_instance_ids: Open browser instance ids from the browser manager.

    Returns:
        The single instance id that should be assumed for this call.

    Raises:
        ValueError: When there are zero open instances, or more than one and no
            session-bound instance to prefer.
    """
    if session_instance:
        return session_instance
    if not open_instance_ids:
        raise ValueError(
            "instance_id is required: no browser is open; call spawn_browser first"
        )
    if len(open_instance_ids) == 1:
        return open_instance_ids[0]
    raise ValueError(
        f"instance_id is required: there are {len(open_instance_ids)} open browsers"
    )


def _extract_instance_ids(open_instances: Any) -> List[str]:
    """Pull instance ids out of BrowserInstance objects or dicts."""
    ids: List[str] = []
    for inst in open_instances or []:
        iid = getattr(inst, "instance_id", None)
        if not iid:
            iid = inst.get("instance_id") if isinstance(inst, dict) else None
        if iid:
            ids.append(iid)
    return ids


class DefaultInstanceMiddleware(Middleware):
    """Inject the session's only browser instance_id when a call omits it.

    Runs as a FastMCP ``on_call_tool`` hook. Because FastMCP validates the tool
    arguments before this hook fires, callers must also make instance_id optional
    in the tool schema (see :func:`make_instance_id_optional`). The middleware
    only touches tools whose schema declares instance_id.

    Args:
        session_instance_fn: Optional callable returning the session instance id.
        open_instances_fn: Optional callable returning the open instance ids.
    """

    def __init__(
        self,
        session_instance_fn: Optional[Callable[[], Optional[str]]] = None,
        open_instances_fn: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self._session_instance_fn = session_instance_fn
        self._open_instances_fn = open_instances_fn

    def _session_instance(self) -> Optional[str]:
        if self._session_instance_fn is not None:
            return self._session_instance_fn()
        try:
            sid = get_current_mcp_session_id()
            if not sid:
                return None
            return mcp_session_manager.get_instance_for_session(sid)
        except Exception:
            return None

    def _open_instances(self) -> List[str]:
        if self._open_instances_fn is not None:
            return _extract_instance_ids(self._open_instances_fn())
        from .server import browser_manager  # pragma: no cover - prod path only

        async def _ids() -> List[str]:
            return _extract_instance_ids(await browser_manager.list_instances())

        self._open_instances = _ids  # type: ignore[method-assign]
        return []

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        args = getattr(context.message, "arguments", None)
        if not isinstance(args, dict):
            return await call_next(context)
        if args.get("instance_id"):
            return await call_next(context)

        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        except Exception:
            return await call_next(context)
        if tool is None:
            return await call_next(context)
        params = getattr(tool, "parameters", None)
        if not (isinstance(params, dict) and "instance_id" in params.get("properties", {})):
            return await call_next(context)

        session_instance = self._session_instance()
        try:
            open_instances = self._open_instances()
            if asyncio.iscoroutine(open_instances):
                open_instances = await open_instances
        except Exception:
            open_instances = []
        chosen = pick_default_instance(session_instance, _extract_instance_ids(open_instances))
        args["instance_id"] = chosen
        return await call_next(context)


def make_instance_id_optional(mcp) -> int:
    """Make instance_id optional on tools that require it.

    For every registered tool whose JSON-schema ``parameters`` lists
    ``instance_id`` in ``required``, remove it from ``required``, give it a
    schema default so validation accepts a missing value, and append a note to
    the tool description.

    Args:
        mcp: A FastMCP server instance.

    Returns:
        Number of tools whose schema was updated.
    """
    tools = getattr(mcp, "_tool_manager", None)
    tools = getattr(tools, "_tools", {}) if tools is not None else {}
    if not isinstance(tools, dict):
        return 0
    count = 0
    for tool in tools.values():
        params = getattr(tool, "parameters", None)
        if not isinstance(params, dict):
            continue
        required = params.get("required")
        if "instance_id" not in (required or []):
            continue
        properties = params.setdefault("properties", {})
        prop = properties.setdefault("instance_id", {})
        if "default" not in prop:
            prop["default"] = ""
        if isinstance(required, list):
            params["required"] = [name for name in required if name != "instance_id"]
        desc = tool.description or ""
        if INSTANCE_DEFAULT_NOTE not in desc:
            tool.description = desc + INSTANCE_DEFAULT_NOTE
        count += 1
    return count


def register_default_instance_middleware(mcp, session_instance_fn=None, open_instances_fn=None) -> None:
    """Register :class:`DefaultInstanceMiddleware` once on ``mcp``.

    Idempotent: if an existing instance is already present, nothing changes.
    """
    for mw in getattr(mcp, "_middleware", []) or []:
        if isinstance(mw, DefaultInstanceMiddleware):
            return
    mcp.add_middleware(DefaultInstanceMiddleware(session_instance_fn, open_instances_fn))
