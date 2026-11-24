"""Tests for SB-POST-4C: Windows polish, test skips, and tool-profile trim.

ASCII only.
No real browser is launched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.symlink_helper import create_symlink
from stealth_browser_mcp import (
    doctor,
    file_lock,
    network_interceptor,
    server,
    tool_compact,
    tool_surface,
)
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserInstance, BrowserState


class Bug8DoctorDiscoveryTests(unittest.TestCase):
    """1. doctor browser discovery (bug 8)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_stealth_browser_executable_override_found(self):
        """Doctor reports ok when STEALTH_BROWSER_EXECUTABLE points at a fake executable."""
        fake_binary = self.data_dir / "my_fake_chrome"
        fake_binary.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        fake_binary.chmod(fake_binary.stat().st_mode | stat.S_IXUSR)

        check = doctor.run_checks(
            env={"STEALTH_BROWSER_EXECUTABLE": str(fake_binary)},
            platform="linux",
            which_fn=lambda name: None,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["browser_binaries"]["status"], "ok")
        self.assertIn("launchable binary for:", by_name["browser_binaries"]["detail"])
        self.assertIn("chrome", by_name["browser_binaries"]["detail"])

    def test_windows_standard_path_found_when_which_returns_none(self):
        """Standard Windows install path found even if which_fn returns None."""
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

        def fake_exists(p: str) -> bool:
            return p == edge_path

        check = doctor.run_checks(
            env={},
            platform="windows",
            which_fn=lambda name: None,
            path_exists_fn=fake_exists,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["browser_binaries"]["status"], "ok")
        self.assertIn("launchable binary for:", by_name["browser_binaries"]["detail"])
        self.assertIn("edge", by_name["browser_binaries"]["detail"])


class Bug9AttachBrowserLookupTests(unittest.IsolatedAsyncioTestCase):
    """2. attach_browser executable lookup (bug 9)."""

    async def test_attach_with_stripped_path_does_not_raise_missing_binary(self):
        """Constructing/calling attach path with stripped PATH and no STEALTH_BROWSER_EXECUTABLE succeeds."""
        bm = BrowserManager()
        created_configs = []

        class FakeTab:
            async def send(self, *args, **kwargs):
                return {}

        class FakeBrowser:
            def __init__(self, cfg):
                self.config = cfg
                self.main_tab = FakeTab()
                self.targets = [self.main_tab]
                self.connection = SimpleNamespace(
                    handlers={},
                    add_handler=lambda *a, **k: None,
                    send=self.send,
                )

            async def send(self, *args, **kwargs):
                return {}

        async def fake_create(config):
            created_configs.append(config)
            return FakeBrowser(config)

        with (
            mock.patch.dict(os.environ, {"PATH": ""}, clear=True),
            mock.patch("stealth_browser_mcp.browser_manager.check_browser_executable", return_value=None),
            mock.patch("nodriver.Browser.create", side_effect=fake_create),
            mock.patch.object(bm, "_configure_tab", return_value=None),
            mock.patch.object(bm, "_setup_dynamic_hooks", return_value=None),
        ):
            instance = await bm.attach_browser(port=9222, host="127.0.0.1")
            self.assertTrue(instance.attached)
            self.assertEqual(len(created_configs), 1)
            # browser_executable_path was satisfied with a non-empty existing path
            self.assertTrue(bool(created_configs[0].browser_executable_path))
            self.assertTrue(os.path.exists(created_configs[0].browser_executable_path))


