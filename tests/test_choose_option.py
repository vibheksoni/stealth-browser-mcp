"""Tests for choose_option custom combobox interaction (ungated unit tests and gated browser suite)."""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import input_actions, server
from stealth_browser_mcp.dom_handler import DOMHandler, choose_option
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


class RecordingTabForCombobox:
    """Fake tab recording CDP commands and answering combobox workflow requests."""

    def __init__(
        self,
        trigger_backend_id=101,
        option_backend_id=202,
        trigger_found=True,
        option_found=True,
        verification_success=True,
    ):
        self.events = []
        self.trigger_backend_id = trigger_backend_id
        self.option_backend_id = option_backend_id
        self.trigger_found = trigger_found
        self.option_found = option_found
        self.verification_success = verification_success
        self.evaluate_count = 0
        self.quads = [[10, 20, 30, 20, 30, 40, 10, 40]]

    async def send(self, command):
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.events.append((method, params))

        if method.startswith("Input."):
            return None
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("resolved-obj"))
        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            if "return this.id" in fn:
                return SimpleNamespace(value="opt-test-id"), None
            return SimpleNamespace(value=True), None
        if method == "DOM.focus":
            return None
        if method == "DOM.scrollIntoViewIfNeeded":
            return None
        if method == "DOM.getDocument":
            if not self.trigger_found:
                return SimpleNamespace(node_id=None)
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            if not self.trigger_found:
                return None
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            if "nodeId" in params:
                return SimpleNamespace(
                    backend_node_id=uc.cdp.dom.BackendNodeId(self.trigger_backend_id)
                )
            return SimpleNamespace(
                backend_node_id=uc.cdp.dom.BackendNodeId(self.option_backend_id)
            )
        if method == "Runtime.evaluate":
            self.evaluate_count += 1
            if params.get("returnByValue"):
                # Verification evaluation or focus input
                expr = params.get("expression", "")
                if "verified" in expr:
                    if self.verification_success:
                        return (
                            SimpleNamespace(
                                type_="object",
                                subtype=None,
                                value={"verified": True, "reason": "single_value_matches"},
                                object_id=None,
                            ),
                            None,
                        )
                    return (
                        SimpleNamespace(
                            type_="object",
                            subtype=None,
                            value={"verified": False},
                            object_id=None,
                        ),
                        None,
                    )
                return (
                    SimpleNamespace(
                        type_="boolean",
                        subtype=None,
                        value=True,
                        object_id=None,
                    ),
                    None,
                )

            # Option finding evaluation
            if self.option_found:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("option-remote-obj-1"),
                    ),
                    None,
                )
            return (
                SimpleNamespace(
                    type_="object",
                    subtype="null",
                    value=None,
                    object_id=None,
                ),
                None,
            )
        if method == "Runtime.releaseObject":
            return None
        return None


class ChooseOptionExpressionTests(unittest.TestCase):
    """Unit tests verifying JS expression generation for combobox option finding and verification."""

    def test_build_find_option_expression_standard_css(self):
        expr = input_actions.build_find_option_expression("Banana", "#fruit-select")
        self.assertIn('"Banana"', expr)
        self.assertIn('"#fruit-select"', expr)
        self.assertIn('[role="listbox"]', expr)
        self.assertIn('[role="option"]', expr)
        self.assertIn('select__option', expr)
        self.assertIn('select__menu', expr)

    def test_build_find_option_expression_with_frame(self):
        expr = input_actions.build_find_option_expression(
            "Apple", "#combo", frame="iframe#widget"
        )
        self.assertIn('"Apple"', expr)
        self.assertIn('"iframe#widget"', expr)

    def test_build_find_option_expression_with_deep_selector(self):
        expr = input_actions.build_find_option_expression(
            "Blue", "app-root >>> .custom-select"
        )
        self.assertIn('"Blue"', expr)
        self.assertIn('"app-root"', expr)
        self.assertIn('".custom-select"', expr)
        self.assertIn("shadowRoot", expr)

    def test_build_find_option_expression_with_xpath(self):
        expr = input_actions.build_find_option_expression(
            "Green", "//div[@role='combobox']"
        )
        self.assertIn('"Green"', expr)
        self.assertIn('"//div[@role=\'combobox\']"', expr)
        self.assertIn("true", expr)  # is_xpath flag

    def test_build_verify_selection_expression(self):
        expr = input_actions.build_verify_selection_expression(
            "Purple", "#color-select", clicked_option_id="opt-purple"
        )
        self.assertIn('"Purple"', expr)
        self.assertIn('"#color-select"', expr)
        self.assertIn('"opt-purple"', expr)
        self.assertIn("aria-selected", expr)
        self.assertIn("single-value", expr)
        self.assertIn("combobox_closed", expr)

    def test_build_focus_input_expression(self):
        expr = input_actions.build_focus_input_expression("#fruit-combobox")
        self.assertIn('"#fruit-combobox"', expr)
        self.assertIn("focus()", expr)


