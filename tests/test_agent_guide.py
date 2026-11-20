"""Unit tests for agent guide, instructions, playbook resource, and input prompt (SB-68).

Tests run without a real browser or network.
ASCII only.
"""

from __future__ import annotations

import asyncio
import unittest

from fastmcp import Client

from stealth_browser_mcp import agent_guide, server, tool_surface


class TestAgentGuideUnit(unittest.IsolatedAsyncioTestCase):
    """Unit tests for agent guidance functions, data tables, and MCP primitives."""

    def test_connect_instructions_bounds_and_ascii(self) -> None:
        """Verify build_connect_instructions size bounds (1200-3200 chars, ~400-800 tokens) and ASCII."""
        text = agent_guide.build_connect_instructions()
        self.assertTrue(text.isascii(), "build_connect_instructions must be 100% ASCII")
        self.assertGreaterEqual(len(text), 1200, "build_connect_instructions below 1200 chars")
        self.assertLessEqual(len(text), 3200, "build_connect_instructions exceeds 3200 chars")
        tokens = len(text) / 4
        self.assertGreaterEqual(tokens, 300, "Token estimate below expected lower bound")
        self.assertLessEqual(tokens, 800, "Token estimate exceeds 800 tokens")

        # Must contain Key features: for backward compatibility with SB-72 tests
        self.assertIn("Key features:", text)
        self.assertIn("Agent Input Guide:", text)

    def test_input_playbook_bounds_and_ascii(self) -> None:
        """Verify build_input_playbook size bounds (up to ~3000 tokens) and ASCII."""
        playbook = agent_guide.build_input_playbook()
        self.assertTrue(playbook.isascii(), "build_input_playbook must be 100% ASCII")
        self.assertGreaterEqual(len(playbook), 2000, "build_input_playbook should be comprehensive")
        tokens = len(playbook) / 4
        self.assertLessEqual(tokens, 3000, "build_input_playbook exceeds 3000 tokens limit")
        self.assertIn("# Browser Input Playbook", playbook)
        self.assertIn("Detailed Recipe Specifications", playbook)

    def test_input_recipes_schema_and_tool_registration(self) -> None:
        """Verify every recipe in INPUT_RECIPES has required fields and registered tool name."""
        all_tools = asyncio.run(server.mcp.get_tools())
        self.assertGreater(len(agent_guide.INPUT_RECIPES), 0)

        required_keys = {"id", "field_type", "when", "tool", "args_example", "verify", "pitfalls"}
        for recipe in agent_guide.INPUT_RECIPES:
            for k in required_keys:
                self.assertIn(k, recipe, f"Recipe missing key '{k}': {recipe}")
            self.assertIsInstance(recipe["args_example"], dict)
            self.assertIsInstance(recipe["pitfalls"], list)
            self.assertIn(
                recipe["tool"],
                all_tools,
                f"Recipe {recipe['id']} references unregistered tool '{recipe['tool']}'",
            )
            # ASCII check on text fields
            self.assertTrue(recipe["id"].isascii())
            self.assertTrue(recipe["field_type"].isascii())
            self.assertTrue(recipe["when"].isascii())
            self.assertTrue(recipe["tool"].isascii())
            self.assertTrue(recipe["verify"].isascii())
            for p in recipe["pitfalls"]:
                self.assertTrue(p.isascii())

    def test_how_to_input_prompt_generator(self) -> None:
        """Verify how_to_input_prompt returns text for all branches."""
        full_text = agent_guide.how_to_input_prompt(None)
        self.assertTrue(full_text.isascii())
        self.assertIn("Input Recipes Summary:", full_text)

        text_recipe = agent_guide.how_to_input_prompt("text")
        self.assertTrue(text_recipe.isascii())
        self.assertIn("text_fill", text_recipe)

        unknown_recipe = agent_guide.how_to_input_prompt("unknown_type_xyz")
        self.assertTrue(unknown_recipe.isascii())
        self.assertIn("No specific recipe found", unknown_recipe)

    async def test_client_instructions_resource_and_prompt(self) -> None:
        """Verify instructions, stealth://guide/input resource, and how_to_input prompt via client."""
        async with Client(server.mcp) as client:
            # 1. Instructions check
            instructions = None
            if hasattr(client, "initialize_result") and client.initialize_result:
                instructions = getattr(client.initialize_result, "instructions", None)
            if not instructions:
                instructions = tool_surface.get_server_instructions(server.mcp)

            self.assertIsNotNone(instructions)
            self.assertIn("Agent Input Guide:", instructions)
            self.assertIn(tool_surface.TOOL_SURFACE_INSTRUCTIONS_MARKER, instructions)

            # 2. Resource check
            resources = await client.list_resources()
            resource_uris = [str(r.uri) for r in resources]
            self.assertIn("stealth://guide/input", resource_uris)

            res_contents = await client.read_resource("stealth://guide/input")
            self.assertGreater(len(res_contents), 0)
            res_item = res_contents[0]
            res_text = getattr(res_item, "text", None) or getattr(res_item, "content", None) or str(res_item)
            if not isinstance(res_text, str):
                res_text = str(res_text)
            self.assertIn("Browser Input Playbook", res_text)

            # 3. Prompt check
            prompts = await client.list_prompts()
            prompt_names = [p.name for p in prompts]
            self.assertIn("how_to_input", prompt_names)

            prompt_res = await client.get_prompt("how_to_input")
            self.assertGreater(len(prompt_res.messages), 0)
            msg_content = prompt_res.messages[0].content
            msg_text = getattr(msg_content, "text", None) or str(msg_content)
            self.assertIn("Input Recipes Summary", msg_text)

            prompt_filtered = await client.get_prompt("how_to_input", {"field_type": "text"})
            self.assertGreater(len(prompt_filtered.messages), 0)
            filtered_content = prompt_filtered.messages[0].content
            filtered_text = getattr(filtered_content, "text", None) or str(filtered_content)
            self.assertIn("text_fill", filtered_text)


if __name__ == "__main__":
    unittest.main()
