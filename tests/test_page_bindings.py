import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

import page_bindings as page_bindings_module
from page_bindings import MAX_PAYLOAD_BYTES, PageBindingManager


class FakeTab:
    """Tab that records CDP methods and answers evaluate calls."""

    def __init__(self, existing_globals=()):
        self.handlers = {}
        self.methods = []
        self.settled = []
        self.existing_globals = set(existing_globals)

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    async def send(self, command):
        request = next(command)
        self.methods.append(request["method"])
        if request["method"] == "Runtime.evaluate":
            expression = request["params"]["expression"]
            if expression.endswith("in globalThis"):
                name = json.loads(expression.split(" in ")[0])
                return SimpleNamespace(value=name in self.existing_globals), None
            if "settle" in expression:
                self.settled.append(expression)
                return SimpleNamespace(value=True), None
            return SimpleNamespace(value=None), None
        if request["method"] == "Page.addScriptToEvaluateOnNewDocument":
            return "script-1"
        return None

    def call(self, name, payload, context_id=7):
        event = SimpleNamespace(name=name, payload=payload, execution_context_id=context_id)
        for handler in list(self.handlers.get(uc.cdp.runtime.BindingCalled, [])):
            handler(event)


def payload(call_id, *args):
    return json.dumps({"id": call_id, "args": list(args)})


class PageBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_validates_names(self):
        manager = PageBindingManager()
        tab = FakeTab(existing_globals={"fetch"})
        with self.assertRaises(ValueError):
            await manager.create(tab, "i", "bad-name")
        with self.assertRaises(ValueError):
            await manager.create(tab, "i", "fetch")
        result = await manager.create(tab, "i", "askAgent")
        self.assertTrue(result["success"])
        self.assertIn("Runtime.addBinding", tab.methods)
        self.assertIn("Page.addScriptToEvaluateOnNewDocument", tab.methods)
        with self.assertRaises(ValueError):
            await manager.create(tab, "i", "askAgent")

    async def test_call_queue_and_resolve(self):
        manager = PageBindingManager()
        tab = FakeTab()
        await manager.create(tab, "i", "askAgent")
        tab.call("askAgent", payload(1, {"q": "hi"}))
        calls = await manager.get_calls("i")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], [{"q": "hi"}])
        self.assertEqual(calls[0]["status"], "pending")
        self.assertEqual(len(await manager.get_calls("i")), 1)

        result = await manager.resolve("i", calls[0]["call_id"], {"ok": True})
        self.assertTrue(result["delivered"])
        self.assertIn('{"ok": true}', tab.settled[-1])
        self.assertEqual(await manager.get_calls("i"), [])
        with self.assertRaises(ValueError):
            await manager.resolve("i", calls[0]["call_id"], 1)

    async def test_wait_for_call(self):
        manager = PageBindingManager()
        tab = FakeTab()
        await manager.create(tab, "i", "askAgent")
        asyncio.get_running_loop().call_later(0.05, tab.call, "askAgent", payload(1, "late"))
        calls = await manager.get_calls("i", wait_seconds=2)
        self.assertEqual(calls[0]["args"], ["late"])

    async def test_rejects_bad_payloads(self):
        manager = PageBindingManager()
        tab = FakeTab()
        await manager.create(tab, "i", "askAgent")
        tab.call("askAgent", "not json")
        tab.call("askAgent", json.dumps({"id": 1, "args": ["x" * MAX_PAYLOAD_BYTES]}))
        tab.call("unknown", payload(2))
        self.assertEqual(await manager.get_calls("i"), [])

    async def test_auto_resolve_and_queue_cap(self):
        manager = PageBindingManager()
        tab = FakeTab()
        await manager.create(tab, "i", "ping", auto_resolve=True, auto_response="pong")
        original = page_bindings_module.MAX_QUEUED_CALLS
        page_bindings_module.MAX_QUEUED_CALLS = 3
        try:
            for index in range(5):
                tab.call("ping", payload(index))
            await asyncio.sleep(0)
            calls = await manager.get_calls("i")
        finally:
            page_bindings_module.MAX_QUEUED_CALLS = original
        self.assertEqual([call["status"] for call in calls], ["auto_resolved"] * 3)
        self.assertEqual(await manager.get_calls("i"), [])
        self.assertTrue(any('"pong"' in expression for expression in tab.settled))

    async def test_prepare_tab_installs_once_and_forget(self):
        manager = PageBindingManager()
        first, second = FakeTab(), FakeTab()
        await manager.create(first, "i", "askAgent")
        await manager.prepare_tab(second, "i")
        await manager.prepare_tab(second, "i")
        self.assertEqual(second.methods.count("Runtime.addBinding"), 1)
        await manager.forget_instance("i")
        self.assertEqual(manager.list_bindings("i"), [])
        self.assertFalse(first.handlers.get(uc.cdp.runtime.BindingCalled))

    async def test_remove_rejects_pending(self):
        manager = PageBindingManager()
        tab = FakeTab()
        await manager.create(tab, "i", "askAgent")
        tab.call("askAgent", payload(1))
        await manager.remove("i", "askAgent")
        self.assertIn('"Binding removed"', tab.settled[-1])
        self.assertIn("Runtime.removeBinding", tab.methods)
        self.assertEqual(await manager.get_calls("i"), [])
        with self.assertRaises(ValueError):
            await manager.remove("i", "askAgent")


if __name__ == "__main__":
    unittest.main()
