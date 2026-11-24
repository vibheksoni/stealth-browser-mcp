"""Text normalization, comparison, and safe preview helpers.

These utilities operate purely on Python strings. They are used to
normalize text before comparison (e.g. form input) and to produce
opaque previews of sensitive values such as passwords without leaking
the value itself.

Design notes:

* Normalization applies Unicode NFC decomposition + recomposition first,
  then collapses line endings and whitespace variants, and finally strips
  invisible zero-width characters while preserving the zero-width
  joiner (U+200D) that is meaningful inside emoji sequences.
* Comparison reports both a strict and a normalized view of equality,
  and surfaces the first differing index so callers can localize a
  mismatch.
* Safe preview never returns the raw value for sensitive input; it
  returns only a length and a short sha256 prefix.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

# U+200B ZERO WIDTH SPACE, U+FEFF ZERO WIDTH NO-BREAK SPACE (BOM).
_ZERO_WIDTH_STRIP = "\u200b\ufeff"


def normalize_text(s):
    """Return a normalized form of ``s``.

    Applies, in order:
      - Unicode NFC (canonical composition), so e + combining acute
        (U+0065 U+0301) becomes e-acute (U+00E9).
      - CRLF and lone CR line endings to LF.
      - Non-breaking space (U+00A0) to a regular space.
      - Removal of zero-width space (U+200B) and BOM (U+FEFF), while
        keeping U+200D (zero-width joiner) used inside emoji sequences.
    """
    if s is None:
        return ""
    out = unicodedata.normalize("NFC", s)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = out.replace("\u00a0", " ")
    stripped = [ch for ch in out if ch not in _ZERO_WIDTH_STRIP]
    return "".join(stripped)


@dataclass(frozen=True)
class TextComparisonResult:
    """Result of comparing an expected and actual string."""

    expected: str
    actual: str
    normalized_expected: str
    normalized_actual: str
    equal: bool
    normalized_equal: bool
    first_diff_index: int

    def to_dict(self):
        return {
            "expected": self.expected,
            "actual": self.actual,
            "normalized_expected": self.normalized_expected,
            "normalized_actual": self.normalized_actual,
            "equal": self.equal,
            "normalized_equal": self.normalized_equal,
            "first_diff_index": self.first_diff_index,
        }


def _first_diff_index(a, b):
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return limit
    return -1


def compare_text(expected, actual, mode="normalize"):
    """Compare ``expected`` against ``actual``.

    ``mode`` selects the comparison semantics:
      - "strict": compare the raw strings.
      - "normalize": compare the normalized strings.
      - "auto": equal if either strict or normalized comparison holds.

    Returns a TextComparisonResult describing equality under both the
    raw and normalized forms, plus the first differing index in the
    normalized strings (or -1 when they are equal).
    """
    ne = normalize_text(expected)
    na = normalize_text(actual)

    strict_equal = expected == actual
    norm_equal = ne == na

    if mode == "strict":
        equal = strict_equal
    elif mode == "normalize":
        equal = norm_equal
    elif mode == "auto":
        equal = strict_equal or norm_equal
    else:
        raise ValueError("mode must be 'strict', 'normalize', or 'auto'")

    first_diff = _first_diff_index(ne, na) if not norm_equal else -1
    return TextComparisonResult(
        expected=expected,
        actual=actual,
        normalized_expected=ne,
        normalized_actual=na,
        equal=equal,
        normalized_equal=norm_equal,
        first_diff_index=first_diff,
    )


def safe_preview(value, is_password=False):
    """Return an opaque preview of ``value``.

    For sensitive (password) input only a character length and a short
    sha256 prefix are returned; the value is never included. For other
    text a repr-style preview is returned, truncated to
    ``max_length`` characters.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)

    if is_password:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return {"length": len(value), "sha256_prefix": digest[:12], "redacted": True}

    preview = repr(value)
    max_length = 120
    if len(preview) > max_length:
        preview = preview[: max_length - 3] + "..."
    return {"text": preview, "length": len(value), "redacted": False}
