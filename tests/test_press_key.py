import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dom_handler import DOMHandler
from key_definitions import (
    MODIFIER_ALT,
    MODIFIER_CONTROL,
    MODIFIER_META,
    MODIFIER_SHIFT,
    resolve_key_descriptor,
    resolve_modifiers,
)

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

RECORDER_PAGE = """<!doctype html>
<html><body><input id="target">
<script>
window.__events = [];
for (const type of ['keydown', 'keypress', 'keyup']) {
  document.addEventListener(type, (e) => {
    window.__events.push({ type: e.type, key: e.key, isTrusted: e.isTrusted,
                           shiftKey: e.shiftKey });
  }, true);
}
</script>
</body></html>"""


class KeyDescriptorTests(unittest.TestCase):
    def test_named_keys_map_to_cdp_fields(self):
        self.assertEqual(
            resolve_key_descriptor("Enter"),
            {"key": "Enter", "code": "Enter", "virtual_key_code": 13, "text": "\r"},
        )
        self.assertEqual(
            resolve_key_descriptor("ArrowDown"),
            {"key": "ArrowDown", "code": "ArrowDown", "virtual_key_code": 40, "text": None},
        )
        self.assertEqual(
            resolve_key_descriptor("Tab"),
            {"key": "Tab", "code": "Tab", "virtual_key_code": 9, "text": None},
        )
        self.assertEqual(
            resolve_key_descriptor("F12"),
            {"key": "F12", "code": "F12", "virtual_key_code": 123, "text": None},
        )

    def test_named_key_lookup_is_case_insensitive(self):
        self.assertEqual(resolve_key_descriptor("enter"), resolve_key_descriptor("Enter"))
        self.assertEqual(resolve_key_descriptor("pagedown")["code"], "PageDown")

    def test_space_accepts_both_spellings(self):
        expected = {"key": " ", "code": "Space", "virtual_key_code": 32, "text": " "}
        self.assertEqual(resolve_key_descriptor("Space"), expected)
        self.assertEqual(resolve_key_descriptor(" "), expected)

    def test_printable_characters_carry_text(self):
        self.assertEqual(
            resolve_key_descriptor("a"),
            {"key": "a", "code": "KeyA", "virtual_key_code": 65, "text": "a"},
        )
        self.assertEqual(
            resolve_key_descriptor("A"),
            {"key": "A", "code": "KeyA", "virtual_key_code": 65, "text": "A"},
        )
        self.assertEqual(
            resolve_key_descriptor("7"),
            {"key": "7", "code": "Digit7", "virtual_key_code": 55, "text": "7"},
        )
        self.assertEqual(
            resolve_key_descriptor("?"),
            {"key": "?", "code": "Slash", "virtual_key_code": 191, "text": "?"},
        )

    def test_unknown_key_lists_supported_keys(self):
        with self.assertRaises(Exception) as ctx:
            resolve_key_descriptor("Frobnicate")
        message = str(ctx.exception)
        self.assertIn("Unsupported key", message)
        self.assertIn("Enter", message)
        self.assertIn("PageDown", message)
        self.assertIn("F12", message)

    def test_empty_key_is_rejected(self):
        with self.assertRaises(Exception):
            resolve_key_descriptor("")


class ModifierMaskTests(unittest.TestCase):
    def test_bit_values(self):
        self.assertEqual((MODIFIER_ALT, MODIFIER_CONTROL, MODIFIER_META, MODIFIER_SHIFT), (1, 2, 4, 8))

    def test_no_modifiers(self):
        self.assertEqual(resolve_modifiers(None), (0, []))
        self.assertEqual(resolve_modifiers([]), (0, []))

    def test_aliases_normalize(self):
        self.assertEqual(resolve_modifiers(["Ctrl"]), (2, ["Control"]))
        self.assertEqual(resolve_modifiers(["cmd"]), (4, ["Meta"]))
        self.assertEqual(resolve_modifiers(["ALT"]), (1, ["Alt"]))

    def test_combined_mask(self):
        mask, names = resolve_modifiers(["Shift", "Control", "Alt", "Meta"])
        self.assertEqual(mask, 15)
        self.assertEqual(names, ["Shift", "Control", "Alt", "Meta"])

    def test_duplicate_aliases_collapse(self):
        self.assertEqual(resolve_modifiers(["Ctrl", "Control"]), (2, ["Control"]))

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            resolve_modifiers(["Hyper"])
        self.assertIn("Unsupported modifier", str(ctx.exception))


class RecordingTab:
    """Minimal Tab stand-in that records dispatched CDP key events."""

    def __init__(self):
        self.events = []

    async def send(self, command):
        payload = next(command)
        assert payload["method"] == "Input.dispatchKeyEvent", payload["method"]
        self.events.append(payload["params"])
        try:
            command.send(None)
        except StopIteration:
            pass
        return None


class PressKeyDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_sends_keydown_and_keyup_with_text(self):
        tab = RecordingTab()
        result = await DOMHandler.press_key(tab, "Enter")

        self.assertEqual(result, {"success": True, "key": "Enter", "count": 1, "modifiers": []})
        self.assertEqual([e["type"] for e in tab.events], ["keyDown", "keyUp"])
        key_down = tab.events[0]
        self.assertEqual(key_down["key"], "Enter")
        self.assertEqual(key_down["code"], "Enter")
        self.assertEqual(key_down["windowsVirtualKeyCode"], 13)
        self.assertEqual(key_down["text"], "\r")

    async def test_count_repeats_press_pairs(self):
        tab = RecordingTab()
        result = await DOMHandler.press_key(tab, "ArrowDown", count=3, delay_ms=0)

        self.assertEqual(result["count"], 3)
        self.assertEqual(len(tab.events), 6)
        self.assertEqual([e["type"] for e in tab.events[:2]], ["keyDown", "keyUp"])
        self.assertNotIn("text", tab.events[0])

    async def test_shift_modifier_keeps_text(self):
        tab = RecordingTab()
        result = await DOMHandler.press_key(tab, "Tab", modifiers=["Shift"])

        self.assertEqual(result["modifiers"], ["Shift"])
        self.assertEqual(tab.events[0]["modifiers"], 8)
        self.assertEqual(tab.events[1]["modifiers"], 8)

    async def test_shifted_printable_key_has_shifted_text(self):
        tab = RecordingTab()
        await DOMHandler.press_key(tab, "a", modifiers=["Shift"])

        key_down = tab.events[0]
        self.assertEqual(key_down["key"], "A")
        self.assertEqual(key_down["text"], "A")
        self.assertEqual(key_down["unmodifiedText"], "a")

    async def test_non_shift_modifier_suppresses_text(self):
        tab = RecordingTab()
        await DOMHandler.press_key(tab, "a", modifiers=["Ctrl"])

        self.assertEqual(tab.events[0]["modifiers"], 2)
        self.assertNotIn("text", tab.events[0])
        self.assertNotIn("unmodifiedText", tab.events[0])

    async def test_unsupported_unicode_character_is_rejected(self):
        tab = RecordingTab()
        with self.assertRaises(Exception):
            await DOMHandler.press_key(tab, "é")
        self.assertEqual(tab.events, [])

    async def test_native_virtual_key_code_is_omitted(self):
        tab = RecordingTab()
        await DOMHandler.press_key(tab, "a")
        self.assertNotIn("nativeVirtualKeyCode", tab.events[0])

    async def test_invalid_count_is_rejected(self):
        tab = RecordingTab()
        with self.assertRaises(Exception) as ctx:
            await DOMHandler.press_key(tab, "Enter", count=0)
        self.assertIn("Invalid count", str(ctx.exception))
        self.assertEqual(tab.events, [])


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class PressKeyBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatched_events_are_trusted(self):
        from browser_manager import BrowserManager
        from models import BrowserOptions

        manager = BrowserManager()
        instance = await manager.spawn_browser(
            BrowserOptions(headless=True, user_agent=HEADLESS_USER_AGENT)
        )
        try:
            await manager.navigate(
                instance.instance_id,
                "data:text/html," + quote(RECORDER_PAGE),
            )
            tab = await manager.get_tab(instance.instance_id)
            self.assertIsNotNone(tab)

            await DOMHandler.press_key(tab, "Enter", selector="#target")
            await DOMHandler.press_key(tab, "ArrowDown")
            await DOMHandler.press_key(tab, "Tab", modifiers=["Shift"])

            recorded = await DOMHandler.execute_script(
                tab, "JSON.stringify(window.__events)"
            )
            events = json.loads(recorded if isinstance(recorded, str) else recorded["result"])

            self.assertTrue(all(e["isTrusted"] for e in events), events)

            enter_events = [e for e in events if e["key"] == "Enter"]
            self.assertEqual(
                [e["type"] for e in enter_events], ["keydown", "keypress", "keyup"]
            )

            arrow_events = [e for e in events if e["key"] == "ArrowDown"]
            self.assertEqual([e["type"] for e in arrow_events], ["keydown", "keyup"])

            tab_events = [e for e in events if e["key"] == "Tab"]
            self.assertEqual([e["type"] for e in tab_events], ["keydown", "keyup"])
            self.assertTrue(all(e["shiftKey"] for e in tab_events), tab_events)
        finally:
            await manager.close_instance(instance.instance_id)


if __name__ == "__main__":
    unittest.main()
