"""Tests for the agent preset (SB-64) and default instance_id resolution.

Covers:
- The 25-tool agent preset surface and the pure preset filter.
- ``pick_default_instance`` selection and error cases.
- ``make_instance_id_optional`` schema mutation plus the
  ``DefaultInstanceMiddleware`` end-to-end through the FastMCP client.
- README tool counts are no longer stale.

ASCII only.
"""

import asyncio
import os
import unittest
from pathlib import Path

from fastmcp import Client, FastMCP

from stealth_browser_mcp import server
from stealth_browser_mcp.instance_default import (
    INSTANCE_DEFAULT_NOTE,
    make_instance_id_optional,
    pick_default_instance,
    register_default_instance_middleware,
)

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"


def _run(coro):
    return asyncio.run(coro)


class AgentPresetTests(unittest.TestCase):
    def test_preset_has_25_known_tools(self):
        # The preset must name exactly 25 distinct tools.
        self.assertEqual(len(server.AGENT_PRESET_TOOLS), 25)
        self.assertEqual(len(set(server.AGENT_PRESET_TOOLS)), 25)

        # Every named tool must exist in the full registered surface. Using the
        # pure filter avoids mutating the global server's tool list.
        full_tools = _run(server.mcp.get_tools())
        all_names = {name: tool for name, tool in full_tools.items()}
        kept_names = server._filter_preset(list(all_names.keys()))
        for name in server.AGENT_PRESET_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, all_names)
                self.assertIn(name, kept_names)
        # The filter keeps exactly the preset tools present in the full surface.
        self.assertEqual(set(kept_names), set(server.AGENT_PRESET_TOOLS))

    def test_apply_agent_preset_on_dummy_server(self):
        dummy = FastMCP("dummy")

        @dummy.tool
        async def navigate(instance_id: str, url: str) -> dict:
            return {}

        @dummy.tool
        async def take_screenshot(instance_id: str = "") -> dict:
            return {}

        @dummy.tool
        async def some_debug_tool(extra: str = "") -> dict:
            return {}

        kept = server.apply_agent_preset(target=dummy)
        self.assertEqual(kept, 2)

        names = set(_run(dummy.get_tools()))
        self.assertIn("navigate", names)
        self.assertIn("take_screenshot", names)
        self.assertNotIn("some_debug_tool", names)

    def test_default_server_tool_count(self):
        self.assertGreaterEqual(len(asyncio.run(server.mcp.get_tools())), 110)

    def test_agent_mode_enabled_helper(self):
        saved = os.environ.get("STEALTH_MCP_PROFILE")
        try:
            os.environ.pop("STEALTH_MCP_PROFILE", None)
            self.assertFalse(server.agent_mode_enabled(False))
            self.assertTrue(server.agent_mode_enabled(True))
            os.environ["STEALTH_MCP_PROFILE"] = "agent"
            self.assertTrue(server.agent_mode_enabled(False))
            os.environ["STEALTH_MCP_PROFILE"] = "AGENT"
            self.assertTrue(server.agent_mode_enabled(False))
        finally:
            if saved is None:
                os.environ.pop("STEALTH_MCP_PROFILE", None)
            else:
                os.environ["STEALTH_MCP_PROFILE"] = saved


class DefaultInstanceSelectionTests(unittest.TestCase):
    def test_session_instance_wins(self):
        chosen = pick_default_instance("session-inst", ["open-a", "open-b"])
        self.assertEqual(chosen, "session-inst")

    def test_single_open_instance(self):
        chosen = pick_default_instance(None, ["only-one"])
        self.assertEqual(chosen, "only-one")

    def test_zero_open_instances_raises(self):
        with self.assertRaises(ValueError) as ctx:
            pick_default_instance(None, [])
        message = str(ctx.exception)
        self.assertIn("spawn_browser", message)
        self.assertIn("no browser is open", message)

    def test_two_open_instances_without_session_raises(self):
        with self.assertRaises(ValueError) as ctx:
            pick_default_instance(None, ["a", "b"])
        self.assertIn("2 open browsers", str(ctx.exception))


def _build_mcp(session_instance_fn, open_instances_fn):
    """Build a throwaway server with the optional-instance wiring."""
    m = FastMCP("t")

    @m.tool
    async def click(instance_id: str, selector: str = "a") -> dict:
        return {"instance_id": instance_id, "selector": selector}

    @m.tool
    async def list_instances() -> list:
        return []

    make_instance_id_optional(m)
    register_default_instance_middleware(m, session_instance_fn, open_instances_fn)
    return m


class OptionalInstanceSchemaAndMiddlewareTests(unittest.TestCase):
    def test_injection_when_omitted(self):
        m = _build_mcp(lambda: "inst-1", lambda: ["inst-1"])

        async def run():
            async with Client(m) as c:
                result = await c.call_tool("click", {"selector": "b"})
                return result.data if hasattr(result, "data") else result

        result = _run(run())
        self.assertEqual(result["instance_id"], "inst-1")
        self.assertEqual(result["selector"], "b")

    def test_explicit_instance_id_wins(self):
        m = _build_mcp(lambda: "inst-1", lambda: ["inst-1"])

        async def run():
            async with Client(m) as c:
                result = await c.call_tool(
                    "click", {"instance_id": "explicit", "selector": "c"}
                )
                return result.data if hasattr(result, "data") else result

        result = _run(run())
        self.assertEqual(result["instance_id"], "explicit")

    def test_zero_open_instances_reports_spawn_browser(self):
        m = _build_mcp(lambda: None, lambda: [])

        async def run():
            async with Client(m) as c:
                return await c.call_tool("click", {"selector": "b"}, raise_on_error=False)

        result = _run(run())
        self.assertTrue(result.is_error)
        self.assertIn("spawn_browser", str(result.content))

    def test_schema_optional_and_note(self):
        m = _build_mcp(lambda: None, lambda: [])

        async def run():
            async with Client(m) as c:
                tools = {tool.name: tool for tool in await c.list_tools()}
            return tools

        tools = _run(run())
        click = tools["click"]
        self.assertNotIn("instance_id", click.inputSchema.get("required", []))
        self.assertIn("instance_id", click.inputSchema["properties"])
        self.assertIn(INSTANCE_DEFAULT_NOTE, click.description)


class ReadmeCountsTests(unittest.TestCase):
    def test_readme_counts_not_stale(self):
        self.assertTrue(README_PATH.is_file(), "README.md missing")
        text = README_PATH.read_text(encoding="utf-8")
        for stale in ("101 tools", "101-tool", "21-tool"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, text)


if __name__ == "__main__":
    unittest.main()
