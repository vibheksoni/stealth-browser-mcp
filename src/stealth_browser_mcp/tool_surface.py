"""Core tool surface, category reference, and expand_tools (SB-72).

Exposes a core set of about 33 tools by default to keep connect cost under 10K tokens.
Extended tools remain callable by name, can be expanded per session or globally,
and are documented in the stealth://tools/reference resource.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool

from . import tool_compact

CORE_TOOLS: tuple[str, ...] = (
    "spawn_browser",
    "attach_browser",
    "list_instances",
    "close_instance",
    "get_instance_state",
    "navigate",
    "wait_for",
    "go_back",
    "reload_page",
    "query_elements",
    "click_element",
    "type_text",
    "fill",
    "press_key",
    "select_option",
    "scroll_page",
    "get_page_content",
    "take_screenshot",
    "get_accessibility_snapshot",
    "execute_script",
    "get_cookies",
    "set_cookie",
    "list_tabs",
    "new_tab",
    "switch_tab",
    "close_tab",
    "paste_text",
    "choose_option",
    "file_upload",
    "go_forward",
    "wait_for_download",
    "expand_tools",
    "list_tool_categories",
)

TOOL_SURFACE_INSTRUCTIONS_MARKER = (
    "The tools listed below represent the core browser automation surface"
)


def set_server_instructions(mcp_or_text: Any, text: Optional[str] = None) -> None:
    """Assign instructions to FastMCP instance via _mcp_server with fallback."""
    if text is None:
        from . import server

        target_mcp = getattr(server, "mcp", None)
        target_text = str(mcp_or_text)
    else:
        target_mcp = mcp_or_text
        target_text = text

    if target_mcp is None:
        return

    mcp_server = getattr(target_mcp, "_mcp_server", None)
    if mcp_server is not None:
        try:
            mcp_server.instructions = target_text
            return
        except Exception:
            pass
    try:
        target_mcp.instructions = target_text
    except Exception:
        pass


def get_server_instructions(mcp: Any = None) -> str:
    """Get current server instructions."""
    if mcp is None:
        from . import server

        mcp = getattr(server, "mcp", None)
    if mcp is None:
        return ""
    mcp_server = getattr(mcp, "_mcp_server", None)
    if mcp_server is not None and getattr(mcp_server, "instructions", None):
        return str(mcp_server.instructions)
    return str(getattr(mcp, "instructions", None) or "")


def init_tool_surface(mcp: Any = None) -> None:
    """Initialize tool surface middleware and instructions idempotently."""
    if mcp is None:
        from . import server

        target_mcp = getattr(server, "mcp", None)
    else:
        target_mcp = mcp
    if target_mcp is None:
        return

    register_tool_surface_middleware(target_mcp)
    all_names = list(
        getattr(getattr(target_mcp, "_tool_manager", None), "_tools", {}).keys()
    )
    surface_instructions = build_tool_surface_instructions(all_names, CORE_TOOLS)
    current = get_server_instructions(target_mcp)
    if TOOL_SURFACE_INSTRUCTIONS_MARKER in current:
        base = current.split(TOOL_SURFACE_INSTRUCTIONS_MARKER)[0].strip()
    else:
        base = current.strip()
    new_instructions = (
        (base + "\n\n" + surface_instructions) if base else surface_instructions
    )
    set_server_instructions(target_mcp, new_instructions)


def _get_tool_one_line_desc(tool: Any) -> str:
    """Return a compact one-line description (first sentence, <= 120 chars)."""
    desc = getattr(tool, "description", None)
    if not desc:
        fn = getattr(tool, "fn", None)
        desc = getattr(fn, "__doc__", None) or getattr(tool, "__doc__", None) or ""
    if desc.endswith(tool_compact.INSTANCE_DEFAULT_NOTE):
        desc = desc[:-len(tool_compact.INSTANCE_DEFAULT_NOTE)].rstrip()

    prefix = tool_compact._description_prefix(desc)
    match = re.search(r"^(.*?[.!?])(?:\s|$)", prefix)
    if match:
        sentence = match.group(1).strip()
    else:
        sentence = prefix.strip()

    if len(sentence) <= 120:
        return sentence
    head = sentence[:120]
    space = head.rfind(" ")
    if space != -1:
        return head[:space].rstrip()
    return head


def build_tool_surface_instructions(
    all_names: Sequence[str],
    core_names: Sequence[str],
) -> str:
    """Build tool surface instructions with core description and hidden category tool lists."""
    core_set = set(core_names)
    non_core_set = {n for n in all_names if n not in core_set}
    groups = tool_compact.group_tools(sorted(all_names))

    paragraph = (
        "The tools listed below represent the core browser automation surface, covering "
        "essential navigation, interaction, inspection, tab, cookie, and screenshot operations. "
        "Additional specialized capabilities for element extraction, deep DOM cloning, network "
        "debugging, CDP function execution, and profile seeding are available as extended tools. "
        "The complete catalog organized by category is available in the stealth://tools/reference "
        "resource or by calling the list_tool_categories tool. To reveal and list tools for a "
        "specific category in your tool surface, call expand_tools with the category name or 'all'. "
        "Extended tools can always be invoked directly by name even if not currently visible in "
        "your client's tool list."
    )

    category_lines: List[str] = []
    for cat, tools in groups.items():
        cat_non_core = [t for t in tools if t in non_core_set]
        if cat_non_core:
            category_lines.append(f"{cat}: {', '.join(cat_non_core)}")

    return paragraph + "\n\n" + "\n".join(category_lines)


def extract_session_key(obj: Any) -> str:
    """Extract an MCP session key from a Context or MiddlewareContext object."""
    ctx = getattr(obj, "fastmcp_context", None) or obj
    if ctx is not None:
        try:
            sid = getattr(ctx, "session_id", None)
            if sid:
                return str(sid)
        except Exception:
            pass
        try:
            sess = getattr(ctx, "session", None)
            if sess is not None:
                fid = getattr(sess, "_fastmcp_id", None)
                if fid:
                    return str(fid)
                return str(id(sess))
        except Exception:
            pass

    try:
        from .mcp_sessions import get_current_mcp_session_id
        sid = get_current_mcp_session_id()
        if sid:
            return str(sid)
    except Exception:
        pass

    try:
        from mcp.server.lowlevel.server import request_ctx
        rc = request_ctx.get()
        if rc and getattr(rc, "session", None):
            sess = rc.session
            fid = getattr(sess, "_fastmcp_id", None)
            if fid:
                return str(fid)
            return str(id(sess))
    except Exception:
        pass

    return "default_session"


class ToolSurfaceMiddleware(Middleware):
    """Filters tools/list to CORE_TOOLS + session-expanded categories in core mode."""

    def __init__(
        self,
        core_tools: Iterable[str] = CORE_TOOLS,
        mode: Optional[str] = None,
    ) -> None:
        self.core_tools: Set[str] = set(core_tools)
        self._mode = mode
        self._expanded: OrderedDict[str, Set[str]] = OrderedDict()
        self._max_sessions = 1024

    def get_mode(self) -> str:
        if self._mode is not None:
            return self._mode
        env_mode = os.getenv("STEALTH_MCP_TOOLS", "").strip().lower()
        if env_mode == "full":
            return "full"
        return "core"

    def set_mode(self, mode: Optional[str]) -> None:
        self._mode = mode.strip().lower() if mode else None

    def get_expanded_categories(self, session_key: str) -> Set[str]:
        return self._expanded.get(session_key, set())

    def expand_category(self, session_key: str, category: str) -> None:
        if session_key not in self._expanded:
            if len(self._expanded) >= self._max_sessions:
                self._expanded.popitem(last=False)
            self._expanded[session_key] = set()
        self._expanded[session_key].add(category)
        self._expanded.move_to_end(session_key)

    def clean_session(self, session_key: str) -> None:
        self._expanded.pop(session_key, None)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, List[Tool]],
    ) -> List[Tool]:
        tools = await call_next(context)
        if self.get_mode() == "full":
            return tools

        session_key = extract_session_key(context)
        expanded = self.get_expanded_categories(session_key)
        if "all" in expanded:
            return tools

        tool_names = [getattr(t, "key", None) or getattr(t, "name", None) for t in tools]
        groups = tool_compact.group_tools(tool_names)
        tool_to_cat: Dict[str, str] = {}
        for cat, names in groups.items():
            for name in names:
                tool_to_cat[name] = cat

        filtered: List[Tool] = []
        for t in tools:
            name = getattr(t, "key", None) or getattr(t, "name", None)
            if not name:
                filtered.append(t)
                continue
            if name in self.core_tools:
                filtered.append(t)
            else:
                cat = tool_to_cat.get(name)
                if cat and cat in expanded:
                    filtered.append(t)

        return filtered


_tool_surface_middleware: Optional[ToolSurfaceMiddleware] = None


def get_tool_surface_middleware(mcp: Any) -> Optional[ToolSurfaceMiddleware]:
    """Retrieve existing ToolSurfaceMiddleware from mcp server if registered."""
    global _tool_surface_middleware
    for mw in getattr(mcp, "middleware", []) or getattr(mcp, "_middleware", []) or []:
        if isinstance(mw, ToolSurfaceMiddleware):
            _tool_surface_middleware = mw
            return mw
    return _tool_surface_middleware


def register_tool_surface_middleware(
    mcp: Any, mode: Optional[str] = None
) -> ToolSurfaceMiddleware:
    """Register ToolSurfaceMiddleware once on mcp server."""
    global _tool_surface_middleware
    for mw in getattr(mcp, "middleware", []) or getattr(mcp, "_middleware", []) or []:
        if isinstance(mw, ToolSurfaceMiddleware):
            if mode is not None:
                mw.set_mode(mode)
            _tool_surface_middleware = mw
            return mw
    mw = ToolSurfaceMiddleware(mode=mode)
    mcp.add_middleware(mw)
    _tool_surface_middleware = mw
    return mw


def clean_session(session_key: str) -> None:
    """Clean up per-session expanded tool state."""
    if _tool_surface_middleware is not None:
        _tool_surface_middleware.clean_session(session_key)


async def expand_tools_impl(mcp: Any, category: str, ctx: Context) -> Dict[str, Any]:
    """Implementation for expand_tools."""
    valid_categories = list(tool_compact.TOOL_GROUPS.keys()) + ["all"]
    if category not in valid_categories:
        raise ToolError(
            f"Unknown category: {category!r}. Valid categories: {', '.join(valid_categories)}"
        )

    mw = get_tool_surface_middleware(mcp) or register_tool_surface_middleware(mcp)
    session_key = extract_session_key(ctx)
    mw.expand_category(session_key, category)

    try:
        if ctx and hasattr(ctx, "session") and ctx.session is not None:
            await ctx.session.send_tool_list_changed()
    except Exception:
        pass

    registry_tools = await mcp.get_tools()
    all_names = sorted(registry_tools.keys())
    groups = tool_compact.group_tools(all_names)

    if category == "all":
        target_names = all_names
    else:
        target_names = groups.get(category, [])

    tools_list: List[str] = []
    for name in target_names:
        tool = registry_tools.get(name)
        desc = _get_tool_one_line_desc(tool) if tool else ""
        if desc:
            tools_list.append(f"{name}: {desc}")
        else:
            tools_list.append(name)

    return {
        "expanded": category,
        "tools": tools_list,
        "note": "tools are callable by name even if your client did not refresh its list",
    }


async def list_tool_categories_impl(mcp: Any) -> Dict[str, Any]:
    """Implementation for list_tool_categories."""
    registry_tools = await mcp.get_tools()
    all_names = sorted(registry_tools.keys())
    groups = tool_compact.group_tools(all_names)

    mw = get_tool_surface_middleware(mcp)
    mode = mw.get_mode() if mw is not None else "core"

    counts = {cat: len(tools) for cat, tools in groups.items()}
    counts["total"] = len(all_names)

    result: Dict[str, Any] = {
        "mode": mode,
        "counts": counts,
        "categories": groups,
    }
    for cat, tools in groups.items():
        result[cat] = tools
    return result


async def tools_reference_impl(mcp: Any) -> str:
    """Implementation for stealth://tools/reference."""
    registry_tools = await mcp.get_tools()
    all_names = sorted(registry_tools.keys())
    groups = tool_compact.group_tools(all_names)

    lines: List[str] = ["# Stealth Browser MCP - Tool Reference\n"]
    for cat, names in groups.items():
        lines.append(f"## {cat}\n")
        for name in names:
            tool = registry_tools.get(name)
            desc = _get_tool_one_line_desc(tool) if tool else ""
            if desc:
                lines.append(f"- **{name}**: {desc}")
            else:
                lines.append(f"- **{name}**")
        lines.append("")
    return "\n".join(lines).strip()
