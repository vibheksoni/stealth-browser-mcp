"""Unit and gated browser tests for cross-origin iframe targeting (SB-17)."""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import input_actions
from stealth_browser_mcp.dom_handler import DOMHandler
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


class FakeCrossOriginTab:
    """Tab stand-in that records CDP commands and simulates cross-origin iframes."""

    def __init__(
        self,
        iframe_backend_node_id: int = 50,
        target_backend_node_id: int = 99,
        frame_id: str = "frame-cross-123",
        context_id: int = 101,
        fail_same_origin: bool = True,
        browser: Any = None,
    ) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.iframe_backend_node_id = iframe_backend_node_id
        self.target_backend_node_id = target_backend_node_id
        self.frame_id = frame_id
        self.context_id = context_id
        self.fail_same_origin = fail_same_origin
        self.browser = browser
        self.quads = [[100, 200, 150, 200, 150, 230, 100, 230]]
        self.closed = False

    async def send(self, command: Any) -> Any:
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.events.append((method, params))

        if method.startswith("Input."):
            return None
        if method == "Page.enable":
            return None
        if method == "Page.createIsolatedWorld":
            return uc.cdp.runtime.ExecutionContextId(self.context_id)
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("resolved-cross-obj"))
        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            if "return this.id || null" in fn:
                return SimpleNamespace(value="opt-test-id"), None
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
            obj_id = params.get("objectId")
            if obj_id == "iframe-remote-obj":
                return SimpleNamespace(
                    backend_node_id=uc.cdp.dom.BackendNodeId(self.iframe_backend_node_id),
                    frame_id=uc.cdp.page.FrameId(self.frame_id),
                )
            if obj_id == "option-remote-obj":
                return SimpleNamespace(
                    backend_node_id=uc.cdp.dom.BackendNodeId(202),
                    frame_id=None,
                )
            return SimpleNamespace(
                backend_node_id=uc.cdp.dom.BackendNodeId(self.target_backend_node_id),
                frame_id=None,
            )
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            ctx = params.get("contextId")

            # Check for verification expression in choose_option
            if "reason" in expr and "verified" in expr:
                return (
                    SimpleNamespace(
                        type_="object",
                        value={"verified": True, "reason": "option_marked_selected"},
                    ),
                    None,
                )

            # Check for find_option expression in choose_option
            if "roleOpts" in expr or "select__option" in expr:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("option-remote-obj"),
                    ),
                    None,
                )

            # Resolving iframe element via RESOLVE_IFRAME_NODE_JS
            if "querySelectorAll('iframe, frame')" in expr:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("iframe-remote-obj"),
                    ),
                    None,
                )

            # Evaluating within isolated world (cross-origin iframe)
            if ctx is not None and ctx == self.context_id:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("target-node-obj"),
                    ),
                    None,
                )

            # Same-origin resolution attempt
            if self.fail_same_origin:
                return (
                    SimpleNamespace(type_="object", subtype="null", value=None, object_id=None),
                    None,
                )
            else:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("same-origin-obj"),
                    ),
                    None,
                )
        if method == "Runtime.releaseObject":
            return None
        return None


