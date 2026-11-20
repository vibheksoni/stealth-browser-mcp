"""Tests for downloads directory configuration, wait_for_download, path escape rejection, and drop_files."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc
from fastmcp import Client

from stealth_browser_mcp import downloads, paths, server
from stealth_browser_mcp.downloads import (
    DownloadManager,
    drop_files,
    ensure_download_directory,
    get_session_download_dir,
    validate_download_target,
    validate_drop_paths,
    validate_pattern,
    wait_for_download,
)
from tests.browser_harness.server import PAGES_DIR, FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}


class RecordingTab:
    """Tab that records CDP commands and answers DOM/Runtime calls for unit tests."""

    def __init__(self, quads=None, backend_node_id=42):
        self.events: List[Any] = []
        self.quads = quads if quads is not None else [[10, 20, 30, 20, 30, 40, 10, 40]]
        self.backend_node_id = backend_node_id
        self.handlers = {}

    def add_handler(self, event_class, handler):
        self.handlers.setdefault(event_class, []).append(handler)

    async def send(self, command):
        request = next(command)
        method, params = request["method"], request.get("params", {})
        self.events.append((method, params))
        if method == "DOM.getContentQuads":
            return self.quads
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj"))
        if method == "DOM.querySelector":
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            return SimpleNamespace(backend_node_id=uc.cdp.dom.BackendNodeId(self.backend_node_id))
        if method == "Runtime.evaluate":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("elem-obj-1")), None
        return None

    async def evaluate(self, expression):
        self.events.append(("evaluate", expression))
        return True


class TestDownloadsUnitUngated(unittest.TestCase):
    """Ungated unit tests for downloads module running without real browser or network."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="stealth_dl_test_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ensure_download_directory_mode_0700(self):
        target = Path(self.test_dir) / "session_dl"
        created = ensure_download_directory(target)
        self.assertTrue(created.is_dir())
        if os.name == "posix":
            mode = created.stat().st_mode & 0o777
            self.assertEqual(mode, 0o700)

    def test_get_session_download_dir(self):
        session_base = Path(self.test_dir) / "profile_session"
        session_base.mkdir(parents=True, exist_ok=True)
        dl_dir = get_session_download_dir("inst_1", base_dir=session_base)
        self.assertTrue(dl_dir.is_dir())
        self.assertEqual(dl_dir, session_base / "downloads")

    def test_path_escape_rejected_patterns(self):
        invalid_patterns = [
            "../secret.txt",
            "../../etc/passwd",
            "/etc/shadow",
            "\\windows\\system32",
            "C:\\autoexec.bat",
            "foo/../../escape.txt",
            "sub/../bar",
        ]
        for pattern in invalid_patterns:
            with self.subTest(pattern=pattern):
                with self.assertRaises(ValueError) as ctx:
                    validate_pattern(pattern)
                self.assertIn("Path escape rejected", str(ctx.exception))

        valid_patterns = ["*.csv", "data_*.pdf", "archive.tar.gz", "simple.txt", None]
        for pattern in valid_patterns:
            with self.subTest(pattern=pattern):
                self.assertEqual(validate_pattern(pattern), pattern)

    def test_path_escape_rejected_targets(self):
        session_dir = Path(self.test_dir) / "session_dir"
        session_dir.mkdir(parents=True, exist_ok=True)

        invalid_targets = [
            "../outside.txt",
            "../../etc/passwd",
            "/tmp/outside_file.txt",
            "nested/../../secret",
        ]
        for target in invalid_targets:
            with self.subTest(target=target):
                with self.assertRaises(ValueError) as ctx:
                    validate_download_target(target, session_dir)
                self.assertIn("Path escape rejected", str(ctx.exception))

        valid = validate_download_target("downloaded_report.csv", session_dir)
        self.assertEqual(valid, session_dir / "downloaded_report.csv")

    def test_wait_for_download_finds_file_in_session_dir(self):
        session_dir = Path(self.test_dir) / "session_downloads"
        session_dir.mkdir(parents=True, exist_ok=True)
        mgr = DownloadManager()
        mgr.set_download_dir("test_instance_1", session_dir)

        test_file = session_dir / "report_2026.csv"
        test_file.write_text("id,val\n1,100\n", encoding="utf-8")

        async def run():
            return await wait_for_download(
                instance="test_instance_1",
                timeout=2.0,
                filename_pattern="report_*.csv",
            )

        downloads.download_manager = mgr
        result = asyncio.run(run())
        self.assertTrue(result["success"])
        self.assertEqual(result["filename"], "report_2026.csv")
        self.assertEqual(result["file_path"], str(test_file.resolve()))
        self.assertGreater(result["file_size"], 0)
        self.assertEqual(result["download_dir"], str(session_dir.resolve()))

    def test_wait_for_download_ignores_crdownload_until_finished(self):
        session_dir = Path(self.test_dir) / "dl_cr"
        session_dir.mkdir(parents=True, exist_ok=True)
        mgr = DownloadManager()
        mgr.set_download_dir("test_instance_cr", session_dir)

        incomplete = session_dir / "data.zip.crdownload"
        incomplete.write_text("partial data", encoding="utf-8")

        async def simulate_download_finish():
            await asyncio.sleep(0.08)
            completed = session_dir / "data.zip"
            incomplete.rename(completed)

        async def run():
            downloads.download_manager = mgr
            task = asyncio.create_task(simulate_download_finish())
            res = await wait_for_download(
                instance="test_instance_cr",
                timeout=2.0,
                filename_pattern="data.zip",
            )
            await task
            return res

        result = asyncio.run(run())
        self.assertTrue(result["success"])
        self.assertEqual(result["filename"], "data.zip")

    def test_wait_for_download_timeout_raises(self):
        session_dir = Path(self.test_dir) / "dl_empty"
        session_dir.mkdir(parents=True, exist_ok=True)
        mgr = DownloadManager()
        mgr.set_download_dir("test_instance_empty", session_dir)
        downloads.download_manager = mgr

        async def run():
            return await wait_for_download(
                instance="test_instance_empty",
                timeout=0.1,
                filename_pattern="nonexistent_*.pdf",
            )

        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(run())
        self.assertIn("Download timed out", str(ctx.exception))

    def test_wait_for_download_rejects_path_escape(self):
        session_dir = Path(self.test_dir) / "dl_sec"
        session_dir.mkdir(parents=True, exist_ok=True)
        mgr = DownloadManager()
        mgr.set_download_dir("test_instance_sec", session_dir)
        downloads.download_manager = mgr

        async def run_pattern():
            return await wait_for_download(
                instance="test_instance_sec",
                timeout=1.0,
                filename_pattern="../evil.txt",
            )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run_pattern())
        self.assertIn("Path escape rejected", str(ctx.exception))

        async def run_target():
            return await wait_for_download(
                instance="test_instance_sec",
                timeout=1.0,
                target_path="/etc/passwd",
            )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run_target())
        self.assertIn("Path escape rejected", str(ctx.exception))

    def test_cdp_set_download_behavior_sent(self):
        tab = RecordingTab()
        mgr = DownloadManager()
        session_dir = Path(self.test_dir) / "cdp_test"

        async def run():
            return await mgr.configure_download_directory(tab, "inst_cdp", download_dir=session_dir)

        configured = asyncio.run(run())
        self.assertEqual(configured, session_dir.resolve())
        self.assertEqual(mgr.get_download_dir("inst_cdp"), session_dir.resolve())

        methods = [ev[0] for ev in tab.events]
        self.assertIn("Browser.setDownloadBehavior", methods)
        params = [ev[1] for ev in tab.events if ev[0] == "Browser.setDownloadBehavior"][0]
        self.assertEqual(params["behavior"], "allow")
        self.assertEqual(params["downloadPath"], str(session_dir.resolve()))
        self.assertTrue(params["eventsEnabled"])

    def test_drop_files_cdp_and_datatransfer_events(self):
        tab = RecordingTab()
        file_path = Path(self.test_dir) / "sample_upload.txt"
        file_path.write_text("Sample file content for drop zone test", encoding="utf-8")

        async def run():
            return await drop_files(
                selector="#dropzone",
                paths=[str(file_path.resolve())],
                tab=tab,
            )

        res = asyncio.run(run())
        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["files"], ["sample_upload.txt"])

        methods = [ev[0] for ev in tab.events]
        self.assertIn("Input.dispatchDragEvent", methods)
        drag_events = [ev[1] for ev in tab.events if ev[0] == "Input.dispatchDragEvent"]
        types = [p.get("type") for p in drag_events]
        self.assertEqual(types, ["dragEnter", "dragOver", "drop"])
        self.assertIn(str(file_path.resolve()), drag_events[-1]["data"]["files"])

    def test_drop_files_rejects_path_escape_and_missing_file(self):
        tab = RecordingTab()

        async def run_escape():
            await drop_files("#dropzone", ["../nonexistent.txt"], tab=tab)

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run_escape())
        self.assertIn("Path escape rejected", str(ctx.exception))

        async def run_missing():
            await drop_files("#dropzone", [str(Path(self.test_dir) / "does_not_exist.txt")], tab=tab)

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(run_missing())
        self.assertIn("does not exist", str(ctx.exception))

    def test_download_manager_lifecycle(self):
        mgr = DownloadManager()
        tab = RecordingTab()
        options = SimpleNamespace(user_data_dir=str(Path(self.test_dir) / "session_profile"))

        async def run():
            await mgr.prepare_tab(tab, "inst_lifecycle", options)
            dl_dir = mgr.get_download_dir("inst_lifecycle")
            self.assertIsNotNone(dl_dir)
            self.assertTrue(dl_dir.is_dir())
            await mgr.forget_instance("inst_lifecycle")
            self.assertIsNone(mgr.get_download_dir("inst_lifecycle"))

        asyncio.run(run())

    def test_mcp_tool_registration(self):
        async def run():
            return await server.mcp.get_tools()

        tools = asyncio.run(run())
        self.assertIn("wait_for_download", tools)
        self.assertIn("drop_files", tools)
        self.assertIn("instance_id", tools["wait_for_download"].parameters["properties"])
        self.assertIn("selector", tools["drop_files"].parameters["properties"])
        self.assertIn("paths", tools["drop_files"].parameters["properties"])


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestDownloadsGated(unittest.TestCase):
    """Gated real-browser tests for downloads and drag-and-drop zones."""

    def test_real_browser_download_and_drop_files(self):
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        fixture_server = FixtureServer()
        fixture_server.start()
        try:
            probe_url = fixture_server.url_for("download.html")
            ready = False
            for _ in range(50):
                try:
                    with urllib.request.urlopen(probe_url, timeout=1.0) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.05)
            self.assertTrue(ready, "FixtureServer failed to start on 127.0.0.1")

            async def run_test():
                temp_profile = tempfile.mkdtemp(prefix="stealth_dl_browser_")
                browser = None
                try:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_profile,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)
                    tab = browser.main_tab

                    # 1. Configure download directory inside the session directory (mode 0700)
                    session_dl = Path(temp_profile) / "downloads"
                    await downloads.download_manager.configure_download_directory(
                        tab,
                        "real_browser_inst",
                        download_dir=session_dl,
                    )
                    self.assertTrue(session_dl.is_dir())
                    if os.name == "posix":
                        self.assertEqual(session_dl.stat().st_mode & 0o777, 0o700)

                    # 2. Navigate to download fixture page
                    await tab.get(fixture_server.url_for("download.html"))
                    title = await tab.evaluate("document.title")
                    self.assertEqual(title, "Download and Drop Fixture")

                    # 3. Trigger download via link click
                    download_link = await tab.select("#download-link")
                    self.assertIsNotNone(download_link)
                    await download_link.click()

                    # 4. Wait for download to land in session directory
                    dl_result = await wait_for_download(
                        instance="real_browser_inst",
                        timeout=10.0,
                        filename_pattern="*fixture_test.txt",
                    )
                    self.assertTrue(dl_result["success"])
                    downloaded_path = Path(dl_result["file_path"])
                    self.assertTrue(downloaded_path.is_file())
                    self.assertEqual(downloaded_path.parent.resolve(), session_dl.resolve())

                    content = downloaded_path.read_bytes()
                    self.assertIn(b"Stealth browser fixture download content", content)

                    # 5. Path escape rejected on wait_for_download
                    with self.assertRaises(ValueError):
                        await wait_for_download(
                            instance="real_browser_inst",
                            timeout=1.0,
                            filename_pattern="../evil_escape.txt",
                        )

                    with self.assertRaises(ValueError):
                        await wait_for_download(
                            instance="real_browser_inst",
                            timeout=1.0,
                            target_path="/etc/passwd",
                        )

                    # 6. Test drop_files onto drag-and-drop zone
                    drop_res = await drop_files(
                        selector="#dropzone",
                        paths=[str(downloaded_path.resolve())],
                        tab=tab,
                        instance_id="real_browser_inst",
                    )
                    self.assertTrue(drop_res["success"])
                    self.assertEqual(drop_res["count"], 1)

                    drop_text = await tab.evaluate("document.getElementById('drop-result').textContent")
                    self.assertIn("fixture_test.txt", drop_text)

                finally:
                    if browser is not None:
                        try:
                            if getattr(browser, "connection", None) is not None:
                                await asyncio.wait_for(
                                    browser.connection.send(uc.cdp.browser.close()),
                                    timeout=3.0,
                                )
                        except Exception:
                            pass
                        if getattr(browser, "_process", None) is not None:
                            try:
                                await _stop_process(browser._process)
                            except Exception:
                                pass
                        connections = [
                            getattr(t, "disconnect", None)
                            for t in list(getattr(browser, "targets", []) or [])
                        ]
                        if getattr(browser, "connection", None) is not None:
                            connections.append(getattr(browser.connection, "disconnect", None))
                        disconnects = [d() for d in connections if d is not None]
                        if disconnects:
                            try:
                                await asyncio.wait(
                                    [asyncio.ensure_future(item) for item in disconnects],
                                    timeout=2.0,
                                )
                            except Exception:
                                pass
                        browser._process = None

                    if os.path.exists(temp_profile):
                        shutil.rmtree(temp_profile, ignore_errors=True)

            asyncio.run(run_test())
        finally:
            fixture_server.stop()


if __name__ == "__main__":
    unittest.main()
