import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from browser_manager import BrowserManager
from cdp_function_executor import CDPFunctionExecutor
from models import BrowserOptions


class FakeTab:
    def __init__(self, browser, target_id):
        self.browser = browser
        self.target = SimpleNamespace(target_id=target_id)
        self.handlers = {}

    def __await__(self):
        if False:
            yield
        return self

    async def close(self):
        self.browser.tabs.remove(self)


class FakeBrowser:
    def __init__(self, count):
        self.tabs = [FakeTab(self, f"tab-{i}") for i in range(count)]
        self.created = 0

    async def update_targets(self):
        return None

    async def get(self, url, new_tab=False):
        self.created += 1
        tab = FakeTab(self, f"new-{self.created}")
        self.tabs.append(tab)
        return tab


def manager_with(browser, current):
    manager = BrowserManager()
    manager._instances["i"] = {
        "browser": browser,
        "tab": current,
        "navigation_count": 0,
        "options": BrowserOptions(),
        "init_scripts": [],
        "configured_tabs": set(),
    }
    return manager


class CloseTabTests(unittest.TestCase):
    def test_closing_current_tab_moves_to_remaining(self):
        browser = FakeBrowser(2)
        manager = manager_with(browser, browser.tabs[1])
        self.assertTrue(asyncio.run(manager.close_tab("i", "tab-1")))
        self.assertEqual(manager._instances["i"]["tab"].target.target_id, "tab-0")

    def test_closing_other_tab_keeps_current(self):
        browser = FakeBrowser(2)
        manager = manager_with(browser, browser.tabs[0])
        self.assertTrue(asyncio.run(manager.close_tab("i", "tab-1")))
        self.assertEqual(manager._instances["i"]["tab"].target.target_id, "tab-0")

    def test_closing_last_tab_opens_blank_tab(self):
        browser = FakeBrowser(1)
        manager = manager_with(browser, browser.tabs[0])
        self.assertTrue(asyncio.run(manager.close_tab("i", "tab-0")))
        self.assertEqual([tab.target.target_id for tab in browser.tabs], ["new-1"])
        self.assertEqual(manager._instances["i"]["tab"].target.target_id, "new-1")

    def test_unknown_tab_and_instance(self):
        browser = FakeBrowser(1)
        manager = manager_with(browser, browser.tabs[0])
        self.assertFalse(asyncio.run(manager.close_tab("i", "missing")))
        self.assertFalse(asyncio.run(manager.close_tab("other", "tab-0")))


class ContextKwargsTests(unittest.TestCase):
    def test_context_kwargs(self):
        self.assertEqual(CDPFunctionExecutor._context_kwargs(None), {})
        self.assertEqual(CDPFunctionExecutor._context_kwargs("  "), {})
        numeric = CDPFunctionExecutor._context_kwargs("3")
        self.assertEqual(numeric, {"context_id": uc.cdp.runtime.ExecutionContextId(3)})
        self.assertEqual(
            CDPFunctionExecutor._context_kwargs("-123.456"),
            {"unique_context_id": "-123.456"},
        )

    def test_evaluate_accepts_context_kwargs(self):
        command = uc.cdp.runtime.evaluate(expression="1", **CDPFunctionExecutor._context_kwargs("7"))
        self.assertEqual(next(command)["params"]["contextId"], 7)
        command = uc.cdp.runtime.evaluate(expression="1", **CDPFunctionExecutor._context_kwargs("abc"))
        self.assertEqual(next(command)["params"]["uniqueContextId"], "abc")


if __name__ == "__main__":
    unittest.main()
