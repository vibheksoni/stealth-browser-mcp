import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client

from stealth_browser_mcp import server

# Tool parameters that default to None and must therefore accept an explicit null.
NULL_DEFAULT_PARAMS = {
    "spawn_browser": ["browser_args", "block_resources", "extra_headers"],
    "execute_cdp_command": ["params"],
    "discover_global_functions": ["context_id"],
    "call_javascript_function": ["args"],
    "inject_and_execute_script": ["context_id"],
    "get_function_executor_info": ["instance_id"],
    "save_session": ["file_path", "origins"],
    "restore_session": ["file_path", "session_data"],
}


def _accepts_null(schema):
    if schema.get("type") == "null":
        return True
    return any(_accepts_null(option) for option in schema.get("anyOf", []))


class NullDefaultSchemaTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
