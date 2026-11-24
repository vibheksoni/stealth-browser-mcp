"""Unit tests for SB-POST-4A: dialogs, navigation errors, and spawn cleanup.

ASCII only. No Chrome launched.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import nodriver as uc
from fastmcp.exceptions import ToolError

from stealth_browser_mcp import browser_launcher, server
from stealth_browser_mcp.browser_args_policy import managed_profiles_root
from stealth_browser_mcp.browser_launcher import clear_profile_locks, kill_process_tree
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.dialog_manager import DialogManager, get_dialog_policy
from stealth_browser_mcp.models import BrowserOptions, BrowserState


class FakeTab:
    """Mock nodriver Tab for unit testing without launching Chrome."""

    def __init__(self, target_id: str = "tab-fake-1", url: str = "about:blank", title: str = "Test"):
        self.target_id = target_id
        self.url = url
        self._title = title
        self.target = mock.MagicMock()
        self.target.target_id = target_id
        self.target.title = title
        self.target.type_ = "page"
        self.handlers: Dict[Any, List[Any]] = {}
        self.sent_commands: List[Dict[str, Any]] = []
        self._dialog_open = False
        self._nav_result: Any = ("frame-1", "loader-1", None)
        self._eval_url = url
        self._eval_title = title
        self._hang_navigate = False

    def add_handler(self, event_type: Any, handler: Any) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_handler(self, event_type: Any, handler: Any) -> None:
        callbacks = self.handlers.get(event_type, [])
        if handler in callbacks:
            callbacks.remove(handler)

    async def send(self, command: Any) -> Any:
        method = None
        cmd_dict = None
        if hasattr(command, "send"):
            try:
                cmd_dict = next(command)
                if isinstance(cmd_dict, dict):
                    method = cmd_dict.get("method")
            except StopIteration as e:
                return e.value
        elif isinstance(command, dict):
            cmd_dict = command
            method = command.get("method")

        params = cmd_dict.get("params", {}) if isinstance(cmd_dict, dict) else {}
        self.sent_commands.append({"method": method, "params": params, "raw": command})

        if method == "Page.handleJavaScriptDialog":
            self._dialog_open = False
            return {}
        elif method == "Page.navigate":
            if self._hang_navigate:
                await asyncio.sleep(9999)
            return self._nav_result
        elif method == "Page.stopLoading":
            return {}
        elif method == "Page.enable":
            return {}
        return None

    async def evaluate(self, expression: str) -> Any:
        if self._dialog_open:
            await asyncio.sleep(9999)
        if "location.href" in expression:
            return self._eval_url
        if "title" in expression:
            return self._eval_title
        return None

    async def fire_event(self, event: Any) -> None:
        for registered_type, handlers in list(self.handlers.items()):
            if isinstance(event, registered_type):
                for handler in list(handlers):
                    res = handler(event)
                    if asyncio.iscoroutine(res):
                        await res


class FakeDialogEvent(uc.cdp.page.JavascriptDialogOpening):
    """Mock JavascriptDialogOpening event."""

    def __init__(
        self,
        type_: Any = uc.cdp.page.DialogType.ALERT,
        message: str = "Test Alert",
        url: str = "http://example.com",
        frame_id: str = "f1",
        default_prompt: Optional[str] = None,
    ):
        super().__init__(
            url=url,
            frame_id=uc.cdp.page.FrameId(frame_id),
            message=message,
            type_=type_,
            has_browser_handler=False,
            default_prompt=default_prompt,
        )


class DialogTests(unittest.TestCase):
    """Tests for JavaScript dialog handling (Bug 1)."""

    def setUp(self):
        self.orig_policy = os.environ.get("STEALTH_DIALOG_POLICY")

    def tearDown(self):
        if self.orig_policy is not None:
            os.environ["STEALTH_DIALOG_POLICY"] = self.orig_policy
        else:
            os.environ.pop("STEALTH_DIALOG_POLICY", None)

    def test_default_policy_dismiss(self):
        os.environ.pop("STEALTH_DIALOG_POLICY", None)
        self.assertEqual(get_dialog_policy(), "dismiss")

        dm = DialogManager()
        tab = FakeTab()
        asyncio.run(dm.configure_tab(tab, "inst-1"))

        # Verify Page.enable was sent
        sent_methods = [cmd.get("method") for cmd in tab.sent_commands]
        self.assertIn("Page.enable", sent_methods)

        # Tab marks dialog open
        tab._dialog_open = True
        event = FakeDialogEvent(type_=uc.cdp.page.DialogType.ALERT, message="Alert msg")
        asyncio.run(tab.fire_event(event))

        # Under dismiss policy, handle_javascript_dialog(accept=False) sent immediately
        handle_cmds = [
            cmd for cmd in tab.sent_commands if cmd.get("method") == "Page.handleJavaScriptDialog"
        ]
        self.assertTrue(len(handle_cmds) > 0)
        self.assertEqual(handle_cmds[-1]["params"]["accept"], False)
        self.assertFalse(tab._dialog_open, "Dialog was dismissed so tab is not blocked")

        # Evaluate is not blocked
        val = asyncio.run(tab.evaluate("window.location.href"))
        self.assertEqual(val, "about:blank")

    def test_beforeunload_always_accepted_in_dismiss_and_manual(self):
        for policy in ("dismiss", "manual"):
            os.environ["STEALTH_DIALOG_POLICY"] = policy
            dm = DialogManager()
            tab = FakeTab()
            asyncio.run(dm.configure_tab(tab, "inst-1"))

            event = FakeDialogEvent(
                type_=uc.cdp.page.DialogType.BEFOREUNLOAD,
                message="Leave site?",
            )
            asyncio.run(tab.fire_event(event))

            handle_cmds = [
                cmd for cmd in tab.sent_commands if cmd.get("method") == "Page.handleJavaScriptDialog"
            ]
            self.assertTrue(len(handle_cmds) > 0)
            self.assertEqual(
                handle_cmds[-1]["params"]["accept"],
                True,
                f"beforeunload must be accepted under {policy} policy",
            )

    def test_manual_policy_queues_and_handle_dialog_clears(self):
        os.environ["STEALTH_DIALOG_POLICY"] = "manual"
        dm = DialogManager()
        tab = FakeTab()
        asyncio.run(dm.configure_tab(tab, "inst-1"))

        event = FakeDialogEvent(
            type_=uc.cdp.page.DialogType.CONFIRM,
            message="Are you sure?",
            frame_id="frame-99",
        )
        asyncio.run(tab.fire_event(event))

        # Manual policy: not auto-handled
        handle_cmds = [
            cmd for cmd in tab.sent_commands if cmd.get("method") == "Page.handleJavaScriptDialog"
        ]
        self.assertEqual(len(handle_cmds), 0)

        # get_dialogs sees pending
        dialogs = dm.get_dialogs("inst-1")
        self.assertEqual(len(dialogs), 1)
        self.assertTrue(dialogs[0]["pending"])
        self.assertFalse(dialogs[0]["handled"])
        self.assertEqual(dialogs[0]["message"], "Are you sure?")
        self.assertEqual(dialogs[0]["type"], "confirm")
        self.assertEqual(dialogs[0]["frame_id"], "frame-99")

        # handle_dialog accept
        res = asyncio.run(dm.handle_dialog("inst-1", accept=True))
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "accept")

        # CDP call was sent
        handle_cmds = [
            cmd for cmd in tab.sent_commands if cmd.get("method") == "Page.handleJavaScriptDialog"
        ]
        self.assertEqual(len(handle_cmds), 1)
        self.assertEqual(handle_cmds[0]["params"]["accept"], True)

        # Pending cleared
        dialogs_after = dm.get_dialogs("inst-1")
        self.assertEqual(len(dialogs_after), 1)
        self.assertFalse(dialogs_after[0]["pending"])
        self.assertTrue(dialogs_after[0]["handled"])

        # Second handle_dialog fails clearly
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(dm.handle_dialog("inst-1", accept=True))
        self.assertIn("No pending dialog", str(ctx.exception))

    def test_policy_env_resolution(self):
        os.environ["STEALTH_DIALOG_POLICY"] = "accept"
        self.assertEqual(get_dialog_policy(), "accept")

        os.environ["STEALTH_DIALOG_POLICY"] = "dismiss"
        self.assertEqual(get_dialog_policy(), "dismiss")

        os.environ["STEALTH_DIALOG_POLICY"] = "invalid_value_xyz"
        self.assertEqual(get_dialog_policy(), "dismiss")


class NavigationErrorAndStatusTests(unittest.TestCase):
    """Tests for Navigation error_text and HTTP status (Bug 2)."""

    def setUp(self):
        self.bm = BrowserManager()
        self.inst_id = "test-inst-nav"
        self.tab = FakeTab()
        self.browser_mock = mock.MagicMock()
        self.browser_mock.update_targets = mock.AsyncMock()
        self.browser_mock.tabs = [self.tab]
        self.inst_obj = mock.MagicMock()
        self.bm._instances[self.inst_id] = {
            "tab": self.tab,
            "browser": self.browser_mock,
            "instance": self.inst_obj,
            "options": BrowserOptions(),
            "navigation_count": 0,
        }

    def test_navigate_error_text_fails(self):
        self.tab._nav_result = ("frame-1", "loader-1", "net::ERR_NAME_NOT_RESOLVED")
        res = asyncio.run(
            self.bm.navigate(self.inst_id, "http://dead.address.fake", timeout=2000)
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "net::ERR_NAME_NOT_RESOLVED")
        self.assertEqual(res["error_text"], "net::ERR_NAME_NOT_RESOLVED")

    def test_navigate_success_with_status_200(self):
        self.tab._nav_result = ("frame-1", "loader-1", None)
        self.tab._eval_url = "http://example.com"
        self.tab._eval_title = "Example Domain"

        orig_send = self.tab.send
        async def fake_navigate(command):
            resp_event = mock.MagicMock(spec=uc.cdp.network.ResponseReceived)
            resp_event.loader_id = "loader-1"
            resp_event.frame_id = "frame-1"
            resp_event.type_ = mock.MagicMock(value="Document")
            resp_event.response = mock.MagicMock(status=200)
            await self.tab.fire_event(resp_event)
            return await orig_send(command)

        self.tab.send = fake_navigate

        res = asyncio.run(
            self.bm.navigate(self.inst_id, "http://example.com", timeout=2000)
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["url"], "http://example.com")
        self.assertEqual(res["title"], "Example Domain")
        self.assertEqual(res["status"], 200)

    def test_navigate_chrome_error_url_fails(self):
        self.tab._nav_result = ("frame-1", "loader-1", None)
        self.tab._eval_url = "chrome-error://chromewebdata/"
        self.tab._eval_title = "Error"

        res = asyncio.run(
            self.bm.navigate(self.inst_id, "http://bad.domain", timeout=2000)
        )
        self.assertFalse(res["success"])
        self.assertIn("chrome-error://", res["url"])


class NavigationTimeoutTests(unittest.TestCase):
    """Tests for Navigation timeout single deadline (Bug 3)."""

    def setUp(self):
        self.bm = BrowserManager()
        self.inst_id = "test-inst-timeout"
        self.tab = FakeTab()
        self.tab._hang_navigate = True
        self.browser_mock = mock.MagicMock()
        self.browser_mock.update_targets = mock.AsyncMock()
        self.browser_mock.tabs = [self.tab]
        self.inst_obj = mock.MagicMock()
        self.bm._instances[self.inst_id] = {
            "tab": self.tab,
            "browser": self.browser_mock,
            "instance": self.inst_obj,
            "options": BrowserOptions(),
            "navigation_count": 0,
        }

    def test_timeout_returns_promptly_and_sends_stop_loading(self):
        self.bm._replace_main_tab = mock.AsyncMock()

        start = time.monotonic()
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                self.bm.navigate(self.inst_id, "http://hang.example.com", timeout=1000)
            )
        elapsed = time.monotonic() - start

        self.assertIn("timed out after 1000ms", str(ctx.exception))
        self.assertLess(elapsed, 4.0, "Timeout must return well under 5s, never 40s")

        # Must send Page.stopLoading
        sent_methods = [cmd.get("method") for cmd in self.tab.sent_commands]
        self.assertIn("Page.stopLoading", sent_methods)

        # Must not call _replace_main_tab on timeout
        self.bm._replace_main_tab.assert_not_called()

    def test_recoverable_error_retries_under_same_deadline(self):
        attempt_count = 0

        async def fake_send_attempt1(command):
            nonlocal attempt_count
            attempt_count += 1
            raise Exception("websocket connection dropped")

        self.tab.send = fake_send_attempt1
        fresh_tab = FakeTab("fresh-tab-2")
        fresh_tab._nav_result = ("frame-1", "loader-1", None)
        fresh_tab._eval_url = "http://recover.example.com"
        fresh_tab._eval_title = "Recovered"

        async def fake_replace(*args, **kwargs):
            return fresh_tab

        self.bm._replace_main_tab = fake_replace

        res = asyncio.run(
            self.bm.navigate(self.inst_id, "http://recover.example.com", timeout=2000)
        )
        self.assertTrue(res["success"])
        self.assertEqual(attempt_count, 1)
        self.assertEqual(res["url"], "http://recover.example.com")

    def test_timeout_is_not_recoverable(self):
        self.assertFalse(
            BrowserManager._is_recoverable_navigation_error(asyncio.TimeoutError())
        )


class SpawnTimeoutAndProfileInUseTests(unittest.TestCase):
    """Tests for spawn timeout process-tree cleanup and profile-in-use (Bugs 6 & 7)."""

    def setUp(self):
        self.bm = BrowserManager()
        self.root = managed_profiles_root()
        self.temp_dir = tempfile.mkdtemp(prefix="stealth_test_profile_", dir=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_spawn_timeout_kills_process_tree_and_clears_locks(self):
        profile_path = Path(self.temp_dir)
        lock_file = profile_path / "SingletonLock"
        lock_file.write_text("fake-lock")

        fake_process = mock.MagicMock()
        fake_process.pid = 999999
        fake_process.returncode = None
        fake_process.poll.return_value = None
        fake_process.wait = mock.MagicMock(return_value=0)

        async def hanging_launch(config, timeout=60.0, args_modifier=None, on_process_started=None):
            if on_process_started:
                on_process_started(fake_process)
            await asyncio.sleep(9999)

        with mock.patch("stealth_browser_mcp.browser_manager.launch_browser", side_effect=hanging_launch):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(
                    self.bm.spawn_browser(
                        user_data_dir=str(profile_path),
                        spawn_timeout=0.2,
                        headless=True,
                        user_agent="Mozilla/5.0 FakeUA",
                    )
                )

        self.assertIn("timed out", str(ctx.exception))
        # Process kill/terminate was invoked
        self.assertTrue(fake_process.kill.called or fake_process.terminate.called)
        # No instances registered
        self.assertEqual(len(self.bm._instances), 0)
        # Lock was cleared
        self.assertFalse(lock_file.exists())

    def test_two_spawns_same_user_data_dir_rejected(self):
        first_id = "inst-first-holder"
        self.bm._instances[first_id] = {
            "options": BrowserOptions(user_data_dir=self.temp_dir),
            "browser": mock.MagicMock(),
            "tab": FakeTab(),
        }

        with mock.patch("stealth_browser_mcp.browser_manager.launch_browser") as mock_launch:
            with self.assertRaises(Exception) as ctx:
                asyncio.run(
                    self.bm.spawn_browser(
                        user_data_dir=self.temp_dir,
                        headless=True,
                    )
                )

            mock_launch.assert_not_called()
            err_msg = str(ctx.exception)
            self.assertIn("profile in use by instance", err_msg)
            self.assertIn(first_id, err_msg)

    def test_exit_code_21_rewrites_error(self):
        async def code_21_launch(config, timeout=60.0, args_modifier=None, on_process_started=None):
            raise Exception("Browser exited during startup with code 21")

        with mock.patch("stealth_browser_mcp.browser_manager.launch_browser", side_effect=code_21_launch):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(
                    self.bm.spawn_browser(
                        user_data_dir=self.temp_dir,
                        headless=True,
                    )
                )

            err_msg = str(ctx.exception)
            self.assertIn("profile in use", err_msg)
            self.assertNotIn("Browser exited during startup with code 21\n", err_msg)



    def test_exit_code_21_does_not_clear_live_profile_lock(self):
        lock_file = Path(self.temp_dir) / "SingletonLock"
        lock_file.write_text("other-process")

        async def code_21_launch(config, timeout=60.0, args_modifier=None, on_process_started=None):
            raise Exception("Browser exited during startup with code 21")

        with mock.patch("stealth_browser_mcp.browser_manager.launch_browser", side_effect=code_21_launch):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(
                    self.bm.spawn_browser(
                        user_data_dir=self.temp_dir,
                        headless=True,
                    )
                )

            self.assertIn("profile in use", str(ctx.exception))
            self.assertTrue(lock_file.exists())
            self.assertEqual(lock_file.read_text(), "other-process")


if __name__ == "__main__":
    unittest.main()
