"""Tests for accessibility snapshot and ref= targeting."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import a11y_snapshot, dom_handler, input_actions, server
from stealth_browser_mcp.a11y_snapshot import (
    AccessibilitySnapshotManager,
    a11y_manager,
    build_a11y_tree,
    extract_ref_id,
    format_text_tree,
    get_accessibility_snapshot,
    is_interactive_node,
    is_ref_selector,
)
from tests.browser_harness.server import FixtureServer

try:
    import window_matrix
except ImportError:
    from tests import window_matrix

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

INPUTS_HTML_PATH = ROOT / "tests" / "browser_harness" / "pages" / "inputs.html"
INPUTS_HTML = INPUTS_HTML_PATH.read_text(encoding="utf-8") if INPUTS_HTML_PATH.exists() else ""


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    if not dir_path or not os.path.exists(dir_path):
        return
    for _ in range(10):
        try:
            shutil.rmtree(dir_path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


class RecordingTabForA11y:
    """Fake tab recording CDP calls and simulating accessibility and DOM responses."""

    def __init__(
        self,
        ax_nodes: Optional[List[Any]] = None,
        ax_error: bool = False,
        backend_node_alive: bool = True,
        backend_node_id: int = 42,
    ):
        self.events: List[Tuple[str, Any]] = []
        self.ax_nodes = ax_nodes
        self.ax_error = ax_error
        self.backend_node_alive = backend_node_alive
        self.backend_node_id = backend_node_id
        self.quads = [[10, 20, 30, 20, 30, 40, 10, 40]]
        self.hit = True

    async def send(self, command):
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.events.append((method, params))

        if method == "Accessibility.enable":
            return None

        if method == "Accessibility.getFullAXTree":
            if self.ax_error:
                raise RuntimeError("Accessibility.getFullAXTree failed")
            return self.ax_nodes or []

        if method == "DOM.describeNode":
            if not self.backend_node_alive:
                raise RuntimeError("Could not find node with given id")
            b_id = params.get("backendNodeId", self.backend_node_id)
            return SimpleNamespace(backend_node_id=uc.cdp.dom.BackendNodeId(b_id))

        if method == "DOM.getDocument":
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))

        if method == "DOM.querySelector":
            return uc.cdp.dom.NodeId(10)

        if method == "DOM.getContentQuads":
            return self.quads

        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj-resolved"))

        if method == "DOM.scrollIntoViewIfNeeded":
            return None

        if method == "DOM.focus":
            return None

        if method == "Runtime.callFunctionOn":
            return SimpleNamespace(value=self.hit), None

        if method.startswith("Input."):
            return None

        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "DOM_WALK_JS" in expr or "getAccessibleName" in expr or "counter" in expr:
                return (
                    SimpleNamespace(
                        value={
                            "root": {
                                "role": "RootWebArea",
                                "name": "Fallback Page",
                                "children": [
                                    {
                                        "role": "heading",
                                        "name": "Header",
                                        "level": 1,
                                        "children": [],
                                    },
                                    {
                                        "role": "button",
                                        "name": "Click Me",
                                        "interactive": True,
                                        "marker": "a11y-m-1",
                                        "children": [],
                                    },
                                ],
                            }
                        }
                    ),
                    None,
                )
            return SimpleNamespace(value=True), None

        return None


def make_mock_ax_node(
    node_id: str,
    role: str,
    name: str = "",
    value: str = "",
    parent_id: Optional[str] = None,
    child_ids: Optional[List[str]] = None,
    backend_dom_node_id: Optional[int] = None,
    properties: Optional[List[Dict[str, Any]]] = None,
    ignored: bool = False,
) -> SimpleNamespace:
    """Helper to create a fake AXNode object."""
    props = []
    if properties:
        for p in properties:
            props.append(
                SimpleNamespace(
                    name=SimpleNamespace(value=p["name"]),
                    value=SimpleNamespace(value=p.get("value")),
                )
            )

    return SimpleNamespace(
        node_id=node_id,
        ignored=ignored,
        role=SimpleNamespace(value=role) if role else None,
        name=SimpleNamespace(value=name) if name else None,
        value=SimpleNamespace(value=value) if value else None,
        description=None,
        parent_id=parent_id,
        child_ids=child_ids or [],
        backend_dom_node_id=uc.cdp.dom.BackendNodeId(backend_dom_node_id) if backend_dom_node_id is not None else None,
        properties=props,
    )


class A11yTreeParsingAndRefTests(unittest.TestCase):
    """Unit tests for building the a11y tree and formatting text with refs."""

    def test_is_ref_selector(self):
        self.assertTrue(is_ref_selector("ref=e1"))
        self.assertTrue(is_ref_selector("REF=E12"))
        self.assertTrue(is_ref_selector('ref="e5"'))
        self.assertTrue(is_ref_selector("ref='e9'"))
        self.assertTrue(is_ref_selector("@e1"))
        self.assertFalse(is_ref_selector("button.submit"))
        self.assertFalse(is_ref_selector("//input[@id='text']"))

        known = {"e1", "e2", "e12"}
        self.assertTrue(is_ref_selector("e1", known_refs=known))
        self.assertFalse(is_ref_selector("e99", known_refs=known))

    def test_extract_ref_id(self):
        self.assertEqual(extract_ref_id("ref=e1"), "e1")
        self.assertEqual(extract_ref_id("REF=e12"), "e12")
        self.assertEqual(extract_ref_id('ref="e5"'), "e5")
        self.assertEqual(extract_ref_id("ref='e7'"), "e7")
        self.assertEqual(extract_ref_id("@e3"), "e3")
        self.assertEqual(extract_ref_id("e4"), "e4")

    def test_is_interactive_node(self):
        self.assertTrue(is_interactive_node({"role": "button"}))
        self.assertTrue(is_interactive_node({"role": "link"}))
        self.assertTrue(is_interactive_node({"role": "textbox"}))
        self.assertTrue(is_interactive_node({"role": "checkbox"}))
        self.assertTrue(is_interactive_node({"role": "combobox"}))
        self.assertTrue(is_interactive_node({"role": "generic", "properties": {"focusable": True}}))
        self.assertTrue(
            is_interactive_node(
                {"role": "generic", "properties": [{"name": "focusable", "value": {"value": True}}]}
            )
        )
        self.assertTrue(
            is_interactive_node(
                {"role": "generic", "properties": [{"name": "focusable", "value": True}]}
            )
        )

        self.assertFalse(is_interactive_node({"role": "RootWebArea"}))
        self.assertFalse(is_interactive_node({"role": "heading"}))
        self.assertFalse(is_interactive_node({"role": "text"}))
        self.assertFalse(is_interactive_node({"role": "generic"}))
        self.assertFalse(
            is_interactive_node({"role": "generic", "properties": {"focusable": False}})
        )
        self.assertFalse(
            is_interactive_node(
                {"role": "generic", "properties": [{"name": "focusable", "value": {"value": False}}]}
            )
        )

    def test_build_a11y_tree_assigns_short_refs_to_interactive_nodes(self):
        nodes = [
            make_mock_ax_node("1", "RootWebArea", name="Test Page", child_ids=["2", "3", "4"]),
            make_mock_ax_node("2", "heading", name="Welcome", parent_id="1", properties=[{"name": "level", "value": 1}]),
            make_mock_ax_node("3", "textbox", name="Username", value="admin", parent_id="1", backend_dom_node_id=101),
            make_mock_ax_node("4", "button", name="Log In", parent_id="1", backend_dom_node_id=102),
        ]

        tree, refs, ref_details = build_a11y_tree(nodes)
        self.assertIsNotNone(tree)
        self.assertEqual(tree["role"], "RootWebArea")
        self.assertEqual(len(tree["children"]), 3)

        # Heading is not interactive -> no ref
        heading_child = tree["children"][0]
        self.assertEqual(heading_child["role"], "heading")
        self.assertIsNone(heading_child["ref"])

        # Textbox is interactive -> gets ref e1
        textbox_child = tree["children"][1]
        self.assertEqual(textbox_child["role"], "textbox")
        self.assertEqual(textbox_child["ref"], "e1")

        # Button is interactive -> gets ref e2
        button_child = tree["children"][2]
        self.assertEqual(button_child["role"], "button")
        self.assertEqual(button_child["ref"], "e2")

        # Check refs mapping
        self.assertEqual(refs["e1"], 101)
        self.assertEqual(refs["e2"], 102)
        self.assertEqual(ref_details["e1"]["role"], "textbox")
        self.assertEqual(ref_details["e2"]["role"], "button")

    def test_build_a11y_tree_flattens_ignored_wrapper_nodes(self):
        nodes = [
            make_mock_ax_node("1", "RootWebArea", name="Page", child_ids=["2"]),
            make_mock_ax_node("2", "generic", ignored=True, parent_id="1", child_ids=["3", "4"]),
            make_mock_ax_node("3", "heading", name="Title", parent_id="2"),
            make_mock_ax_node("4", "button", name="Submit", parent_id="2", backend_dom_node_id=201),
        ]

        tree, refs, ref_details = build_a11y_tree(nodes)
        self.assertIsNotNone(tree)
        # Ignored node "2" should be collapsed, promoting "3" and "4" to RootWebArea
        self.assertEqual(len(tree["children"]), 2)
        self.assertEqual(tree["children"][0]["role"], "heading")
        self.assertEqual(tree["children"][1]["role"], "button")
        self.assertEqual(tree["children"][1]["ref"], "e1")
        self.assertEqual(refs["e1"], 201)


class A11ySizeCapTests(unittest.TestCase):
    """Unit tests for hard size caps (max_nodes, max_chars, truncated flag)."""

    def setUp(self):
        # Create a tree with 1 root and 10 child nodes
        child_ids = [str(i) for i in range(2, 12)]
        self.nodes = [
            make_mock_ax_node("1", "RootWebArea", name="Big Page", child_ids=child_ids)
        ]
        for idx, cid in enumerate(child_ids, start=1):
            self.nodes.append(
                make_mock_ax_node(
                    cid,
                    "button",
                    name=f"Button {idx}",
                    parent_id="1",
                    backend_dom_node_id=100 + idx,
                )
            )

    def test_max_nodes_cap_enforced(self):
        tree, refs, details = build_a11y_tree(self.nodes)

        # Capped at 4 nodes (1 root + 3 children)
        text, count, truncated = format_text_tree(tree, max_nodes=4, max_chars=10000)
        self.assertEqual(count, 4)
        self.assertTrue(truncated)
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 4)

    def test_max_chars_cap_enforced(self):
        tree, refs, details = build_a11y_tree(self.nodes)

        # Formatted tree is normally ~300 chars. Cap at 60 chars.
        text, count, truncated = format_text_tree(tree, max_nodes=100, max_chars=60)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 60)

    def test_within_limits_truncated_is_false(self):
        tree, refs, details = build_a11y_tree(self.nodes)

        text, count, truncated = format_text_tree(tree, max_nodes=500, max_chars=50000)
        self.assertEqual(count, 11)  # 1 root + 10 children
        self.assertFalse(truncated)
        self.assertIn('(ref=e1)', text)
        self.assertIn('(ref=e10)', text)


class A11ySnapshotManagerTests(unittest.TestCase):
    """Unit tests for AccessibilitySnapshotManager storing and resolving refs."""

    def setUp(self):
        self.mgr = AccessibilitySnapshotManager()
        self.fake_tab = SimpleNamespace(target=SimpleNamespace(target_id="tab-1"))

    def test_resolve_ref_success(self):
        self.mgr.store_snapshot(
            instance_id="inst-1",
            tab=self.fake_tab,
            refs={"e1": 101, "e2": 102},
            tree_text="RootWebArea\n  button (ref=e1)",
            tree_json={"role": "RootWebArea"},
            ref_details={"e1": {"role": "button", "backend_node_id": 101}},
        )

        # Can resolve by instance_id
        node_id = self.mgr.resolve_ref("e1", instance_id="inst-1")
        self.assertEqual(node_id, 101)

        # Can resolve by tab
        node_id_tab = self.mgr.resolve_ref("e2", tab=self.fake_tab)
        self.assertEqual(node_id_tab, 102)

    def test_stale_ref_no_snapshot_raises_clear_error(self):
        with self.assertRaises(Exception) as ctx:
            self.mgr.resolve_ref("e1", instance_id="inst-empty")
        self.assertIn("no accessibility snapshot has been taken", str(ctx.exception))

    def test_stale_ref_unknown_ref_raises_clear_error(self):
        self.mgr.store_snapshot(
            instance_id="inst-1",
            tab=self.fake_tab,
            refs={"e1": 101},
            tree_text="text",
            tree_json={},
        )
        with self.assertRaises(Exception) as ctx:
            self.mgr.resolve_ref("e999", instance_id="inst-1")
        self.assertIn("ref 'e999' not found in latest accessibility snapshot", str(ctx.exception))

    def test_forget_instance_cleans_stored_snapshot(self):
        self.mgr.store_snapshot(
            instance_id="inst-1",
            tab=None,
            refs={"e1": 101},
            tree_text="text",
            tree_json={},
        )
        self.mgr.forget_instance("inst-1")
        with self.assertRaises(Exception) as ctx:
            self.mgr.resolve_ref("e1", instance_id="inst-1")
        self.assertIn("no accessibility snapshot has been taken", str(ctx.exception))


class A11yInputActionsTargetingTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests verifying find_node and DOMHandler click/type/fill by ref=."""

    def setUp(self):
        a11y_manager.clear_all()

    async def test_find_node_resolves_ref(self):
        tab = RecordingTabForA11y(backend_node_alive=True, backend_node_id=55)
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e1": 55},
            tree_text="button (ref=e1)",
            tree_json={},
        )

        node_id = await input_actions.find_node(tab, "ref=e1", instance_id="test-inst")
        self.assertEqual(node_id, 55)

        # Check DOM.describeNode was dispatched to verify node liveness
        describe_events = [e for e in tab.events if e[0] == "DOM.describeNode"]
        self.assertEqual(len(describe_events), 1)

    async def test_find_node_with_stale_missing_ref_raises(self):
        tab = RecordingTabForA11y()
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e1": 55},
            tree_text="",
            tree_json={},
        )

        with self.assertRaises(Exception) as ctx:
            await input_actions.find_node(tab, "ref=e999", instance_id="test-inst")
        self.assertIn("ref 'e999' not found in latest accessibility snapshot", str(ctx.exception))

    async def test_find_node_with_dead_dom_element_raises_stale_error(self):
        tab = RecordingTabForA11y(backend_node_alive=False)
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e1": 77},
            tree_text="",
            tree_json={},
        )

        with self.assertRaises(Exception) as ctx:
            await input_actions.find_node(tab, "ref=e1", instance_id="test-inst")
        self.assertIn("element no longer exists in DOM", str(ctx.exception))

    async def test_click_element_by_ref(self):
        tab = RecordingTabForA11y(backend_node_alive=True, backend_node_id=88)
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e1": 88},
            tree_text="button (ref=e1)",
            tree_json={},
        )

        result = await dom_handler.DOMHandler.click_element(
            tab, "ref=e1", instance_id="test-inst"
        )
        self.assertTrue(result)

        mouse_events = [e for e in tab.events if e[0].startswith("Input.dispatchMouseEvent")]
        self.assertGreater(len(mouse_events), 0)

    async def test_type_text_by_ref(self):
        tab = RecordingTabForA11y(backend_node_alive=True, backend_node_id=99)
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e2": 99},
            tree_text="textbox (ref=e2)",
            tree_json={},
        )

        result = await dom_handler.DOMHandler.type_text(
            tab, "ref=e2", "hello", instance_id="test-inst"
        )
        self.assertTrue(result)

        key_events = [e for e in tab.events if e[0].startswith("Input.dispatchKeyEvent")]
        self.assertGreater(len(key_events), 0)

    async def test_fill_by_ref(self):
        tab = RecordingTabForA11y(backend_node_alive=True, backend_node_id=105)
        a11y_manager.store_snapshot(
            instance_id="test-inst",
            tab=tab,
            refs={"e3": 105},
            tree_text="textbox (ref=e3)",
            tree_json={},
        )

        result = await dom_handler.DOMHandler.fill(
            tab, "ref=e3", "value123", instance_id="test-inst"
        )
        self.assertTrue(result)

        key_events = [e for e in tab.events if e[0].startswith("Input.dispatchKeyEvent")]
        self.assertGreater(len(key_events), 0)


