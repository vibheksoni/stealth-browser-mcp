import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

import input_actions


class RecordingTab:
    """Tab that records Input events and answers DOM and Runtime calls."""

    def __init__(self, quads=None, hit=True):
        self.events = []
        self.quads = quads if quads is not None else [[10, 20, 30, 20, 30, 40, 10, 40]]
        self.hit = hit

    async def send(self, command):
        request = next(command)
        method, params = request["method"], request.get("params", {})
        if method.startswith("Input."):
            self.events.append((method, params.get("type"), params.get("key") or params.get("text"), params.get("modifiers", 0)))
            return None
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj"))
        if method == "Runtime.callFunctionOn":
            self.events.append(("callFunctionOn", params["functionDeclaration"], None, 0))
            return SimpleNamespace(value=self.hit), None
        return None


class CharacterKeyTests(unittest.TestCase):
    def test_character_key(self):
        self.assertEqual(input_actions.character_key("a")["code"], "KeyA")
        self.assertEqual(input_actions.character_key("!")["code"], "Digit1")
        self.assertIsNone(input_actions.character_key("é"))
        self.assertIsNone(input_actions.character_key("ab"))

    def test_typing_uses_key_events_and_shift(self):
        tab = RecordingTab()

        async def run():
            for char in "aB!é\n":
                await input_actions.type_character(tab, char)
            await input_actions.type_character(tab, "\n", newline_as_enter=True, shift_enter=True)

        asyncio.run(run())
        kinds = [(event[0], event[1], event[2]) for event in tab.events]
        self.assertEqual(kinds[0:2], [("Input.dispatchKeyEvent", "keyDown", "a"), ("Input.dispatchKeyEvent", "keyUp", "a")])
        self.assertEqual(kinds[2], ("Input.dispatchKeyEvent", "rawKeyDown", "Shift"))
        self.assertEqual(tab.events[3][3], input_actions.MODIFIER_SHIFT)
        self.assertIn(("Input.insertText", None, "é"), kinds)
        self.assertIn(("Input.insertText", None, "\n"), kinds)
        self.assertEqual(tab.events[-2][2], "Enter")
        self.assertEqual(tab.events[-2][3], input_actions.MODIFIER_SHIFT)


class ClickTests(unittest.TestCase):
    def test_trusted_click_moves_presses_and_releases(self):
        tab = RecordingTab()
        used_mouse = asyncio.run(input_actions.click_node(tab, 5))
        self.assertTrue(used_mouse)
        mouse = [event[1] for event in tab.events if event[0] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse.count("mouseMoved"), input_actions.MOUSE_MOVE_STEPS)
        self.assertEqual(mouse[-2:], ["mousePressed", "mouseReleased"])

    def test_covered_or_hidden_falls_back_to_script(self):
        for tab in (RecordingTab(hit=False), RecordingTab(quads=[])):
            used_mouse = asyncio.run(input_actions.click_node(tab, 5))
            self.assertFalse(used_mouse)
            self.assertFalse(any(event[0] == "Input.dispatchMouseEvent" for event in tab.events))
            self.assertTrue(any("click()" in (event[1] or "") for event in tab.events))


if __name__ == "__main__":
    unittest.main()
