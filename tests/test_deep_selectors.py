import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import input_actions
from stealth_browser_mcp.dom_handler import DOMHandler

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


class FakeRecordingTab:
    """Tab stand-in that records CDP commands and answers DOM/Runtime requests."""

    def __init__(self, backend_node_id=42, evaluate_object_id="elem-obj-1", poll_success_count=1):
        self.events = []
        self.backend_node_id = backend_node_id
        self.evaluate_object_id = evaluate_object_id
        self.poll_success_count = poll_success_count
        self.evaluate_call_count = 0
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
            return SimpleNamespace(value=True), None
        if method == "DOM.focus":
            return None
        if method == "DOM.scrollIntoViewIfNeeded":
            return None
        if method == "DOM.getDocument":
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            return SimpleNamespace(backend_node_id=uc.cdp.dom.BackendNodeId(self.backend_node_id))
        if method == "Runtime.evaluate":
            self.evaluate_call_count += 1
            if self.evaluate_call_count < self.poll_success_count:
                return SimpleNamespace(type_="object", subtype="null", value=None, object_id=None), None
            if self.evaluate_object_id:
                return SimpleNamespace(
                    type_="object",
                    subtype="node",
                    object_id=uc.cdp.runtime.RemoteObjectId(self.evaluate_object_id),
                ), None
            return SimpleNamespace(type_="object", subtype="null", value=None, object_id=None), None
        if method == "Runtime.releaseObject":
            return None
        return None


class SelectorParserTests(unittest.TestCase):
    def test_is_xpath_selector(self):
        self.assertTrue(input_actions.is_xpath_selector("//button"))
        self.assertTrue(input_actions.is_xpath_selector("//input[@name='q']"))
        self.assertTrue(input_actions.is_xpath_selector("xpath=//button"))
        self.assertTrue(input_actions.is_xpath_selector("XPATH=//div/span"))
        self.assertTrue(input_actions.is_xpath_selector("xpath=.//input"))
        self.assertFalse(input_actions.is_xpath_selector("button.submit"))
        self.assertFalse(input_actions.is_xpath_selector("#target"))
        self.assertFalse(input_actions.is_xpath_selector("div > span"))
        self.assertFalse(input_actions.is_xpath_selector("custom-card >>> button"))

    def test_extract_xpath(self):
        self.assertEqual(input_actions.extract_xpath("//button"), "//button")
        self.assertEqual(input_actions.extract_xpath("xpath=//button[@id='btn']"), "//button[@id='btn']")
        self.assertEqual(input_actions.extract_xpath("XPATH=  .//div  "), ".//div")
        self.assertEqual(input_actions.extract_xpath("//input[@type='text']"), "//input[@type='text']")

    def test_split_deep_selector(self):
        self.assertEqual(input_actions.split_deep_selector("host >>> child"), ["host", "child"])
        self.assertEqual(input_actions.split_deep_selector("host>>>child"), ["host", "child"])
        self.assertEqual(input_actions.split_deep_selector("host >> child"), ["host", "child"])
        self.assertEqual(input_actions.split_deep_selector("a >>> b >>> c"), ["a", "b", "c"])
        self.assertEqual(input_actions.split_deep_selector("a >> b >> c"), ["a", "b", "c"])
        self.assertEqual(input_actions.split_deep_selector("a >>> b >> c"), ["a", "b", "c"])
        self.assertEqual(input_actions.split_deep_selector("div > span"), ["div > span"])
        self.assertEqual(input_actions.split_deep_selector("#single-id"), ["#single-id"])

    def test_split_deep_selector_respects_quotes(self):
        selector_with_quotes = 'button[data-val=">>>"] >>> span'
        self.assertEqual(
            input_actions.split_deep_selector(selector_with_quotes),
            ['button[data-val=">>>"]', 'span'],
        )
        selector_single_quotes = "button[data-val=' >> '] >> span"
        self.assertEqual(
            input_actions.split_deep_selector(selector_single_quotes),
            ["button[data-val=' >> ']", "span"],
        )
        selector_quoted_only = 'div[data-action=">>>"]'
        self.assertEqual(
            input_actions.split_deep_selector(selector_quoted_only),
            ['div[data-action=">>>"]'],
        )
        self.assertFalse(input_actions.is_deep_selector(selector_quoted_only))

    def test_is_deep_selector(self):
        self.assertTrue(input_actions.is_deep_selector("host >>> button"))
        self.assertTrue(input_actions.is_deep_selector("host >> button"))
        self.assertTrue(input_actions.is_deep_selector("app-root >>> user-profile >>> button.save"))
        self.assertFalse(input_actions.is_deep_selector("button.submit"))
        self.assertFalse(input_actions.is_deep_selector("div > span"))
        self.assertFalse(input_actions.is_deep_selector("//button"))

    def test_build_resolve_expression(self):
        expr = input_actions.build_resolve_expression(
            selector="host >>> button",
            frame="iframe#sub",
            parts=["host", "button"],
            is_xpath=False,
            xpath_expr="",
        )
        self.assertIn("iframe#sub", expr)
        self.assertIn('"host"', expr)
        self.assertIn('"button"', expr)
        self.assertIn("shadowRoot", expr)

        xpath_expr = input_actions.build_resolve_expression(
            selector="//button",
            frame=None,
            parts=None,
            is_xpath=True,
            xpath_expr="//button",
        )
        self.assertIn("evaluate", xpath_expr)
        self.assertIn('"//button"', xpath_expr)
        self.assertIn("true", xpath_expr)


class FindNodeUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_css_selector_uses_cdp_query(self):
        tab = FakeRecordingTab(backend_node_id=101)
        node_id = await input_actions.find_node(tab, "input#user")
        self.assertEqual(node_id, 101)

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.getDocument", methods)
        self.assertIn("DOM.querySelector", methods)
        self.assertIn("DOM.describeNode", methods)
        self.assertNotIn("Runtime.evaluate", methods)

    async def test_xpath_slash_slash_uses_runtime_evaluate(self):
        tab = FakeRecordingTab(backend_node_id=102)
        node_id = await input_actions.find_node(tab, "//button[@id='save']")
        self.assertEqual(node_id, 102)

        methods = [e[0] for e in tab.events]
        self.assertIn("Runtime.evaluate", methods)
        self.assertIn("DOM.describeNode", methods)
        self.assertIn("Runtime.releaseObject", methods)
        self.assertNotIn("DOM.querySelector", methods)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("//button[@id='save']", eval_params["expression"])
        self.assertIn("true", eval_params["expression"])

    async def test_xpath_with_prefix_uses_runtime_evaluate(self):
        tab = FakeRecordingTab(backend_node_id=103)
        node_id = await input_actions.find_node(tab, "xpath=//input[@name='q']")
        self.assertEqual(node_id, 103)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("//input[@name='q']", eval_params["expression"])

    async def test_deep_selector_triple_chevron(self):
        tab = FakeRecordingTab(backend_node_id=104)
        node_id = await input_actions.find_node(tab, "custom-card >>> button.action")
        self.assertEqual(node_id, 104)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("custom-card", eval_params["expression"])
        self.assertIn("button.action", eval_params["expression"])
        self.assertIn("shadowRoot", eval_params["expression"])

    async def test_deep_selector_double_chevron(self):
        tab = FakeRecordingTab(backend_node_id=105)
        node_id = await input_actions.find_node(tab, "custom-card >> button.action")
        self.assertEqual(node_id, 105)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("custom-card", eval_params["expression"])
        self.assertIn("button.action", eval_params["expression"])

    async def test_multi_level_deep_selector(self):
        tab = FakeRecordingTab(backend_node_id=106)
        node_id = await input_actions.find_node(tab, "app-root >>> user-profile >>> img.avatar")
        self.assertEqual(node_id, 106)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("app-root", eval_params["expression"])
        self.assertIn("user-profile", eval_params["expression"])
        self.assertIn("img.avatar", eval_params["expression"])

    async def test_same_origin_frame_css(self):
        tab = FakeRecordingTab(backend_node_id=107)
        node_id = await input_actions.find_node(tab, "input#password", frame="iframe#login-frame")
        self.assertEqual(node_id, 107)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("iframe#login-frame", eval_params["expression"])
        self.assertIn("input#password", eval_params["expression"])

    async def test_same_origin_frame_xpath(self):
        tab = FakeRecordingTab(backend_node_id=108)
        node_id = await input_actions.find_node(tab, "//button[@type='submit']", frame="iframe.content")
        self.assertEqual(node_id, 108)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("iframe.content", eval_params["expression"])
        self.assertIn("//button[@type='submit']", eval_params["expression"])

    async def test_same_origin_frame_deep_selector(self):
        tab = FakeRecordingTab(backend_node_id=109)
        node_id = await input_actions.find_node(tab, "comp-host >>> .inner-btn", frame="#embed-frame")
        self.assertEqual(node_id, 109)

        eval_params = [e[1] for e in tab.events if e[0] == "Runtime.evaluate"][0]
        self.assertIn("#embed-frame", eval_params["expression"])
        self.assertIn("comp-host", eval_params["expression"])
        self.assertIn(".inner-btn", eval_params["expression"])

    async def test_element_not_found_returns_none(self):
        tab = FakeRecordingTab(evaluate_object_id=None)
        node_id = await input_actions.find_node(tab, "missing >>> element", timeout=0.0)
        self.assertIsNone(node_id)

    async def test_element_found_on_poll_retry(self):
        tab = FakeRecordingTab(backend_node_id=110, poll_success_count=2)
        node_id = await input_actions.find_node(tab, "delayed >>> element", timeout=0.3)
        self.assertEqual(node_id, 110)
        self.assertGreaterEqual(tab.evaluate_call_count, 2)


class DOMHandlerTargetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_element_with_deep_selector(self):
        tab = FakeRecordingTab(backend_node_id=201)
        result = await DOMHandler.click_element(tab, "card-host >>> button#ok")
        self.assertTrue(result)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("card-host", eval_events[0][1]["expression"])
        self.assertIn("button#ok", eval_events[0][1]["expression"])

    async def test_click_element_with_frame(self):
        tab = FakeRecordingTab(backend_node_id=202)
        result = await DOMHandler.click_element(tab, "#submit", frame="iframe#checkout")
        self.assertTrue(result)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("iframe#checkout", eval_events[0][1]["expression"])
        self.assertIn("#submit", eval_events[0][1]["expression"])

    async def test_type_text_with_xpath(self):
        tab = FakeRecordingTab(backend_node_id=203)
        result = await DOMHandler.type_text(tab, "//input[@id='email']", "test@example.com")
        self.assertTrue(result)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("//input[@id='email']", eval_events[0][1]["expression"])

    async def test_type_text_with_frame(self):
        tab = FakeRecordingTab(backend_node_id=204)
        result = await DOMHandler.type_text(tab, "#username", "alice", frame="iframe#login")
        self.assertTrue(result)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("iframe#login", eval_events[0][1]["expression"])

    async def test_paste_text_with_deep_selector(self):
        tab = FakeRecordingTab(backend_node_id=205)
        result = await DOMHandler.paste_text(tab, "editor-box >>> textarea", "pasted text")
        self.assertTrue(result)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("editor-box", eval_events[0][1]["expression"])

    async def test_press_key_with_xpath(self):
        tab = FakeRecordingTab(backend_node_id=206)
        result = await DOMHandler.press_key(tab, "Enter", selector="//input[@name='search']")
        self.assertEqual(result["key"], "Enter")
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertEqual(len(eval_events), 1)
        self.assertIn("//input[@name='search']", eval_events[0][1]["expression"])

    async def test_wait_for_element_with_frame(self):
        tab = FakeRecordingTab(backend_node_id=207)
        found = await DOMHandler.wait_for_element(tab, "button.confirm", timeout=200, frame="iframe#modal")
        self.assertTrue(found)
        eval_events = [e for e in tab.events if e[0] == "Runtime.evaluate"]
        self.assertGreaterEqual(len(eval_events), 1)
        self.assertIn("iframe#modal", eval_events[0][1]["expression"])

    async def test_element_not_found_raises_with_frame_context(self):
        tab = FakeRecordingTab(evaluate_object_id=None)
        with self.assertRaises(Exception) as ctx:
            await DOMHandler.click_element(tab, "#missing", timeout=0, frame="iframe#auth")
        self.assertIn("Element not found: #missing (in frame iframe#auth)", str(ctx.exception))


SHADOW_DOM_FIXTURE_PAGE = """<!doctype html>
<html>
<body>
  <div id="host-element"></div>
  <script>
    const host = document.getElementById('host-element');
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `
      <input id="shadow-input" type="text" value="">
      <button id="shadow-button">Click Me</button>
    `;
    window.__clicked = false;
    shadow.getElementById('shadow-button').addEventListener('click', () => {
      window.__clicked = true;
    });
  </script>
</body>
</html>"""

IFRAME_FIXTURE_PAGE = """<!doctype html>
<html>
<body>
  <iframe id="test-iframe" name="test-iframe" srcdoc="
    <!doctype html>
    <html>
    <body>
      <input id='inner-input' type='text' value=''>
      <button id='inner-button'>Frame Button</button>
      <script>
        window.__frameClicked = false;
        document.getElementById('inner-button').addEventListener('click', () => {
          window.__frameClicked = true;
        });
      </script>
    </body>
    </html>
  "></iframe>
</body>
</html>"""

