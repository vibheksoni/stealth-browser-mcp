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

from stealth_browser_mcp.fill_native_setter import (
    FILL_NATIVE_SETTER_JS,
    NATIVE_SETTER_JS,
    fill_native_setter,
)

try:
    import window_matrix
except ImportError:
    from tests import window_matrix


class RecordingTab:
    """Tab that records CDP commands and answers DOM and Runtime calls."""

    def __init__(self, quads=None, hit=True, backend_node_id=42, return_value="test-result-value"):
        self.events = []
        self.call_function_on_records = []
        self.quads = quads if quads is not None else [[10, 20, 30, 20, 30, 40, 10, 40]]
        self.hit = hit
        self.backend_node_id = backend_node_id
        self.return_value = return_value

    async def send(self, command):
        request = next(command)
        method, params = request["method"], request.get("params", {})
        if method.startswith("Input."):
            self.events.append(
                (method, params.get("type"), params.get("key") or params.get("text"), params.get("modifiers", 0))
            )
            return None
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            self.events.append(("DOM.resolveNode", params.get("backendNodeId"), None, 0))
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj-123"))
        if method == "Runtime.callFunctionOn":
            args = params.get("arguments", [])
            self.events.append(("callFunctionOn", params["functionDeclaration"], args, 0))
            self.call_function_on_records.append({
                "method": method,
                "functionDeclaration": params["functionDeclaration"],
                "arguments": args,
                "objectId": params.get("objectId"),
                "returnByValue": params.get("returnByValue"),
                "awaitPromise": params.get("awaitPromise"),
            })
            val = self.return_value if self.return_value is not None else self.hit
            return SimpleNamespace(value=val), None
        if method == "DOM.getDocument":
            self.events.append(("DOM.getDocument", params.get("depth"), None, 0))
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            sel = params.get("selector")
            self.events.append(("DOM.querySelector", sel, None, 0))
            if sel == "missing" or sel == "non-existent" or sel == "42":
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


