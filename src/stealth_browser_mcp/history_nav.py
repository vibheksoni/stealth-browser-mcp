"""Navigation history decision helpers (SB-65).

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

from typing import List, Optional


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
            return "No previous page in history; this is the first page."
        if current_index - 1 < len(urls):
            prev_url = urls[current_index - 1].strip()
            if (
                prev_url.startswith("chrome://newtab")
                or prev_url == "about:blank"
                or prev_url.startswith("about:blank")
            ):
                return "No previous page in history (previous entry is a blank tab)."
        return None

    if normalized in ("forward", "go_forward"):
        if not urls or current_index >= len(urls) - 1:
            return "No next page in history."
        return None

    return None