class A11ySnapshotToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests verifying get_accessibility_snapshot on fake tabs."""

    def setUp(self):
        a11y_manager.clear_all()

    async def test_cdp_snapshot_success(self):
        nodes = [
            make_mock_ax_node("1", "RootWebArea", name="Inputs", child_ids=["2", "3"]),
            make_mock_ax_node("2", "textbox", name="Text", value="", parent_id="1", backend_dom_node_id=201),
            make_mock_ax_node("3", "button", name="Submit", parent_id="1", backend_dom_node_id=202),
        ]
        tab = RecordingTabForA11y(ax_nodes=nodes)

        result = await get_accessibility_snapshot(tab, instance_id="inst-test")
        self.assertIn("RootWebArea", result["text"])
        self.assertIn('(ref=e1)', result["text"])
        self.assertIn('(ref=e2)', result["text"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["node_count"], 3)
        self.assertIn("e1", result["refs"])
        self.assertIn("e2", result["refs"])

    async def test_dom_walk_fallback_on_cdp_error(self):
        tab = RecordingTabForA11y(ax_error=True)

        result = await get_accessibility_snapshot(tab, instance_id="inst-fallback")
        self.assertIn("RootWebArea", result["text"])
        self.assertIn("Click Me", result["text"])
        self.assertIn("Header", result["text"])

    async def test_mcp_tool_registration_and_aliases(self):
        from fastmcp import Client

        self.assertIn("get_accessibility_snapshot", server.SECTION_TOOLS["element-interaction"])
        self.assertIn("take_accessibility_snapshot", server.SECTION_TOOLS["element-interaction"])
        self.assertIn("fill", server.SECTION_TOOLS["element-interaction"])
        self.assertIn("fill_element", server.SECTION_TOOLS["element-interaction"])

        tools = await server.mcp.get_tools()
        self.assertIn("get_accessibility_snapshot", tools)
        self.assertIn("take_accessibility_snapshot", tools)
        self.assertIn("fill", tools)
        self.assertIn("fill_element", tools)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class RealBrowserA11ySnapshotTests(unittest.IsolatedAsyncioTestCase):
    """Gated real-browser tests verifying accessibility snapshot and ref= targeting."""

    async def asyncSetUp(self):
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions
        from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled

        window_matrix.skip_if_no_display(self)
        self.temp_dir = tempfile.mkdtemp(prefix="stealth_a11y_test_")
        self.server = FixtureServer().start()
        self.manager = BrowserManager()
        self.instance = await asyncio.wait_for(
            self.manager.spawn_browser(
                BrowserOptions(
                    headless=window_matrix.HEADLESS,
                    window=window_matrix.window_mode(),
                    user_data_dir=self.temp_dir,
                    user_agent=HEADLESS_USER_AGENT,
                    sandbox=False if sandbox_must_be_disabled() else True,
                )
            ),
            timeout=20.0,
        )
        self.tab = await asyncio.wait_for(
            self.manager.get_tab(self.instance.instance_id),
            timeout=20.0,
        )

    async def asyncTearDown(self):
        from stealth_browser_mcp.process_cleanup import process_cleanup

        if hasattr(self, "instance") and self.instance:
            try:
                await asyncio.wait_for(
                    self.manager.close_instance(self.instance.instance_id),
                    timeout=5.0,
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(self.manager.close_all(), timeout=5.0)
        except Exception:
            pass
        if hasattr(self, "temp_dir") and self.temp_dir:
            try:
                for pid in process_cleanup._get_browser_pids_for_profile(self.temp_dir):
                    process_cleanup._kill_process_by_pid(pid, "test_harness")
            except Exception:
                pass
            _safe_cleanup_dir(self.temp_dir)
        if hasattr(self, "server") and self.server:
            try:
                self.server.stop()
            except Exception:
                pass

    def _get_inputs_url(self) -> str:
        try:
            if hasattr(self, "server") and self.server:
                url = self.server.url_for("inputs.html")
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        return url
        except Exception:
            pass
        return "data:text/html," + quote(INPUTS_HTML)

    async def test_real_browser_snapshot_and_click_by_ref(self):
        url = self._get_inputs_url()
        await asyncio.wait_for(
            self.manager.navigate(self.instance.instance_id, url),
            timeout=20.0,
        )
        self.tab = await asyncio.wait_for(
            self.manager.get_tab(self.instance.instance_id),
            timeout=20.0,
        )
        tab = self.tab
        await asyncio.sleep(0.3)

        # 1. Take accessibility snapshot
        snapshot = await asyncio.wait_for(
            dom_handler.get_accessibility_snapshot(
                tab,
                instance_id=self.instance.instance_id,
                max_nodes=100,
            ),
            timeout=20.0,
        )
        self.assertIsNotNone(snapshot)
        text = snapshot["text"]
        self.assertIn("Plain Inputs Fixture", text)
        self.assertIn("ref=e", text)
        self.assertFalse(snapshot["truncated"])

        # Find ref for the Submit button
        submit_ref = None
        for ref_id, info in snapshot["refs"].items():
            if info.get("role") == "button" and "Submit" in (info.get("name") or ""):
                submit_ref = ref_id
                break
        self.assertIsNotNone(submit_ref, f"Submit button ref not found in {snapshot['refs']}")

        # 2. Attach click tracker to Submit button and click by ref=
        await asyncio.wait_for(
            tab.evaluate(
                "window.__btnClicked = false; "
                "document.getElementById('submit-btn').addEventListener('click', () => { window.__btnClicked = true; });"
            ),
            timeout=20.0,
        )
        clicked = await asyncio.wait_for(
            dom_handler.DOMHandler.click_element(
                tab,
                f"ref={submit_ref}",
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )
        self.assertTrue(clicked)
        await asyncio.sleep(0.1)

        btn_clicked = await asyncio.wait_for(
            tab.evaluate("window.__btnClicked"),
            timeout=20.0,
        )
        self.assertTrue(btn_clicked)

        # 3. Also test clicking checkbox by ref=
        checkbox_ref = None
        for ref_id, info in snapshot["refs"].items():
            if info.get("role") == "checkbox" or "Checkbox" in (info.get("name") or ""):
                checkbox_ref = ref_id
                break
        self.assertIsNotNone(checkbox_ref)
        await asyncio.wait_for(
            dom_handler.DOMHandler.click_element(
                tab,
                f"ref={checkbox_ref}",
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )
        is_checked = await asyncio.wait_for(
            tab.evaluate("document.getElementById('checkbox-input').checked"),
            timeout=20.0,
        )
        self.assertTrue(is_checked)

    async def test_real_browser_fill_and_type_by_ref(self):
        url = self._get_inputs_url()
        await asyncio.wait_for(
            self.manager.navigate(self.instance.instance_id, url),
            timeout=20.0,
        )
        self.tab = await asyncio.wait_for(
            self.manager.get_tab(self.instance.instance_id),
            timeout=20.0,
        )
        tab = self.tab
        await asyncio.sleep(0.3)

        snapshot = await asyncio.wait_for(
            dom_handler.get_accessibility_snapshot(
                tab,
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )

        # Find ref for text input
        text_input_ref = None
        for ref_id, info in snapshot["refs"].items():
            if "Text Input" in (info.get("name") or ""):
                text_input_ref = ref_id
                break
        self.assertIsNotNone(text_input_ref, f"Text Input ref not found in {snapshot['refs']}")

        # Fill by ref=
        filled = await asyncio.wait_for(
            dom_handler.DOMHandler.fill(
                tab,
                f"ref={text_input_ref}",
                "Automated Text",
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )
        self.assertTrue(filled)

        val = await asyncio.wait_for(
            tab.evaluate("document.getElementById('text-input').value"),
            timeout=20.0,
        )
        self.assertEqual(val, "Automated Text")

        # Also test type_text on email input by ref=
        email_ref = None
        for ref_id, info in snapshot["refs"].items():
            if "Email" in (info.get("name") or ""):
                email_ref = ref_id
                break
        self.assertIsNotNone(email_ref, f"Email ref not found in {snapshot['refs']}")

        typed = await asyncio.wait_for(
            dom_handler.DOMHandler.type_text(
                tab,
                f"ref={email_ref}",
                "user@test.com",
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )
        self.assertTrue(typed)
        email_val = await asyncio.wait_for(
            tab.evaluate("document.getElementById('email-input').value"),
            timeout=20.0,
        )
        self.assertEqual(email_val, "user@test.com")

    async def test_real_browser_size_cap_truncated(self):
        url = self._get_inputs_url()
        await asyncio.wait_for(
            self.manager.navigate(self.instance.instance_id, url),
            timeout=20.0,
        )
        self.tab = await asyncio.wait_for(
            self.manager.get_tab(self.instance.instance_id),
            timeout=20.0,
        )
        tab = self.tab
        await asyncio.sleep(0.3)

        # Cap max_nodes at 3
        snapshot = await asyncio.wait_for(
            dom_handler.get_accessibility_snapshot(
                tab,
                instance_id=self.instance.instance_id,
                max_nodes=3,
            ),
            timeout=20.0,
        )
        self.assertTrue(snapshot["truncated"])
        self.assertLessEqual(snapshot["node_count"], 3)

    async def test_real_browser_stale_ref_raises(self):
        url = self._get_inputs_url()
        await asyncio.wait_for(
            self.manager.navigate(self.instance.instance_id, url),
            timeout=20.0,
        )
        self.tab = await asyncio.wait_for(
            self.manager.get_tab(self.instance.instance_id),
            timeout=20.0,
        )
        tab = self.tab
        await asyncio.sleep(0.3)

        await asyncio.wait_for(
            dom_handler.get_accessibility_snapshot(
                tab,
                instance_id=self.instance.instance_id,
            ),
            timeout=20.0,
        )

        with self.assertRaises(Exception) as ctx:
            await asyncio.wait_for(
                dom_handler.DOMHandler.click_element(
                    tab,
                    "ref=e9999",
                    instance_id=self.instance.instance_id,
                ),
                timeout=20.0,
            )
        self.assertIn("ref 'e9999' not found in latest accessibility snapshot", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