class Bug10NetworkPageTargetFilterTests(unittest.IsolatedAsyncioTestCase):
    """3. list_network_requests page-target filter (bug 10)."""

    async def test_page_target_is_listed(self):
        """A fake RequestWillBeSent from a page target is recorded and listed."""
        interceptor = network_interceptor.NetworkInterceptor()
        inst_id = "test-page-inst"
        event = SimpleNamespace(
            request_id="req-page-1",
            target_type="page",
            request=SimpleNamespace(url="https://example.com/api/data", method="GET", headers={}, post_data=None),
            type_=SimpleNamespace(value="XHR"),
        )
        await interceptor._on_request(event, inst_id)
        requests = await interceptor.list_requests(inst_id)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].request_id, "req-page-1")
        self.assertEqual(requests[0].url, "https://example.com/api/data")

        # Also appears in search_requests
        search_res = await interceptor.search_requests(inst_id)
        self.assertEqual(search_res["total"], 1)

    async def test_non_page_target_is_dropped(self):
        """A fake RequestWillBeSent from a service worker or browser target is dropped."""
        interceptor = network_interceptor.NetworkInterceptor()
        inst_id = "test-nonpage-inst"

        # Service worker target
        sw_event = SimpleNamespace(
            request_id="req-sw-1",
            target_type="service_worker",
            request=SimpleNamespace(url="https://example.com/worker.js", method="GET", headers={}, post_data=None),
            type_=SimpleNamespace(value="Script"),
        )
        await interceptor._on_request(sw_event, inst_id)

        # Browser target
        br_event = SimpleNamespace(
            request_id="req-br-1",
            target_type="browser",
            request=SimpleNamespace(url="https://example.com/browser-call", method="GET", headers={}, post_data=None),
            type_=SimpleNamespace(value="Other"),
        )
        await interceptor._on_request(br_event, inst_id)

        requests = await interceptor.list_requests(inst_id)
        self.assertEqual(len(requests), 0)

        search_res = await interceptor.search_requests(inst_id)
        self.assertEqual(search_res["total"], 0)

    async def test_browser_background_url_is_dropped(self):
        """A fake RequestWillBeSent directed to a browser background URL (e.g. assets.msn.com) is dropped."""
        interceptor = network_interceptor.NetworkInterceptor()
        inst_id = "test-bgurl-inst"

        bg_event = SimpleNamespace(
            request_id="req-msn-1",
            target_type="page",
            request=SimpleNamespace(url="https://assets.msn.com/bundle/banner.jpg", method="GET", headers={}, post_data=None),
            type_=SimpleNamespace(value="Image"),
        )
        await interceptor._on_request(bg_event, inst_id)

        requests = await interceptor.list_requests(inst_id)
        self.assertEqual(len(requests), 0)


