"""Unit tests and gated browser integration tests for fill_os_paste (SB-15)."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import (
    dom_handler,
    fill_os_paste,
    fill_tool,
    key_definitions,
    os_clipboard,
)
from stealth_browser_mcp.file_lock import FileLock, LockTimeout
from stealth_browser_mcp.fill_os_paste import fill_os_paste as fill_os_paste_impl
from stealth_browser_mcp.os_clipboard import (
    ENV_FLAG,
    ClipboardDisabledError,
)
from stealth_browser_mcp.paths import DATA_DIR_ENV

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

SRC_DIR = str(ROOT / "src")

WAITER_SCRIPT = """
import sys
from pathlib import Path

sys.path.insert(0, {src!r})
from stealth_browser_mcp import file_lock

lock_path = sys.argv[1]
timeout = float(sys.argv[2])
lock = file_lock.FileLock(lock_path, timeout=timeout)
try:
    lock.acquire()
except file_lock.LockTimeout:
    sys.exit(3)
except Exception:
    sys.exit(1)
lock.release()
sys.exit(0)
"""


class RecordingLock:
    """Lock fake recording acquire and release calls and whether held."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.held: bool = False

    def acquire(self, timeout: Optional[float] = None) -> "RecordingLock":
        self.calls.append("acquire")
        self.held = True
        return self

    def release(self) -> "RecordingLock":
        self.calls.append("release")
        self.held = False
        return self


class RecordingBackend:
    """Fake OS clipboard backend tracking text get/set operations."""

    def __init__(self, current: str = "") -> None:
        self.current = current
        self.sets: List[str] = []
        self.gets: int = 0

    def get_text(self) -> str:
        self.gets += 1
        return self.current

    def set_text(self, text: str) -> None:
        self.sets.append(text)
        self.current = text


def _fake_backend_factory(backend: RecordingBackend):
    def _factory() -> RecordingBackend:
        return backend
    return _factory


class FakeTabForPaste:
    """Tab recording CDP commands and answering DOM, Runtime, and Input calls."""

    def __init__(
        self,
        backend_node_id: int = 42,
        facts: Optional[Dict[str, Any]] = None,
        on_send_hook: Optional[Any] = None,
        raise_on_key_event: bool = False,
    ) -> None:
        self.backend_node_id = backend_node_id
        self.facts = facts or {"tag_name": "input", "type": "text", "input_type": "text"}
        self.calls: List[tuple] = []
        self.events: List[tuple] = []
        self.on_send_hook = on_send_hook
        self.raise_on_key_event = raise_on_key_event
        self.current_value: str = ""

    async def send(self, command: Any) -> Any:
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.calls.append((method, params))

        if self.on_send_hook:
            self.on_send_hook(method, params)

        if method.startswith("Input."):
            if self.raise_on_key_event:
                raise RuntimeError("Simulated CDP key event dispatch error")

            t = params.get("type")
            key = params.get("key") or params.get("text")
            modifiers = params.get("modifiers", 0)
            commands = params.get("commands", [])
            self.events.append((method, t, key, modifiers, commands))

            if "paste" in commands:
                try:
                    backend = os_clipboard._get_backend()
                    if backend is not None:
                        self.current_value = backend.get_text()
                except Exception:
                    pass
            return None

        if method == "DOM.focus":
            return None

        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj-42"))

        if method == "DOM.describeNode":
            return SimpleNamespace(
                backend_node_id=uc.cdp.dom.BackendNodeId(self.backend_node_id)
            )

        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            if "tag_name" in fn and "is_content_editable" in fn:
                return SimpleNamespace(value=self.facts), None

            if "selectedIndex" in fn or "HTMLInputElement" in fn:
                return SimpleNamespace(
                    value={"value": self.current_value, "text": self.current_value}
                ), None

            return SimpleNamespace(value=True), None

        if method == "Runtime.evaluate":
            return SimpleNamespace(
                value={"value": self.current_value, "text": self.current_value}
            ), None

        return None


