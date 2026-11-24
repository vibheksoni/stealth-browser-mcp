import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client

from stealth_browser_mcp import server

# Tool parameters that default to None and must therefore accept an explicit null.
NULL_DEFAULT_PARAMS = {
    "spawn_browser": ["browser_args", "block_resources", "extra_headers", "seed", "check_url", "cap"],
    "execute_cdp_command": ["params"],
    "discover_global_functions": ["context_id"],
    "call_javascript_function": ["args"],
    "inject_and_execute_script": ["context_id"],
    "get_function_executor_info": ["instance_id"],
    "save_session": ["file_path", "origins"],
    "restore_session": ["file_path", "session_data"],
    "choose_option": ["filter_text", "frame"],
    "wait_for": ["selector", "state", "text", "url"],
    "wait_for_download": ["instance_id", "filename_pattern", "instance"],
    "drop_files": ["instance_id"],
    "get_accessibility_snapshot": ["frame"],
    "take_accessibility_snapshot": ["frame"],
    "fill": ["frame", "value", "value_env"],
    "fill_element": ["frame", "value", "value_env"],
    "paste_text": ["frame", "mode"],
    "export_har": ["path"],
    "snapshot_seed": ["label"],
    "refresh_seed": ["profile", "source_dir"],
    "save_to_seed": ["seed"],
}


def _accepts_null(schema):
    if schema.get("type") == "null":
        return True
    return any(_accepts_null(option) for option in schema.get("anyOf", []))


class NullDefaultSchemaTests(unittest.TestCase):
    def setUp(self):
        self._prev_tools_mode = os.environ.get("STEALTH_MCP_TOOLS")
        os.environ["STEALTH_MCP_TOOLS"] = "full"

    def tearDown(self):
        if self._prev_tools_mode is None:
            os.environ.pop("STEALTH_MCP_TOOLS", None)
        else:
            os.environ["STEALTH_MCP_TOOLS"] = self._prev_tools_mode

    def test_none_default_params_advertise_nullable_type(self):
        async def run():
            async with Client(server.mcp) as client:
                return {tool.name: tool for tool in await client.list_tools()}

        tools = asyncio.run(run())
        for tool_name, params in NULL_DEFAULT_PARAMS.items():
            properties = tools[tool_name].inputSchema["properties"]
            for param in params:
                with self.subTest(tool=tool_name, param=param):
                    self.assertIsNone(properties[param].get("default"))
                    self.assertTrue(_accepts_null(properties[param]), properties[param])

    def test_explicit_null_is_accepted(self):
        async def run():
            async with Client(server.mcp) as client:
                return await client.call_tool(
                    "get_function_executor_info", {"instance_id": None}, raise_on_error=False
                )

        result = asyncio.run(run())
        self.assertFalse(result.is_error, result.content)

    def test_export_har_schema(self):
        async def run():
            async with Client(server.mcp) as client:
                return {tool.name: tool for tool in await client.list_tools()}

        tools = asyncio.run(run())
        self.assertIn("export_har", tools)
        tool = tools["export_har"]
        props = tool.inputSchema["properties"]
        self.assertIn("instance_id", props)
        self.assertIn("path", props)
        self.assertIn("include_sensitive", props)
        required = tool.inputSchema.get("required", [])
        self.assertIn("instance_id", required)

    def test_save_to_seed_schema(self):
        async def run():
            async with Client(server.mcp) as client:
                return {tool.name: tool for tool in await client.list_tools()}

        tools = asyncio.run(run())
        self.assertIn("save_to_seed", tools)
        tool = tools["save_to_seed"]
        props = tool.inputSchema["properties"]
        self.assertIn("session_id", props)
        self.assertIn("seed", props)
        self.assertIn("close_first", props)
        self.assertIn("flush_wait_seconds", props)
        required = tool.inputSchema.get("required", [])
        self.assertIn("session_id", required)
        self.assertNotIn("seed", required)
        self.assertNotIn("close_first", required)
        self.assertNotIn("flush_wait_seconds", required)


if __name__ == "__main__":
    unittest.main()