class ChooseOptionUnitTests(unittest.IsolatedAsyncioTestCase):
    """Ungated unit tests for choose_option with RecordingTab fakes."""

    async def test_choose_option_success_flow(self):
        tab = RecordingTabForCombobox(trigger_backend_id=10, option_backend_id=20)
        result = await DOMHandler.choose_option(tab, "#color-select", "Blue", timeout=500)
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Blue")
        self.assertEqual(result["option"], "Blue")
        self.assertEqual(result["verification"], "single_value_matches")

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.querySelector", methods)
        self.assertIn("Runtime.evaluate", methods)

        # Verify mouse events were dispatched for trigger and option clicks
        mouse_events = [e for e in tab.events if e[0] == "Input.dispatchMouseEvent"]
        self.assertGreaterEqual(len(mouse_events), 2)

    async def test_choose_option_module_level_alias(self):
        tab = RecordingTabForCombobox(trigger_backend_id=11, option_backend_id=22)
        result = await choose_option(tab, "#color-select", "Green", timeout=500)
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Green")

    async def test_choose_option_with_filter_text(self):
        tab = RecordingTabForCombobox(trigger_backend_id=12, option_backend_id=24)
        result = await DOMHandler.choose_option(
            tab, "#fruit-combobox", "Cherry", filter_text="cher", timeout=500
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Cherry")

        # Verify trusted key events were dispatched for filter_text "cher"
        key_events = [e for e in tab.events if e[0] == "Input.dispatchKeyEvent"]
        self.assertGreaterEqual(len(key_events), 8)  # keyDown + keyUp per character

    async def test_choose_option_with_deep_selector(self):
        tab = RecordingTabForCombobox(trigger_backend_id=14, option_backend_id=28)
        result = await DOMHandler.choose_option(
            tab, "host-panel >>> #picker", "Large", timeout=500
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Large")

        eval_calls = [e[1]["expression"] for e in tab.events if e[0] == "Runtime.evaluate"]
        has_deep = any("host-panel" in expr and "shadowRoot" in expr for expr in eval_calls)
        self.assertTrue(has_deep)

    async def test_choose_option_with_frame(self):
        tab = RecordingTabForCombobox(trigger_backend_id=15, option_backend_id=30)
        result = await DOMHandler.choose_option(
            tab, "#dropdown", "Option 1", frame="iframe#frame1", timeout=500
        )
        self.assertTrue(result["success"])
        eval_calls = [e[1]["expression"] for e in tab.events if e[0] == "Runtime.evaluate"]
        has_frame = any("iframe#frame1" in expr for expr in eval_calls)
        self.assertTrue(has_frame)

    async def test_choose_option_with_xpath(self):
        tab = RecordingTabForCombobox(trigger_backend_id=16, option_backend_id=32)
        result = await DOMHandler.choose_option(
            tab, "//div[@role='combobox']", "Option X", timeout=500
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Option X")
        eval_calls = [e[1]["expression"] for e in tab.events if e[0] == "Runtime.evaluate"]
        has_xpath = any("//div[@role='combobox']" in expr for expr in eval_calls)
        self.assertTrue(has_xpath)

    async def test_choose_option_trigger_not_found_raises(self):
        tab = RecordingTabForCombobox(trigger_found=False)
        with self.assertRaises(Exception) as ctx:
            await DOMHandler.choose_option(tab, "#missing-trigger", "Any", timeout=50)
        self.assertIn("Failed to choose option", str(ctx.exception))
        self.assertIn("Element not found", str(ctx.exception))

    async def test_choose_option_option_not_found_raises(self):
        tab = RecordingTabForCombobox(option_found=False)
        with self.assertRaises(Exception) as ctx:
            await DOMHandler.choose_option(tab, "#combo", "NonExistent", timeout=100)
        self.assertIn("Failed to choose option", str(ctx.exception))
        self.assertIn("Option 'NonExistent' not found", str(ctx.exception))

    async def test_choose_option_verification_failure_raises(self):
        tab = RecordingTabForCombobox(verification_success=False)
        with self.assertRaises(Exception) as ctx:
            await DOMHandler.choose_option(tab, "#combo", "Banana", timeout=100)
        self.assertIn("Failed to choose option", str(ctx.exception))
        self.assertIn("selection could not be verified", str(ctx.exception))

    async def test_choose_option_mcp_tool_registered(self):
        from fastmcp import Client

        self.assertIn("choose_option", server.SECTION_TOOLS["element-interaction"])
        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        self.assertIn("choose_option", tools)
        tool = tools["choose_option"]
        self.assertEqual(tool.name, "choose_option")
        schema = tool.inputSchema
        self.assertIn("selector", schema["properties"])
        self.assertIn("option_text", schema["properties"])
        self.assertIn("filter_text", schema["properties"])
        self.assertIn("frame", schema["properties"])
        self.assertIn("timeout", schema["properties"])


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class RealBrowserComboboxFixtureTests(unittest.IsolatedAsyncioTestCase):
    """Gated real-browser tests verifying choose_option against fixture pages."""

    async def asyncSetUp(self):
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions

        self.server = FixtureServer().start()
        self.manager = BrowserManager()
        self.instance = await self.manager.spawn_browser(
            BrowserOptions(headless=True, user_agent=HEADLESS_USER_AGENT)
        )
        self.tab = await self.manager.get_tab(self.instance.instance_id)

    async def asyncTearDown(self):
        if hasattr(self, "instance") and self.instance:
            try:
                await self.manager.close_instance(self.instance.instance_id)
            except Exception:
                pass
        if hasattr(self, "server") and self.server:
            self.server.stop()

    async def test_real_browser_react_select_choose(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("react_select.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#color-select",
            "Blue",
            timeout=5000,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Blue")

        # Verify DOM state
        val_text = await self.tab.evaluate(
            "document.querySelector('#color-select .select__single-value').textContent"
        )
        self.assertEqual(val_text, "Blue")

        result_text = await self.tab.evaluate(
            "document.getElementById('color-result').textContent"
        )
        self.assertIn("Blue", result_text)

    async def test_real_browser_react_select_filter_and_choose(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("react_select.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#color-select",
            "Purple",
            filter_text="pur",
            timeout=5000,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Purple")

        val_text = await self.tab.evaluate(
            "document.querySelector('#color-select .select__single-value').textContent"
        )
        self.assertEqual(val_text, "Purple")

    async def test_real_browser_react_select_emotion_classes(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("react_select.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#flavor-select",
            "Strawberry",
            timeout=5000,
        )
        self.assertTrue(result["success"])

        result_text = await self.tab.evaluate(
            "document.getElementById('flavor-result').textContent"
        )
        self.assertIn("Strawberry", result_text)

    async def test_real_browser_aria_combobox_input_choose(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("aria_combobox.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#fruit-combobox",
            "Banana",
            timeout=5000,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["selected"], "Banana")

        # Verify input value updated and option marked selected
        input_val = await self.tab.evaluate(
            "document.getElementById('fruit-combobox').value"
        )
        self.assertEqual(input_val, "Banana")

        is_selected = await self.tab.evaluate(
            "document.getElementById('opt-banana').getAttribute('aria-selected')"
        )
        self.assertEqual(is_selected, "true")

        result_text = await self.tab.evaluate(
            "document.getElementById('fruit-result').textContent"
        )
        self.assertIn("Banana", result_text)

    async def test_real_browser_aria_combobox_filter_and_choose(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("aria_combobox.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#fruit-combobox",
            "Cherry",
            filter_text="cher",
            timeout=5000,
        )
        self.assertTrue(result["success"])

        input_val = await self.tab.evaluate(
            "document.getElementById('fruit-combobox').value"
        )
        self.assertEqual(input_val, "Cherry")

    async def test_real_browser_aria_div_combobox_choose(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("aria_combobox.html"),
        )
        await asyncio.sleep(0.3)

        result = await DOMHandler.choose_option(
            self.tab,
            "#size-combobox",
            "Large",
            timeout=5000,
        )
        self.assertTrue(result["success"])

        display_text = await self.tab.evaluate(
            "document.getElementById('size-display').textContent"
        )
        self.assertEqual(display_text, "Large")

        result_text = await self.tab.evaluate(
            "document.getElementById('size-result').textContent"
        )
        self.assertIn("Large", result_text)

    async def test_real_browser_option_not_found_raises(self):
        await self.manager.navigate(
            self.instance.instance_id,
            self.server.url_for("aria_combobox.html"),
        )
        await asyncio.sleep(0.3)

        with self.assertRaises(Exception) as ctx:
            await DOMHandler.choose_option(
                self.tab,
                "#fruit-combobox",
                "Dragonfruit",
                timeout=1000,
            )
        self.assertIn("Failed to choose option", str(ctx.exception))
        self.assertIn("Option 'Dragonfruit' not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
