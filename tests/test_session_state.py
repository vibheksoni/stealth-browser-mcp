"""Tests for session save and restore functionality (SB-33)."""

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import cdp_raw, session_state
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions, BrowserState
from tests.browser_harness.server import FixtureServer

try:
    import window_matrix
except ImportError:
    from tests import window_matrix

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    """Safely remove a temporary directory."""
    if not dir_path or not os.path.exists(dir_path):
        return
    for attempt in range(10):
        try:
            shutil.rmtree(dir_path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


class FakeCDPTab:
    """Fake tab supporting CDP commands for Network and DOMStorage."""

    def __init__(
        self,
        browser: Optional[Any] = None,
        target_id: str = "tab-0",
        url: str = "http://127.0.0.1:54321/login.html",
        cookies: Optional[List[Dict[str, Any]]] = None,
        storage: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self.browser = browser
        self.target = SimpleNamespace(target_id=target_id, url=url)
        self.url = url
        self.cookies: List[Dict[str, Any]] = list(cookies or [])
        self.storage: Dict[str, Dict[str, str]] = {
            k: dict(v) for k, v in (storage or {}).items()
        }
        self.sent_commands: List[Tuple[str, Dict[str, Any]]] = []

    async def send(self, command: Any) -> Any:
        if hasattr(command, "__next__"):
            request = next(command)
        elif isinstance(command, dict):
            request = command
        else:
            request = {"method": getattr(command, "method", str(command)), "params": {}}

        method = request.get("method", "")
        params = request.get("params", {})
        self.sent_commands.append((method, params))

        result = self._dispatch(method, params)

        if hasattr(command, "send"):
            try:
                command.send(result)
            except StopIteration as exc:
                return exc.value
        return result

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "Network.getAllCookies":
            return {"cookies": [dict(c) for c in self.cookies]}

        if method == "Network.setCookies":
            incoming = params.get("cookies", [])
            for c in incoming:
                self.cookies = [
                    existing
                    for existing in self.cookies
                    if not (
                        existing.get("name") == c.get("name")
                        and existing.get("domain") == c.get("domain")
                    )
                ]
                self.cookies.append(dict(c))
            return {}

        if method == "Network.setCookie":
            self.cookies = [
                existing
                for existing in self.cookies
                if not (
                    existing.get("name") == params.get("name")
                    and existing.get("domain") == params.get("domain")
                )
            ]
            self.cookies.append(dict(params))
            return {"success": True}

        if method == "Network.clearBrowserCookies":
            self.cookies.clear()
            return {}

        if method == "DOMStorage.enable":
            return {}

        if method == "DOMStorage.getDOMStorageItems":
            storage_id = params.get("storageId", {})
            origin = storage_id.get("securityOrigin")
            origin_items = self.storage.get(origin, {})
            entries = [[k, v] for k, v in sorted(origin_items.items())]
            return {"entries": entries}

        if method == "DOMStorage.setDOMStorageItem":
            storage_id = params.get("storageId", {})
            origin = storage_id.get("securityOrigin")
            key = params.get("key")
            value = params.get("value")
            if origin not in self.storage:
                self.storage[origin] = {}
            self.storage[origin][str(key)] = str(value)
            return {}

        if method == "DOMStorage.clear":
            storage_id = params.get("storageId", {})
            origin = storage_id.get("securityOrigin")
            if origin in self.storage:
                self.storage[origin].clear()
            return {}

        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "localStorage.key" in expr or "localStorage.length" in expr:
                parsed = urllib.parse.urlsplit(self.url)
                curr_origin = f"{parsed.scheme}://{parsed.netloc}"
                items = [[k, v] for k, v in self.storage.get(curr_origin, {}).items()]
                return {"result": {"value": items}}
            return {"result": {}}

        if method in ("Page.enable", "Page.addScriptToEvaluateOnNewDocument"):
            return "script-id-1"

        return {}


class FakeBrowserForSession:
    """Fake browser holding multiple tabs."""

    def __init__(self, tabs: List[FakeCDPTab]) -> None:
        self.tabs = tabs
        for tab in self.tabs:
            tab.browser = self


class FakeBrowserInstance:
    """Fake BrowserInstance supporting update_activity for BrowserManager."""

    def __init__(self, instance_id: str = "test_inst") -> None:
        self.instance_id = instance_id
        self.state = BrowserState.READY

    def update_activity(self) -> None:
        pass


def create_fake_manager(
    browser: FakeBrowserForSession,
    current_tab: FakeCDPTab,
    instance_id: str = "test_inst",
) -> BrowserManager:
    """Build a BrowserManager pre-populated with a fake browser instance."""
    manager = BrowserManager()
    manager._instances[instance_id] = {
        "browser": browser,
        "tab": current_tab,
        "instance": FakeBrowserInstance(instance_id),
        "navigation_count": 0,
        "options": BrowserOptions(),
        "init_scripts": [],
        "configured_tabs": set(),
        "idle_timeout_seconds": 0,
        "network_data": [],
    }
    return manager


class TestSessionStateUnit(unittest.TestCase):
    """Ungated unit tests for session save and restore using fakes and temp dirs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_session_unit_")
        self._old_data_dir = os.environ.get("STEALTH_BROWSER_MCP_DATA_DIR")
        os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = self.temp_dir

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("STEALTH_BROWSER_MCP_DATA_DIR", None)
        else:
            os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = self._old_data_dir
        _safe_cleanup_dir(self.temp_dir)

    def test_default_session_path_creation(self) -> None:
        """Verify default_session_path generates a path and ensures directory exists."""
        path = session_state.default_session_path("inst_123")
        self.assertTrue(path.name.endswith(".json"))
        self.assertIn("inst_123", path.name)
        self.assertTrue(path.parent.is_dir())

    def test_write_and_read_session_file_mode_0600(self) -> None:
        """Verify session file is written with mode 0600 on POSIX and round-trips JSON."""
        target_file = Path(self.temp_dir) / "test_session.json"
        state = {
            "cookies": [
                {
                    "name": "auth_token",
                    "value": "xyz123",
                    "domain": "example.com",
                    "path": "/",
                    "expires": 1800000000.0,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": "https://example.com",
                    "localStorage": [{"name": "theme", "value": "dark"}],
                }
            ],
        }

        written_path = session_state.write_session_file(target_file, state)
        self.assertEqual(written_path, target_file.resolve())
        self.assertTrue(target_file.is_file())

        if os.name == "posix":
            mode = os.stat(target_file).st_mode
            permissions = stat.S_IMODE(mode)
            self.assertEqual(
                permissions,
                0o600,
                f"Expected mode 0600, got {oct(permissions)}",
            )

        read_data = session_state.read_session_file(target_file)
        self.assertEqual(read_data, state)

    def test_write_session_file_overwrites_with_mode_0600(self) -> None:
        """Verify overwriting an existing file resets its permissions to mode 0600."""
        target_file = Path(self.temp_dir) / "existing.json"
        target_file.write_text("{}", encoding="utf-8")
        if os.name == "posix":
            os.chmod(target_file, 0o644)

        session_state.write_session_file(target_file, {"cookies": []})
        if os.name == "posix":
            mode = os.stat(target_file).st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_read_session_file_errors(self) -> None:
        """Verify read_session_file raises appropriate errors on missing or invalid files."""
        nonexistent = Path(self.temp_dir) / "does_not_exist.json"
        with self.assertRaises(FileNotFoundError):
            session_state.read_session_file(nonexistent)

        invalid_json = Path(self.temp_dir) / "invalid.json"
        invalid_json.write_text("not json content", encoding="utf-8")
        with self.assertRaises(Exception):
            session_state.read_session_file(invalid_json)

        array_json = Path(self.temp_dir) / "array.json"
        array_json.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            session_state.read_session_file(array_json)

    def test_sanitize_cookie_for_cdp(self) -> None:
        """Verify cookie sanitization for CDP Network.setCookies requirements."""
        # 1. Playwright-style session cookie (expires = -1)
        pw_cookie = {
            "name": "sess",
            "value": "val",
            "domain": "example.com",
            "path": "/sub",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "lax",
            "size": 42,
            "session": True,
        }
        sanitized = session_state.sanitize_cookie_for_cdp(pw_cookie)
        self.assertEqual(sanitized["name"], "sess")
        self.assertEqual(sanitized["value"], "val")
        self.assertEqual(sanitized["domain"], "example.com")
        self.assertEqual(sanitized["path"], "/sub")
        self.assertEqual(sanitized["httpOnly"], True)
        self.assertEqual(sanitized["secure"], True)
        self.assertEqual(sanitized["sameSite"], "Lax")
        self.assertEqual(sanitized["url"], "https://example.com/sub")
        self.assertNotIn("expires", sanitized)
        self.assertNotIn("size", sanitized)
        self.assertNotIn("session", sanitized)

        # 2. Cookie with valid expiration timestamp and port
        ts_cookie = {
            "name": "token",
            "value": "abc",
            "domain": ".127.0.0.1",
            "path": "/",
            "expires": 1800000000.5,
            "secure": False,
            "sourcePort": 54321,
            "priority": "medium",
        }
        sanitized_ts = session_state.sanitize_cookie_for_cdp(ts_cookie)
        self.assertEqual(sanitized_ts["expires"], 1800000000.5)
        self.assertEqual(sanitized_ts["sourcePort"], 54321)
        self.assertEqual(sanitized_ts["priority"], "Medium")
        self.assertEqual(sanitized_ts["url"], "http://127.0.0.1:54321/")

    def test_save_session_state_with_fake_tab(self) -> None:
        """Verify save_session_state extracts cookies and localStorage per origin."""
        fake_tab = FakeCDPTab(
            url="http://127.0.0.1:54321/dashboard.html",
            cookies=[
                {
                    "name": "token",
                    "value": "secret",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": 1800000000.0,
                    "httpOnly": True,
                    "secure": False,
                    "sameSite": "Lax",
                }
            ],
            storage={
                "http://127.0.0.1:54321": {
                    "user_id": "42",
                    "active_view": "list",
                }
            },
        )

        async def run() -> Dict[str, Any]:
            return await session_state.save_session_state(fake_tab)

        result = asyncio.run(run())
        self.assertIn("cookies", result)
        self.assertIn("origins", result)
        self.assertEqual(len(result["cookies"]), 1)
        self.assertEqual(result["cookies"][0]["name"], "token")

        self.assertEqual(len(result["origins"]), 1)
        origin_entry = result["origins"][0]
        self.assertEqual(origin_entry["origin"], "http://127.0.0.1:54321")
        self.assertEqual(
            origin_entry["localStorage"],
            [
                {"name": "active_view", "value": "list"},
                {"name": "user_id", "value": "42"},
            ],
        )

    def test_restore_session_state_with_fake_tab(self) -> None:
        """Verify restore_session_state populates cookies and localStorage per origin."""
        fake_tab = FakeCDPTab(
            url="http://127.0.0.1:54321/index.html",
            cookies=[],
            storage={},
        )

        state = {
            "cookies": [
                {
                    "name": "auth_cookie",
                    "value": "cookie_val",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "secure": False,
                }
            ],
            "origins": [
                {
                    "origin": "http://127.0.0.1:54321",
                    "localStorage": [
                        {"name": "stored_key", "value": "stored_val"},
                    ],
                }
            ],
        }

        async def run() -> Dict[str, Any]:
            return await session_state.restore_session_state(
                fake_tab, state, clear_existing=True
            )

        summary = asyncio.run(run())
        self.assertTrue(summary["success"])
        self.assertEqual(summary["cookies_restored"], 1)
        self.assertEqual(summary["origins_restored"], 1)

        self.assertEqual(len(fake_tab.cookies), 1)
        self.assertEqual(fake_tab.cookies[0]["name"], "auth_cookie")
        self.assertEqual(
            fake_tab.storage["http://127.0.0.1:54321"],
            {"stored_key": "stored_val"},
        )

    def test_round_trip_into_fresh_fake_profile(self) -> None:
        """Verify round-trip save and restore into a fresh fake browser tab."""
        initial_cookies = [
            {
                "name": "session_id",
                "value": "sess_999",
                "domain": "127.0.0.1",
                "path": "/",
                "expires": 1800000000.0,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ]
        initial_storage = {
            "http://127.0.0.1:54321": {
                "user": "alice",
                "role": "admin",
            }
        }

        tab_a = FakeCDPTab(
            url="http://127.0.0.1:54321/app",
            cookies=initial_cookies,
            storage=initial_storage,
        )
        browser_a = FakeBrowserForSession([tab_a])
        manager_a = create_fake_manager(browser_a, tab_a)

        saved_file = Path(self.temp_dir) / "roundtrip_session.json"

        async def step_1() -> Dict[str, Any]:
            return await session_state.save_session(
                "test_inst",
                file_path=str(saved_file),
                browser_manager=manager_a,
            )

        save_res = asyncio.run(step_1())
        self.assertTrue(save_res["success"])
        self.assertTrue(saved_file.is_file())

        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(saved_file).st_mode), 0o600)

        # Fresh profile: Tab B starts completely empty
        tab_b = FakeCDPTab(
            url="about:blank",
            cookies=[],
            storage={},
        )
        browser_b = FakeBrowserForSession([tab_b])
        manager_b = create_fake_manager(browser_b, tab_b)

        async def step_2() -> Dict[str, Any]:
            return await session_state.restore_session(
                "test_inst",
                file_path=str(saved_file),
                browser_manager=manager_b,
            )

        restore_res = asyncio.run(step_2())
        self.assertTrue(restore_res["success"])
        self.assertEqual(restore_res["cookies_restored"], 1)
        self.assertEqual(restore_res["origins_restored"], 1)

        tab_b.url = "http://127.0.0.1:54321/app"

        async def step_3() -> Dict[str, Any]:
            second_file = Path(self.temp_dir) / "roundtrip_b.json"
            return await session_state.save_session(
                "test_inst",
                file_path=str(second_file),
                browser_manager=manager_b,
            )

        save_res_b = asyncio.run(step_3())
        self.assertTrue(save_res_b["success"])

        data_a = json.loads(saved_file.read_text(encoding="utf-8"))
        data_b = json.loads(Path(save_res_b["file_path"]).read_text(encoding="utf-8"))

        self.assertEqual(data_a["cookies"][0]["name"], data_b["cookies"][0]["name"])
        self.assertEqual(data_a["cookies"][0]["value"], data_b["cookies"][0]["value"])
        self.assertEqual(data_a["origins"], data_b["origins"])

    def test_restore_session_data_formats(self) -> None:
        """Verify restore_session accepts dict or JSON string session_data."""
        fake_tab = FakeCDPTab(url="http://127.0.0.1:54321/", cookies=[], storage={})
        browser = FakeBrowserForSession([fake_tab])
        manager = create_fake_manager(browser, fake_tab)

        payload = {
            "cookies": [{"name": "c1", "value": "v1", "domain": "127.0.0.1"}],
            "origins": [{"origin": "http://127.0.0.1:54321", "localStorage": [{"name": "k1", "value": "v1"}]}],
        }

        # 1. Dict
        res_dict = asyncio.run(session_state.restore_session("test_inst", session_data=payload, browser_manager=manager))
        self.assertTrue(res_dict["success"])
        self.assertEqual(fake_tab.cookies[0]["name"], "c1")

        # 2. String
        res_str = asyncio.run(session_state.restore_session("test_inst", session_data=json.dumps(payload), browser_manager=manager))
        self.assertTrue(res_str["success"])

        # 3. Missing both file_path and session_data when no default file exists
        def_path = session_state.default_session_path("test_inst")
        if def_path.is_file():
            try:
                def_path.unlink()
            except OSError:
                pass
        with self.assertRaises(ValueError):
            asyncio.run(session_state.restore_session("nonexistent_without_default", browser_manager=manager))
        with self.assertRaises(ValueError):
            asyncio.run(session_state.restore_session("test_inst", browser_manager=manager))


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestSessionStateGated(unittest.TestCase):
    """Gated real-browser integration tests for session save and restore."""

    def test_round_trip_login_into_fresh_temp_profile(self) -> None:
        """Verify login session save and restore into a fresh temporary browser profile."""
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions
        from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
        from stealth_browser_mcp.process_cleanup import process_cleanup

        async def run_test() -> None:
            window_matrix.skip_if_no_display(self)
            start_time = time.monotonic()
            temp_dir_a = tempfile.mkdtemp(prefix="stealth_session_real_a_")
            temp_dir_b = tempfile.mkdtemp(prefix="stealth_session_real_b_")
            session_file_path = Path(temp_dir_a) / "saved_session.json"

            manager_a = BrowserManager()
            manager_b = BrowserManager()
            instance_a = None
            instance_b = None

            try:
                with FixtureServer() as server:
                    login_url = server.url_for("login.html")

                    # Phase 1: Launch Profile A, log in, and save session
                    opts_a = BrowserOptions(
                        headless=window_matrix.HEADLESS,
                        window=window_matrix.window_mode(),
                        user_data_dir=temp_dir_a,
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    instance_a = await manager_a.spawn_browser(opts_a)
                    await manager_a.navigate(
                        instance_a.instance_id,
                        login_url,
                        wait_until="load",
                        timeout=15000,
                    )
                    tab_a = await manager_a.get_navigation_tab(instance_a.instance_id)

                    initial_status = await tab_a.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(initial_status, "logged_out")

                    await tab_a.evaluate("document.getElementById('login-button').click()")
                    await asyncio.sleep(0.1)

                    logged_status = await tab_a.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(logged_status, "logged_in")

                    save_res = await session_state.save_session(
                        instance_a.instance_id,
                        file_path=str(session_file_path),
                        browser_manager=manager_a,
                    )
                    self.assertTrue(save_res["success"])
                    self.assertTrue(session_file_path.is_file())

                    if os.name == "posix":
                        mode = os.stat(session_file_path).st_mode
                        self.assertEqual(
                            stat.S_IMODE(mode),
                            0o600,
                            f"Expected mode 0600, got {oct(stat.S_IMODE(mode))}",
                        )

                    session_data = json.loads(session_file_path.read_text(encoding="utf-8"))
                    self.assertGreater(len(session_data["cookies"]), 0)
                    self.assertGreater(len(session_data["origins"]), 0)

                    # Close Profile A
                    await manager_a.close_instance(instance_a.instance_id)
                    instance_a = None

                    # Phase 2: Launch fresh Profile B (empty profile dir)
                    opts_b = BrowserOptions(
                        headless=window_matrix.HEADLESS,
                        window=window_matrix.window_mode(),
                        user_data_dir=temp_dir_b,
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    instance_b = await manager_b.spawn_browser(opts_b)

                    # Restore session into Profile B
                    restore_res = await session_state.restore_session(
                        instance_b.instance_id,
                        file_path=str(session_file_path),
                        browser_manager=manager_b,
                    )
                    self.assertTrue(restore_res["success"])

                    # Navigate Profile B to the login page
                    await manager_b.navigate(
                        instance_b.instance_id,
                        login_url,
                        wait_until="load",
                        timeout=15000,
                    )
                    tab_b = await manager_b.get_navigation_tab(instance_b.instance_id)

                    # Profile B should be immediately logged in without clicking login
                    b_status = await tab_b.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(
                        b_status,
                        "logged_in",
                        "Fresh profile did not carry over logged_in state after restore",
                    )

                    cookie_b = await tab_b.evaluate("document.cookie")
                    self.assertIn("session_token=secret_fixture_cookie_12345", cookie_b)

                    storage_token = await tab_b.evaluate(
                        "localStorage.getItem('session_token')"
                    )
                    self.assertEqual(storage_token, "secret_fixture_cookie_12345")

                    storage_user = await tab_b.evaluate(
                        "localStorage.getItem('auth_user')"
                    )
                    self.assertEqual(storage_user, "testadmin")

            finally:
                if instance_a is not None:
                    try:
                        await manager_a.close_instance(instance_a.instance_id)
                    except Exception:
                        pass
                if instance_b is not None:
                    try:
                        await manager_b.close_instance(instance_b.instance_id)
                    except Exception:
                        pass
                try:
                    await manager_a.close_all()
                    await manager_b.close_all()
                except Exception:
                    pass
                for p_dir in (temp_dir_a, temp_dir_b):
                    try:
                        for pid in process_cleanup._get_browser_pids_for_profile(p_dir):
                            process_cleanup._kill_process_by_pid(pid, "test_session_state")
                    except Exception:
                        pass
                    _safe_cleanup_dir(p_dir)

            elapsed = time.monotonic() - start_time
            self.assertLess(elapsed, 60.0, f"Gated session test exceeded 60s ({elapsed:.2f}s)")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
