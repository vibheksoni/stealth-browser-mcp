"""Tests for SB-69: attach to a running Chrome and managed-profile launcher.

ASCII only.
Unit tests run free of real browsers.
Gated real browser test runs only when STEALTH_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fastmcp import Client
from fastmcp.exceptions import ToolError

from scripts.launch_chrome_managed import (
    build_chrome_args,
    find_chrome,
    is_default_profile_dir,
)
from stealth_browser_mcp import server
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserInstance, BrowserOptions, BrowserState


class TestAttachBrowserLoopbackValidation(unittest.IsolatedAsyncioTestCase):
    """Test loopback host validation and security restrictions."""

    async def test_accept_loopback_hosts(self) -> None:
        """Loopback hosts (127.0.0.1, localhost, ::1) must be accepted."""
        for host in ("127.0.0.1", "localhost", "::1"):
            with (
                mock.patch.object(server, "_probe_version", return_value=True),
                mock.patch.object(
                    server.browser_manager,
                    "attach_browser",
                    return_value=BrowserInstance(
                        instance_id=f"test-loopback-{host}",
                        state=BrowserState.READY,
                        attached=True,
                    ),
                ),
            ):
                res = await server.attach_browser.fn(port=9222, host=host)
                self.assertTrue(res.get("attached"))
                self.assertEqual(res.get("host"), host)

    async def test_reject_non_loopback_hosts(self) -> None:
        """Non-loopback hosts must be rejected with ToolError when env is unset."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_REMOTE_ATTACH", None)
            for host in ("10.0.0.5", "example.com"):
                with self.subTest(host=host):
                    with self.assertRaises(ToolError) as ctx:
                        await server.attach_browser.fn(port=9222, host=host)
                    err_msg = str(ctx.exception)
                    self.assertIn("not a loopback address", err_msg)
                    self.assertIn("STEALTH_ALLOW_REMOTE_ATTACH=1", err_msg)

    async def test_env_override_allows_remote_attach(self) -> None:
        """Setting STEALTH_ALLOW_REMOTE_ATTACH=1 allows non-loopback hosts."""
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_REMOTE_ATTACH": "1"}):
            with (
                mock.patch.object(server, "_probe_version", return_value=True),
                mock.patch.object(
                    server.browser_manager,
                    "attach_browser",
                    return_value=BrowserInstance(
                        instance_id="test-remote-ok",
                        state=BrowserState.READY,
                        attached=True,
                    ),
                ),
            ):
                res = await server.attach_browser.fn(port=9222, host="10.0.0.5")
                self.assertTrue(res.get("attached"))
                self.assertEqual(res.get("host"), "10.0.0.5")


class TestAttachBrowserUnreachablePort(unittest.IsolatedAsyncioTestCase):
    """Test behavior when the remote debugging port is unreachable."""

    async def test_unreachable_port_raises_tool_error_with_guidance(self) -> None:
        """Unreachable port must raise ToolError mentioning --remote-debugging-port and MANAGED_PROFILES."""
        with mock.patch.object(server, "_probe_version", return_value=False):
            with self.assertRaises(ToolError) as ctx:
                await server.attach_browser.fn(port=9222, host="127.0.0.1")
            err_msg = str(ctx.exception)
            self.assertIn("--remote-debugging-port", err_msg)
            self.assertIn("MANAGED_PROFILES", err_msg)


