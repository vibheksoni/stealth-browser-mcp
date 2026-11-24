"""SB-85 / review F16: execute_cdp_command must not bypass F7 URL or write rules."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stealth_browser_mcp.cdp_function_executor import CDPFunctionExecutor
from stealth_browser_mcp.cdp_policy import (
    CdpPolicyError,
    canonicalize_cdp_command,
    prepare_cdp_command,
    raw_cdp_enabled,
    refuse_raw_cdp_off_loopback,
)


class CanonicalizeTests(unittest.TestCase):
    def test_prefixes_and_case(self) -> None:
        self.assertEqual(canonicalize_cdp_command("evaluate"), "runtime.evaluate")
        self.assertEqual(canonicalize_cdp_command("Runtime.evaluate"), "runtime.evaluate")
        self.assertEqual(canonicalize_cdp_command("Page.navigate"), "page.navigate")
        self.assertEqual(canonicalize_cdp_command("page.Navigate"), "page.navigate")
        self.assertEqual(canonicalize_cdp_command("setDownloadBehavior"), "page.set_download_behavior")
        self.assertEqual(
            canonicalize_cdp_command("Page.setDownloadBehavior"),
            "page.set_download_behavior",
        )
        self.assertEqual(
            canonicalize_cdp_command("Browser.setDownloadBehavior"),
            "browser.set_download_behavior",
        )
        self.assertEqual(canonicalize_cdp_command("callFunctionOn"), "runtime.call_function_on")
        self.assertEqual(canonicalize_cdp_command("captureScreenshot"), "page.capture_screenshot")


class AllowlistTests(unittest.TestCase):
    def test_evaluate_and_screenshot_allowed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            prepare_cdp_command("Runtime.evaluate", {"expression": "1+1"})
            prepare_cdp_command("Page.captureScreenshot", {"format": "png"})
            prepare_cdp_command("getLayoutMetrics", {})
            prepare_cdp_command("Page.reload", {})

    def test_off_list_method_requires_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            with self.assertRaisesRegex(CdpPolicyError, "allowlist"):
                prepare_cdp_command("Page.setBypassCSP", {"enabled": True})
            with self.assertRaisesRegex(CdpPolicyError, "allowlist"):
                prepare_cdp_command("Page.setDocumentContent", {"html": "<p>"})
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_RAW_CDP": "1"}):
            prepare_cdp_command("Page.setBypassCSP", {"enabled": True})


class ReviewNavigateExploitTests(unittest.TestCase):
    """Live F16 exploit: Page.navigate to file:///etc/hostname and chrome://version."""

    def test_file_and_chrome_urls_are_blocked(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            for command, url in (
                ("Page.navigate", "file:///etc/hostname"),
                ("page.navigate", "FILE:///etc/hostname"),
                ("navigate", "file:///etc/hostname"),
                ("Page.navigate", "chrome://version"),
                ("Page.navigate", "devtools://devtools"),
            ):
                with self.subTest(command=command, url=url):
                    with self.assertRaises(CdpPolicyError):
                        prepare_cdp_command(command, {"url": url})

    def test_file_url_still_blocked_with_raw_cdp(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"STEALTH_ALLOW_RAW_CDP": "1"},
            clear=False,
        ):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            with self.assertRaises(CdpPolicyError):
                prepare_cdp_command("Page.navigate", {"url": "file:///etc/hostname"})

    def test_https_navigate_allowed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
            prepare_cdp_command("Page.navigate", {"url": "https://example.com/"})


class ReviewDownloadExploitTests(unittest.TestCase):
    """Live F16 exploit: Page.setDownloadBehavior writes outside the data dir."""

    def test_set_download_behavior_blocked_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            with self.assertRaisesRegex(CdpPolicyError, "setDownloadBehavior"):
                prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": "/tmp/secrev2/out/dl"},
                )

    def test_raw_cdp_still_rejects_path_outside_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            outside = Path(tmp) / "out" / "dl"
            with mock.patch.dict(
                os.environ,
                {
                    "STEALTH_ALLOW_RAW_CDP": "1",
                    "STEALTH_BROWSER_MCP_DATA_DIR": str(data),
                },
            ):
                with self.assertRaisesRegex(CdpPolicyError, "data directory"):
                    prepare_cdp_command(
                        "Page.setDownloadBehavior",
                        {"behavior": "allow", "download_path": str(outside)},
                    )
                confined = data / "downloads"
                confined.mkdir()
                params = prepare_cdp_command(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": str(confined)},
                )
                self.assertTrue(
                    Path(params["download_path"]).resolve().is_relative_to(data.resolve())
                )


class RawCdpFlagTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            self.assertFalse(raw_cdp_enabled())

    def test_refuse_off_loopback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
            refuse_raw_cdp_off_loopback("0.0.0.0")
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_RAW_CDP": "1"}):
            refuse_raw_cdp_off_loopback("127.0.0.1")
            refuse_raw_cdp_off_loopback("localhost")
            with self.assertRaisesRegex(ValueError, "STEALTH_ALLOW_RAW_CDP"):
                refuse_raw_cdp_off_loopback("0.0.0.0")
            with self.assertRaisesRegex(ValueError, "STEALTH_ALLOW_RAW_CDP"):
                refuse_raw_cdp_off_loopback("192.168.1.10")


class ExecutorDoesNotSendBlockedCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_navigate_never_reaches_chrome(self) -> None:
        tab = mock.Mock()
        tab.send = mock.AsyncMock()
        executor = CDPFunctionExecutor()
        with mock.patch.object(executor, "enable_page", new=mock.AsyncMock()):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STEALTH_ALLOW_FILE_URLS", None)
                os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
                result = await executor.execute_cdp_command(
                    tab,
                    "Page.navigate",
                    {"url": "file:///etc/hostname"},
                )
        self.assertFalse(result["success"])
        self.assertIn("file", result["error"].lower())
        tab.send.assert_not_called()

    async def test_set_download_behavior_never_reaches_chrome(self) -> None:
        tab = mock.Mock()
        tab.send = mock.AsyncMock()
        executor = CDPFunctionExecutor()
        with mock.patch.object(executor, "enable_page", new=mock.AsyncMock()):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STEALTH_ALLOW_RAW_CDP", None)
                result = await executor.execute_cdp_command(
                    tab,
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "download_path": "/tmp/secrev2/out/dl"},
                )
        self.assertFalse(result["success"])
        self.assertIn("setDownloadBehavior", result["error"])
        tab.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
