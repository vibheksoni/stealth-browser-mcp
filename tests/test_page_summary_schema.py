"""Tests for output schema of page-summary-wrapped tools (SB-61).

ASCII only. Free of browsers and network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any, Dict
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client, FastMCP
from stealth_browser_mcp.page_summary import with_page_summary


class TestPageSummarySchema(unittest.TestCase):
    """Test output schema behavior for tools wrapped by with_page_summary."""

    def test_wrapped_bool_tool_through_real_client(self) -> None:
        """Verify wrapped bool-returning tool succeeds with object output schema."""
        async def _run() -> Any:
            m = FastMCP("t")

            async def click(instance_id: str, selector: str = "a") -> bool:
                return True

            wrapped = with_page_summary(click)
            m.tool(wrapped)

            async with Client(m) as c:
                return await c.call_tool("click", {"instance_id": "i"}, raise_on_error=False)

        result = asyncio.run(_run())
        self.assertFalse(result.is_error)
        self.assertIsInstance(result.data, dict)
        self.assertIs(result.data.get("success"), True)
        self.assertIn("page", result.data)

    def test_dict_and_str_returning_tools_not_errors(self) -> None:
        """Verify dict- and str-returning wrapped tools report no errors and include page summary."""
        async def _run() -> tuple[Any, Any]:
            m = FastMCP("t_dict_str")

            async def dict_tool(instance_id: str) -> Dict[str, Any]:
                return {"ok": 1}

            async def str_tool(instance_id: str) -> str:
                return "hi"

            m.tool(with_page_summary(dict_tool))
            m.tool(with_page_summary(str_tool))

            async with Client(m) as c:
                res_dict = await c.call_tool("dict_tool", {"instance_id": "i"}, raise_on_error=False)
                res_str = await c.call_tool("str_tool", {"instance_id": "i"}, raise_on_error=False)
                return res_dict, res_str

        res_dict, res_str = asyncio.run(_run())
        self.assertFalse(res_dict.is_error)
        self.assertIsInstance(res_dict.data, dict)
        self.assertEqual(res_dict.data.get("ok"), 1)
        self.assertIn("page", res_dict.data)

        self.assertFalse(res_str.is_error)
        self.assertIsInstance(res_str.data, dict)
        self.assertEqual(res_str.data.get("result"), "hi")
        self.assertIn("page", res_str.data)

    def test_all_registered_action_tools_have_object_output_schema(self) -> None:
        """Verify all registered action tools have object output schema."""
        from stealth_browser_mcp import server

        tools = asyncio.run(server.mcp.get_tools())
        checked_count = 0
        for name in server.ACTION_TOOL_NAMES:
            if name in tools:
                tool = tools[name]
                self.assertTrue(
                    tool.output_schema is None or tool.output_schema.get("type") == "object",
                    f"Tool {name} has invalid output_schema: {tool.output_schema}",
                )
                checked_count += 1

        self.assertGreaterEqual(
            checked_count,
            5,
            f"Expected at least 5 action tools to be checked, found {checked_count}",
        )


if __name__ == "__main__":
    unittest.main()