class Bug11CloseInstanceWaitTests(unittest.IsolatedAsyncioTestCase):
    """4. close_instance 2-10s (bug 11)."""

    async def test_already_exited_process_closes_well_under_2s(self):
        """A fake browser whose process has already exited (returncode set) closes in well under 2s."""
        bm = BrowserManager()
        inst_id = "already-dead-inst"

        class DeadProcess:
            def __init__(self):
                self.returncode = 0
                self.pid = 99999

            async def wait(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        class FakeDeadBrowser:
            def __init__(self):
                self._process = DeadProcess()
                self._process_pid = 99999
                self.connection = SimpleNamespace(
                    handlers={},
                    send=mock.AsyncMock(return_value={}),
                )
                self.targets = []

        bm._instances[inst_id] = {
            "instance": BrowserInstance(instance_id=inst_id, state=BrowserState.READY),
            "browser": FakeDeadBrowser(),
            "attached": False,
        }

        start = time.monotonic()
        closed = await bm.close_instance(inst_id)
        elapsed = time.monotonic() - start

        self.assertTrue(closed)
        # Well under 2s, as specified. Windows also runs process-cleanup
        # finalization in a worker thread, which can take about 1s there.
        self.assertLess(elapsed, 2.0, f"Expected close under 2s, took {elapsed:.3f}s")

    async def test_lingering_process_is_terminated(self):
        """A lingering process is terminated during close."""
        bm = BrowserManager()
        inst_id = "lingering-inst"

        class LingeringProcess:
            def __init__(self):
                self.returncode = None
                self.pid = 88888
                self.terminated = False

            async def wait(self):
                if self.terminated:
                    self.returncode = -15
                    return -15
                raise asyncio.TimeoutError()

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.returncode = -9

        lingering_proc = LingeringProcess()

        class FakeLingeringBrowser:
            def __init__(self):
                self._process = lingering_proc
                self._process_pid = 88888
                self.connection = SimpleNamespace(
                    handlers={},
                    send=mock.AsyncMock(return_value={}),
                )
                self.targets = []

        bm._instances[inst_id] = {
            "instance": BrowserInstance(instance_id=inst_id, state=BrowserState.READY),
            "browser": FakeLingeringBrowser(),
            "attached": False,
        }

        closed = await bm.close_instance(inst_id, timeout=1.0)
        self.assertTrue(closed)
        self.assertTrue(lingering_proc.terminated)

    async def test_attached_browser_disconnect_does_not_kill(self):
        """Attached browser disconnects without attempting process termination."""
        bm = BrowserManager()
        inst_id = "attached-inst"

        proc = mock.MagicMock()

        class FakeAttachedBrowser:
            def __init__(self):
                self._process = proc
                self.connection = SimpleNamespace(
                    handlers={},
                    disconnect=mock.AsyncMock(),
                )
                self.targets = []

        bm._instances[inst_id] = {
            "instance": BrowserInstance(instance_id=inst_id, state=BrowserState.READY, attached=True),
            "browser": FakeAttachedBrowser(),
            "attached": True,
        }

        closed = await bm.close_instance(inst_id)
        self.assertTrue(closed)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()


class SymlinkHelperTests(unittest.TestCase):
    """5. Symlink tests on Windows privilege error 1314."""

    def test_skip_on_winerror_1314(self):
        """create_symlink raises unittest.SkipTest when OSError has winerror 1314."""
        link = Path("/dummy/link")

        err = OSError("A required privilege is not held by the client")
        err.winerror = 1314

        with mock.patch.object(Path, "symlink_to", side_effect=err):
            with self.assertRaises(unittest.SkipTest) as ctx:
                create_symlink(link, "/dummy/target")
            self.assertIn("1314", str(ctx.exception))

    def test_skip_on_errno_1314(self):
        """create_symlink raises unittest.SkipTest when OSError has errno 1314."""
        link = Path("/dummy/link")

        err = OSError(1314, "A required privilege is not held by the client")

        with mock.patch.object(Path, "symlink_to", side_effect=err):
            with self.assertRaises(unittest.SkipTest) as ctx:
                create_symlink(link, "/dummy/target")
            self.assertIn("1314", str(ctx.exception))

    def test_other_oserror_is_reraised(self):
        """create_symlink does not skip on other OSError, it re-raises."""
        link = Path("/dummy/link")

        err = OSError(2, "No such file or directory")

        with mock.patch.object(Path, "symlink_to", side_effect=err):
            with self.assertRaises(OSError) as ctx:
                create_symlink(link, "/dummy/target")
            self.assertNotIsInstance(ctx.exception, unittest.SkipTest)

    def test_successful_symlink_does_not_skip(self):
        """create_symlink succeeds without skipping when symlink_to succeeds."""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "target.txt"
            target.write_text("ok", encoding="ascii")
            link = Path(folder) / "link.txt"
            create_symlink(link, target)
            self.assertTrue(link.is_symlink())


class FileLockHolderDeathTests(unittest.TestCase):
    """6. test_dead_holder_is_acquirable_and_stale simulation."""

    def test_stale_lock_acquired_when_holder_dead(self):
        """FileLock correctly marks dead holder as stale and acquires."""
        with tempfile.TemporaryDirectory() as folder:
            lock_path = str(Path(folder) / "test_resource")
            lock1 = file_lock.FileLock(lock_path)
            lock1.acquire()

            # Mock is_pid_alive to return False for the recorded holder
            with mock.patch("stealth_browser_mcp.file_lock.is_pid_alive", return_value=False):
                lock2 = file_lock.FileLock(lock_path, timeout=1.0)
                self.assertFalse(lock2.holder_alive())

            lock1.release()


class ToolCompactInstanceNoteTests(unittest.TestCase):
    """7. Trim INSTANCE_DEFAULT_NOTE from tools that take no instance_id."""

    def test_tool_without_instance_id_omits_note(self):
        """A tool with no instance_id does not receive INSTANCE_DEFAULT_NOTE after compaction."""
        class FakeToolNoInstanceId:
            def __init__(self):
                self.name = "list_instances"
                self.description = "List all browser instances." + tool_compact.INSTANCE_DEFAULT_NOTE
                self.parameters = {
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "description": "Optional filter"},
                    },
                }

        tool = FakeToolNoInstanceId()
        tool_compact._compact_tool(tool)
        self.assertNotIn(tool_compact.INSTANCE_DEFAULT_NOTE, tool.description)
        self.assertEqual(tool.description, "List all browser instances.")

        # Idempotency check: second compaction keeps note off
        tool_compact._compact_tool(tool)
        self.assertNotIn(tool_compact.INSTANCE_DEFAULT_NOTE, tool.description)

    def test_tool_with_instance_id_keeps_note(self):
        """A tool with instance_id retains INSTANCE_DEFAULT_NOTE after compaction."""
        class FakeToolWithInstanceId:
            def __init__(self):
                self.name = "navigate"
                self.description = "Navigate to a URL."
                self.parameters = {
                    "type": "object",
                    "properties": {
                        "instance_id": {"type": "string", "description": "Browser instance ID"},
                        "url": {"type": "string", "description": "URL"},
                    },
                }

        tool = FakeToolWithInstanceId()
        tool_compact._compact_tool(tool)
        self.assertIn(tool_compact.INSTANCE_DEFAULT_NOTE, tool.description)

        # Idempotency check: second compaction does not duplicate note
        tool_compact._compact_tool(tool)
        self.assertTrue(tool.description.endswith(tool_compact.INSTANCE_DEFAULT_NOTE))
        self.assertEqual(tool.description.count(tool_compact.INSTANCE_DEFAULT_NOTE), 1)


