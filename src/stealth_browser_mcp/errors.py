"""User-friendly error helpers with actionable next-step hints (SB-73).

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

from typing import Any, Optional

from fastmcp.exceptions import ToolError


def instance_not_found_error(instance_id: Any) -> ToolError:
    """Create a ToolError for an unknown or closed instance ID."""
    return ToolError(
        f"Instance not found: {instance_id}. It may have been closed or belongs to another session. "
        "Call list_instances to see your open browsers, or spawn_browser to open one."
    )


def tab_not_found_error(tab_id: Optional[str] = None) -> ToolError:
    """Create a ToolError when a specified tab is not found."""
    target = f": {tab_id}" if tab_id else ""
    return ToolError(
        f"Tab not found{target}. It may have been closed. "
        "Call list_tabs to see open tabs, or new_tab to create one."
    )


def no_active_tab_error(instance_id: Optional[str] = None) -> ToolError:
    """Create a ToolError when an instance has no active tab."""
    target = f" for instance '{instance_id}'" if instance_id else ""
    return ToolError(
        f"No active tab found{target}. "
        "Call list_tabs to inspect available tabs, or new_tab to open a new tab."
    )