class CrossOriginIframeUnitTests(unittest.IsolatedAsyncioTestCase):
    """Ungated unit tests for cross-origin iframe targeting using fake CDP tabs."""

    async def asyncSetUp(self) -> None:
        input_actions._frame_contexts.clear()
        input_actions._node_contexts.clear()
        input_actions._node_tabs.clear()

    async def test_cross_origin_find_node_via_isolated_world(self) -> None:
        tab = FakeCrossOriginTab(
            iframe_backend_node_id=50,
            target_backend_node_id=99,
            frame_id="frame-cross-abc",
            context_id=101,
            fail_same_origin=True,
        )
        node_id = await input_actions.find_node(tab, "#cross-input", frame="iframe#cross")
        self.assertEqual(node_id, 99)

        methods = [e[0] for e in tab.events]
        self.assertIn("Page.createIsolatedWorld", methods)
        self.assertIn("Runtime.evaluate", methods)
        self.assertIn("DOM.describeNode", methods)

        # Confirm Page.createIsolatedWorld received the right frameId
        cw_calls = [e[1] for e in tab.events if e[0] == "Page.createIsolatedWorld"]
        self.assertEqual(len(cw_calls), 1)
        self.assertEqual(cw_calls[0]["frameId"], "frame-cross-abc")
        self.assertEqual(cw_calls[0]["worldName"], "stealth_cross_origin")

        # Confirm target node was resolved within the isolated world contextId
        eval_calls = [e[1] for e in tab.events if e[0] == "Runtime.evaluate" and "contextId" in e[1]]
        self.assertTrue(len(eval_calls) >= 1)
        self.assertEqual(eval_calls[0]["contextId"], 101)

    async def test_cross_origin_find_node_with_xpath(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=123,
            frame_id="frame-xpath-456",
            context_id=102,
        )
        node_id = await input_actions.find_node(
            tab,
            "//button[@id='cross-button']",
            frame="iframe.cross-class",
        )
        self.assertEqual(node_id, 123)

        eval_calls = [e[1] for e in tab.events if e[0] == "Runtime.evaluate" and "contextId" in e[1]]
        self.assertTrue(len(eval_calls) >= 1)
        self.assertEqual(eval_calls[0]["contextId"], 102)
        self.assertIn("//button[@id='cross-button']", eval_calls[0]["expression"])

    async def test_cross_origin_find_node_with_deep_selector(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=124,
            frame_id="frame-deep-789",
            context_id=103,
        )
        node_id = await input_actions.find_node(
            tab,
            "custom-widget >>> button.save-btn",
            frame="#cross-iframe",
        )
        self.assertEqual(node_id, 124)

        eval_calls = [e[1] for e in tab.events if e[0] == "Runtime.evaluate" and "contextId" in e[1]]
        self.assertTrue(len(eval_calls) >= 1)
        self.assertEqual(eval_calls[0]["contextId"], 103)
        self.assertIn("custom-widget", eval_calls[0]["expression"])
        self.assertIn("button.save-btn", eval_calls[0]["expression"])

    async def test_cross_origin_click_element(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=99,
            frame_id="frame-click-1",
            context_id=104,
        )
        clicked = await DOMHandler.click_element(tab, "#cross-button", frame="#cross-iframe")
        self.assertTrue(clicked)

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.scrollIntoViewIfNeeded", methods)
        self.assertIn("DOM.getContentQuads", methods)
        self.assertIn("Input.dispatchMouseEvent", methods)

        mouse_events = [e[1] for e in tab.events if e[0] == "Input.dispatchMouseEvent"]
        types = [me["type"] for me in mouse_events]
        self.assertIn("mouseMoved", types)
        self.assertIn("mousePressed", types)
        self.assertIn("mouseReleased", types)

    async def test_cross_origin_type_text(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=99,
            frame_id="frame-type-1",
            context_id=105,
        )
        typed = await DOMHandler.type_text(
            tab,
            "#cross-input",
            "hello",
            clear_first=True,
            delay_ms=0,
            frame="#cross-iframe",
        )
        self.assertTrue(typed)

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.focus", methods)
        self.assertIn("Input.dispatchKeyEvent", methods)

    async def test_cross_origin_paste_text(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=99,
            frame_id="frame-paste-1",
            context_id=106,
        )
        pasted = await DOMHandler.paste_text(
            tab,
            "#cross-input",
            "pasted-content",
            clear_first=True,
            frame="#cross-iframe",
        )
        self.assertTrue(pasted)

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.focus", methods)
        self.assertIn("Input.insertText", methods)

        insert_calls = [e[1] for e in tab.events if e[0] == "Input.insertText"]
        self.assertTrue(any(ic.get("text") == "pasted-content" for ic in insert_calls))

    async def test_cross_origin_press_key(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=99,
            frame_id="frame-press-1",
            context_id=107,
        )
        res = await DOMHandler.press_key(
            tab,
            "Enter",
            selector="#cross-input",
            frame="#cross-iframe",
        )
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("key"), "Enter")

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.focus", methods)
        self.assertIn("Input.dispatchKeyEvent", methods)

    async def test_cross_origin_wait_for_element(self) -> None:
        tab = FakeCrossOriginTab(
            target_backend_node_id=99,
            frame_id="frame-wait-1",
            context_id=108,
        )
        found = await DOMHandler.wait_for_element(
            tab,
            "#cross-input",
            timeout=500,
            visible=True,
            frame="#cross-iframe",
        )
        self.assertTrue(found)

        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.resolveNode", methods)
        self.assertIn("Runtime.callFunctionOn", methods)

    async def test_cross_origin_element_not_found_returns_none(self) -> None:
        class FakeNotFoundTab(FakeCrossOriginTab):
            async def send(self, command: Any) -> Any:
                req = next(command)
                if req["method"] == "Runtime.evaluate" and req.get("params", {}).get("contextId"):
                    self.events.append((req["method"], req.get("params", {})))
                    return SimpleNamespace(type_="object", subtype="null", value=None, object_id=None), None
                return await super().send(command)

        tab = FakeNotFoundTab()
        node_id = await input_actions.find_node(tab, "#missing-elem", timeout=0.0, frame="#cross-iframe")
        self.assertIsNone(node_id)

    async def test_same_origin_does_not_fall_back(self) -> None:
        tab = FakeCrossOriginTab(fail_same_origin=False)
        node_id = await input_actions.find_node(tab, "#same-elem", frame="#same-frame")
        self.assertEqual(node_id, tab.target_backend_node_id)

        methods = [e[0] for e in tab.events]
        self.assertNotIn("Page.createIsolatedWorld", methods)

    async def test_cross_origin_resolution_via_oopif_target(self) -> None:
        target_tab = FakeCrossOriginTab(
            target_backend_node_id=888,
            frame_id="oopif-frame-1",
            context_id=201,
        )
        target_info = SimpleNamespace(
            opener_frame_id="oopif-frame-1",
            target_id="target-oopif-1",
            url="http://127.0.0.1:9001/cross_origin_inner.html",
        )
        target_tab.type_ = "iframe"
        target_tab.target = target_info

        fake_browser = SimpleNamespace(targets=[target_tab])
        main_tab = FakeCrossOriginTab(
            iframe_backend_node_id=60,
            frame_id="oopif-frame-1",
            browser=fake_browser,
        )

        ctx = await input_actions.resolve_frame_context(main_tab, "#oopif-iframe")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.target_tab, target_tab)
        self.assertEqual(ctx.frame_id, "oopif-frame-1")

    async def test_cross_origin_choose_option(self) -> None:
        tab = FakeCrossOriginTab(
            iframe_backend_node_id=50,
            target_backend_node_id=99,
            frame_id="frame-choose-1",
            context_id=109,
        )
        result = await DOMHandler.choose_option(
            tab,
            "#combobox",
            "Option A",
            filter_text="Opt",
            timeout=1000,
            frame="#cross-iframe",
        )
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("selected"), "Option A")


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class RealBrowserCrossOriginIframeTests(unittest.IsolatedAsyncioTestCase):
    """Real browser tests with two-port fixture servers for cross-origin iframe interaction."""

    async def asyncSetUp(self) -> None:
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions

        self.server_a = FixtureServer(host="127.0.0.1", port=0).start()
        self.server_b = FixtureServer(host="127.0.0.1", port=0).start()

        self.manager = BrowserManager()
        self.instance = await self.manager.spawn_browser(
            BrowserOptions(headless=True, user_agent=HEADLESS_USER_AGENT)
        )
        self.tab = await self.manager.get_tab(self.instance.instance_id)

    async def asyncTearDown(self) -> None:
        if hasattr(self, "instance") and self.instance:
            await self.manager.close_instance(self.instance.instance_id)
        if hasattr(self, "server_a") and self.server_a:
            self.server_a.stop()
        if hasattr(self, "server_b") and self.server_b:
            self.server_b.stop()

    async def test_two_port_cross_origin_iframe_interaction(self) -> None:
        inner_url = self.server_b.url_for("cross_origin_inner.html")
        outer_url = self.server_a.url_for(f"cross_origin_outer.html?frame_src={quote(inner_url)}")

        await self.manager.navigate(self.instance.instance_id, outer_url)
        await asyncio.sleep(0.5)

        # 1. Type text into input inside cross-origin iframe
        typed = await DOMHandler.type_text(
            self.tab,
            "#cross-input",
            "cross-typed-text",
            clear_first=True,
            delay_ms=0,
            frame="#cross-iframe",
        )
        self.assertTrue(typed)

        # 2. Click button inside cross-origin iframe
        clicked = await DOMHandler.click_element(
            self.tab,
            "#cross-button",
            timeout=5000,
            frame="#cross-iframe",
        )
        self.assertTrue(clicked)

        # 3. Wait for status inside cross-origin iframe to update
        waited = await DOMHandler.wait_for_element(
            self.tab,
            "#cross-status",
            timeout=5000,
            text_content="button clicked",
            frame="#cross-iframe",
        )
        self.assertTrue(waited)

        # 4. Paste text into input inside cross-origin iframe
        pasted = await DOMHandler.paste_text(
            self.tab,
            "#cross-input",
            "cross-pasted-text",
            clear_first=True,
            frame="#cross-iframe",
        )
        self.assertTrue(pasted)

        # 5. Press Enter in cross-origin iframe input
        pressed = await DOMHandler.press_key(
            self.tab,
            "Enter",
            selector="#cross-input",
            frame="#cross-iframe",
        )
        self.assertTrue(pressed.get("success"))

    async def test_two_port_cross_origin_iframe_xpath(self) -> None:
        inner_url = self.server_b.url_for("cross_origin_inner.html")
        outer_url = self.server_a.url_for(f"cross_origin_outer.html?frame_src={quote(inner_url)}")

        await self.manager.navigate(self.instance.instance_id, outer_url)
        await asyncio.sleep(0.5)

        # Target input and button via XPath inside the cross-origin iframe
        typed = await DOMHandler.type_text(
            self.tab,
            "xpath=//input[@id='cross-input']",
            "xpath-cross-typed",
            clear_first=True,
            delay_ms=0,
            frame="#cross-iframe",
        )
        self.assertTrue(typed)

        clicked = await DOMHandler.click_element(
            self.tab,
            "//button[@id='cross-button']",
            timeout=5000,
            frame="#cross-iframe",
        )
        self.assertTrue(clicked)

        waited = await DOMHandler.wait_for_element(
            self.tab,
            "//span[@id='cross-status']",
            timeout=5000,
            text_content="xpath-cross-typed",
            frame="#cross-iframe",
        )
        self.assertTrue(waited)


if __name__ == "__main__":
    unittest.main()