class TestAttachedInstanceCloseDisconnectsOnly(unittest.IsolatedAsyncioTestCase):
    """Test that closing an attached browser only disconnects and never terminates it."""

    async def test_attached_instance_close_does_not_call_stop_or_kill(self) -> None:
        """Fake browser must not have stop() or process.kill()/terminate() called, and profile preserved."""
        temp_dir = tempfile.mkdtemp(prefix="stealth_attached_profile_")
        try:
            class FakeBrowser:
                def __init__(self):
                    self.stop_called = False
                    self.connection = SimpleNamespace(
                        disconnect=mock.AsyncMock(),
                        handlers={},
                    )
                    self.targets = []
                    self._process = mock.MagicMock()

                def stop(self):
                    self.stop_called = True

            fake_browser = FakeBrowser()
            instance_id = "attached-fake-close-test"
            instance = BrowserInstance(
                instance_id=instance_id,
                headless=False,
                window="keep",
                attached=True,
            )

            async with server.browser_manager._lock:
                server.browser_manager._instances[instance_id] = {
                    "browser": fake_browser,
                    "tab": SimpleNamespace(disconnect=mock.AsyncMock(), url="about:blank"),
                    "instance": instance,
                    "options": BrowserOptions(window="keep", user_data_dir=temp_dir),
                    "attached": True,
                    "window": "keep",
                    "spawn_diagnostics": {},
                }

            closed = await server.close_instance.fn(instance_id)
            self.assertTrue(closed)
            self.assertEqual(instance.state, BrowserState.CLOSED)
            self.assertFalse(fake_browser.stop_called)
            self.assertFalse(fake_browser._process.kill.called)
            self.assertFalse(fake_browser._process.terminate.called)
            self.assertTrue(Path(temp_dir).is_dir())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestListAndGetInstanceState(unittest.IsolatedAsyncioTestCase):
    """Test that list_instances and get_instance_state expose the attached attribute."""

    async def test_list_instances_and_get_instance_state_show_attached(self) -> None:
        """Both list_instances and get_instance_state output must include attached=True."""
        instance_id = "attached-query-test"
        instance = BrowserInstance(
            instance_id=instance_id,
            state=BrowserState.READY,
            current_url="https://example.com",
            title="Example Title",
            attached=True,
        )

        async def fake_evaluate(expr: str) -> Any:
            if "location" in expr:
                return "https://example.com"
            if "title" in expr:
                return "Example Title"
            if "readyState" in expr:
                return "complete"
            if "keys" in expr:
                return "[]" if "JSON.stringify" in expr else []
            if "devicePixelRatio" in expr:
                return (
                    json.dumps({"width": 1920, "height": 1080, "devicePixelRatio": 1})
                    if "JSON.stringify" in expr
                    else {"width": 1920, "height": 1080, "devicePixelRatio": 1}
                )
            return ""

        async def fake_send(*a: Any, **k: Any) -> Any:
            return {"cookies": []}

        fake_tab = SimpleNamespace(
            evaluate=mock.AsyncMock(side_effect=fake_evaluate),
            send=mock.AsyncMock(side_effect=fake_send),
            url="https://example.com",
            title="Example Title",
            target=SimpleNamespace(
                target_id="tab-target-1",
                title="Example Title",
                url="https://example.com",
                type_="page",
            ),
            disconnect=mock.AsyncMock(),
        )

        async with server.browser_manager._lock:
            server.browser_manager._instances[instance_id] = {
                "browser": SimpleNamespace(
                    connection=SimpleNamespace(handlers={}, disconnect=mock.AsyncMock()),
                    targets=[],
                ),
                "tab": fake_tab,
                "instance": instance,
                "options": BrowserOptions(window="keep"),
                "attached": True,
                "window": "keep",
                "spawn_diagnostics": {},
            }

        try:
            instances = await server.list_instances.fn()
            found = next((i for i in instances if i["instance_id"] == instance_id), None)
            self.assertIsNotNone(found)
            self.assertTrue(found.get("attached"))

            state = await server.get_instance_state.fn(instance_id)
            self.assertIsNotNone(state)
            self.assertTrue(state.get("attached"))
        finally:
            async with server.browser_manager._lock:
                server.browser_manager._instances.pop(instance_id, None)