class FillOSPasteUnitTests(unittest.TestCase):
    """Unit tests for fill_os_paste input action and strategy wiring."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_flag_off_raises_clear_error(self) -> None:
        """Calling fill_os_paste with flag disabled raises ClipboardDisabledError naming the env var."""
        os.environ.pop(ENV_FLAG, None)
        tab = FakeTabForPaste(backend_node_id=42)

        with self.assertRaises(ClipboardDisabledError) as ctx:
            asyncio.run(fill_os_paste_impl(tab, 42, "text to paste"))
        self.assertIn(ENV_FLAG, str(ctx.exception))

        with self.assertRaises(ClipboardDisabledError) as ctx:
            asyncio.run(
                fill_tool.fill(tab, 42, value="text to paste", strategy="os_paste", verify=False)
            )
        self.assertIn(ENV_FLAG, str(ctx.exception))

    def test_user_clipboard_restored_on_success(self) -> None:
        """User clipboard text is preserved and restored after paste finishes."""
        os.environ[ENV_FLAG] = "1"
        backend = RecordingBackend(current="original user clipboard text")
        tab = FakeTabForPaste(backend_node_id=42)

        with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
            result = asyncio.run(fill_os_paste_impl(tab, 42, "pasted text"))

        self.assertTrue(result)
        self.assertEqual(backend.current, "original user clipboard text")
        self.assertIn("pasted text", backend.sets)
        self.assertEqual(backend.sets[-1], "original user clipboard text")

    def test_user_clipboard_restored_on_error(self) -> None:
        """User clipboard text is restored even when CDP key event dispatch raises."""
        os.environ[ENV_FLAG] = "1"
        backend = RecordingBackend(current="restore me on crash")
        tab = FakeTabForPaste(backend_node_id=42, raise_on_key_event=True)

        with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(fill_os_paste_impl(tab, 42, "ephemeral text"))
            self.assertIn("Simulated CDP key event dispatch error", str(ctx.exception))

        self.assertEqual(backend.current, "restore me on crash")
        self.assertEqual(backend.sets[-1], "restore me on crash")

    def test_lock_held_across_set_and_paste(self) -> None:
        """Lock is held during the whole set+paste window and released after."""
        os.environ[ENV_FLAG] = "1"
        backend = RecordingBackend(current="initial")
        lock = RecordingLock()

        held_during_paste = []

        def on_send(method: str, params: Dict[str, Any]) -> None:
            if method.startswith("Input.dispatchKeyEvent"):
                held_during_paste.append((lock.held, backend.current))

        tab = FakeTabForPaste(backend_node_id=42, on_send_hook=on_send)

        with unittest.mock.patch.object(os_clipboard, "_clipboard_lock", lambda: lock):
            with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
                asyncio.run(fill_os_paste_impl(tab, 42, "paste payload"))

        self.assertGreater(len(held_during_paste), 0)
        for was_held, clip_content in held_during_paste:
            self.assertTrue(was_held, "Lock must be held while dispatching paste key events")
            self.assertEqual(clip_content, "paste payload")

        self.assertEqual(lock.calls, ["acquire", "release"])
        self.assertFalse(lock.held)
        self.assertEqual(backend.current, "initial")

    def test_fill_tool_accepts_os_paste_strategy_names(self) -> None:
        """fill tool accepts strategy='os_paste', 'clipboard', and 'os_clipboard'."""
        os.environ[ENV_FLAG] = "1"

        for strat_name in ("os_paste", "clipboard", "os_clipboard"):
            with self.subTest(strategy=strat_name):
                backend = RecordingBackend(current="saved")
                tab = FakeTabForPaste(backend_node_id=42)

                with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
                    res = asyncio.run(
                        fill_tool.fill(
                            tab,
                            42,
                            value=f"value for {strat_name}",
                            strategy=strat_name,
                            verify=True,
                        )
                    )

                self.assertTrue(res["success"])
                self.assertTrue(res["verified"])
                self.assertEqual(res["strategy"], "os_paste")
                self.assertEqual(tab.current_value, f"value for {strat_name}")
                self.assertEqual(backend.current, "saved")

    def test_auto_strategy_never_chooses_os_paste_when_flag_off(self) -> None:
        """strategy='auto' never selects os_paste when opt-in flag is off."""
        os.environ.pop(ENV_FLAG, None)
        tab = FakeTabForPaste(backend_node_id=42)

        res = asyncio.run(
            fill_tool.fill(tab, 42, value="auto text", strategy="auto", verify=False)
        )
        self.assertNotEqual(res["strategy"], "os_paste")
        self.assertEqual(res["strategy"], "native_setter")

        # Also when verify=True and value mismatches, never fallback to os_paste if flag is off
        tab_fail = FakeTabForPaste(
            backend_node_id=42,
            facts={"tag_name": "div", "is_content_editable": True, "contenteditable": "true"},
        )
        res_fail = asyncio.run(
            fill_tool.fill(tab_fail, 42, value="auto text", strategy="auto", verify=True)
        )
        self.assertFalse(res_fail["verified"])
        self.assertNotEqual(res_fail["strategy"], "os_paste")
        self.assertEqual(res_fail["strategy"], "synthetic_paste")

    def test_auto_strategy_falls_back_to_os_paste_when_other_fails_and_flag_on(self) -> None:
        """strategy='auto' falls back to os_paste only when verification failed and flag is on."""
        os.environ[ENV_FLAG] = "1"
        backend = RecordingBackend(current="clipboard_user")

        tab = FakeTabForPaste(
            backend_node_id=42,
            facts={"tag_name": "div", "is_content_editable": True, "contenteditable": "true"},
        )

        with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
            res = asyncio.run(
                fill_tool.fill(
                    tab,
                    42,
                    value="fallback text",
                    strategy="auto",
                    verify=True,
                )
            )

        self.assertTrue(res["verified"])
        self.assertEqual(res["strategy"], "os_paste")
        self.assertEqual(tab.current_value, "fallback text")
        self.assertEqual(backend.current, "clipboard_user")

    def test_second_process_blocks_on_lock(self) -> None:
        """Second process trying to acquire the clipboard lock blocks and times out."""
        os.environ[ENV_FLAG] = "1"
        backend = RecordingBackend(current="initial")

        lock_path = str(Path(self._tmp.name) / "locks" / "clipboard.lock")
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)

        waiter_code = WAITER_SCRIPT.format(src=SRC_DIR)

        with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
            with os_clipboard.clipboard_paste_session("holding text"):
                start_time = time.monotonic()
                proc = subprocess.run(
                    [sys.executable, "-c", waiter_code, lock_path, "0.3"],
                    check=False,
                    cwd=str(ROOT),
                )
                elapsed = time.monotonic() - start_time
                self.assertEqual(proc.returncode, 3, "Second process should time out with code 3")
                self.assertGreaterEqual(elapsed, 0.25)

        proc_after = subprocess.run(
            [sys.executable, "-c", waiter_code, lock_path, "1.5"],
            check=False,
            cwd=str(ROOT),
        )
        self.assertEqual(
            proc_after.returncode, 0, "Second process should acquire lock after release"
        )


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestFillOSPasteGatedBrowser(unittest.TestCase):
    """Gated real-browser integration tests with paste-only fixture editor."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        self._tmp_data = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp_data.name
        os.environ[ENV_FLAG] = "1"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp_data.cleanup()

    def test_paste_only_fixture_editor_accepts(self) -> None:
        """Paste-only fixture editor accepts paste keystrokes with in-memory fake clipboard backend."""
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )
        from stealth_browser_mcp.process_cleanup import process_cleanup
        from tests.browser_harness.server import FixtureServer

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        fake_backend = RecordingBackend(current="initial_clipboard_text")

        def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
            if not dir_path or not os.path.exists(dir_path):
                return
            for attempt in range(10):
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

        async def run_browser() -> None:
            temp_dir = tempfile.mkdtemp(prefix="stealth_os_paste_test_")
            browser: Optional[Any] = None
            try:
                with FixtureServer() as fixture_srv:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)
                    tab = await browser.get(fixture_srv.url_for("paste_only.html"))

                    await tab.evaluate("""
                        window.__keyEvents = [];
                        document.getElementById('paste-editor').addEventListener('keydown', function(e) {
                            window.__keyEvents.push({
                                key: e.key,
                                ctrlKey: !!e.ctrlKey,
                                metaKey: !!e.metaKey,
                                code: e.code
                            });
                        });
                    """)

                    res = await fill_tool.fill(
                        tab=tab,
                        target="#paste-editor",
                        value="Real Clipboard Editor Content",
                        strategy="os_paste",
                        verify=False,
                    )
                    self.assertTrue(res.get("success"))
                    self.assertEqual(res.get("strategy"), "os_paste")

                    raw_events = await tab.evaluate("JSON.stringify(window.__keyEvents)")
                    if hasattr(raw_events, "value"):
                        raw_events = raw_events.value
                    elif isinstance(raw_events, dict) and "value" in raw_events:
                        raw_events = raw_events["value"]
                    key_events = json.loads(raw_events) if isinstance(raw_events, str) else (raw_events or [])
                    self.assertTrue(
                        any(
                            ev.get("key") in ("v", "V") and (ev.get("ctrlKey") or ev.get("metaKey"))
                            for ev in key_events
                        ),
                        f"Expected Ctrl+V key event to reach fixture page, got: {key_events}",
                    )

                    self.assertEqual(
                        fake_backend.sets,
                        ["Real Clipboard Editor Content", "initial_clipboard_text"],
                    )
                    self.assertEqual(fake_backend.current, "initial_clipboard_text")
                    self.assertGreaterEqual(fake_backend.gets, 1)

            finally:
                if browser is not None:
                    # 1. Close browser via CDP
                    try:
                        if getattr(browser, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    # 2. Terminate main process
                    if getattr(browser, "_process", None) is not None:
                        try:
                            await _stop_process(browser._process)
                        except Exception:
                            pass
                    # 3. Disconnect open target handlers
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
                    browser._process_pid = None

                # 4. Clean up any lingering processes referencing temp profile
                try:
                    for pid in process_cleanup._get_browser_pids_for_profile(temp_dir):
                        process_cleanup._kill_process_by_pid(pid, "test_harness")
                except Exception:
                    pass

                # 5. Remove temp profile directory with retrying cleanup
                _safe_cleanup_dir(temp_dir)

        with unittest.mock.patch.object(
            os_clipboard, "_get_backend", _fake_backend_factory(fake_backend)
        ):
            asyncio.run(run_browser())


if __name__ == "__main__":
    unittest.main()
