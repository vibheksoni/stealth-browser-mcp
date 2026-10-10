import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from dynamic_hook_system import DynamicHookSystem


class FakeTab:
    def __init__(self):
        self.handlers = {}
        self.methods = []

    async def send(self, command):
        self.methods.append(next(command)["method"])
        return None

    def add_handler(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_handler(self, event_type, handler=None):
        if event_type not in self.handlers:
            return
        if handler is None:
            self.handlers.pop(event_type, None)
            return
        self.handlers[event_type].remove(handler)
        if not self.handlers[event_type]:
            self.handlers.pop(event_type, None)

    def emit(self, event_type, event):
        for handler in list(self.handlers.get(event_type, [])):
            handler(event)


ALLOW_HOOK = """
def process_request(request):
    return HookAction(action="continue")
"""


class DynamicHookCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_cancels_request_tasks_and_removes_handler(self):
        system = DynamicHookSystem()
        tab = FakeTab()
        instance_id = "browser-1"
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_request(tab, event, request_instance_id):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        system._on_request_paused = pending_request
        system.add_instance(instance_id)
        await system.setup_interception(tab, instance_id)
        await system.create_hook("all", {"url_pattern": "*"}, ALLOW_HOOK)

        self.assertIn(uc.cdp.fetch.RequestPaused, tab.handlers)
        tab.emit(uc.cdp.fetch.RequestPaused, object())
        await asyncio.wait_for(started.wait(), timeout=1)

        request_task = next(iter(system._request_tasks[instance_id]))
        await system.cleanup_instance(instance_id)

        self.assertTrue(request_task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(uc.cdp.fetch.RequestPaused, tab.handlers)
        self.assertNotIn(instance_id, system._request_tasks)
        self.assertNotIn(instance_id, system._interception_handlers)
        self.assertNotIn(instance_id, system.instance_hooks)


class OnDemandInterceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_follows_active_hooks(self):
        system = DynamicHookSystem()
        tab = FakeTab()
        system.add_instance("b")
        await system.setup_interception(tab, "b")
        self.assertEqual(tab.methods, [])
        self.assertNotIn(uc.cdp.fetch.RequestPaused, tab.handlers)

        hook_id = await system.create_hook("api", {"url_pattern": "*api*|*graphql*"}, ALLOW_HOOK)
        self.assertEqual(tab.methods, ["Fetch.enable"])
        self.assertEqual(len(tab.handlers[uc.cdp.fetch.RequestPaused]), 1)

        await system.create_hook("img", {"url_pattern": "*.png"}, ALLOW_HOOK, instance_ids=["b"])
        self.assertEqual(tab.methods, ["Fetch.enable", "Fetch.enable"])
        self.assertEqual(len(tab.handlers[uc.cdp.fetch.RequestPaused]), 1)

        await system.remove_hook(hook_id)
        self.assertEqual(tab.methods[-1], "Fetch.enable")
        self.assertEqual(len(system._request_patterns(system._hooks_for_instance("b"))), 1)

        remaining = next(iter(system.hooks))
        await system.remove_hook(remaining)
        self.assertEqual(tab.methods[-1], "Fetch.disable")
        self.assertNotIn(uc.cdp.fetch.RequestPaused, tab.handlers)

    async def test_hooks_for_other_instances_do_not_intercept(self):
        system = DynamicHookSystem()
        tab = FakeTab()
        system.add_instance("a")
        await system.setup_interception(tab, "a")
        await system.create_hook("only-b", {"url_pattern": "*"}, ALLOW_HOOK, instance_ids=["b"])
        self.assertEqual(tab.methods, [])

    async def test_new_tab_replaces_handler(self):
        system = DynamicHookSystem()
        first, second = FakeTab(), FakeTab()
        system.add_instance("a")
        await system.create_hook("all", {"url_pattern": "*"}, ALLOW_HOOK)
        await system.setup_interception(first, "a")
        await system.setup_interception(second, "a")
        self.assertNotIn(uc.cdp.fetch.RequestPaused, first.handlers)
        self.assertIn(uc.cdp.fetch.RequestPaused, second.handlers)


if __name__ == "__main__":
    unittest.main()
