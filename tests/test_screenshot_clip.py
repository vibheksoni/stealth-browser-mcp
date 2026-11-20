"""Tests for element and region screenshots (SB-71).

Includes unit tests for pure helpers (validate_clip_args, compute_clip_from_rect),
fake-tab unit tests for the tool execution path, and gated real-browser tests
verifying selector and clip screenshots against local HTML fixtures.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
from stealth_browser_mcp.process_cleanup import process_cleanup
from stealth_browser_mcp.screenshot_clip import (
    capture_clip_screenshot,
    compute_clip_from_rect,
    resolve_element_clip,
    validate_clip_args,
)
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    """Safely remove a temporary directory with retry fallback."""
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


def _make_png_bytes(width: int = 100, height: int = 50) -> bytes:
    """Create a minimal valid PNG image of the requested size."""
    img = Image.new("RGBA", (width, height), (0, 128, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestScreenshotClipHelpers(unittest.TestCase):
    """Ungated unit tests for validate_clip_args and compute_clip_from_rect."""

    def test_both_args_error(self) -> None:
        """Verify error when both selector and clip are provided."""
        with self.assertRaises(ValueError) as cm:
            validate_clip_args(
                selector="#save-btn",
                clip={"x": 10, "y": 20, "width": 100, "height": 50},
            )
        self.assertIn("mutually exclusive", str(cm.exception))

    def test_invalid_size_zero_or_negative(self) -> None:
        """Verify errors when clip width or height is zero or negative."""
        with self.assertRaises(ValueError) as cm:
            validate_clip_args(clip={"x": 0, "y": 0, "width": 0, "height": 50})
        self.assertIn("positive numbers", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            validate_clip_args(clip={"x": 0, "y": 0, "width": -10, "height": 50})
        self.assertIn("positive numbers", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            validate_clip_args(clip={"x": 0, "y": 0, "width": 50, "height": 0})
        self.assertIn("positive numbers", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            validate_clip_args(clip={"x": 0, "y": 0, "width": 50, "height": -5})
        self.assertIn("positive numbers", str(cm.exception))

    def test_invalid_clip_keys_or_types(self) -> None:
        """Verify errors on missing keys, non-numeric values, or non-dict clip."""
        with self.assertRaises(ValueError):
            validate_clip_args(clip={"x": 0, "y": 0, "width": 100})  # missing height

        with self.assertRaises(ValueError):
            validate_clip_args(clip={"x": "bad", "y": 0, "width": 100, "height": 50})

        with self.assertRaises(ValueError):
            validate_clip_args(clip="not-a-dict")

    def test_valid_clip_and_selector(self) -> None:
        """Verify valid inputs return expected normalization."""
        # Valid clip returns float dictionary
        res = validate_clip_args(clip={"x": 10, "y": 20, "width": 100, "height": 50})
        self.assertEqual(res, {"x": 10.0, "y": 20.0, "width": 100.0, "height": 50.0})

        # Single positional dict argument is recognized as clip
        res_pos = validate_clip_args({"x": 5, "y": 15, "width": 80, "height": 40})
        self.assertEqual(res_pos, {"x": 5.0, "y": 15.0, "width": 80.0, "height": 40.0})

        # Valid selector returns None
        self.assertIsNone(validate_clip_args(selector="#my-element"))

        # Both None returns None
        self.assertIsNone(validate_clip_args(None, None))

        # Empty selector raises ValueError
        with self.assertRaises(ValueError):
            validate_clip_args(selector="")
        with self.assertRaises(ValueError):
            validate_clip_args(selector="   ")

    def test_rect_plus_scroll_to_page_clip(self) -> None:
        """Verify compute_clip_from_rect calculates page-relative coordinates."""
        rect = {"x": 50.0, "y": 100.0, "width": 200.0, "height": 80.0}
        scroll = {"x": 10.0, "y": 25.0}
        clip = compute_clip_from_rect(rect, scroll)
        self.assertEqual(clip, {"x": 60.0, "y": 125.0, "width": 200.0, "height": 80.0})

        # Test with left/top and scrollX/scrollY
        rect_lt = {"left": 40.0, "top": 60.0, "width": 120.0, "height": 90.0}
        scroll_alt = {"scrollX": 15.0, "scrollY": 30.0}
        clip_lt = compute_clip_from_rect(rect_lt, scroll_alt)
        self.assertEqual(clip_lt, {"x": 55.0, "y": 90.0, "width": 120.0, "height": 90.0})

        # Test with numeric scroll arguments
        clip_num = compute_clip_from_rect(rect, scroll=10.0, scroll_y=25.0)
        self.assertEqual(clip_num, {"x": 60.0, "y": 125.0, "width": 200.0, "height": 80.0})

        # Test with zero scroll
        clip_zero = compute_clip_from_rect(rect)
        self.assertEqual(clip_zero, {"x": 50.0, "y": 100.0, "width": 200.0, "height": 80.0})

    def test_zero_size_element_error(self) -> None:
        """Verify compute_clip_from_rect raises on zero or negative dimensions."""
        with self.assertRaises(ValueError) as cm:
            compute_clip_from_rect({"x": 10, "y": 10, "width": 0, "height": 50})
        self.assertIn("zero or negative size", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            compute_clip_from_rect({"x": 10, "y": 10, "width": 50, "height": 0})
        self.assertIn("zero or negative size", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            compute_clip_from_rect({"x": 10, "y": 10, "width": -5, "height": 50})
        self.assertIn("zero or negative size", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            compute_clip_from_rect({"x": 10, "y": 10, "width": 50, "height": -5})
        self.assertIn("zero or negative size", str(cm.exception))


class FakeTab:
    """Fake nodriver Tab for unit testing screenshot execution paths."""

    def __init__(
        self,
        rect: Optional[Dict[str, float]] = None,
        scroll: Optional[Dict[str, float]] = None,
        not_found: bool = False,
        invalid_selector: bool = False,
    ) -> None:
        self.rect = rect or {"x": 10.0, "y": 20.0, "width": 100.0, "height": 50.0}
        self.scroll = scroll or {"x": 5.0, "y": 10.0}
        self.not_found = not_found
        self.invalid_selector = invalid_selector
        self.sent_commands: list = []

    async def evaluate(self, expr: str, **kwargs: Any) -> Any:
        if self.invalid_selector:
            return {"error": "invalid_selector", "message": "SyntaxError: not a valid selector"}
        if self.not_found:
            return {"error": "not_found"}
        return {
            "found": True,
            "rect": self.rect,
            "scroll": self.scroll,
        }

    async def send(self, cmd: Any) -> Any:
        self.sent_commands.append(cmd)
        w = max(1, int(self.rect.get("width", 100)))
        h = max(1, int(self.rect.get("height", 50)))
        png_bytes = _make_png_bytes(w, h)
        return base64.b64encode(png_bytes).decode("ascii")


class TestScreenshotClipFakeTab(unittest.IsolatedAsyncioTestCase):
    """Ungated unit tests for tool paths using FakeTab."""

    async def test_take_screenshot_with_selector_fake_tab(self) -> None:
        """Verify take_screenshot with selector on a fake tab."""
        fake_tab = FakeTab(
            rect={"x": 10.0, "y": 20.0, "width": 120.0, "height": 60.0},
            scroll={"x": 5.0, "y": 15.0},
        )
        with patch.object(server.browser_manager, "get_tab", return_value=fake_tab):
            res = await server.take_screenshot.fn("inst1", selector="#my-btn")

        self.assertIsNotNone(res)
        self.assertIn("structured_content", dir(res))
        meta = res.structured_content
        self.assertEqual(meta.get("selector"), "#my-btn")
        self.assertEqual(meta.get("region"), {"x": 15.0, "y": 35.0, "width": 120.0, "height": 60.0})

        # Verify image content and dimensions with struct
        image_content = next(c for c in res.content if getattr(c, "type", None) == "image")
        png_bytes = base64.b64decode(image_content.data)
        self.assertEqual(png_bytes[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png_bytes[16:24])
        self.assertEqual(w, 120)
        self.assertEqual(h, 60)

    async def test_take_screenshot_with_clip_fake_tab(self) -> None:
        """Verify take_screenshot with clip on a fake tab."""
        fake_tab = FakeTab()
        clip_input = {"x": 12.0, "y": 34.0, "width": 150.0, "height": 75.0}
        with patch.object(server.browser_manager, "get_tab", return_value=fake_tab):
            res = await server.take_screenshot.fn("inst1", clip=clip_input)

        meta = res.structured_content
        self.assertEqual(meta.get("region"), clip_input)
        self.assertNotIn("selector", meta)

    async def test_take_screenshot_file_path_fake_tab(self) -> None:
        """Verify take_screenshot with file_path writes the file and records path."""
        fake_tab = FakeTab()
        with tempfile.TemporaryDirectory() as td:
            save_file = Path(td) / "custom_capture.png"
            with patch.object(server.browser_manager, "get_tab", return_value=fake_tab):
                res = await server.take_screenshot.fn(
                    "inst1",
                    selector="#save",
                    file_path=str(save_file),
                )
            self.assertTrue(save_file.exists())
            self.assertGreater(save_file.stat().st_size, 0)
            meta = res.structured_content
            self.assertEqual(meta.get("path"), str(save_file.absolute()))
            self.assertEqual(meta.get("selector"), "#save")
            self.assertIn("region", meta)

    async def test_take_screenshot_selector_not_found(self) -> None:
        """Verify clear error message when selector element is not found."""
        fake_tab = FakeTab(not_found=True)
        with patch.object(server.browser_manager, "get_tab", return_value=fake_tab):
            with self.assertRaises(Exception) as cm:
                await server.take_screenshot.fn("inst1", selector="#nonexistent")
            err_msg = str(cm.exception)
            self.assertIn("#nonexistent", err_msg)
            self.assertIn("query_elements", err_msg)

    async def test_take_screenshot_both_args_error(self) -> None:
        """Verify mutual exclusivity error via take_screenshot."""
        with self.assertRaises(ValueError) as cm:
            await server.take_screenshot.fn(
                "inst1",
                selector="#foo",
                clip={"x": 0, "y": 0, "width": 10, "height": 10},
            )
        self.assertIn("mutually exclusive", str(cm.exception))


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestScreenshotClipGated(unittest.IsolatedAsyncioTestCase):
    """Gated real-browser integration tests for element and region screenshots."""

    async def asyncSetUp(self) -> None:
        """Start local fixture server and spawn headless browser instance."""
        self.server = FixtureServer().start()
        self.temp_dir = tempfile.mkdtemp(prefix="stealth_clip_")
        self.manager = server.browser_manager
        options = BrowserOptions(
            headless=True,
            user_data_dir=self.temp_dir,
            sandbox=False if sandbox_must_be_disabled() else True,
        )
        self.instance = await self.manager.spawn_browser(options)
        self.instance_id = self.instance.instance_id
        await self.manager.navigate(
            self.instance_id,
            self.server.url_for("project_tool.html#/new"),
            wait_until="load",
            timeout=15000,
        )
        self.tab = await self.manager.get_tab(self.instance_id)

    async def asyncTearDown(self) -> None:
        """Clean up browser instance, processes, temporary directory, and server."""
        if hasattr(self, "instance") and self.instance:
            try:
                await self.manager.close_instance(self.instance.instance_id)
            except Exception:
                pass
        try:
            for pid in process_cleanup._get_browser_pids_for_profile(self.temp_dir):
                process_cleanup._kill_process_by_pid(pid, "test_screenshot_clip")
        except Exception:
            pass
        _safe_cleanup_dir(self.temp_dir)
        if hasattr(self, "server") and self.server:
            self.server.stop()

    async def test_real_browser_screenshot_by_selector(self) -> None:
        """Capture screenshot of #save button by selector and verify dimensions."""
        res = await server.take_screenshot.fn(self.instance_id, selector="#save")
        self.assertIsNotNone(res)
        meta = res.structured_content
        self.assertEqual(meta.get("selector"), "#save")
        self.assertIn("region", meta)
        region = meta["region"]
        self.assertGreater(region["width"], 0)
        self.assertGreater(region["height"], 0)

        image_content = next(c for c in res.content if getattr(c, "type", None) == "image")
        png_bytes = base64.b64decode(image_content.data)
        self.assertEqual(png_bytes[:8], b"\x89PNG\r\n\x1a\n")

        # Read PNG width and height from IHDR chunk with struct
        img_w, img_h = struct.unpack(">II", png_bytes[16:24])
        self.assertAlmostEqual(img_w, region["width"], delta=2)
        self.assertAlmostEqual(img_h, region["height"], delta=2)

    async def test_real_browser_screenshot_by_clip(self) -> None:
        """Capture screenshot of a page region by clip and verify dimensions."""
        clip_req = {"x": 10.0, "y": 10.0, "width": 160.0, "height": 80.0}
        res = await server.take_screenshot.fn(self.instance_id, clip=clip_req)
        self.assertIsNotNone(res)
        meta = res.structured_content
        self.assertEqual(meta.get("region"), clip_req)
        self.assertNotIn("selector", meta)

        image_content = next(c for c in res.content if getattr(c, "type", None) == "image")
        png_bytes = base64.b64decode(image_content.data)
        self.assertEqual(png_bytes[:8], b"\x89PNG\r\n\x1a\n")

        img_w, img_h = struct.unpack(">II", png_bytes[16:24])
        self.assertAlmostEqual(img_w, 160, delta=2)
        self.assertAlmostEqual(img_h, 80, delta=2)

    async def test_real_browser_selector_not_found(self) -> None:
        """Verify clear error message when selector is missing on the live page."""
        with self.assertRaises(Exception) as cm:
            await server.take_screenshot.fn(self.instance_id, selector="#missing-save-element")
        err_msg = str(cm.exception)
        self.assertIn("#missing-save-element", err_msg)
        self.assertIn("query_elements", err_msg)


if __name__ == "__main__":
    unittest.main()