class TestFillNativeSetterUnit(unittest.TestCase):
    def test_setter_js_structure(self):
        self.assertIn("Object.getOwnPropertyDescriptor", NATIVE_SETTER_JS)
        self.assertIn("'value'", NATIVE_SETTER_JS)
        self.assertIn(".set", NATIVE_SETTER_JS)
        self.assertTrue(
            "Object.getOwnPropertyDescriptor(proto, 'value').set" in NATIVE_SETTER_JS or
            "Object.getOwnPropertyDescriptor(proto,'value').set" in NATIVE_SETTER_JS
        )
        self.assertIn("'input'", NATIVE_SETTER_JS)
        self.assertIn("'change'", NATIVE_SETTER_JS)
        self.assertIn("bubbles: true", NATIVE_SETTER_JS)
        self.assertIn("HTMLInputElement", NATIVE_SETTER_JS)
        self.assertIn("HTMLTextAreaElement", NATIVE_SETTER_JS)
        self.assertIn("return this.value", NATIVE_SETTER_JS)
        self.assertEqual(FILL_NATIVE_SETTER_JS, NATIVE_SETTER_JS)

    def test_fill_native_setter_backend_node_id(self):
        tab = RecordingTab(backend_node_id=42, return_value="hello-react-val")
        result = asyncio.run(fill_native_setter(tab, 42, "hello-react-val"))

        self.assertEqual(result, "hello-react-val")
        self.assertEqual(len(tab.call_function_on_records), 1)

        record = tab.call_function_on_records[0]
        fn = record["functionDeclaration"]
        args = record["arguments"]

        # Prototype-descriptor call
        self.assertIn("Object.getOwnPropertyDescriptor", fn)
        self.assertIn("'value'", fn)
        self.assertIn(".set", fn)
        self.assertTrue(
            "Object.getOwnPropertyDescriptor(proto, 'value').set" in fn or
            "Object.getOwnPropertyDescriptor(proto,'value').set" in fn
        )

        # Both input and change events with bubbles: true
        self.assertIn("'input'", fn)
        self.assertIn("'change'", fn)
        self.assertIn("bubbles: true", fn)

        # Args pass the text as an argument
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0]["value"], "hello-react-val")

        # Never string-interpolated into JS function
        self.assertNotIn("hello-react-val", fn)

    def test_fill_native_setter_selector(self):
        tab = RecordingTab(backend_node_id=88, return_value="submitted-text")
        result = asyncio.run(fill_native_setter(tab, "input#user-name", "submitted-text"))

        self.assertEqual(result, "submitted-text")
        methods = [e[0] for e in tab.events]
        self.assertIn("DOM.getDocument", methods)
        self.assertIn("DOM.querySelector", methods)
        self.assertIn("DOM.describeNode", methods)
        self.assertIn("callFunctionOn", methods)

        record = tab.call_function_on_records[0]
        self.assertEqual(record["arguments"][0]["value"], "submitted-text")
        self.assertNotIn("submitted-text", record["functionDeclaration"])

    def test_fill_native_setter_special_characters(self):
        special_text = 'quotes "double" \'single\' \\backslash\\ \n newline \t tab <script>alert("xss")</script>'
        tab = RecordingTab(backend_node_id=42, return_value=special_text)
        result = asyncio.run(fill_native_setter(tab, 42, special_text))

        self.assertEqual(result, special_text)
        record = tab.call_function_on_records[0]
        self.assertEqual(record["arguments"][0]["value"], special_text)
        self.assertNotIn(special_text, record["functionDeclaration"])
        self.assertNotIn('<script>', record["functionDeclaration"])

    def test_fill_native_setter_empty_string(self):
        tab = RecordingTab(backend_node_id=42, return_value="")
        result = asyncio.run(fill_native_setter(tab, 42, ""))

        self.assertEqual(result, "")
        record = tab.call_function_on_records[0]
        self.assertEqual(record["arguments"][0]["value"], "")

    def test_fill_native_setter_missing_selector_raises_value_error(self):
        tab = RecordingTab(backend_node_id=42)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(fill_native_setter(tab, "missing", "some-text"))
        self.assertIn("Element not found for selector: missing", str(ctx.exception))

    def test_fill_native_setter_missing_target_raises_value_error(self):
        tab = RecordingTab(backend_node_id=42)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(fill_native_setter(tab, None, "some-text"))
        self.assertIn("must be provided", str(ctx.exception))

    def test_fill_native_setter_invalid_target_type_raises_type_error(self):
        tab = RecordingTab(backend_node_id=42)
        with self.assertRaises(TypeError) as ctx:
            asyncio.run(fill_native_setter(tab, ["invalid", "list"], "some-text"))
        self.assertIn("backend_node_id_or_selector must be int or str", str(ctx.exception))

    def test_fill_native_setter_keyword_aliases(self):
        tab = RecordingTab(backend_node_id=42, return_value="alias-val")
        result = asyncio.run(fill_native_setter(tab, selector="input#alias", text="alias-val"))
        self.assertEqual(result, "alias-val")
        self.assertEqual(tab.call_function_on_records[0]["arguments"][0]["value"], "alias-val")

        tab2 = RecordingTab(backend_node_id=77, return_value="node-val")
        result2 = asyncio.run(fill_native_setter(tab2, backend_node_id=77, value="node-val"))
        self.assertEqual(result2, "node-val")
        self.assertEqual(tab2.call_function_on_records[0]["arguments"][0]["value"], "node-val")

    def test_fill_native_setter_numeric_string_fallback(self):
        tab = RecordingTab(backend_node_id=42, return_value="num-fallback")
        result = asyncio.run(fill_native_setter(tab, "42", "num-fallback"))
        self.assertEqual(result, "num-fallback")
        self.assertEqual(tab.call_function_on_records[0]["arguments"][0]["value"], "num-fallback")


@unittest.skipUnless(
    os.getenv("STEALTH_BROWSER_TESTS") == "1",
    "Real browser tests require STEALTH_BROWSER_TESTS=1",
)
class TestFillNativeSetterRealBrowser(unittest.TestCase):
    def test_native_setter_on_real_browser(self):
        async def run():
            window_matrix.skip_if_no_display(self)
            browser = await uc.start(headless=window_matrix.HEADLESS)
            try:
                html = (
                    "data:text/html,"
                    "<!DOCTYPE html><html><body>"
                    "<input id='target' type='text' value='initial'>"
                    "<script>"
                    "const inp = document.getElementById('target');"
                    "window.__events = [];"
                    "inp.addEventListener('input', () => window.__events.push('input'));"
                    "inp.addEventListener('change', () => window.__events.push('change'));"
                    "</script>"
                    "</body></html>"
                )
                tab = await browser.get(html)
                result = await fill_native_setter(tab, "#target", "react-verified-val")
                self.assertEqual(result, "react-verified-val")

                async def _check_value():
                    v = await tab.evaluate("document.getElementById('target').value")
                    return v == "react-verified-val"

                await window_matrix.wait_until_async(_check_value, timeout=5.0)

                async def _check_events():
                    events_raw = await tab.evaluate("JSON.stringify(window.__events)")
                    if isinstance(events_raw, dict) and "value" in events_raw:
                        events_raw = events_raw["value"]
                    events = (
                        json.loads(events_raw)
                        if isinstance(events_raw, str)
                        else [item["value"] if isinstance(item, dict) and "value" in item else item for item in events_raw]
                    )
                    if "input" in events and "change" in events:
                        return events
                    return None

                events = await window_matrix.wait_until_async(_check_events, timeout=5.0)
                self.assertIn("input", events)
                self.assertIn("change", events)
            finally:
                browser.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