class TestLauncherHelpers(unittest.TestCase):
    """Test helper functions in scripts/launch_chrome_managed.py."""

    def test_default_profile_refusal_linux(self) -> None:
        """Linux default Chrome/Chromium profile paths must be refused."""
        for p in (
            "~/.config/google-chrome",
            "/home/alice/.config/google-chrome",
            "/home/alice/.config/chromium",
            "/home/alice/.config/google-chrome/Default",
        ):
            self.assertTrue(is_default_profile_dir(p, "linux"), f"Expected refusal for {p}")
        self.assertFalse(is_default_profile_dir("~/.stealth-mcp/managed-profile", "linux"))
        self.assertFalse(is_default_profile_dir("/tmp/custom-profile", "linux"))

    def test_default_profile_refusal_mac(self) -> None:
        """macOS default Chrome/Chromium profile paths must be refused."""
        for p in (
            "~/Library/Application Support/Google/Chrome",
            "/Users/alice/Library/Application Support/Google/Chrome",
            "/Users/alice/Library/Application Support/Chromium",
            "/Users/alice/Library/Application Support/Google/Chrome/Profile 1",
        ):
            self.assertTrue(is_default_profile_dir(p, "darwin"), f"Expected refusal for {p}")
        self.assertFalse(is_default_profile_dir("~/.stealth-mcp/managed-profile", "darwin"))
        self.assertFalse(is_default_profile_dir("/Users/alice/custom-profile", "darwin"))

    def test_default_profile_refusal_windows(self) -> None:
        """Windows default Chrome/Chromium profile paths must be refused."""
        for p in (
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data",
            r"%LOCALAPPDATA%\Google\Chrome\User Data",
            r"C:\Users\alice\AppData\Local\Chromium\User Data",
            r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default",
        ):
            self.assertTrue(is_default_profile_dir(p, "win32"), f"Expected refusal for {p}")
        self.assertFalse(is_default_profile_dir(r"C:\Users\alice\.stealth-mcp\managed-profile", "win32"))
        self.assertFalse(is_default_profile_dir(r"D:\automation-profile", "win32"))

    def test_build_chrome_args_window_modes(self) -> None:
        """Launch args for each mode must include port, profile, and no --headless."""
        for mode in ("normal", "minimized", "offscreen"):
            with self.subTest(mode=mode):
                args = build_chrome_args("/path/to/profile", port=9222, window=mode)
                self.assertIn("--remote-debugging-port=9222", args)
                self.assertIn("--user-data-dir=/path/to/profile", args)
                self.assertFalse(any("--headless" in a for a in args))

        offscreen_args = build_chrome_args("/path", port=9222, window="offscreen")
        self.assertIn("--window-position=-32000,-32000", offscreen_args)

        minimized_args = build_chrome_args("/path", port=9222, window="minimized")
        self.assertIn("--start-minimized", minimized_args)

        normal_args = build_chrome_args("/path", port=9222, window="normal")
        self.assertNotIn("--start-minimized", normal_args)
        self.assertNotIn("--window-position=-32000,-32000", normal_args)

    def test_find_chrome_with_candidates(self) -> None:
        """find_chrome returns first existing candidate file or None."""
        with tempfile.NamedTemporaryFile() as tf:
            self.assertEqual(find_chrome([tf.name]), tf.name)
            self.assertEqual(find_chrome(["/nonexistent/path", tf.name]), tf.name)
        self.assertIsNone(find_chrome(["/nonexistent/binary/chrome"]))


class TestAttachBrowserToolRegistration(unittest.IsolatedAsyncioTestCase):
    """Test tool registration and metadata constraints."""

    async def test_tool_registered_with_short_description(self) -> None:
        """attach_browser must be registered with a description <= 300 characters."""
        registry_tools = await server.mcp.get_tools()
        self.assertIn("attach_browser", registry_tools)
        reg_tool = registry_tools["attach_browser"]
        self.assertTrue(reg_tool.description)
        self.assertLessEqual(len(reg_tool.description), 300)

        async with Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        self.assertIn("attach_browser", tools)
        tool = tools["attach_browser"]
        self.assertTrue(tool.description)
        self.assertLessEqual(len(tool.description), 300)


class TestAttachBrowserGated(unittest.IsolatedAsyncioTestCase):
    """Gated real Chrome attachment test (runs only when STEALTH_BROWSER_TESTS=1)."""

    @unittest.skipUnless(
        os.environ.get("STEALTH_BROWSER_TESTS") == "1",
        "Gated real browser test",
    )
    async def test_attach_real_chrome_and_navigate(self) -> None:
        """Attach to a real Chrome process, navigate, read title, and verify process survives close."""
        chrome_exe = find_chrome()
        if not chrome_exe:
            self.skipTest("No Chrome executable found")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        temp_dir = tempfile.mkdtemp(prefix="stealth_gated_managed_")
        chrome_args = build_chrome_args(
            temp_dir,
            port=port,
            extra_args=["--headless=new", "--no-sandbox"],
        )

        proc = subprocess.Popen(
            [chrome_exe, *chrome_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        instance_id = None
        try:
            deadline = asyncio.get_event_loop().time() + 15.0
            ready = False
            while asyncio.get_event_loop().time() < deadline:
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
                    with urllib.request.urlopen(req, timeout=1.0) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    await asyncio.sleep(0.5)

            if not ready:
                self.fail(f"Real Chrome did not start listening on port {port} within 15 seconds")

            res = await server.attach_browser.fn(port=port, host="127.0.0.1")
            instance_id = res["instance_id"]
            self.assertTrue(res.get("attached"))

            await server.navigate.fn(
                instance_id=instance_id,
                url="data:text/html,<title>Attached Real Chrome</title><h1>Hello</h1>",
            )

            eval_res = await server.execute_script.fn(
                instance_id=instance_id,
                script="document.title",
            )
            self.assertEqual(eval_res.get("result"), "Attached Real Chrome")

            await server.close_instance.fn(instance_id=instance_id)

            self.assertIsNone(
                proc.poll(),
                "Chrome process must remain alive after close_instance for attached browser",
            )
        finally:
            if instance_id:
                try:
                    await server.browser_manager.close_instance(instance_id)
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
