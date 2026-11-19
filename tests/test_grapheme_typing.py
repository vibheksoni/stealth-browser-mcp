"""Unit and gated browser integration tests for grapheme-aware typing and IME option (SB-22)."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import input_actions
from stealth_browser_mcp.dom_handler import DOMHandler
from stealth_browser_mcp.grapheme_typing import is_cjk, split_graphemes, type_graphemes
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    """Safely remove a temporary directory with retry and ignore_errors fallback."""
    if not dir_path or not os.path.exists(dir_path):
        return
    for attempt in range(10):
        try:
            shutil.rmtree(dir_path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


class RecordingTab:
    """Tab that records Input events and answers DOM and Runtime calls."""

    def __init__(self, quads=None, hit=True, backend_node_id=42):
        self.events = []
        self.quads = quads if quads is not None else [[10, 20, 30, 20, 30, 40, 10, 40]]
        self.hit = hit
        self.backend_node_id = backend_node_id

    async def send(self, command):
        request = next(command)
        method, params = request["method"], request.get("params", {})
        if method.startswith("Input."):
            self.events.append((
                method,
                params.get("type"),
                params.get("key") or params.get("text"),
                params.get("modifiers", 0),
            ))
            return None
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj"))
        if method == "Runtime.callFunctionOn":
            self.events.append(("callFunctionOn", params["functionDeclaration"], None, 0))
            return SimpleNamespace(value=self.hit), None
        if method == "DOM.getDocument":
            self.events.append(("DOM.getDocument", params.get("depth"), None, 0))
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            self.events.append(("DOM.querySelector", params.get("selector"), None, 0))
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            self.events.append(("DOM.describeNode", params.get("objectId") or params.get("nodeId"), None, 0))
            return SimpleNamespace(backend_node_id=uc.cdp.dom.BackendNodeId(self.backend_node_id))
        if method == "Runtime.evaluate":
            self.events.append(("Runtime.evaluate", params.get("expression"), None, 0))
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("elem-obj-1")), None
        if method == "Runtime.releaseObject":
            self.events.append(("Runtime.releaseObject", params.get("objectId"), None, 0))
            return None
        return None


class TestSplitGraphemes(unittest.TestCase):
    """Unit tests for split_graphemes without browser or network."""

    def test_empty_string(self):
        self.assertEqual(split_graphemes(""), [])

    def test_ascii(self):
        self.assertEqual(split_graphemes("hello world!"), list("hello world!"))

    def test_crlf(self):
        self.assertEqual(split_graphemes("\r\n"), ["\r\n"])
        self.assertEqual(split_graphemes("a\r\nb"), ["a", "\r\n", "b"])
        self.assertEqual(split_graphemes("\r\r"), ["\r", "\r"])
        self.assertEqual(split_graphemes("\n\n"), ["\n", "\n"])
        self.assertEqual(split_graphemes("\n\r"), ["\n", "\r"])

    def test_combining_accents(self):
        # e + combining acute
        self.assertEqual(split_graphemes("e\u0301"), ["e\u0301"])
        self.assertEqual(split_graphemes("cafe\u0301"), ["c", "a", "f", "e\u0301"])
        # o + combining circumflex + combining acute
        self.assertEqual(split_graphemes("o\u0302\u0301"), ["o\u0302\u0301"])
        # a + combining diaeresis / umlaut
        self.assertEqual(split_graphemes("a\u0308b"), ["a\u0308", "b"])

    def test_variation_selectors(self):
        # female sign + VS16
        self.assertEqual(split_graphemes("\u2640\uFE0F"), ["\u2640\uFE0F"])
        # red heart + VS16
        self.assertEqual(split_graphemes("\u2764\uFE0F"), ["\u2764\uFE0F"])
        # keycap digit 1 (1 + VS16 + enclosing keycap)
        self.assertEqual(split_graphemes("1\uFE0F\u20E3"), ["1\uFE0F\u20E3"])
        # keycap number sign (# + VS16 + enclosing keycap)
        self.assertEqual(split_graphemes("#\uFE0F\u20E3"), ["#\uFE0F\u20E3"])

    def test_skin_tone_modifiers(self):
        # thumbs up + medium skin tone
        self.assertEqual(split_graphemes("\U0001F44D\U0001F3FD"), ["\U0001F44D\U0001F3FD"])
        # boy + light skin tone
        self.assertEqual(split_graphemes("\U0001F466\U0001F3FB"), ["\U0001F466\U0001F3FB"])
        # multiple emoji with modifiers
        self.assertEqual(
            split_graphemes("\U0001F44D\U0001F3FD\U0001F44D"),
            ["\U0001F44D\U0001F3FD", "\U0001F44D"],
        )

    def test_emoji_zwj_sequences(self):
        # Family: Man + ZWJ + Woman + ZWJ + Girl
        zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        self.assertEqual(split_graphemes(zwj_family), [zwj_family])

        # Family: Man + ZWJ + Woman + ZWJ + Girl + ZWJ + Boy
        zwj_family_4 = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
        self.assertEqual(split_graphemes(zwj_family_4), [zwj_family_4])

        # Woman technologist: Woman + ZWJ + Laptop
        woman_tech = "\U0001F469\u200D\U0001F4BB"
        self.assertEqual(split_graphemes(woman_tech), [woman_tech])

        # Rainbow flag: White flag + VS16 + ZWJ + Rainbow
        rainbow_flag = "\U0001F3F3\uFE0F\u200D\U0001F308"
        self.assertEqual(split_graphemes(rainbow_flag), [rainbow_flag])

        # Woman runner light skin: Runner + Light Skin + ZWJ + Female Sign + VS16
        woman_runner = "\U0001F3C3\U0001F3FB\u200D\u2640\uFE0F"
        self.assertEqual(split_graphemes(woman_runner), [woman_runner])

        # People holding hands with different skin tones
        hands = "\U0001F9D1\U0001F3FB\u200D\U0001F91D\u200D\U0001F9D1\U0001F3FD"
        self.assertEqual(split_graphemes(hands), [hands])

    def test_flags(self):
        # US flag: RI U + RI S
        us_flag = "\U0001F1FA\U0001F1F8"
        self.assertEqual(split_graphemes(us_flag), [us_flag])

        # US flag + France flag
        fr_flag = "\U0001F1EB\U0001F1F7"
        self.assertEqual(split_graphemes(us_flag + fr_flag), [us_flag, fr_flag])

        # Three RIs: first pair stays together, third is separate
        self.assertEqual(split_graphemes(us_flag + "\U0001F1EB"), [us_flag, "\U0001F1EB"])

        # Subdivision flag (England: Black flag + tag g + tag b + tag e + tag n + tag g + cancel tag)
        england_flag = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
        self.assertEqual(split_graphemes(england_flag), [england_flag])

    def test_hangul(self):
        # Jamo sequence L (G) + V (A) + T (N) -> one syllable
        jamo_gan = "\u1100\u1161\u11AB"
        self.assertEqual(split_graphemes(jamo_gan), [jamo_gan])

        # Precomposed LV (GA) + T (N)
        lv_plus_t = "\uAC00\u11AB"
        self.assertEqual(split_graphemes(lv_plus_t), [lv_plus_t])

        # Two precomposed syllables (HAN + GEUL)
        hangeul = "\uD55C\uAE00"
        self.assertEqual(split_graphemes(hangeul), ["\uD55C", "\uAE00"])

        # Jamo L + V followed by L + V
        gana = "\u1100\u1161\u1102\u1161"
        self.assertEqual(split_graphemes(gana), ["\u1100\u1161", "\u1102\u1161"])

    def test_cjk(self):
        # Chinese: "World" (2 ideographs)
        cjk_text = "\u4e16\u754c"
        self.assertEqual(split_graphemes(cjk_text), ["\u4e16", "\u754c"])

        # Japanese Hiragana: "konnichiwa" (5 kana)
        kana = "\u3053\u3093\u306b\u3061\u306f"
        self.assertEqual(split_graphemes(kana), ["\u3053", "\u3093", "\u306b", "\u3061", "\u306f"])

    def test_mixed(self):
        text = "Hi \U0001F468\u200D\U0001F469\u200D\U0001F467 \u4e16\u754c!"
        expected = ["H", "i", " ", "\U0001F468\u200D\U0001F469\u200D\U0001F467", " ", "\u4e16", "\u754c", "!"]
        self.assertEqual(split_graphemes(text), expected)


class TestIsCJK(unittest.TestCase):
    """Unit tests for CJK character detection."""

    def test_cjk_characters(self):
        self.assertTrue(is_cjk("\u4e16"))
        self.assertTrue(is_cjk("\u3053"))
        self.assertTrue(is_cjk("\u30A2"))
        self.assertTrue(is_cjk("\uD55C"))

    def test_non_cjk_characters(self):
        self.assertFalse(is_cjk("hello"))
        self.assertFalse(is_cjk("\U0001F468"))
        self.assertFalse(is_cjk("123"))
        self.assertFalse(is_cjk("!@#$"))


class TestTypeGraphemesRecordingTab(unittest.TestCase):
    """Unit tests using RecordingTab to assert CDP input events."""

    def test_zwj_sent_as_one_insert_text(self):
        """Verify that a multi-codepoint ZWJ emoji sequence is sent as ONE insertText call."""
        tab = RecordingTab()
        zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"

        asyncio.run(type_graphemes(tab, zwj_family))

        insert_events = [e for e in tab.events if e[0] == "Input.insertText"]
        self.assertEqual(len(insert_events), 1)
        self.assertEqual(insert_events[0], ("Input.insertText", None, zwj_family, 0))
        self.assertFalse(any(e[0] == "Input.dispatchKeyEvent" for e in tab.events))

    def test_cjk_without_ime(self):
        """Verify that CJK text without IME sends insertText for each ideograph."""
        tab = RecordingTab()
        cjk_text = "\u4e16\u754c"

        asyncio.run(type_graphemes(tab, cjk_text, ime=False))

        insert_events = [e for e in tab.events if e[0] == "Input.insertText"]
        self.assertEqual(len(insert_events), 2)
        self.assertEqual(insert_events[0][2], "\u4e16")
        self.assertEqual(insert_events[1][2], "\u754c")
        self.assertFalse(any(e[0] == "Input.imeSetComposition" for e in tab.events))

    def test_cjk_with_ime(self):
        """Verify that CJK text with ime=True sends imeSetComposition then insertText."""
        tab = RecordingTab()
        cjk_text = "\u4e16\u754c"

        asyncio.run(type_graphemes(tab, cjk_text, ime=True))

        methods = [e[0] for e in tab.events]
        expected_methods = [
            "Input.imeSetComposition",
            "Input.insertText",
            "Input.imeSetComposition",
            "Input.insertText",
        ]
        self.assertEqual(methods, expected_methods)
        self.assertEqual(tab.events[0], ("Input.imeSetComposition", None, "\u4e16", 0))
        self.assertEqual(tab.events[1], ("Input.insertText", None, "\u4e16", 0))
        self.assertEqual(tab.events[2], ("Input.imeSetComposition", None, "\u754c", 0))
        self.assertEqual(tab.events[3], ("Input.insertText", None, "\u754c", 0))

    def test_ascii_uses_type_character(self):
        """Verify that pure ASCII characters use trusted key down and key up events."""
        tab = RecordingTab()

        asyncio.run(type_graphemes(tab, "ab"))

        key_events = [e for e in tab.events if e[0] == "Input.dispatchKeyEvent"]
        self.assertEqual(len(key_events), 4)
        self.assertFalse(any(e[0] == "Input.insertText" for e in tab.events))

    def test_mixed_text(self):
        """Verify that mixed ASCII, ZWJ emoji, and CJK text use their respective paths."""
        tab = RecordingTab()
        zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        text = "A " + zwj_family + " \u4e16"

        asyncio.run(type_graphemes(tab, text))

        insert_events = [e for e in tab.events if e[0] == "Input.insertText"]
        self.assertEqual(len(insert_events), 2)
        self.assertEqual(insert_events[0][2], zwj_family)
        self.assertEqual(insert_events[1][2], "\u4e16")

        key_events = [e for e in tab.events if e[0] == "Input.dispatchKeyEvent"]
        self.assertGreater(len(key_events), 0)

    def test_dom_handler_type_text_grapheme_auto(self):
        """Verify that DOMHandler.type_text automatically routes non-ASCII text to grapheme typing."""
        tab = RecordingTab(backend_node_id=12)
        zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"

        asyncio.run(DOMHandler.type_text(tab, "#text-input", zwj_family, clear_first=False))

        insert_events = [e for e in tab.events if e[0] == "Input.insertText"]
        self.assertEqual(len(insert_events), 1)
        self.assertEqual(insert_events[0][2], zwj_family)

    def test_dom_handler_type_text_grapheme_disabled(self):
        """Verify that grapheme=False falls back to character-by-character iteration."""
        tab = RecordingTab(backend_node_id=12)
        zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"

        asyncio.run(DOMHandler.type_text(
            tab, "#text-input", zwj_family, clear_first=False, grapheme=False
        ))

        insert_events = [e for e in tab.events if e[0] == "Input.insertText"]
        # Without grapheme clustering, each codepoint is typed separately (5 calls)
        self.assertEqual(len(insert_events), 5)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestGraphemeTypingBrowserGated(unittest.TestCase):
    """Gated real-browser integration tests verifying Emoji ZWJ + CJK read back exactly."""

    def test_emoji_zwj_and_cjk_read_back_exactly(self):
        """Launch headless Chrome and verify that typing emoji ZWJ and CJK reads back exactly."""
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        async def run_test() -> None:
            temp_dir = tempfile.mkdtemp(prefix="stealth_grapheme_test_")
            browser: Optional[Any] = None
            try:
                with FixtureServer() as server:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)

                    # 1. Test on inputs.html
                    tab = await browser.get(server.url_for("inputs.html"))

                    zwj_family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
                    cjk_text = "\u4e16\u754c\u3053\u3093\u306b\u3061\u306f\uD55C\uAE00"

                    # Type emoji ZWJ sequence and verify exact read-back
                    await DOMHandler.type_text(tab, "#text-input", zwj_family, clear_first=True)
                    val = await tab.evaluate("document.getElementById('text-input').value")
                    self.assertEqual(val, zwj_family)

                    # Type CJK text and verify exact read-back
                    await DOMHandler.type_text(tab, "#text-input", cjk_text, clear_first=True)
                    val = await tab.evaluate("document.getElementById('text-input').value")
                    self.assertEqual(val, cjk_text)

                    # Type CJK text with ime=True option and verify exact read-back
                    await DOMHandler.type_text(
                        tab, "#text-input", cjk_text, clear_first=True, ime=True
                    )
                    val = await tab.evaluate("document.getElementById('text-input').value")
                    self.assertEqual(val, cjk_text)

                    # Type combined text: ASCII + Emoji ZWJ + CJK
                    combined = "Hello " + zwj_family + " " + cjk_text + "!"
                    await DOMHandler.type_text(tab, "#text-input", combined, clear_first=True)
                    val = await tab.evaluate("document.getElementById('text-input').value")
                    self.assertEqual(val, combined)

                    # 2. Test on dedicated grapheme_input.html fixture page
                    tab_fixture = await browser.get(server.url_for("grapheme_input.html"))
                    await DOMHandler.type_text(
                        tab_fixture, "#grapheme-input", zwj_family, clear_first=True
                    )
                    val_fixture = await tab_fixture.evaluate(
                        "document.getElementById('grapheme-input').value"
                    )
                    self.assertEqual(val_fixture, zwj_family)

                    await DOMHandler.type_text(
                        tab_fixture, "#grapheme-input", cjk_text, clear_first=True, ime=True
                    )
                    val_cjk = await tab_fixture.evaluate(
                        "document.getElementById('grapheme-input').value"
                    )
                    self.assertEqual(val_cjk, cjk_text)

            finally:
                if browser is not None:
                    try:
                        if getattr(browser, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    if getattr(browser, "_process", None) is not None:
                        try:
                            await _stop_process(browser._process)
                        except Exception:
                            pass
                    connections = [
                        getattr(t, "disconnect", None)
                        for t in list(getattr(browser, "targets", []) or [])
                    ]
                    if getattr(browser, "connection", None) is not None:
                        connections.append(getattr(browser.connection, "disconnect", None))
                    disconnects = [d() for d in connections if d is not None]
                    if disconnects:
                        try:
                            await asyncio.wait(
                                [asyncio.ensure_future(item) for item in disconnects],
                                timeout=2.0,
                            )
                        except Exception:
                            pass
                    browser._process = None
                if temp_dir:
                    _safe_cleanup_dir(temp_dir)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