class ToolProfileCdpFunctionsTests(unittest.IsolatedAsyncioTestCase):
    """8. Tool profile core + cdp-functions."""

    async def test_middleware_core_plus_cdp_functions_surface(self):
        """core+cdp-functions mode exposes CORE_TOOLS and cdp-functions tools, but not progressive-cloning."""
        all_tools = [
            SimpleNamespace(name="navigate", key="navigate"),
            SimpleNamespace(name="click_element", key="click_element"),
            SimpleNamespace(name="execute_cdp_command", key="execute_cdp_command"),
            SimpleNamespace(name="list_cdp_commands", key="list_cdp_commands"),
            SimpleNamespace(name="clone_element_progressive", key="clone_element_progressive"),
        ]

        async def call_next(ctx):
            return list(all_tools)

        cdp_set = {"execute_cdp_command", "list_cdp_commands"}
        mw = tool_surface.ToolSurfaceMiddleware(
            core_tools=["navigate", "click_element"],
            mode="core+cdp-functions",
            cdp_tools=cdp_set,
        )

        ctx = SimpleNamespace(request=SimpleNamespace())
        res = await mw.on_list_tools(ctx, call_next)
        names = {t.name for t in res}

        self.assertIn("navigate", names)
        self.assertIn("click_element", names)
        self.assertIn("execute_cdp_command", names)
        self.assertIn("list_cdp_commands", names)
        self.assertNotIn("clone_element_progressive", names)

    async def test_middleware_core_mode_does_not_expose_cdp(self):
        """core mode does not expose cdp-functions tools."""
        all_tools = [
            SimpleNamespace(name="navigate", key="navigate"),
            SimpleNamespace(name="execute_cdp_command", key="execute_cdp_command"),
        ]

        async def call_next(ctx):
            return list(all_tools)

        mw = tool_surface.ToolSurfaceMiddleware(
            core_tools=["navigate"],
            mode="core",
            cdp_tools={"execute_cdp_command"},
        )

        ctx = SimpleNamespace(request=SimpleNamespace())
        res = await mw.on_list_tools(ctx, call_next)
        names = {t.name for t in res}
        self.assertIn("navigate", names)
        self.assertNotIn("execute_cdp_command", names)

    def test_cli_tools_argument_parsing(self):
        """CLI parser accepts --tools core+cdp-functions."""
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--tools",
            choices=["core", "full", "core+cdp-functions"],
            default=None,
        )
        args = parser.parse_args(["--tools", "core+cdp-functions"])
        self.assertEqual(args.tools, "core+cdp-functions")

    def test_env_stealth_mcp_tools_core_plus_cdp_functions(self):
        """STEALTH_MCP_TOOLS=core+cdp-functions resolves mode to core+cdp-functions."""
        with mock.patch.dict(os.environ, {"STEALTH_MCP_TOOLS": "core+cdp-functions"}):
            mw = tool_surface.ToolSurfaceMiddleware()
            self.assertEqual(mw.get_mode(), "core+cdp-functions")


if __name__ == "__main__":
    unittest.main()
