"""Tests for text normalization, comparison, and safe preview helpers."""

import hashlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.text_compare import (
    compare_text,
    normalize_text,
    safe_preview,
)

# Reusable Unicode building blocks.
COMBINING_ACUTE = "\u0301"
E_ACUTE = "\u00e9"
COMBINED_E = "e" + COMBINING_ACUTE
NBSP = "\u00a0"
ZWSP = "\u200b"
ZWJ = "\u200d"
BOM = "\ufeff"
# Family emoji: Man + ZWJ + Woman + ZWJ + Girl (a valid ZWJ sequence).
FAMILY = "man" + ZWJ + "woman" + ZWJ + "girl"
FAMILY_WITH_ZWSP = ZWSP + "man" + ZWJ + "woman" + ZWJ + "girl" + ZWSP


class TestNormalizeTable(unittest.TestCase):
    """Table-driven normalization: (input, normalized_expected)."""

    CASES = [
        # Combining marks compose to precomposed form (NFC).
        (COMBINED_E, E_ACUTE),
        # CRLF collapses to LF.
        ("a\r\nb", "a\nb"),
        # Lone CR collapses to LF.
        ("a\rb", "a\nb"),
        # NBSP becomes a normal space.
        ("a{}b".format(NBSP), "a b"),
        # Zero-width space is removed.
        ("a{}b".format(ZWSP), "ab"),
        # BOM (U+FEFF) is removed.
        (BOM + "hello", "hello"),
        # ZWJ inside emoji sequences is preserved.
        (FAMILY, FAMILY),
        # ZWJ emoji sequence survives surrounding zero-width space.
        (FAMILY_WITH_ZWSP, FAMILY),
        # Empty string stays empty.
        ("", ""),
        # None becomes empty string.
        (None, ""),
        # Line endings and NBSP and BOM all handled together.
        (BOM + "x\ry" + NBSP + "z", "x\ny z"),
        # Precomposed form is idempotent under NFC.
        (E_ACUTE, E_ACUTE),
        # Plain ASCII is unchanged.
        ("hello world", "hello world"),
        # ZWJ preserved but ZWSP stripped in a mixed sequence.
        (ZWSP + FAMILY + ZWSP, FAMILY),
        # CRLF inside a ZWJ emoji sequence keeps the joiner.
        ("a\r\n" + FAMILY, "a\n" + FAMILY),
    ]

    def test_normalize(self):
        for i, (inp, expected) in enumerate(self.CASES):
            with self.subTest(case=i):
                self.assertEqual(normalize_text(inp), expected)


class TestCompareStrict(unittest.TestCase):
    """Strict comparison is raw string equality."""

    def test_combining_strict_not_equal_but_normalizes_equal(self):
        res = compare_text(COMBINED_E, E_ACUTE, mode="strict")
        self.assertFalse(res.equal)
        self.assertTrue(res.normalized_equal)

    def test_crlf_diff_not_equal_strict(self):
        res = compare_text("a\r\nb", "a\nb", mode="strict")
        self.assertFalse(res.equal)

    def test_zwj_preserved_strict_equal(self):
        res = compare_text(FAMILY, FAMILY, mode="strict")
        self.assertTrue(res.equal)

    def test_first_diff_index_position(self):
        res = compare_text("abc", "abd", mode="strict")
        self.assertFalse(res.equal)
        self.assertEqual(res.first_diff_index, 2)

    def test_first_diff_index_length_difference(self):
        res = compare_text("abc", "abcd", mode="strict")
        self.assertFalse(res.equal)
        self.assertEqual(res.first_diff_index, 3)


class TestCompareNormalize(unittest.TestCase):
    """Normalized comparison ignores combining marks, line endings, NBSP, BOM, ZWSP."""

    def test_combining_composes_equal(self):
        res = compare_text(COMBINED_E, E_ACUTE, mode="normalize")
        self.assertTrue(res.normalized_equal)
        self.assertEqual(res.first_diff_index, -1)

    def test_crlf_normalizes_equal(self):
        res = compare_text("a\r\nb", "a\nb", mode="normalize")
        self.assertTrue(res.normalized_equal)

    def test_nbsp_normalizes_equal(self):
        res = compare_text("a{}b".format(NBSP), "a b", mode="normalize")
        self.assertTrue(res.normalized_equal)

    def test_bom_and_zwsp_normalizes_equal(self):
        res = compare_text(BOM + ZWSP + FAMILY, FAMILY, mode="normalize")
        self.assertTrue(res.normalized_equal)

    def test_zwj_still_differs_when_joiner_missing(self):
        res = compare_text(FAMILY, "man" + "woman" + "girl", mode="normalize")
        self.assertFalse(res.normalized_equal)
        self.assertGreater(res.first_diff_index, -1)

    def test_none_inputs(self):
        res = compare_text(None, "", mode="normalize")
        self.assertTrue(res.normalized_equal)
        self.assertEqual(res.first_diff_index, -1)


class TestCompareAuto(unittest.TestCase):
    """Auto mode is equal when either strict or normalized holds."""

    def test_combining_auto_equal(self):
        res = compare_text(COMBINED_E, E_ACUTE, mode="auto")
        self.assertTrue(res.equal)

    def test_strict_only_auto_equal(self):
        res = compare_text("abc", "abc", mode="auto")
        self.assertTrue(res.equal)

    def test_no_match_auto_not_equal(self):
        res = compare_text("abc", "abd", mode="auto")
        self.assertFalse(res.equal)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            compare_text("a", "b", mode="bogus")


class TestSafePreview(unittest.TestCase):
    """Safe preview never leaks password values; text previews are truncated."""

    def test_password_no_value_leaked(self):
        secret = "hunter2-p@ss word\u00a0with\u200bZWSP"
        out = safe_preview(secret, is_password=True)
        self.assertTrue(out["redacted"])
        self.assertNotIn("hunter2", out["sha256_prefix"])
        self.assertNotIn("value", out)
        self.assertIn("sha256_prefix", out)
        self.assertIn("length", out)

    def test_password_length_counts_code_points(self):
        out = safe_preview("abcde", is_password=True)
        self.assertEqual(out["length"], 5)

    def test_password_sha256_prefix_matches_sha256(self):
        secret = "correct horse battery staple"
        full = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        out = safe_preview(secret, is_password=True)
        self.assertEqual(out["sha256_prefix"], full[:12])

    def test_password_none_value(self):
        out = safe_preview(None, is_password=True)
        self.assertEqual(out["length"], 0)
        self.assertTrue(out["redacted"])

    def test_text_preview_includes_repr(self):
        out = safe_preview("hello", is_password=False)
        self.assertFalse(out["redacted"])
        self.assertEqual(out["text"], repr("hello"))

    def test_text_preview_truncated(self):
        long_text = "x" * 500
        out = safe_preview(long_text, is_password=False)
        self.assertIn("...", out["text"])
        self.assertLessEqual(len(out["text"]), 120)

    def test_text_none_value(self):
        out = safe_preview(None, is_password=False)
        self.assertEqual(out["text"], repr(""))
        self.assertEqual(out["length"], 0)

    def test_non_string_coerced(self):
        out = safe_preview(12345, is_password=False)
        self.assertEqual(out["text"], repr("12345"))


if __name__ == "__main__":
    unittest.main()
