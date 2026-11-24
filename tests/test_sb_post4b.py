"""Unit tests for SB-POST-4B (bounded minimized screenshots and iframe frame honoring)."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc
from nodriver import cdp

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import a11y_snapshot, dom_handler, fill_tool, input_actions, server
from stealth_browser_mcp.a11y_snapshot import get_accessibility_snapshot
from stealth_browser_mcp.browser_manager import BrowserManager, BrowserOptions
from stealth_browser_mcp.cdp_raw import raw_command
from stealth_browser_mcp.dom_handler import DOMHandler
from stealth_browser_mcp.server import ScreenshotTimeoutError, browser_manager, take_screenshot

take_screenshot_fn = getattr(take_screenshot, "fn", take_screenshot)


class Post4bScreenshotTests(unittest.IsolatedAsyncioTestCase):
    """Bug 4 tests: minimized window screenshot rejection and capture timeout bounding."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = self.temp_dir.name

    async def asyncTearDown(self) -> None:
        os.environ.pop("STEALTH_BROWSER_MCP_DATA_DIR", None)
        self.temp_dir.cleanup()

    async def test_minimized_instance_mode_rejects_immediately(self) -> None:
        """Stored window mode 'minimized' on instance rejects screenshot immediately."""
        fake_tab = MagicMock()
        fake_tab.save_screenshot = AsyncMock(
            side_effect=AssertionError("save_screenshot should not be called when window is minimized")
        )
        fake_tab.evaluate = AsyncMock(return_value=1.0)

        fake_inst = {"window": "minimized"}

        with patch.object(browser_manager, "get_tab", new=AsyncMock(return_value=fake_tab)), \
             patch.object(browser_manager, "get_instance", new=AsyncMock(return_value=fake_inst)):
            start_time = time.monotonic()
            with self.assertRaises(ToolError) as ctx:
                await take_screenshot_fn(instance_id="min_inst")
            elapsed = time.monotonic() - start_time

            self.assertIn("Cannot take a screenshot of a minimized window", str(ctx.exception))
            self.assertLess(elapsed, 2.0)
            fake_tab.save_screenshot.assert_not_called()

    async def test_live_window_state_minimized_rejects_immediately(self) -> None:
        """Live CDP windowState 'minimized' rejects screenshot immediately without waiting."""
        fake_tab = MagicMock()
        fake_tab.save_screenshot = AsyncMock(
            side_effect=AssertionError("save_screenshot should not be called when windowState is minimized")
        )
        fake_tab.evaluate = AsyncMock(return_value=1.0)

        # Tab send returns windowState minimized for Browser.getWindowForTarget
        async def fake_send(cmd: Any) -> Any:
            request = next(cmd) if hasattr(cmd, "__next__") else {}
            return {"bounds": {"windowState": "minimized", "left": 0, "top": 0, "width": 800, "height": 600}}

        fake_tab.send = fake_send
        fake_inst = {"window": "normal"}

        with patch.object(browser_manager, "get_tab", new=AsyncMock(return_value=fake_tab)), \
             patch.object(browser_manager, "get_instance", new=AsyncMock(return_value=fake_inst)):
            start_time = time.monotonic()
            with self.assertRaises(ToolError) as ctx:
                await take_screenshot_fn(instance_id="min_live_inst")
            elapsed = time.monotonic() - start_time

            self.assertIn("Cannot take a screenshot of a minimized window", str(ctx.exception))
            self.assertLess(elapsed, 2.0)
            fake_tab.save_screenshot.assert_not_called()

    async def test_hanging_save_screenshot_hits_timeout_bound(self) -> None:
        """Non-minimized instance hits timeout bound and raises ScreenshotTimeoutError."""
        fake_tab = MagicMock()

        async def hanging_save(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(60.0)

        fake_tab.save_screenshot = hanging_save
        fake_tab.evaluate = AsyncMock(return_value=1.0)

        async def fake_send(cmd: Any) -> Any:
            return {"bounds": {"windowState": "normal", "left": 0, "top": 0, "width": 800, "height": 600}}

        fake_tab.send = fake_send
        fake_inst = {"window": "normal"}

        # Patch SCREENSHOT_CAPTURE_TIMEOUT to 0.2s for fast test execution
        with patch.object(browser_manager, "get_tab", new=AsyncMock(return_value=fake_tab)), \
             patch.object(browser_manager, "get_instance", new=AsyncMock(return_value=fake_inst)), \
             patch.dict(server.__dict__, {"SCREENSHOT_CAPTURE_TIMEOUT": 0.2}):
            start_time = time.monotonic()
            with self.assertRaises(ScreenshotTimeoutError) as ctx:
                await take_screenshot_fn(instance_id="hanging_inst")
            elapsed = time.monotonic() - start_time

            self.assertIn("Screenshot capture timed out", str(ctx.exception))
            self.assertLess(elapsed, 2.0)
            self.assertGreaterEqual(elapsed, 0.15)


class Post4bTabAutoAttachTests(unittest.IsolatedAsyncioTestCase):
    """Bug 5 tests: Tab configuration sends Target.setAutoAttach."""

    async def test_configure_tab_sends_target_set_auto_attach(self) -> None:
        """_configure_tab sends Target.setAutoAttach with autoAttach=True and flatten=True."""
        sent_requests: List[Dict[str, Any]] = []

        class FakeTab:
            target = SimpleNamespace(target_id="tab-target-1")

            async def send(self, cmd: Any) -> Any:
                if hasattr(cmd, "__next__"):
                    req = next(cmd)
                    sent_requests.append(req)
                return None

        tab = FakeTab()
        options = BrowserOptions(window="headless")

        await browser_manager._configure_tab(
            tab=tab,
            instance_id="test_inst",
            options=options,
            init_scripts=[],
            configured_tabs=set(),
        )

        auto_attach_calls = [
            req for req in sent_requests if req.get("method") == "Target.setAutoAttach"
        ]
        self.assertTrue(len(auto_attach_calls) >= 1, "Expected Target.setAutoAttach to be called")
        params = auto_attach_calls[0].get("params", {})
        self.assertTrue(params.get("autoAttach"))
        self.assertTrue(params.get("flatten"))
        self.assertFalse(params.get("waitForDebuggerOnStart"))


class FakeIframeTargetTab:
    """Mock tab representing an auto-attached OOPIF session."""

    def __init__(self, target_id: str = "oopif-session-1", url: str = "https://iframe.example.com/inner") -> None:
        self.target_id = target_id
        self.url = url
        self.type_ = "iframe"
        self.target = SimpleNamespace(
            target_id=target_id,
            type_="iframe",
            url=url,
            opener_frame_id="frame-oopif-1",
            frame_id="frame-oopif-1",
        )
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.closed = False
        self.quads = [[10, 20, 30, 20, 30, 40, 10, 40]]

    async def send(self, command: Any) -> Any:
        req = next(command) if hasattr(command, "__next__") else {}
        method = req.get("method", "")
        params = req.get("params", {})
        self.events.append((method, params))

        if method == "DOM.getDocument":
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            return SimpleNamespace(backend_node_id=uc.cdp.dom.BackendNodeId(999), frame_id=None)
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("resolved-obj-999"))
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.scrollIntoViewIfNeeded":
            return None
        if method == "DOM.focus":
            return None
        if method.startswith("Input."):
            return None
        if method == "Runtime.callFunctionOn":
            return SimpleNamespace(value={"tag_name": "input", "element_kind": "text"}), None
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "reason" in expr and "verified" in expr:
                return (
                    SimpleNamespace(
                        type_="object",
                        value={"verified": True, "reason": "native_setter"},
                    ),
                    None,
                )
            return SimpleNamespace(type_="object", subtype="node", object_id=uc.cdp.runtime.RemoteObjectId("resolved-obj-999")), None
        return None

    async def evaluate(self, expr: str) -> Any:
        return True


