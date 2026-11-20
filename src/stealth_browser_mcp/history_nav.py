"""Navigation history decision helpers (SB-65, SB-73).

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

from typing import List, Optional

FIRST_PAGE_MESSAGE = "No previous page in history; this is the first page."
BLANK_PREV_PAGE_MESSAGE = (
    "No previous page in history (previous entry is a blank tab)."
)
NO_NEXT_PAGE_MESSAGE = "No next page in history."

# Backward compatibility aliases
NO_PREV_PAGE_FIRST_MESSAGE = FIRST_PAGE_MESSAGE
NO_PREV_PAGE_BLANK_MESSAGE = BLANK_PREV_PAGE_MESSAGE


def is_internal_start_page(url: Optional[str]) -> bool:
    """Check if a URL is an internal browser start or blank page."""
    u = (url or "").strip().lower()
    return (
        u == "about:blank"
        or u.startswith("about:blank")
        or u == "newtab"
        or u.startswith("newtab")
        or u.startswith("chrome://new-tab-page")
        or u.startswith("chrome://newtab")
        or u.startswith("chrome-search://")
    )


def decide_history_move(
    direction: str,
    current_index: int,
    urls: List[str],
) -> Optional[str]:
    """Decide if a history navigation move should be refused.

    Args:
        direction (str): Navigation direction ('back' or 'forward').
        current_index (int): Index of current navigation entry.
        urls (List[str]): URLs in the navigation history.

    Returns:
        Optional[str]: Error message if navigation should be refused,
            or None if allowed.
    """
    normalized = direction.strip().lower()
    if normalized in ("back", "go_back"):
        if current_index <= 0 or not urls:
            return FIRST_PAGE_MESSAGE
        if current_index - 1 < len(urls) and is_internal_start_page(urls[current_index - 1]):
            return BLANK_PREV_PAGE_MESSAGE
        return None

    if normalized in ("forward", "go_forward"):
        if not urls or current_index >= len(urls) - 1:
            return NO_NEXT_PAGE_MESSAGE
        return None

    return None