XPATH_FIXTURE_PAGE = """<!doctype html>
<html>
<body>
  <form id="login-form">
    <label for="user-field">Username</label>
    <input id="user-field" name="username" type="text" value="">
    <button id="submit-btn" type="button">Submit Form</button>
  </form>
  <script>
    window.__formSubmitted = false;
    document.getElementById('submit-btn').addEventListener('click', () => {
      window.__formSubmitted = true;
    });
  </script>
</body>
</html>"""


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class RealBrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions

        self.manager = BrowserManager()
        self.instance = await self.manager.spawn_browser(
            BrowserOptions(headless=True, user_agent=HEADLESS_USER_AGENT)
        )
        self.tab = await self.manager.get_tab(self.instance.instance_id)

    async def asyncTearDown(self):
        if hasattr(self, "instance") and self.instance:
            await self.manager.close_instance(self.instance.instance_id)

    async def test_real_browser_shadow_dom_targeting(self):
        await self.manager.navigate(
            self.instance.instance_id,
            "data:text/html," + quote(SHADOW_DOM_FIXTURE_PAGE),
        )
        await asyncio.sleep(0.2)

        # Type text through shadow DOM deep selector
        typed = await DOMHandler.type_text(
            self.tab,
            "#host-element >>> #shadow-input",
            "hello-shadow",
            clear_first=True,
            delay_ms=0,
        )
        self.assertTrue(typed)

        # Click button through shadow DOM deep selector
        clicked = await DOMHandler.click_element(
            self.tab,
            "#host-element >>> #shadow-button",
            timeout=5000,
        )
        self.assertTrue(clicked)

        # Verify values in browser context
        val = await self.tab.evaluate(
            "document.getElementById('host-element').shadowRoot.getElementById('shadow-input').value"
        )
        self.assertEqual(val, "hello-shadow")

        was_clicked = await self.tab.evaluate("window.__clicked")
        self.assertTrue(was_clicked)

    async def test_real_browser_same_origin_iframe_targeting(self):
        await self.manager.navigate(
            self.instance.instance_id,
            "data:text/html," + quote(IFRAME_FIXTURE_PAGE),
        )
        await asyncio.sleep(0.3)

        # Type text into input inside iframe
        typed = await DOMHandler.type_text(
            self.tab,
            "#inner-input",
            "hello-iframe",
            clear_first=True,
            delay_ms=0,
            frame="iframe#test-iframe",
        )
        self.assertTrue(typed)

        # Click button inside iframe
        clicked = await DOMHandler.click_element(
            self.tab,
            "#inner-button",
            timeout=5000,
            frame="iframe#test-iframe",
        )
        self.assertTrue(clicked)

        # Verify values inside iframe context
        val = await self.tab.evaluate(
            "document.getElementById('test-iframe').contentDocument.getElementById('inner-input').value"
        )
        self.assertEqual(val, "hello-iframe")

        was_clicked = await self.tab.evaluate(
            "document.getElementById('test-iframe').contentWindow.__frameClicked"
        )
        self.assertTrue(was_clicked)

    async def test_real_browser_xpath_targeting(self):
        await self.manager.navigate(
            self.instance.instance_id,
            "data:text/html," + quote(XPATH_FIXTURE_PAGE),
        )
        await asyncio.sleep(0.2)

        # Type text using xpath= prefix
        typed = await DOMHandler.type_text(
            self.tab,
            "xpath=//input[@name='username']",
            "xpath-user",
            clear_first=True,
            delay_ms=0,
        )
        self.assertTrue(typed)

        # Click button using // prefix
        clicked = await DOMHandler.click_element(
            self.tab,
            "//button[@id='submit-btn']",
            timeout=5000,
        )
        self.assertTrue(clicked)

        # Verify
        val = await self.tab.evaluate("document.getElementById('user-field').value")
        self.assertEqual(val, "xpath-user")

        was_submitted = await self.tab.evaluate("window.__formSubmitted")
        self.assertTrue(was_submitted)

    async def test_real_browser_existing_selector_unchanged(self):
        await self.manager.navigate(
            self.instance.instance_id,
            "data:text/html," + quote(XPATH_FIXTURE_PAGE),
        )
        await asyncio.sleep(0.2)

        typed = await DOMHandler.type_text(
            self.tab,
            "#user-field",
            "plain-css-user",
            clear_first=True,
            delay_ms=0,
        )
        self.assertTrue(typed)

        clicked = await DOMHandler.click_element(
            self.tab,
            "#submit-btn",
            timeout=5000,
        )
        self.assertTrue(clicked)

        val = await self.tab.evaluate("document.getElementById('user-field').value")
        self.assertEqual(val, "plain-css-user")


if __name__ == "__main__":
    unittest.main()
