"""Unicode grapheme cluster splitting and typing for stealth-browser-mcp.

Implements practical UAX #29 grapheme cluster boundaries using stdlib
unicodedata only, plus grapheme-aware CDP input typing with IME simulation
support for CJK text.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional
import unicodedata

from nodriver import cdp

from . import input_actions


def _is_extending(cp: int, char: str) -> bool:
    """Check if character is an extending character or mark."""
    # Combining marks: Mn (nonspacing), Mc (spacing combining), Me (enclosing)
    if unicodedata.category(char) in ("Mn", "Mc", "Me"):
        return True
    # Variation selectors (VS1-VS16: U+FE00..U+FE0F, VS17-VS256: U+E0100..U+E01EF)
    if (0xFE00 <= cp <= 0xFE0F) or (0xE0100 <= cp <= 0xE01EF):
        return True
    # Emoji skin-tone modifiers (Fitzpatrick types 1-6: U+1F3FB..U+1F3FF)
    if 0x1F3FB <= cp <= 0x1F3FF:
        return True
    # Zero-width joiner (U+200D) and zero-width non-joiner (U+200C)
    if char in ("\u200D", "\u200C"):
        return True
    # Subdivision flag tag characters (U+E0020..U+E007F)
    if 0xE0020 <= cp <= 0xE007F:
        return True
    return False


def _hangul_jamo_type(cp: int) -> Optional[str]:
    """Classify code point into Hangul jamo or syllable type."""
    # L: Leading consonant jamo
    if (0x1100 <= cp <= 0x115F) or (0xA960 <= cp <= 0xA97C):
        return "L"
    # V: Vowel jamo
    if (0x1160 <= cp <= 0x11A7) or (0xD7B0 <= cp <= 0xD7C6):
        return "V"
    # T: Trailing consonant jamo
    if (0x11A8 <= cp <= 0x11FF) or (0xD7CB <= cp <= 0xD7FB):
        return "T"
    # Precomposed Hangul syllable
    if 0xAC00 <= cp <= 0xD7A3:
        if (cp - 0xAC00) % 28 == 0:
            return "LV"
        return "LVT"
    return None


def _is_hangul_connect(prev_cp: int, curr_cp: int) -> bool:
    """Check if two consecutive code points form a Hangul syllable sequence."""
    prev_type = _hangul_jamo_type(prev_cp)
    if prev_type is None:
        return False
    curr_type = _hangul_jamo_type(curr_cp)
    if curr_type is None:
        return False

    # GB6: L x (L | V | LV | LVT)
    if prev_type == "L" and curr_type in ("L", "V", "LV", "LVT"):
        return True
    # GB7: (LV | V) x (V | T)
    if prev_type in ("LV", "V") and curr_type in ("V", "T"):
        return True
    # GB8: (LVT | T) x T
    if prev_type in ("LVT", "T") and curr_type == "T":
        return True

    return False


def split_graphemes(text: str) -> List[str]:
    """
    Split text into extended grapheme clusters without third-party dependencies.

    Implements the practical subset of UAX #29:
      - CRLF (\\r\\n as a single cluster)
      - Combining marks (categories Mn, Mc, Me)
      - Variation selectors (VS1-VS16, VS17-VS256)
      - Zero-width joiner (ZWJ) emoji sequences
      - Regional indicator flag pairs
      - Emoji skin-tone modifiers (Fitzpatrick scale)
      - Hangul jamo sequences (L, V, T, LV, LVT)
    """
    if not text:
        return []

    clusters: List[str] = []
    current_cluster: List[str] = []
    prev_char = ""
    ri_count = 0

    for char in text:
        cp = ord(char)
        prev_cp = ord(prev_char) if prev_char else 0

        if not current_cluster:
            current_cluster.append(char)
            prev_char = char
            ri_count = 1 if (0x1F1E6 <= cp <= 0x1F1FF) else 0
            continue

        should_break = True

        # Rule 1: CRLF
        if prev_char == "\r" and char == "\n":
            should_break = False
        elif prev_char in ("\r", "\n"):
            should_break = True
        elif char in ("\r", "\n"):
            should_break = True
        else:
            # Rule 2: Regional Indicator pairs
            is_curr_ri = 0x1F1E6 <= cp <= 0x1F1FF
            if is_curr_ri:
                if ri_count % 2 == 1:
                    should_break = False
                else:
                    should_break = True
            else:
                # Rule 3: Extending characters (combining marks, variation selectors, skin tone, ZWJ/ZWNJ)
                if _is_extending(cp, char):
                    should_break = False
                # Rule 4: Previous char was ZWJ
                elif prev_char == "\u200D":
                    should_break = False
                # Rule 5: Hangul jamo sequence
                elif _is_hangul_connect(prev_cp, cp):
                    should_break = False

        if should_break:
            clusters.append("".join(current_cluster))
            current_cluster = [char]
            ri_count = 1 if (0x1F1E6 <= cp <= 0x1F1FF) else 0
        else:
            current_cluster.append(char)
            if 0x1F1E6 <= cp <= 0x1F1FF:
                ri_count += 1
            else:
                ri_count = 0

        prev_char = char

    if current_cluster:
        clusters.append("".join(current_cluster))

    return clusters


def is_cjk(text: str) -> bool:
    """Check if text contains any Chinese, Japanese, or Korean characters."""
    for char in text:
        cp = ord(char)
        if (
            (0x4E00 <= cp <= 0x9FFF)       # CJK Unified Ideographs
            or (0x3400 <= cp <= 0x4DBF)   # CJK Extension A
            or (0x20000 <= cp <= 0x2A6DF) # CJK Extension B
            or (0x2A700 <= cp <= 0x2B73F) # CJK Extension C
            or (0x2B740 <= cp <= 0x2B81F) # CJK Extension D
            or (0x2B820 <= cp <= 0x2CEAF) # CJK Extension E
            or (0x2CEB0 <= cp <= 0x2EBEF) # CJK Extension F
            or (0x30000 <= cp <= 0x323AF) # CJK Extension G, H
            or (0x2EBF0 <= cp <= 0x2EE5D) # CJK Extension I
            or (0xF900 <= cp <= 0xFAFF)   # CJK Compatibility Ideographs
            or (0x2F800 <= cp <= 0x2FA1F) # CJK Compatibility Supplement
            or (0x3040 <= cp <= 0x309F)   # Hiragana
            or (0x30A0 <= cp <= 0x30FF)   # Katakana
            or (0x31F0 <= cp <= 0x31FF)   # Katakana Phonetic Extensions
            or (0x3100 <= cp <= 0x312F)   # Bopomofo
            or (0x31A0 <= cp <= 0x31BF)   # Bopomofo Extended
            or (0xAC00 <= cp <= 0xD7AF)   # Hangul Syllables
            or (0x1100 <= cp <= 0x11FF)   # Hangul Jamo
            or (0x3130 <= cp <= 0x318F)   # Hangul Compatibility Jamo
            or (0xA960 <= cp <= 0xA97F)   # Hangul Jamo Extended-A
            or (0xD7B0 <= cp <= 0xD7FF)   # Hangul Jamo Extended-B
            or (0x3000 <= cp <= 0x303F)   # CJK Symbols and Punctuation
            or (0xFF00 <= cp <= 0xFFEF)   # Halfwidth and Fullwidth Forms
        ):
            return True
    return False


async def type_graphemes(
    tab: Any,
    text: str,
    ime: bool = False,
    delay_ms: int = 0,
    parse_newlines: bool = False,
    shift_enter: bool = False,
) -> None:
    """
    Type text grapheme cluster by grapheme cluster.

    - ASCII and single BMP non-CJK characters use input_actions.type_character.
    - Multi-codepoint clusters (emoji ZWJ sequences, flags, combining marks) and
      CJK characters are sent via CDP Input.insertText as one unit.
    - When ime=True, CJK clusters are preceded by CDP Input.imeSetComposition.
    """
    if not text:
        return

    clusters = split_graphemes(text)
    delay = max(delay_ms, 0) / 1000

    for index, cluster in enumerate(clusters):
        if is_cjk(cluster):
            if ime:
                await tab.send(cdp.input_.ime_set_composition(
                    text=cluster,
                    selection_start=len(cluster),
                    selection_end=len(cluster),
                ))
            await tab.send(cdp.input_.insert_text(cluster))
        elif cluster == "\r\n":
            if parse_newlines or shift_enter:
                await input_actions.type_character(
                    tab, "\n", newline_as_enter=True, shift_enter=shift_enter
                )
            else:
                await tab.send(cdp.input_.insert_text("\r\n"))
        elif len(cluster) > 1 or ord(cluster) > 0xFFFF:
            await tab.send(cdp.input_.insert_text(cluster))
        else:
            await input_actions.type_character(
                tab,
                cluster,
                newline_as_enter=parse_newlines or shift_enter,
                shift_enter=shift_enter,
            )

        if delay and index < len(clusters) - 1:
            await asyncio.sleep(delay)
