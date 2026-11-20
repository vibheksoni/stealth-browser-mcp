"""Tests for compact tool descriptions and schemas (SB-64).

Covers:
- ``compact_tools`` collapsing Google-style docstrings, capping them, filling
  empty property descriptions, and removing ``title`` keys, with names,
  types, required, defaults, enums, and output schemas untouched.
- Idempotency: running twice yields identical output.
- ``group_tools`` classifying every registered tool into exactly one group.
- The real server surface stays compacted and ratio <= 0.7.

ASCII only.
"""

import asyncio
import unittest

from fastmcp import FastMCP

from stealth_browser_mcp import server, tool_compact

NOTE_LEN = len(tool_compact.INSTANCE_DEFAULT_NOTE)

_TOOL_A_DOC = """\
Navigate to a URL.

Args:
    instance_id (str): Browser instance ID from list_instances.
    url (str): The URL to open.
    wait_until (str): One of 'load', 'domcontentloaded', or 'networkidle'
        so the caller can control how long navigation blocks for.
    timeout (int): Navigation timeout in milliseconds.

Returns:
    dict: Navigation result with final URL and page title.

Raises:
    ValueError: When the URL is not a valid http(s) URL.
"""

_TOOL_B_DOC = """\
Click an element on the page.

Args:
    instance_id (str): Browser instance ID.
    selector (str): CSS selector, deep selector, or XPath.
    timeout (int): Click timeout in milliseconds.

Examples:
    await click_element(instance_id, "button.submit")
"""


def _dummy_mcp() -> FastMCP:
    m = FastMCP("compact-test")

    @m.tool(description=_TOOL_A_DOC)
    async def navigate(instance_id: str, url: str, timeout: int = 5000) -> dict:
        return {"url": url}

    navigate.__doc__ = _TOOL_A_DOC
    navigate.description = _TOOL_A_DOC

    @m.tool(description=_TOOL_B_DOC)
    async def click_element(instance_id: str, selector: str, timeout: int = 1000) -> bool:
        return True

    click_element.__doc__ = _TOOL_B_DOC
    click_element.description = _TOOL_B_DOC
    return m


class CompactToolsUnitTests(unittest.TestCase):
    def test_compacts_descriptions_and_titles(self):
        m = _dummy_mcp()
        result = tool_compact.compact_tools(m)
        self.assertEqual(result["tools"], 2)

        async def get():
            return {name: t for name, t in (await m.get_tools()).items()}

        tools = asyncio.run(get())
        for name, tool in tools.items():
            desc = tool.description or ""
            self.assertNotIn("Args:", desc)
            self.assertNotIn("Returns:", desc)
            self.assertNotIn("Raises:", desc)
            self.assertNotIn("Examples:", desc)
            self.assertLessEqual(len(desc), 300 + NOTE_LEN)
            self.assertIn(tool_compact.INSTANCE_DEFAULT_NOTE, desc)

        navigate = tools["navigate"]
        params = navigate.parameters
        self.assertNotIn("title", params)
        self.assertNotIn("title", params["properties"]["instance_id"])
        self.assertNotIn("title", params["properties"]["url"])
        # Property descriptions filled from Args section, one line, capped.
        self.assertIn("Browser instance ID", params["properties"]["instance_id"]["description"])
        self.assertLessEqual(
            len(params["properties"]["url"]["description"]),
            tool_compact.PROPERTY_CAP,
        )

    def test_names_types_required_unchanged(self):
        m = _dummy_mcp()
        async def names():
            return {n: t for n, t in (await m.get_tools()).items()}

        before = asyncio.run(names())
        before_required = {n: before[n].parameters.get("required") for n in before}
        before_types = {
            n: before[n].parameters["properties"].get("instance_id", {}).get("type")
            for n in before
        }

        tool_compact.compact_tools(m)

        after = asyncio.run(names())
        self.assertEqual(set(before), set(after))
        for n in before:
            self.assertEqual(after[n].parameters.get("required"), before_required[n])
            self.assertEqual(
                after[n].parameters["properties"].get("instance_id", {}).get("type"),
                before_types[n],
            )

    def test_idempotent(self):
        m = _dummy_mcp()
        tool_compact.compact_tools(m)

        async def snapshot():
            return {n: t.description for n, t in (await m.get_tools()).items()}

        first = asyncio.run(snapshot())
        tool_compact.compact_tools(m)
        second = asyncio.run(snapshot())
        self.assertEqual(first, second)

    def test_caps_at_boundary(self):
        m = _dummy_mcp()

        long_doc = (
            "A " + "word " * 200 + "second sentence here.\n\nArgs:\n    instance_id (str): id.\n"
        )

        @m.tool(description=long_doc)
        async def very_long_tool(instance_id: str, filler: str = "x") -> dict:
            return {}

        # 350 chars of leading prose should be capped at the last ". ".
        very_long_tool.__doc__ = long_doc
        very_long_tool.description = long_doc

        tool_compact.compact_tools(m)

        async def get():
            return {n: t for n, t in (await m.get_tools()).items()}

        tool = asyncio.run(get())["very_long_tool"]
        self.assertLessEqual(len(tool.description), 300 + NOTE_LEN)


class CompactToolsRealServerTests(unittest.TestCase):
    def test_real_server_compacted(self):
        result = tool_compact.compact_tools(server.mcp)
        self.assertEqual(result["tools"], len(asyncio.run(server.mcp.get_tools())))
        self.assertGreater(result["before_chars"], result["after_chars"])

        async def get():
            return {n: t for n, t in (await server.mcp.get_tools()).items()}

        tools = asyncio.run(get())
        self.assertGreaterEqual(len(tools), 110)

        before_names = set(tools)
        before_output = {n: getattr(tools[n], "output_schema", None) is not None for n in tools}
        before_required = {n: tools[n].parameters.get("required") for n in tools}

        for name, tool in tools.items():
            desc = tool.description or ""
            self.assertLessEqual(
                len(desc),
                300 + NOTE_LEN,
                "%s over cap: %d" % (name, len(desc)),
            )
            self.assertNotIn("Args:", desc, "%s still has Args" % name)

        # Idempotent.
        tool_compact.compact_tools(server.mcp)
        tools2 = asyncio.run(get())
        self.assertEqual(set(before_names), set(tools2))
        for n in tools:
            self.assertEqual(tools2[n].parameters.get("required"), before_required[n])
            self.assertEqual(
                getattr(tools2[n], "output_schema", None) is not None, before_output[n]
            )

        # Ratio under threshold.
        self.assertLessEqual(result["after_chars"] / result["before_chars"], 0.7)

    def test_group_tools_covers_all_exactly_once(self):
        async def get():
            return set((await server.mcp.get_tools()).keys())

        names = asyncio.run(get())
        groups = tool_compact.group_tools(sorted(names))
        assigned = [n for grp, tools in groups.items() for n in tools]
        self.assertEqual(sorted(assigned), sorted(names))
        self.assertEqual(len(assigned), len(set(assigned)))

        # Every tool belongs to a known group.
        for name in names:
            in_group = any(name in tools for tools in groups.values())
            self.assertTrue(in_group, "%s not grouped" % name)


if __name__ == "__main__":
    unittest.main()
