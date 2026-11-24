"""Content size capping and boundary-aware truncation helpers (SB-73).

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

MIN_PAGE_CONTENT_CHARS = 1000
MAX_PAGE_CONTENT_CHARS = 200000
DEFAULT_PAGE_CONTENT_CHARS = 40000


def clamp_max_chars(val: Any, default: int = DEFAULT_PAGE_CONTENT_CHARS) -> int:
    """Clamp max_chars to allowed range [1000, 200000]. 0 is not unlimited."""
    try:
        num = int(val)
    except (ValueError, TypeError):
        num = default
    if num <= 0:
        return MIN_PAGE_CONTENT_CHARS
    return max(MIN_PAGE_CONTENT_CHARS, min(num, MAX_PAGE_CONTENT_CHARS))


def _truncate_boundary(text: str, limit: int) -> str:
    """Truncate text to at most limit characters, preferring a line break or word boundary."""
    if len(text) <= limit:
        return text

    sub = text[:limit]

    # Look for last newline within the slice
    last_nl = sub.rfind("\n")
    if last_nl != -1 and last_nl >= int(limit * 0.4):
        return sub[:last_nl].rstrip()

    # If the character immediately following the cut is whitespace, clean cut
    if len(text) > limit and text[limit].isspace():
        return sub.rstrip()

    # Look for last word boundary (whitespace) in sub
    for i in range(len(sub) - 1, -1, -1):
        if sub[i].isspace():
            if i >= int(limit * 0.3):
                return sub[:i].rstrip()
            break

    # Fallback to limit if no suitable boundary found
    return sub.rstrip()


class CapResult(dict):
    """Dictionary holding capped content result with attribute access support."""

    def __init__(
        self,
        content: str,
        truncated: bool,
        original_chars: int,
        note: Optional[str] = None,
    ) -> None:
        d: Dict[str, Any] = {
            "content": content,
            "text": content,
            "truncated": truncated,
            "original_chars": original_chars,
        }
        if truncated and note:
            d["note"] = note
        super().__init__(d)
        self.content = content
        self.text = content
        self.truncated = truncated
        self.original_chars = original_chars
        self.note = note


def cap_content(text: Optional[str], max_chars: int = DEFAULT_PAGE_CONTENT_CHARS) -> CapResult:
    """Cap text content to max_chars with boundary truncation and metadata.

    Args:
        text (Optional[str]): Input string to cap.
        max_chars (int): Maximum characters to keep.

    Returns:
        CapResult: Dict-like object with content, text, truncated, original_chars,
            and note (when truncated).
    """
    raw_text = text if text is not None else ""
    orig_len = len(raw_text)

    try:
        limit = int(max_chars)
    except (ValueError, TypeError):
        limit = DEFAULT_PAGE_CONTENT_CHARS

    if limit <= 0:
        limit = MIN_PAGE_CONTENT_CHARS

    if orig_len <= limit:
        return CapResult(
            content=raw_text,
            truncated=False,
            original_chars=orig_len,
        )

    capped_text = _truncate_boundary(raw_text, limit)
    note = (
        f"Content truncated to {len(capped_text)} characters (original: {orig_len} characters). "
        "Pass a selector or higher max_chars, or use get_accessibility_snapshot."
    )
    return CapResult(
        content=capped_text,
        truncated=True,
        original_chars=orig_len,
        note=note,
    )
