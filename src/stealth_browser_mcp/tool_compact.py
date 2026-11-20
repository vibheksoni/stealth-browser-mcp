"""Compact tool descriptions and schemas to cut connect-time token cost (SB-64).

MCP clients fetch every tool's name, JSON-schema parameters, and description when
they connect. The registered tools ship with long Google-style docstrings and
redundant ``title`` keys, which inflates that handshake. :func:`compact_tools`
shrinks the descriptions and drops the titles in place, preserving behaviour:
names, types, required, defaults, enums, and output schemas are untouched.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .instance_default import INSTANCE_DEFAULT_NOTE

DESCRIPTION_CAP = 300
PROPERTY_CAP = 110

# Section header lines that terminate the free-text description prefix.
_DESCRIPTION_HEADERS = ("Args:", "Returns:", "Raises:", "Example:", "Examples:")
_ARG_SECTION_MARKERS = ("Args:", "Arguments:", "Parameters:", "Params:")
_ARG_SECTION_ENDS = (
    "returns",
    "return",
    "raises",
    "example",
    "notes",
    "note",
    "yields",
    "attributes",
    "references",
    "also",
)

# Ordered precedence: the first matching group wins, and every tool falls back
# to "advanced" so nothing is ever dropped.
_TOOL_GROUP_RULES: List[Tuple[str, Callable[[str], bool]]] = [
    (
        "browser",
        lambda n: n in {
            "spawn_browser",
            "attach_browser",
            "list_instances",
            "close_instance",
            "get_instance_state",
            "navigate",
            "wait_for",
            "go_back",
            "go_forward",
            "reload_page",
        },
    ),
    (
        "input",
        lambda n: n in {
            "click_element",
            "type_text",
            "fill",
            "fill_element",
            "paste_text",
            "press_key",
            "select_option",
            "choose_option",
            "file_upload",
            "drop_files",
            "scroll_page",
        },
    ),
    (
        "read",
        lambda n: n in {
            "query_elements",
            "get_page_content",
            "get_accessibility_snapshot",
            "take_accessibility_snapshot",
            "get_element_state",
            "execute_script",
            "wait_for_element",
        },
    ),
    (
        "screenshots",
        lambda n: n == "take_screenshot",
    ),
    (
        "tabs",
        lambda n: n in {
            "list_tabs",
            "new_tab",
            "switch_tab",
            "close_tab",
            "get_active_tab",
        },
    ),
    (
        "cookies and storage",
        lambda n: n in {
            "get_cookies",
            "set_cookie",
            "clear_cookies",
            "save_session",
            "restore_session",
        },
    ),
    (
        "network",
        lambda n: (
            n.startswith(
                (
                    "list_network",
                    "get_request",
                    "get_response",
                    "search_network",
                    "export_",
                    "import_network",
                    "set_network",
                    "get_network",
                )
            )
            or n.startswith("modify_headers")
        ),
    ),
    (
        "profiles and seeds",
        lambda n: "seed" in n,
    ),
    (
        "extraction and cloning",
        lambda n: (
            (
                n.startswith(
                    (
                        "extract_",
                        "clone_",
                        "expand_",
                        "list_stored",
                        "clear_stored",
                        "list_clone",
                        "cleanup_clone",
                    )
                )
                and n != "expand_tools"
            )
            or n == "clear_all_elements"
            or n.endswith("_to_file")
        ),
    ),
    ("advanced", lambda n: True),
]

# Canonical group name order, preserved for deterministic output.
TOOL_GROUPS: Dict[str, Callable[[str], bool]] = {
    name: matcher for name, matcher in _TOOL_GROUP_RULES
}


def _collapse_ws(text: str) -> str:
    """Collapse runs of whitespace into single spaces and trim."""
    return re.sub(r"\s+", " ", text).strip()


def _cap_description(text: str) -> str:
    """Cap ``text`` at ``DESCRIPTION_CAP`` at a sentence/word boundary, no ellipsis."""
    if len(text) <= DESCRIPTION_CAP:
        return text
    head = text[:DESCRIPTION_CAP]
    dot = head.rfind(". ")
    if dot != -1:
        return head[: dot + 1]
    space = head.rfind(" ")
    if space != -1:
        return head[:space].rstrip()
    return head


def _cap_word(text: str, cap: int) -> str:
    """Cap ``text`` at ``cap`` chars at a word boundary, no ellipsis."""
    if len(text) <= cap:
        return text
    head = text[:cap]
    space = head.rfind(" ")
    if space != -1:
        return head[:space].rstrip()
    return head


def _description_prefix(desc: str) -> str:
    """Text of ``desc`` before the first Args/Returns/Raises/Example header line."""
    out: List[str] = []
    for line in desc.splitlines():
        stripped = line.strip()
        if stripped.startswith(_DESCRIPTION_HEADERS):
            break
        out.append(stripped)
    return _collapse_ws(" ".join(out))


def _parse_args_section(docstring: Optional[str]) -> Dict[str, str]:
    """Parse the ``Args:`` section of a docstring into ``{param name: text}``.

    Parameter lines look like ``name (type): text``. Continuation lines indented
    deeper than the parameter line are appended to that parameter's text.
    """
    if not docstring:
        return {}
    lines = docstring.splitlines()
    args_start = None
    for i, line in enumerate(lines):
        if line.strip() in _ARG_SECTION_MARKERS:
            args_start = i
            break
    if args_start is None:
        return {}
    end = len(lines)
    for i in range(args_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.endswith(":") and stripped.split()[0].lower() in _ARG_SECTION_ENDS:
            end = i
            break
    section = lines[args_start + 1:end]

    param_line_re = re.compile(r"^(\w+)(?:\s*\([^)]*\))?:\s*(.*)$")
    result: Dict[str, str] = {}
    order: List[str] = []
    param_indent: Optional[int] = None
    current: Optional[str] = None
    for raw in section:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        match = param_line_re.match(raw.strip())
        if match and (param_indent is None or indent >= param_indent):
            if param_indent is None:
                param_indent = indent
            name = match.group(1)
            text = match.group(2).strip()
            result[name] = text
            order.append(name)
            current = name
            continue
        if current is not None and param_indent is not None and indent > param_indent:
            result[current] = (result[current] + " " + raw.strip()).strip()
    return result


def _strip_note(desc: str) -> str:
    """Remove a trailing ``INSTANCE_DEFAULT_NOTE`` so compaction is idempotent."""
    if desc.endswith(INSTANCE_DEFAULT_NOTE):
        return desc[: -len(INSTANCE_DEFAULT_NOTE)]
    return desc


def _clean_property_schema(pdef: Dict[str, Any]) -> None:
    """Remove default: null and redundant title keys while preserving nullable anyOf."""
    pdef.pop("title", None)
    if "default" in pdef and pdef["default"] is None:
        pdef.pop("default")

    any_of = pdef.get("anyOf")
    if isinstance(any_of, list):
        for branch in any_of:
            if isinstance(branch, dict):
                branch.pop("title", None)


def _compact_tool(tool: Any) -> None:
    """Compact a single ``Tool`` object in place."""
    desc = (
        tool.description
        or getattr(tool, "__doc__", None)
        or getattr(getattr(tool, "fn", None), "__doc__", None)
        or ""
    )
    base = _strip_note(desc)

    prefix = _collapse_ws(_description_prefix(base))
    prefix = _cap_description(prefix)
    if not prefix.endswith(INSTANCE_DEFAULT_NOTE):
        prefix = prefix + INSTANCE_DEFAULT_NOTE
    tool.description = prefix

    params = getattr(tool, "parameters", None)
    if not isinstance(params, dict):
        return
    params.pop("title", None)

    args_text = _parse_args_section(base)
    if not args_text:
        fn_doc = (
            getattr(getattr(tool, "fn", None), "__doc__", None)
            or getattr(tool, "__doc__", None)
        )
        if fn_doc and fn_doc != base:
            args_text = _parse_args_section(fn_doc)
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return
    for pname, pdef in properties.items():
        if not isinstance(pdef, dict):
            continue
        _clean_property_schema(pdef)
        if not pdef.get("description") and pname in args_text:
            pdef["description"] = _cap_word(_collapse_ws(args_text[pname]), PROPERTY_CAP)
        elif pdef.get("description"):
            pdef["description"] = _cap_word(_collapse_ws(pdef["description"]), PROPERTY_CAP)


def compact_tools(mcp: Any) -> Dict[str, int]:
    """Compact tool descriptions and schemas on ``mcp`` in place.

    Collapses Google-style docstrings down to the text before the first
    Args/Returns/Raises/Example header, caps them, drops ``title`` keys, and fills
    empty property descriptions from the parsed Args section. Names, types,
    required, defaults, enums, and output schemas are left untouched. Running it
    twice produces identical output.

    Args:
        mcp: A FastMCP server instance with a ``_tool_manager._tools`` dict.

    Returns:
        A mapping with the number of tools processed and the total description
        character counts before and after compaction.
    """
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", {}) if tool_manager is not None else {}
    if not isinstance(tools, dict):
        return {"tools": 0, "before_chars": 0, "after_chars": 0}

    before = 0
    after = 0
    for tool in tools.values():
        before += len(tool.description or "")
        _compact_tool(tool)
        after += len(tool.description or "")
    return {"tools": len(tools), "before_chars": before, "after_chars": after}


def group_tools(all_names: List[str]) -> Dict[str, List[str]]:
    """Assign every tool name to exactly one group; never drops a tool.

    Groups are matched in :data:`TOOL_GROUPS` order; the first match wins and
    everything else lands in "advanced".

    Args:
        all_names: Registered tool names to classify.

    Returns:
        A mapping of group name to the sorted list of its tool names.
    """
    grouped: Dict[str, List[str]] = {name: [] for name in TOOL_GROUPS}
    for name in all_names:
        for group_name, matcher in TOOL_GROUPS.items():
            if matcher(name):
                grouped[group_name].append(name)
                break
    return {group: sorted(names) for group, names in grouped.items()}