class FakeParentTab:
    """Mock tab representing the top-level document."""

    def __init__(self, iframe_target: FakeIframeTargetTab) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.closed = False
        self.browser = SimpleNamespace(targets=[self, iframe_target])
        self.target = SimpleNamespace(target_id="parent-tab-1", type_="page", url="https://example.com/parent")
        self.iframe_target = iframe_target

    async def send(self, command: Any) -> Any:
        req = next(command) if hasattr(command, "__next__") else {}
        method = req.get("method", "")
        params = req.get("params", {})
        self.events.append((method, params))

        if method == "DOM.describeNode":
            obj_id = params.get("objectId")
            if obj_id == "iframe-node-obj":
                return SimpleNamespace(
                    backend_node_id=uc.cdp.dom.BackendNodeId(50),
                    frame_id=uc.cdp.page.FrameId("frame-oopif-1"),
                )
            return SimpleNamespace(
                backend_node_id=uc.cdp.dom.BackendNodeId(50),
                frame_id=None,
            )
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            # Only return iframe node when evaluating iframe resolver JS (RESOLVE_IFRAME_NODE_JS)
            if "function (frameRef)" in expr:
                return (
                    SimpleNamespace(
                        type_="object",
                        subtype="node",
                        object_id=uc.cdp.runtime.RemoteObjectId("iframe-node-obj"),
                    ),
                    None,
                )
            # Same-origin resolution fails in cross-origin iframe
            return (
                SimpleNamespace(type_="object", subtype="null", value=None, object_id=None),
                None,
            )
        if method == "Runtime.releaseObject":
            return None
        return None


class Post4bCrossOriginIframeActionRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Bug 5 tests: Actions route to auto-attached OOPIF session when frame is provided."""

    async def asyncSetUp(self) -> None:
        input_actions._frame_contexts.clear()
        input_actions._node_contexts.clear()
        input_actions._node_tabs.clear()

    async def test_type_text_routes_to_oopif_target(self) -> None:
        """DOMHandler.type_text routes input events to attached OOPIF target session."""
        iframe_target = FakeIframeTargetTab(target_id="oopif-target-1", url="https://iframe.example.com/inner")
        parent_tab = FakeParentTab(iframe_target)

        typed = await DOMHandler.type_text(
            parent_tab,
            selector="#input",
            text="hello",
            clear_first=False,
            delay_ms=0,
            frame="iframe#my-frame",
        )
        self.assertTrue(typed)

        # Check that Input.dispatchKeyEvent was delivered to iframe_target
        iframe_methods = [e[0] for e in iframe_target.events]
        self.assertIn("Input.dispatchKeyEvent", iframe_methods)

        # Check that Input.dispatchKeyEvent was NOT delivered to parent_tab
        parent_methods = [e[0] for e in parent_tab.events]
        self.assertNotIn("Input.dispatchKeyEvent", parent_methods)

    async def test_fill_routes_to_oopif_target(self) -> None:
        """fill_tool.fill routes interaction to attached OOPIF target session."""
        iframe_target = FakeIframeTargetTab(target_id="oopif-target-1", url="https://iframe.example.com/inner")
        parent_tab = FakeParentTab(iframe_target)

        result = await DOMHandler.fill(
            parent_tab,
            selector="#input",
            value="world",
            frame="iframe#my-frame",
            strategy="native_setter",
            verify=False,
        )
        self.assertTrue(result.get("success"))

        # Verify interaction occurred on iframe_target and not parent_tab
        iframe_methods = [e[0] for e in iframe_target.events]
        self.assertIn("Runtime.callFunctionOn", iframe_methods)

        parent_methods = [e[0] for e in parent_tab.events]
        self.assertNotIn("Runtime.callFunctionOn", parent_methods)


class Post4bA11ySnapshotFrameTests(unittest.IsolatedAsyncioTestCase):
    """Bug 5 tests: get_accessibility_snapshot honors frame argument."""

    async def asyncSetUp(self) -> None:
        input_actions._frame_contexts.clear()
        input_actions._node_contexts.clear()
        input_actions._node_tabs.clear()

    async def test_a11y_snapshot_passes_frame_id_when_frame_provided(self) -> None:
        """get_accessibility_snapshot passes frameId to Accessibility.getFullAXTree."""
        events: List[Tuple[str, Dict[str, Any]]] = []

        class FakeA11yTab:
            async def send(self, command: Any) -> Any:
                req = next(command) if hasattr(command, "__next__") else {}
                method = req.get("method", "")
                params = req.get("params", {})
                events.append((method, params))

                if method == "Accessibility.enable":
                    return None
                if method == "Accessibility.getFullAXTree":
                    return {
                        "nodes": [
                            {
                                "nodeId": "1",
                                "role": {"type": "role", "value": "RootWebArea"},
                                "name": {"type": "computedString", "value": "Frame Page"},
                                "childIds": ["2"],
                            },
                            {
                                "nodeId": "2",
                                "role": {"type": "role", "value": "button"},
                                "name": {"type": "computedString", "value": "Inside Frame"},
                                "backendDOMNodeId": 101,
                            },
                        ]
                    }
                return None

        tab = FakeA11yTab()
        result = await get_accessibility_snapshot(tab, frame="frame-oopif-99")

        self.assertIn("Inside Frame", result["text"])
        ax_calls = [e for e in events if e[0] == "Accessibility.getFullAXTree"]
        self.assertEqual(len(ax_calls), 1)
        self.assertEqual(ax_calls[0][1].get("frameId"), "frame-oopif-99")

    async def test_a11y_snapshot_without_frame_omits_frame_id(self) -> None:
        """get_accessibility_snapshot does not pass frameId when frame is None."""
        events: List[Tuple[str, Dict[str, Any]]] = []

        class FakeA11yTab:
            async def send(self, command: Any) -> Any:
                req = next(command) if hasattr(command, "__next__") else {}
                method = req.get("method", "")
                params = req.get("params", {})
                events.append((method, params))

                if method == "Accessibility.enable":
                    return None
                if method == "Accessibility.getFullAXTree":
                    return {
                        "nodes": [
                            {
                                "nodeId": "1",
                                "role": {"type": "role", "value": "RootWebArea"},
                                "name": {"type": "computedString", "value": "Root Page"},
                            }
                        ]
                    }
                return None

        tab = FakeA11yTab()
        result = await get_accessibility_snapshot(tab, frame=None)

        self.assertIn("Root Page", result["text"])
        ax_calls = [e for e in events if e[0] == "Accessibility.getFullAXTree"]
        self.assertEqual(len(ax_calls), 1)
        self.assertNotIn("frameId", ax_calls[0][1])

    async def test_a11y_snapshot_dom_walk_fallback_honors_frame(self) -> None:
        """_dom_walk_fallback passes frame argument to DOM_WALK_JS evaluation."""
        events: List[Tuple[str, Dict[str, Any]]] = []

        class FakeFallbackTab:
            async def send(self, command: Any) -> Any:
                req = next(command) if hasattr(command, "__next__") else {}
                method = req.get("method", "")
                params = req.get("params", {})
                events.append((method, params))

                if method == "Accessibility.enable":
                    return None
                if method == "Accessibility.getFullAXTree":
                    # Fail getFullAXTree to trigger fallback
                    raise RuntimeError("Accessibility.getFullAXTree unavailable")
                if method == "Runtime.evaluate":
                    return (
                        SimpleNamespace(
                            value={
                                "root": {
                                    "role": "RootWebArea",
                                    "name": "Fallback Iframe Page",
                                    "children": [],
                                }
                            }
                        ),
                        None,
                    )
                return None

        tab = FakeFallbackTab()
        result = await get_accessibility_snapshot(tab, frame="iframe#my-frame")

        self.assertIn("Fallback Iframe Page", result["text"])
        eval_calls = [e for e in events if e[0] == "Runtime.evaluate"]
        self.assertTrue(len(eval_calls) >= 1)
        # Check that the evaluation expression passed the frame parameter "iframe#my-frame"
        self.assertIn('"iframe#my-frame"', eval_calls[0][1].get("expression", ""))


if __name__ == "__main__":
    unittest.main()
