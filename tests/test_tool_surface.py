"""Tests for tool surface filtering, category reference, and expand_tools (SB-72).

Covers:
- (a) Default server in core mode: tools/list contains exactly CORE_TOOLS (count 28-36).
- (b) JSON size of core tools/list is <= 40000 chars.
- (c) expand_tools adds category tools to later list_tools for same session;
      extended tools are callable by name before expanding.
- (d) Full mode lists >= 110 tools.
- (e) Every registered tool belongs to exactly one category and CORE_TOOLS exist.
- (f) list_tool_categories and resource stealth://tools/reference return every tool.
- (g) Compaction removes default: null and preserves nullable anyOf idempotently.
- (h) build_tool_surface_instructions mentions every non-core tool exactly once
      with token estimate <= 1500.

ASCII only.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import unittest
from typing import Optional

from fastmcp import Client, FastMCP

from stealth_browser_mcp import server, tool_compact, tool_surface


class ToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_server_core_mode(self):
        """(a) Default server in core mode lists exactly CORE_TOOLS (count 28-36)."""
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools}
        self.assertEqual(names, set(tool_surface.CORE_TOOLS))
        self.assertGreaterEqual(len(names), 28)
        self.assertLessEqual(len(names), 36)

    async def test_json_size_under_40k(self):
        """(b) JSON size of core mode tools/list is <= 40000 chars."""
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
        dumped = json.dumps([t.model_dump() for t in tools], separators=(",", ":"))
        self.assertLessEqual(len(dumped), 40000)

    async def test_expand_tools_and_callable_by_name(self):
        """(c) expand_tools adds category tools; extended tool callable before expanding."""
        async with Client(server.mcp) as client:
            # 1. Extended tool is callable by name before expanding without "unknown tool" error
            res = await client.call_tool(
                "fill_element",
                {"instance_id": "dummy_inst", "selector": "input", "value": "test"},
                raise_on_error=False,
            )
            error_text = str(res.content) if res.content else ""
            self.assertNotIn("unknown tool", error_text.lower())

            # 2. Before expanding, fill_element is not listed
            tools_before = {t.name for t in await client.list_tools()}
            self.assertNotIn("fill_element", tools_before)

            # 3. Call expand_tools("input")
            exp_res = await client.call_tool("expand_tools", {"category": "input"})
            self.assertFalse(exp_res.is_error)
            data = (
                exp_res.data
                if hasattr(exp_res, "data") and exp_res.data
                else json.loads(exp_res.content[0].text)
                if hasattr(exp_res, "content")
                else {}
            )
            self.assertEqual(data.get("expanded"), "input")
            self.assertIn("tools", data)
            self.assertIn("note", data)

            # 4. After expanding, fill_element is listed for this session
            tools_after = {t.name for t in await client.list_tools()}
            self.assertIn("fill_element", tools_after)

    async def test_expand_tools_unknown_category(self):
        """expand_tools with invalid category raises ToolError listing valid categories."""
        async with Client(server.mcp) as client:
            res = await client.call_tool(
                "expand_tools",
                {"category": "invalid_category_xyz"},
                raise_on_error=False,
            )
            self.assertTrue(res.is_error)
            self.assertIn("Valid categories", str(res.content))

    async def test_full_mode(self):
        """(d) Full mode lists >= 110 tools."""
        old_env = os.environ.get("STEALTH_MCP_TOOLS")
        os.environ["STEALTH_MCP_TOOLS"] = "full"
        try:
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
            self.assertGreaterEqual(len(tools), 110)
        finally:
            if old_env is None:
                os.environ.pop("STEALTH_MCP_TOOLS", None)
            else:
                os.environ["STEALTH_MCP_TOOLS"] = old_env

    def test_categories_and_core_tools_exist(self):
        """(e) Every registered tool belongs to exactly one category and CORE_TOOLS exist."""
        all_tools = asyncio.run(server.mcp.get_tools())
        all_names = sorted(all_tools.keys())
        groups = tool_compact.group_tools(all_names)

        assigned = [n for names in groups.values() for n in names]
        self.assertEqual(sorted(assigned), all_names)
        self.assertEqual(len(assigned), len(set(assigned)))

        for name in tool_surface.CORE_TOOLS:
            self.assertIn(name, all_tools)

    async def test_list_tool_categories_and_resource(self):
        """(f) list_tool_categories and resource return every tool."""
        all_tools = await server.mcp.get_tools()
        all_names = set(all_tools.keys())

        # list_tool_categories returns every tool
        cat_data = await server.list_tool_categories.fn()
        found_tools = set()
        if "categories" in cat_data:
            for names in cat_data["categories"].values():
                found_tools.update(names)
        else:
            for k, v in cat_data.items():
                if isinstance(v, list):
                    found_tools.update(v)
        self.assertEqual(found_tools, all_names)
        self.assertIn("mode", cat_data)
        self.assertIn("counts", cat_data)

        # stealth://tools/reference resource returns every tool
        async with Client(server.mcp) as client:
            res_contents = await client.read_resource("stealth://tools/reference")
        text = res_contents[0].text if hasattr(res_contents[0], "text") else str(res_contents[0])
        for name in all_names:
            self.assertIn(name, text)

    def test_compaction_removes_null_default_and_preserves_anyof_idempotent(self):
        """(g) Compaction removes default:null and preserves nullable anyOf idempotently."""
        dummy = FastMCP("test_compaction")

        @dummy.tool
        async def sample_tool(
            a: Optional[str] = None,
            b: str = "default_val",
            c: Optional[int] = None,
        ) -> dict:
            return {}

        sample = asyncio.run(dummy.get_tool("sample_tool"))
        props = sample.parameters["properties"]
        self.assertIn("anyOf", props["a"])
        self.assertIsNone(props["a"].get("default"))

        tool_compact.compact_tools(dummy)

        props_after = sample.parameters["properties"]
        self.assertNotIn("default", props_after["a"])
        self.assertIn("anyOf", props_after["a"])
        self.assertTrue(any(branch == {"type": "null"} for branch in props_after["a"]["anyOf"]))
        self.assertIn("anyOf", props_after["c"])
        self.assertTrue(any(branch == {"type": "null"} for branch in props_after["c"]["anyOf"]))
        self.assertEqual(props_after["b"].get("default"), "default_val")

        snapshot1 = copy.deepcopy(sample.parameters)
        tool_compact.compact_tools(dummy)
        snapshot2 = copy.deepcopy(sample.parameters)
        self.assertEqual(snapshot1, snapshot2)

    def test_build_tool_surface_instructions(self):
        """(h) Instructions mention every non-core tool exactly once; token estimate <= 1500."""
        all_names = list(asyncio.run(server.mcp.get_tools()).keys())
        text = tool_surface.build_tool_surface_instructions(all_names, tool_surface.CORE_TOOLS)
        non_core = set(all_names) - set(tool_surface.CORE_TOOLS)

        for name in non_core:
            matches = re.findall(rf"\b{re.escape(name)}\b", text)
            self.assertEqual(
                len(matches),
                1,
                f"Tool {name} expected exactly once in instructions, found {len(matches)}",
            )

        token_estimate = len(text) / 4
        self.assertLessEqual(token_estimate, 1500)

    def test_set_server_instructions_and_idempotence(self):
        """Instructions helper sets server instructions and init_tool_surface is idempotent."""
        instructions = tool_surface.get_server_instructions(server.mcp)
        self.assertIn("Key features:", instructions)
        self.assertIn(tool_surface.TOOL_SURFACE_INSTRUCTIONS_MARKER, instructions)

        # Idempotence: calling init_tool_surface multiple times does not duplicate
        tool_surface.init_tool_surface(server.mcp)
        instructions2 = tool_surface.get_server_instructions(server.mcp)
        marker_count = instructions2.count(tool_surface.TOOL_SURFACE_INSTRUCTIONS_MARKER)
        self.assertEqual(marker_count, 1)

        # Helper set_server_instructions works with fallback
        dummy = FastMCP("test_instructions_dummy")
        tool_surface.set_server_instructions(dummy, "custom instructions")
        dummy_instructions = tool_surface.get_server_instructions(dummy)
        self.assertEqual(dummy_instructions, "custom instructions")


if __name__ == "__main__":
    unittest.main()
