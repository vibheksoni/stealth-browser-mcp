"""Unit tests for synthetic paste strategy."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import input_actions
from stealth_browser_mcp.fill_synthetic_paste import (
    SYNTHETIC_PASTE_JS,
    _extract_default_prevented,
    _resolve_backend_node_id,
    fill_synthetic_paste,
)

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}


class RecordingTab:
    """Tab that records DOM and Runtime calls, matching test_input_actions.py pattern."""

    def __init__(self, hit=True, backend_node_id=42, call_result=None):
        self.events = []
        self.calls = []
        self.hit = hit
        self.backend_node_id = backend_node_id
        self.call_result = call_result

    async def send(self, command):
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.calls.append((method, params))

        if method.startswith("Input."):
            self.events.append(
                (method, params.get("type"), params.get("key") or params.get("text"), params.get("modifiers", 0))
            )
            return None
        if method == "DOM.focus":
            self.events.append(("DOM.focus", params.get("backendNodeId"), None, 0))
            return None
        if method == "DOM.getContentQuads":
            return [[10, 20, 30, 20, 30, 40, 10, 40]]
        if method == "DOM.resolveNode":
            self.events.append(("DOM.resolveNode", params.get("backendNodeId"), None, 0))
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj-42"))
        if method == "Runtime.callFunctionOn":
            self.events.append(("callFunctionOn", params["functionDeclaration"], None, 0))
            val = self.call_result if self.call_result is not None else self.hit
            return SimpleNamespace(value=val), None
        if method == "DOM.getDocument":
            self.events.append(("DOM.getDocument", params.get("depth"), None, 0))
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            self.events.append(("DOM.querySelector", params.get("selector"), None, 0))
            if params.get("selector") == ".missing":
                return None
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


class SyntheticPastePayloadTests(unittest.TestCase):
    """Assert payload JavaScript construction and dispatch semantics."""

    def test_payload_js_constructs(self):
        """Payload JS contains DataTransfer, ClipboardEvent, and proper properties."""
        js = SYNTHETIC_PASTE_JS
        self.assertIn("new DataTransfer()", js)
        self.assertIn("dt.setData('text/plain'", js)
        self.assertIn("dt.setData('text/html'", js)
        self.assertIn("new ClipboardEvent('paste'", js)
        self.assertIn("clipboardData: dt", js)
        self.assertIn("bubbles: true", js)
        self.assertIn("cancelable: true", js)
        self.assertIn("defaultPrevented", js)
        self.assertIn("dispatchEvent", js)
        self.assertIn("this.focus", js)

    def test_payload_js_passed_to_runtime_call(self):
        """The synthetic paste script is passed unchanged to Runtime.callFunctionOn."""
        tab = RecordingTab(hit=True)
        result = asyncio.run(fill_synthetic_paste(tab, 42, "hello text"))
        self.assertEqual(result, {"default_prevented": True})

        call_on_node_calls = [c for c in tab.calls if c[0] == "Runtime.callFunctionOn"]
        self.assertEqual(len(call_on_node_calls), 1)
        params = call_on_node_calls[0][1]
        self.assertEqual(params["functionDeclaration"], SYNTHETIC_PASTE_JS)
        self.assertEqual(params["objectId"], "obj-42")
        self.assertTrue(params["returnByValue"])
        self.assertTrue(params["awaitPromise"])


class SyntheticPasteArgumentPassingTests(unittest.TestCase):
    """Assert arguments are passed via CDP callFunctionOn and never interpolated."""

    def test_plain_text_argument_passing(self):
        """Plain text is passed as first argument and not interpolated into JS."""
        tab = RecordingTab(hit=True)
        text = "Hello, world! Some plain text."
        asyncio.run(fill_synthetic_paste(tab, 42, text))

        call = [c for c in tab.calls if c[0] == "Runtime.callFunctionOn"][0]
        params = call[1]
        args = params["arguments"]

        self.assertEqual(len(args), 2)
        self.assertEqual(args[0]["value"], text)
        self.assertNotIn("value", args[1])
        self.assertNotIn(text, params["functionDeclaration"])

    def test_plain_text_and_html_argument_passing(self):
        """Plain text and HTML are both passed as arguments and not interpolated."""
        tab = RecordingTab(hit=True)
        text = "Plain version"
        html = "<b>Rich version</b>"
        asyncio.run(fill_synthetic_paste(tab, 42, text, html=html))

        call = [c for c in tab.calls if c[0] == "Runtime.callFunctionOn"][0]
        params = call[1]
        args = params["arguments"]

        self.assertEqual(len(args), 2)
        self.assertEqual(args[0]["value"], text)
        self.assertEqual(args[1]["value"], html)
        self.assertNotIn(text, params["functionDeclaration"])
        self.assertNotIn(html, params["functionDeclaration"])

    def test_special_characters_and_newlines(self):
        """Quotes, newlines, tabs, and backslashes pass safely without interpolation."""
        tab = RecordingTab(hit=True)
        text = "Line 1\nLine 2\t\"double\" and 'single' \\ slash"
        html = "<p class=\"alert\">Line 1<br>Line 2 &amp; 'test'</p>"
        asyncio.run(fill_synthetic_paste(tab, 42, text, html=html))

        call = [c for c in tab.calls if c[0] == "Runtime.callFunctionOn"][0]
        params = call[1]
        args = params["arguments"]

        self.assertEqual(args[0]["value"], text)
        self.assertEqual(args[1]["value"], html)
        self.assertNotIn(text, params["functionDeclaration"])
        self.assertNotIn(html, params["functionDeclaration"])

    def test_empty_strings(self):
        """Empty text and html are handled cleanly."""
        tab = RecordingTab(hit=True)
        asyncio.run(fill_synthetic_paste(tab, 42, "", html=""))

        call = [c for c in tab.calls if c[0] == "Runtime.callFunctionOn"][0]
        params = call[1]
        args = params["arguments"]

        self.assertEqual(args[0]["value"], "")
        self.assertEqual(args[1]["value"], "")


class SyntheticPasteReturnValueMappingTests(unittest.TestCase):
    """Assert mapping of defaultPrevented true/false from JavaScript to Python."""

    def test_prevented_dict_with_both_keys(self):
        """Dictionary with defaultPrevented=True maps to default_prevented=True."""
        tab = RecordingTab(call_result={"defaultPrevented": True, "default_prevented": True})
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": True})

    def test_not_prevented_dict_with_both_keys(self):
        """Dictionary with defaultPrevented=False maps to default_prevented=False."""
        tab = RecordingTab(call_result={"defaultPrevented": False, "default_prevented": False})
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": False})

    def test_prevented_camel_case_only(self):
        """Dictionary with only defaultPrevented=True maps correctly."""
        tab = RecordingTab(call_result={"defaultPrevented": True})
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": True})

    def test_prevented_snake_case_only(self):
        """Dictionary with only default_prevented=True maps correctly."""
        tab = RecordingTab(call_result={"default_prevented": True})
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": True})

    def test_not_prevented_camel_case_only(self):
        """Dictionary with only defaultPrevented=False maps correctly."""
        tab = RecordingTab(call_result={"defaultPrevented": False})
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": False})

    def test_boolean_hit_true(self):
        """Direct boolean True return maps to default_prevented=True."""
        tab = RecordingTab(hit=True)
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": True})

    def test_boolean_hit_false(self):
        """Direct boolean False return maps to default_prevented=False."""
        tab = RecordingTab(hit=False)
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": False})

    def test_extract_default_prevented_helper(self):
        """Direct unit testing of _extract_default_prevented edge cases."""
        self.assertTrue(_extract_default_prevented({"default_prevented": True}))
        self.assertFalse(_extract_default_prevented({"default_prevented": False}))
        self.assertTrue(_extract_default_prevented({"defaultPrevented": True}))
        self.assertFalse(_extract_default_prevented({"defaultPrevented": False}))
        self.assertTrue(_extract_default_prevented(True))
        self.assertFalse(_extract_default_prevented(False))
        self.assertFalse(_extract_default_prevented(None))
        self.assertFalse(_extract_default_prevented({}))
        self.assertFalse(_extract_default_prevented("True"))
        self.assertFalse(_extract_default_prevented(1))


class SyntheticPasteTargetResolutionTests(unittest.TestCase):
    """Assert element target resolution (int, element object, CSS selector)."""

    def test_target_integer_backend_node_id(self):
        """Target passed as integer backend node id is used directly."""
        tab = RecordingTab(backend_node_id=42)
        result = asyncio.run(fill_synthetic_paste(tab, 42, "text"))
        self.assertEqual(result, {"default_prevented": True})

        focus_calls = [c for c in tab.calls if c[0] == "DOM.focus"]
        self.assertEqual(len(focus_calls), 1)
        self.assertEqual(focus_calls[0][1]["backendNodeId"], 42)

    def test_target_element_object(self):
        """Target passed as an element-like object with backend_node_id."""
        tab = RecordingTab(backend_node_id=42)
        elem = SimpleNamespace(backend_node_id=88)
        result = asyncio.run(fill_synthetic_paste(tab, elem, "text"))
        self.assertEqual(result, {"default_prevented": True})

        focus_calls = [c for c in tab.calls if c[0] == "DOM.focus"]
        self.assertEqual(len(focus_calls), 1)
        self.assertEqual(focus_calls[0][1]["backendNodeId"], 88)

    def test_target_css_selector_resolved(self):
        """Target passed as CSS selector string is resolved via find_node."""
        tab = RecordingTab(backend_node_id=99)
        result = asyncio.run(fill_synthetic_paste(tab, "#editor", "text"))
        self.assertEqual(result, {"default_prevented": True})

        focus_calls = [c for c in tab.calls if c[0] == "DOM.focus"]
        self.assertEqual(len(focus_calls), 1)
        self.assertEqual(focus_calls[0][1]["backendNodeId"], 99)

    def test_target_selector_not_found_raises(self):
        """Selector that cannot be found raises RuntimeError."""
        tab = RecordingTab()
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(fill_synthetic_paste(tab, ".missing", "text"))
        self.assertIn("Element not found: .missing", str(ctx.exception))

    def test_target_unsupported_type_raises(self):
        """Unsupported target type raises TypeError."""
        tab = RecordingTab()
        with self.assertRaises(TypeError) as ctx:
            asyncio.run(fill_synthetic_paste(tab, None, "text"))
        self.assertIn("Unsupported target type", str(ctx.exception))


@unittest.skipUnless(RUN_BROWSER_TESTS, "Browser tests disabled (set STEALTH_BROWSER_TESTS=1)")
class RealBrowserSyntheticPasteTests(unittest.TestCase):
    """Optional real browser tests for synthetic paste, run only when gated."""

    def test_real_browser_paste_event_handled(self):
        async def run_test():
            user_data_dir = tempfile.mkdtemp(prefix="stealth_paste_real_")
            browser = None
            try:
                browser = await uc.start(
                    headless=True,
                    user_data_dir=user_data_dir,
                    browser_args=["--no-sandbox", "--disable-gpu"],
                )
                tab = await browser.get("about:blank")
                await tab.evaluate(
                    """(() => {
                        const input = document.createElement('input');
                        input.id = 'paste-target';
                        input.addEventListener('paste', (e) => {
                            e.preventDefault();
                        });
                        document.body.appendChild(input);
                    })()"""
                )
                result = await fill_synthetic_paste(tab, "#paste-target", "hello real world")
                self.assertTrue(result["default_prevented"])
            finally:
                if browser:
                    try:
                        browser.stop()
                    except Exception:
                        pass
                shutil.rmtree(user_data_dir, ignore_errors=True)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
