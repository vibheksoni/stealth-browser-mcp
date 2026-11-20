"""Screenshot wire-content tests without browser or cleanup side effects."""

import ast
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any, Dict, Optional, Union
from unittest.mock import patch

from fastmcp import Client, FastMCP
from fastmcp.client.transports import PythonStdioTransport
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import screenshot_clip, screenshot_encoder
from stealth_browser_mcp.screenshot_clip import (
    capture_clip_screenshot,
    compute_clip_from_rect,
    resolve_element_clip,
    validate_clip_args,
)
from stealth_browser_mcp.screenshot_encoder import DEFAULT_QUALITY, fit_screenshot, validate_options


class ScreenshotContentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.captures = []
        buffer = io.BytesIO()
        Image.new("RGBA", (96, 64), (30, 60, 90, 128)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.capture_error = None
        self.capture_started = None

    def server(self):
        # Load the exact production function without importing server.py's global
        # cleanup/browser objects. Registration and client serialization are real.
        tree = ast.parse((ROOT / "src/stealth_browser_mcp/server.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == "take_screenshot")
        mcp = FastMCP("screenshot-wire-test")
        owner = self

        class Tab:
            async def save_screenshot(self, path, **options):
                owner.captures.append((Path(path), options))
                Path(path).write_bytes(owner.png)
                if owner.capture_error:
                    raise owner.capture_error
                if owner.capture_started:
                    owner.capture_started.set()
                    await asyncio.Event().wait()

        class Manager:
            async def get_tab(self, instance_id):
                return Tab() if instance_id == "owned" else None

        namespace = dict(
            __name__="screenshot_test", section_tool=lambda _: mcp.tool,
            browser_manager=Manager(), Path=Path, Any=Any, Dict=Dict, Optional=Optional, Union=Union,
            ToolResult=ToolResult, ImageContent=ImageContent, TextContent=TextContent,
            tempfile=tempfile, os=os, base64=base64, json=json, DEFAULT_QUALITY=DEFAULT_QUALITY,
            fit_screenshot=fit_screenshot, validate_screenshot_options=validate_options,
            validate_clip_args=validate_clip_args,
            resolve_element_clip=resolve_element_clip,
            capture_clip_screenshot=capture_clip_screenshot,
            compute_clip_from_rect=compute_clip_from_rect,
            screenshot_clip=screenshot_clip,
        )
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / "src/stealth_browser_mcp/server.py"), "exec"), namespace)
        return mcp

    def call(self, **kwargs):
        async def run():
            async with Client(self.server()) as client:
                result = await client.call_tool("take_screenshot", {"instance_id": "owned", **kwargs}, raise_on_error=False)
                # Exercise the actual MCP CallToolResult JSON representation.
                wire = CallToolResult(content=result.content, structuredContent=result.structured_content,
                                      isError=result.is_error).model_dump_json()
                return CallToolResult.model_validate_json(wire)
        return asyncio.run(run())

    def test_png_is_an_image_on_mcp_wire_with_complete_hash_metadata(self):
        result = self.call()
        self.assertFalse(result.isError)
        self.assertEqual(len(result.content), 2)
        image = result.content[0]
        self.assertIsInstance(image, ImageContent)
        self.assertEqual(image.mimeType, "image/png")
        data = base64.b64decode(image.data, validate=True)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (96, 64))
        self.assertEqual(result.structuredContent["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result.structuredContent["bytes"], len(data))
        self.assertTrue(result.structuredContent["complete"])
        self.assertFalse(self.captures[0][0].exists())
        self.assertNotIn("file_path", result.structuredContent)
        self.assertEqual(json.loads(result.content[1].text), result.structuredContent)

    def test_jpg_alias_has_correct_mime_and_flattens_transparency(self):
        result = self.call(format="jpg", full_page=True)
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0].mimeType, "image/jpeg")
        image = Image.open(io.BytesIO(base64.b64decode(result.content[0].data)))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.mode, "RGB")
        self.assertTrue(all(abs(a - b) < 4 for a, b in zip(image.getpixel((30, 30)), (142, 157, 172))))
        self.assertTrue(self.captures[0][1]["full_page"])

    def test_stdio_client_receives_image_without_server_file_access(self):
        async def run():
            transport = PythonStdioTransport(
                Path(__file__), args=["--serve-fixture"],
                python_cmd=sys.executable, cwd=self.directory.name, keep_alive=False,
            )
            async with Client(transport) as client:
                self.assertIn("take_screenshot", [tool.name for tool in await client.list_tools()])
                return await client.call_tool("take_screenshot", {"instance_id": "owned"})

        result = asyncio.run(run())
        self.assertEqual(len(result.content), 2)
        self.assertIsInstance(result.content[0], ImageContent)
        image = base64.b64decode(result.content[0].data, validate=True)
        self.assertEqual(Image.open(io.BytesIO(image)).size, (96, 64))
        self.assertEqual(result.structured_content["sha256"], hashlib.sha256(image).hexdigest())

    def test_near_limit_image_crosses_stdio_without_truncation(self):
        async def run():
            transport = PythonStdioTransport(
                Path(__file__), args=["--serve-large-fixture"],
                python_cmd=sys.executable, cwd=self.directory.name, keep_alive=False,
            )
            async with Client(transport) as client:
                return await client.call_tool("take_screenshot", {"instance_id": "owned"})
        result = asyncio.run(run())
        image = base64.b64decode(result.content[0].data, validate=True)
        self.assertGreater(len(image), screenshot_encoder.INLINE_MAX_BYTES * .9)
        self.assertLessEqual(len(image), screenshot_encoder.INLINE_MAX_BYTES)
        self.assertEqual(Image.open(io.BytesIO(image)).size, (1100, 1100))
        self.assertEqual(result.structured_content["scale"], 1.0)
        self.assertEqual(result.structured_content["sha256"], hashlib.sha256(image).hexdigest())

    def test_explicit_file_export_stays_available(self):
        target = Path(self.directory.name) / "exports" / "capture.png"
        result = self.call(file_path=str(target))
        self.assertFalse(result.isError)
        self.assertTrue(target.is_file())
        self.assertIn(str(target), result.content[0].text)
        self.assertNotIn("Read tool", result.content[0].text)

    def test_invalid_format_fails_before_capture(self):
        self.assertTrue(self.call(format="gif").isError)
        self.assertEqual(self.captures, [])

    def test_oversized_capture_fails_and_removes_temporary_file(self):
        self.png = b"x" * 2048
        with patch.object(screenshot_encoder, "MAX_CAPTURE_BYTES", 1024):
            result = self.call()
        self.assertTrue(result.isError)
        self.assertIn("inline capture limit", result.content[0].text)
        self.assertFalse(self.captures[0][0].exists())

    def test_excessive_pixels_are_rejected_before_decode(self):
        header = self.png[12:16] + struct.pack(">II", 4097, 4097) + self.png[24:29]
        self.png = self.png[:12] + header + struct.pack(">I", zlib.crc32(header)) + self.png[33:]
        with patch.object(screenshot_encoder, "MAX_DECODE_PIXELS", 16 * 1024 * 1024):
            result = self.call()
        self.assertTrue(result.isError)
        self.assertIn("dimensions exceed the decode limit", result.content[0].text)
        self.assertFalse(self.captures[0][0].exists())

    def test_capture_failure_removes_temporary_file(self):
        self.capture_error = RuntimeError("Synthetic capture failure")
        self.assertTrue(self.call().isError)
        self.assertFalse(self.captures[0][0].exists())

    def test_cancelled_capture_removes_temporary_file(self):
        async def run():
            self.capture_started = asyncio.Event()
            async with Client(self.server()) as client:
                task = asyncio.create_task(client.call_tool("take_screenshot", {"instance_id": "owned"}))
                await asyncio.wait_for(self.capture_started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        asyncio.run(run())
        self.assertFalse(self.captures[0][0].exists())

    def test_encoded_output_limit_is_not_silently_spilled_or_truncated(self):
        # A synthetic encoder result tests the precise post-compression boundary.
        # Input remains a valid small capture; only output encoding is mocked.
        self.png = ScreenshotContentTests.small_png()
        oversized = b"x" * (screenshot_encoder.INLINE_MAX_BYTES + 1)
        with patch.object(Image.Image, "save", lambda _, stream, **kw: stream.write(oversized)):
            result = self.call()
        self.assertTrue(result.isError)
        self.assertIn("could not be reduced", result.content[0].text)
        self.assertFalse(self.captures[0][0].exists())

    def test_tall_full_page_is_scaled_instead_of_failing(self):
        buffer = io.BytesIO()
        Image.new("RGB", (400, 20000), (200, 10, 10)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        result = self.call(full_page=True)
        self.assertFalse(result.isError)
        meta = result.structuredContent
        self.assertEqual((meta["width"], meta["height"]), (160, screenshot_encoder.MAX_IMAGE_SIDE))
        self.assertEqual((meta["original_width"], meta["original_height"]), (400, 20000))
        self.assertEqual(meta["scale"], 0.4)
        image = Image.open(io.BytesIO(base64.b64decode(result.content[0].data)))
        self.assertEqual(image.size, (160, 8000))

    def test_large_encoding_is_scaled_to_fit_byte_limit(self):
        buffer = io.BytesIO()
        Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        with patch.object(screenshot_encoder, "INLINE_MAX_BYTES", 300_000):
            result = self.call()
        self.assertFalse(result.isError)
        self.assertLessEqual(result.structuredContent["bytes"], 300_000)
        self.assertLess(result.structuredContent["scale"], 1.0)

    def test_max_width_and_quality(self):
        buffer = io.BytesIO()
        Image.frombytes("RGB", (800, 400), os.urandom(800 * 400 * 3)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        small = self.call(format="jpeg", quality=20, max_width=200).structuredContent
        large = self.call(format="jpeg", quality=95, max_width=200).structuredContent
        self.assertEqual((small["width"], small["height"], small["scale"]), (200, 100, 0.25))
        self.assertEqual((small["quality"], large["quality"]), (20, 95))
        self.assertLess(small["bytes"], large["bytes"])
        self.assertIsNone(self.call(format="png").structuredContent["quality"])
        self.assertEqual(self.call(max_width=5000).structuredContent["scale"], 1.0)

    def test_capture_is_lossless_before_encoding(self):
        self.call(format="jpeg")
        self.assertEqual(self.captures[0][1]["format"], "png")

    def test_invalid_quality_or_width_fails_before_capture(self):
        for options in ({"quality": 0}, {"quality": 101}, {"max_width": 0}):
            self.assertTrue(self.call(**options).isError, options)
        self.assertEqual(self.captures, [])

    @staticmethod
    def small_png():
        buffer = io.BytesIO()
        Image.new("RGB", (1, 1)).save(buffer, format="PNG")
        return buffer.getvalue()


if __name__ == "__main__":
    if sys.argv[1:] in (["--serve-fixture"], ["--serve-large-fixture"]):
        fixture = ScreenshotContentTests()
        fixture.setUp()
        if sys.argv[1] == "--serve-large-fixture":
            buffer = io.BytesIO()
            Image.frombytes("RGB", (1100, 1100), os.urandom(1100 * 1100 * 3)).save(buffer, format="PNG")
            fixture.png = buffer.getvalue()
        try:
            fixture.server().run(show_banner=False)
        finally:
            fixture.doCleanups()
    else:
        unittest.main()
