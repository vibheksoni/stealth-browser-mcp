"""Unit tests and gated browser integration tests for session spawn (SB-47).

Ungated unit tests use synthetic fixtures, temporary directories, and fakes.
No real browser is launched and no network is used in unit tests.
Real-browser tests are gated by STEALTH_BROWSER_TESTS=1.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import (
    browser_sources,
    paths,
    profiles,
    session_registry,
    session_spawn,
)
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from tests.browser_harness.server import FixtureServer

try:
    import window_matrix
except ImportError:
    from tests import window_matrix

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class _TempDataDir(unittest.TestCase):
    """Sets up an isolated temporary data dir for tests."""

    def setUp(self):
        self._saved_data = os.environ.get(paths.DATA_DIR_ENV)
        self._saved_cap = os.environ.get(session_registry._CAP_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-session-spawn-test-")
        os.environ[paths.DATA_DIR_ENV] = self._tmp
        os.environ.pop(session_registry._CAP_ENV, None)

    def tearDown(self):
        if self._saved_data is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data
        if self._saved_cap is None:
            os.environ.pop(session_registry._CAP_ENV, None)
        else:
            os.environ[session_registry._CAP_ENV] = self._saved_cap
        shutil.rmtree(self._tmp, ignore_errors=True)


class FakeProcess:
    def __init__(self, pid: int = 12345):
        self.pid = pid
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0

    async def wait(self):
        return 0


class FakeTab:
    def __init__(self, cookies: Optional[List[Dict[str, Any]]] = None):
        self.cookies = cookies if cookies is not None else []
        self.target = SimpleNamespace(target_id="tab-1", title="Test", type_="page")

    async def send(self, command: Any) -> Any:
        if hasattr(command, "__next__") or hasattr(command, "send"):
            try:
                req = next(command)
            except StopIteration as stop:
                return stop.value
            method = req.get("method", "")
            params = req.get("params", {})
            resp: Dict[str, Any] = {}
            if method in ("Network.getCookies", "Network.getAllCookies"):
                resp = {"cookies": list(self.cookies)}
            elif method == "Browser.close":
                resp = {}
            try:
                command.send(resp)
            except StopIteration as stop:
                return stop.value
            return resp
        return None

    async def set_window_size(self, left=0, top=0, width=1920, height=1080):
        pass

    async def evaluate(self, expression: str):
        return None


class FakeBrowser:
    def __init__(self, cookies: Optional[List[Dict[str, Any]]] = None, pid: int = 24680):
        self._process = FakeProcess(pid=pid)
        self._process_pid = pid
        self.main_tab = FakeTab(cookies=cookies)
        self.tabs = [self.main_tab]
        self.connection = None
        self.targets = []
        self.config = SimpleNamespace(user_data_dir="/tmp/fake", uses_custom_data_dir=True)

    async def update_targets(self):
        pass


def make_synthetic_seed(
    name: str = "seed-a",
    source_browser: str = "chrome",
    profile_subdir: str = "Default",
    password_store: Optional[str] = "gnome-libsecret",
    cookie_rows: Optional[List[tuple]] = None,
) -> Path:
    """Create a synthetic seed directory with seed.json and cookie database."""
    seed_dir = profiles.seed_dir(name)
    seed_dir.mkdir(parents=True, exist_ok=True)
    profiles.create_seed(
        name=name,
        source_browser=source_browser,
        source_dir=str(seed_dir / "src"),
        profile_subdir=profile_subdir,
        password_store=password_store,
        defaults={"headless": True},
        now=time.time(),
    )

    # Write a synthetic Local State file
    local_state = {
        "profile": {
            "info_cache": [{"name": profile_subdir}],
            "last_used": 0,
        }
    }
    (seed_dir / browser_sources.LOCAL_STATE_FILE).write_text(
        json.dumps(local_state, ensure_ascii=True),
        encoding="ascii",
    )

    # Write a synthetic Cookies database under Network/Cookies
    cookie_dir = seed_dir / "Network"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    cookie_db = cookie_dir / "Cookies"
    con = sqlite3.connect(str(cookie_db))
    try:
        con.execute(
            "CREATE TABLE cookies ("
            "creation_utc INTEGER NOT NULL,"
            "host_key TEXT NOT NULL,"
            "top_frame_site_key TEXT NOT NULL,"
            "name TEXT NOT NULL,"
            "value TEXT NOT NULL,"
            "encrypted_value BLOB NOT NULL,"
            "path TEXT NOT NULL,"
            "expires_utc INTEGER NOT NULL,"
            "is_secure INTEGER NOT NULL,"
            "is_httponly INTEGER NOT NULL,"
            "last_access_utc INTEGER NOT NULL,"
            "has_expires INTEGER NOT NULL,"
            "is_persistent INTEGER NOT NULL,"
            "priority INTEGER NOT NULL,"
            "samesite INTEGER NOT NULL,"
            "source_scheme INTEGER NOT NULL,"
            "source_port INTEGER NOT NULL,"
            "is_same_party INTEGER NOT NULL"
            ")"
        )
        if cookie_rows is None:
            cookie_rows = [
                (
                    13300000000000000,
                    "127.0.0.1",
                    "",
                    "session_token",
                    "secret_fixture_cookie_12345",
                    b"",
                    "/",
                    13300086400000000,
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                    1,
                    1,
                    8000,
                    0,
                )
            ]
        con.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            cookie_rows,
        )
        con.commit()
    finally:
        con.close()

    # Write dummy Preferences
    (seed_dir / "Preferences").write_text("{}", encoding="ascii")
    return seed_dir


class TestSpawnPlanConstruction(_TempDataDir):
    """Unit tests for building spawn arguments and config (SB-47)."""

    def test_args_contain_disable_sync_and_profile_directory(self):
        seed_data = {
            "source_browser": "chrome",
            "profile_subdir": "Profile 2",
            "password_store": "gnome-libsecret",
        }
        args, modifier = session_spawn.build_spawn_args_and_config(
            seed_data,
            caller_args=["--no-sandbox"],
            platform_name="linux",
        )
        self.assertIn("--disable-sync", args)
        self.assertIn("--profile-directory=Profile 2", args)

    def test_linux_drops_password_store_basic_and_passes_known(self):
        seed_data = {
            "source_browser": "chrome",
            "profile_subdir": "Default",
            "password_store": "gnome-libsecret",
        }
        args, modifier = session_spawn.build_spawn_args_and_config(
            seed_data,
            caller_args=["--password-store=basic", "--disable-gpu"],
            platform_name="linux",
        )
        # In the caller args, --password-store=basic must be replaced by the seed's store
        self.assertNotIn("--password-store=basic", args)
        self.assertIn("--password-store=gnome-libsecret", args)

        # The returned modifier must also strip --password-store=basic if nodriver config adds it
        self.assertIsNotNone(modifier)
        nodriver_args = ["--password-store=basic", "--no-first-run"]
        modified = modifier(nodriver_args)
        self.assertNotIn("--password-store=basic", modified)
        self.assertIn("--password-store=gnome-libsecret", modified)

    def test_linux_drops_password_store_basic_when_seed_store_is_none(self):
        seed_data = {
            "source_browser": "chrome",
            "profile_subdir": "Default",
            "password_store": None,
        }
        args, modifier = session_spawn.build_spawn_args_and_config(
            seed_data,
            caller_args=["--password-store=basic"],
            platform_name="linux",
        )
        self.assertNotIn("--password-store=basic", args)
        self.assertFalse(any(a.startswith("--password-store=") for a in args))

        self.assertIsNotNone(modifier)
        nodriver_args = ["--password-store=basic", "--headless=new"]
        modified = modifier(nodriver_args)
        self.assertNotIn("--password-store=basic", modified)
        self.assertFalse(any(a.startswith("--password-store=") for a in modified))

    def test_non_linux_leaves_password_store_unmodified(self):
        seed_data = {
            "source_browser": "chrome",
            "profile_subdir": "Default",
            "password_store": None,
        }
        args, modifier = session_spawn.build_spawn_args_and_config(
            seed_data,
            caller_args=["--some-flag"],
            platform_name="windows",
        )
        self.assertIsNone(modifier)
        self.assertIn("--disable-sync", args)
        self.assertIn("--profile-directory=Default", args)


class TestFamilyBinarySelection(_TempDataDir):
    """Unit tests for browser family binary selection (SB-47)."""

    def test_chrome_family_candidates(self):
        candidates = browser_sources.binary_candidates("chrome")
        self.assertIn("google-chrome", candidates)
        self.assertIn("chromium", candidates)

        with mock.patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda c: "/usr/bin/google-chrome" if c == "google-chrome" else None
            with mock.patch("os.path.isfile", return_value=True), mock.patch("os.access", return_value=True):
                bin_path = session_spawn.find_family_binary("chrome")
                self.assertEqual(bin_path, "/usr/bin/google-chrome")

    def test_edge_family_candidates(self):
        candidates = browser_sources.binary_candidates("edge")
        self.assertIn("microsoft-edge", candidates)

        with mock.patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda c: "/usr/bin/microsoft-edge" if c == "microsoft-edge" else None
            with mock.patch("os.path.isfile", return_value=True), mock.patch("os.access", return_value=True):
                bin_path = session_spawn.find_family_binary("edge")
                self.assertEqual(bin_path, "/usr/bin/microsoft-edge")

    def test_brave_family_candidates(self):
        candidates = browser_sources.binary_candidates("brave")
        self.assertIn("brave-browser", candidates)

        with mock.patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda c: "/usr/bin/brave-browser" if c == "brave-browser" else None
            with mock.patch("os.path.isfile", return_value=True), mock.patch("os.access", return_value=True):
                bin_path = session_spawn.find_family_binary("brave")
                self.assertEqual(bin_path, "/usr/bin/brave-browser")

    def test_unknown_family_falls_back_or_raises(self):
        with mock.patch("shutil.which", return_value=None), mock.patch(
            "stealth_browser_mcp.session_spawn.check_browser_executable", return_value=None
        ), mock.patch(
            "stealth_browser_mcp.session_spawn.browser_sources.binary_candidates", return_value=[]
        ):
            with mock.patch("os.path.exists", return_value=False), mock.patch("os.path.isfile", return_value=False):
                with self.assertRaises(RuntimeError):
                    session_spawn.find_family_binary("nonexistent_browser_family")


class TestCapEnforcement(_TempDataDir):
    """Unit tests for machine-wide session cap enforcement (SB-47 / SB-43)."""

    def test_cap_enforcement_across_spawns(self):
        make_synthetic_seed("cap-seed")

        # Spawn 1: under cap=2 -> succeeds
        s1_id, s1_dir, _ = session_spawn.prepare_session_spawn("cap-seed", cap=2)
        self.assertTrue(s1_dir.is_dir())
        self.assertTrue(profiles.session_exists(s1_id))

        # Spawn 2: under cap=2 -> succeeds
        s2_id, s2_dir, _ = session_spawn.prepare_session_spawn("cap-seed", cap=2)
        self.assertTrue(s2_dir.is_dir())
        self.assertTrue(profiles.session_exists(s2_id))

        # Spawn 3: cap=2 reached -> raises ProfileError
        with self.assertRaises(profiles.ProfileError) as ctx:
            session_spawn.prepare_session_spawn("cap-seed", cap=2)
        self.assertIn("cap of 2 reached", str(ctx.exception))

        # Close session 1 -> frees one slot
        session_spawn.cleanup_session(s1_id)
        self.assertFalse(profiles.session_exists(s1_id))

        # Spawn 4: now room available -> succeeds
        s4_id, s4_dir, _ = session_spawn.prepare_session_spawn("cap-seed", cap=2)
        self.assertTrue(profiles.session_exists(s4_id))

        session_spawn.cleanup_session(s2_id)
        session_spawn.cleanup_session(s4_id)


class TestTimingAndCarryoverLog(_TempDataDir):
    """Unit tests for timing logs and carryover checks (SB-47)."""

    def test_timing_recorded_in_session_json_and_result(self):
        make_synthetic_seed("time-seed")
        s_id, s_dir, seed_data = session_spawn.prepare_session_spawn("time-seed")
        clone_ms = session_spawn.clone_seed_to_session("time-seed", s_id, seed_data)
        self.assertGreaterEqual(clone_ms, 0.0)

        fake_browser = FakeBrowser(pid=55555)
        t_launch_start = time.monotonic() - 0.05
        t_spawn_start = t_launch_start - (clone_ms / 1000.0)

        result = asyncio.run(
            session_spawn.finish_session_spawn(
                session_id=s_id,
                seed_name="time-seed",
                session_dir=s_dir,
                browser=fake_browser,
                clone_ms=clone_ms,
                t_launch_start=t_launch_start,
                t_spawn_start=t_spawn_start,
                check_url=None,
            )
        )

        self.assertEqual(result["session_id"], s_id)
        self.assertEqual(result["seed"], "time-seed")
        self.assertIn("timing", result)
        self.assertGreaterEqual(result["timing"]["clone_ms"], 0.0)
        self.assertGreaterEqual(result["timing"]["launch_ms"], 0.0)
        self.assertGreaterEqual(result["timing"]["total_ms"], 0.0)

        # Verify session.json on disk contains the timings and browser_pid
        disk_data = profiles.load_session(s_id)
        self.assertEqual(disk_data["browser_pid"], 55555)
        self.assertEqual(disk_data["clone_ms"], result["timing"]["clone_ms"])
        self.assertEqual(disk_data["launch_ms"], result["timing"]["launch_ms"])
        self.assertEqual(disk_data["total_ms"], result["timing"]["total_ms"])

        session_spawn.cleanup_session(s_id)

    def test_carryover_check_with_cookies(self):
        make_synthetic_seed("carry-seed")
        s_id, s_dir, seed_data = session_spawn.prepare_session_spawn("carry-seed")
        clone_ms = session_spawn.clone_seed_to_session("carry-seed", s_id, seed_data)

        fake_cookies = [{"name": "session_token", "value": "secret_xyz"}]
        fake_browser = FakeBrowser(cookies=fake_cookies)

        result = asyncio.run(
            session_spawn.finish_session_spawn(
                session_id=s_id,
                seed_name="carry-seed",
                session_dir=s_dir,
                browser=fake_browser,
                clone_ms=clone_ms,
                t_launch_start=time.monotonic(),
                t_spawn_start=time.monotonic(),
                check_url="http://127.0.0.1:8000/login.html",
            )
        )
        self.assertTrue(result.get("carried_over"))
        # Ensure cookie values are NEVER exposed in the result dict
        res_text = json.dumps(result)
        self.assertNotIn("secret_xyz", res_text)

        session_spawn.cleanup_session(s_id)

    def test_carryover_check_without_cookies(self):
        make_synthetic_seed("carry-none-seed")
        s_id, s_dir, seed_data = session_spawn.prepare_session_spawn("carry-none-seed")
        clone_ms = session_spawn.clone_seed_to_session("carry-none-seed", s_id, seed_data)

        fake_browser = FakeBrowser(cookies=[])
        result = asyncio.run(
            session_spawn.finish_session_spawn(
                session_id=s_id,
                seed_name="carry-none-seed",
                session_dir=s_dir,
                browser=fake_browser,
                clone_ms=clone_ms,
                t_launch_start=time.monotonic(),
                t_spawn_start=time.monotonic(),
                check_url="http://127.0.0.1:8000/login.html",
            )
        )
        self.assertFalse(result.get("carried_over"))
        session_spawn.cleanup_session(s_id)


class TestCleanupOnClose(_TempDataDir):
    """Unit tests for directory removal and registry cleanup on close (SB-47)."""

    def test_close_removes_dir_and_registry(self):
        make_synthetic_seed("clean-seed")
        s_id, s_dir, seed_data = session_spawn.prepare_session_spawn("clean-seed")
        session_spawn.clone_seed_to_session("clean-seed", s_id, seed_data)

        self.assertTrue(s_dir.is_dir())
        self.assertTrue(profiles.session_exists(s_id))

        session_spawn.cleanup_session(s_id, keep=False)

        self.assertFalse(s_dir.is_dir())
        self.assertFalse(profiles.session_exists(s_id))

    def test_keep_true_preserves_directory(self):
        make_synthetic_seed("keep-seed")
        s_id, s_dir, seed_data = session_spawn.prepare_session_spawn("keep-seed")
        session_spawn.clone_seed_to_session("keep-seed", s_id, seed_data)

        self.assertTrue(s_dir.is_dir())

        session_spawn.cleanup_session(s_id, keep=True)

        self.assertTrue(s_dir.is_dir())
        # Clean up manually
        shutil.rmtree(s_dir, ignore_errors=True)

    def test_startup_janitor_removes_dead_sessions(self):
        make_synthetic_seed("janitor-seed")
        # Session 1: dead pid (999999999)
        s1 = session_registry.create_session(
            seed="janitor-seed",
            pid=999999999,
        )["id"]
        s1_dir = profiles.session_dir(s1)
        self.assertTrue(s1_dir.is_dir())

        # Session 2: live pid (os.getpid())
        s2 = session_registry.create_session(
            seed="janitor-seed",
            pid=os.getpid(),
        )["id"]
        s2_dir = profiles.session_dir(s2)
        self.assertTrue(s2_dir.is_dir())

        removed = session_spawn.startup_janitor()
        self.assertIn(s1, removed)
        self.assertNotIn(s2, removed)

        self.assertFalse(s1_dir.is_dir())
        self.assertTrue(s2_dir.is_dir())

        session_spawn.cleanup_session(s2)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestSessionSpawnGatedRealBrowser(_TempDataDir):
    """Gated real-browser acceptance test for SB-47.

    Acceptance criteria:
    - Build a synthetic seed by launching Chrome on a temp dir, log in to fixture, close.
    - Refresh/clone into a seed dir.
    - Spawn two sessions from the one seed.
    - Both sessions logged in to fixture.
    - Logout in one session leaves the other logged in.
    - Closing one session leaves the other running.
    - Session dirs removed after close.
    - Spawn time recorded.
    - Runtime cap: completes within 60s.
    """

    def test_gated_two_sessions_from_one_seed(self):
        import nodriver as uc
        from stealth_browser_mcp import seed_tools
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )

        test_start_time = time.monotonic()

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        async def run_acceptance():
            window_matrix.skip_if_no_display(self)
            temp_source_dir = tempfile.mkdtemp(prefix="stealth_sb47_gated_source_")
            browser_seed_creator = None
            manager = BrowserManager()
            inst1 = None
            inst2 = None
            seed_name = "sb47-gated-seed"

            try:
                with FixtureServer() as server:
                    # Step 1: Create synthetic source by logging in on temp profile
                    sandbox_flag = False if sandbox_must_be_disabled() else True
                    config = uc.Config(
                        headless=window_matrix.HEADLESS,
                        user_data_dir=temp_source_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=sandbox_flag,
                    )
                    browser_seed_creator = await launch_browser(config, timeout=30.0)
                    login_url = server.url_for("login.html")
                    tab0 = await browser_seed_creator.get(login_url)

                    # Click login button
                    await tab0.evaluate("document.getElementById('login-button').click()")
                    await asyncio.sleep(0.5)

                    # Set persistent cookies
                    await tab0.evaluate(
                        "document.cookie = 'session_token=secret_fixture_cookie_12345; Path=/; Max-Age=86400; SameSite=Lax';"
                        "document.cookie = 'auth_user=testadmin; Path=/; Max-Age=86400; SameSite=Lax';"
                    )
                    await asyncio.sleep(0.5)

                    # Orderly shutdown of source browser to flush SQLite cookie store
                    proc = getattr(browser_seed_creator, "_process", None)
                    if getattr(browser_seed_creator, "connection", None) is not None:
                        try:
                            await asyncio.wait_for(
                                browser_seed_creator.connection.send(uc.cdp.browser.close()),
                                timeout=5.0,
                            )
                        except Exception:
                            pass
                    if proc is not None:
                        try:
                            await _stop_process(proc)
                        except Exception:
                            pass

                    # Wait for browser process to exit; kill if still alive after 10 s
                    if proc is not None:
                        deadline_proc = time.monotonic() + 10.0
                        while time.monotonic() < deadline_proc:
                            poll_val = proc.poll() if hasattr(proc, "poll") else None
                            if poll_val is not None:
                                break
                            await asyncio.sleep(0.1)
                        else:
                            try:
                                if hasattr(proc, "kill"):
                                    proc.kill()
                            except Exception:
                                pass
                    browser_seed_creator = None

                    # Wait for Cookies sqlite file to exist and contain rows before reading/copying
                    def _cookies_db_has_rows() -> bool:
                        candidates = [
                            Path(temp_source_dir) / "Default" / "Network" / "Cookies",
                            Path(temp_source_dir) / "Default" / "Cookies",
                            Path(temp_source_dir) / "Network" / "Cookies",
                            Path(temp_source_dir) / "Cookies",
                        ]
                        target_db = None
                        for cand in candidates:
                            if cand.is_file():
                                target_db = cand
                                break
                        if not target_db:
                            return False
                        try:
                            con = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
                            try:
                                count = con.execute("SELECT COUNT(*) FROM cookies").fetchone()
                                return bool(count and count[0] > 0)
                            finally:
                                con.close()
                        except Exception:
                            return False

                    await window_matrix.wait_until_async(
                        _cookies_db_has_rows,
                        timeout=15.0,
                        interval=0.1,
                        message="Cookies sqlite file was not flushed with rows",
                    )

                    # Step 2: Refresh seed using seed_tools
                    ref_res = seed_tools.refresh_seed(
                        name=seed_name,
                        source_browser="chrome",
                        source_dir=temp_source_dir,
                        platform="linux",
                    )
                    self.assertEqual(ref_res["status"], "ok")
                    self.assertTrue(profiles.seed_exists(seed_name))

                    # Step 3: Spawn two sessions from the one seed
                    opt1 = BrowserOptions(
                        headless=window_matrix.HEADLESS,
                        window=window_matrix.window_mode(),
                        sandbox=sandbox_flag,
                        seed=seed_name,
                        check_url=login_url,
                    )
                    inst1 = await manager.spawn_browser(opt1)
                    s1_info = await manager.get_session_info(inst1.instance_id)
                    self.assertIsNotNone(s1_info)
                    self.assertIn("clone_ms", s1_info)
                    self.assertIn("launch_ms", s1_info)
                    self.assertIn("total_ms", s1_info)
                    self.assertTrue(s1_info.get("carried_over"))
                    s1_dir = Path(s1_info["user_data_dir"])
                    self.assertTrue(s1_dir.is_dir())

                    opt2 = BrowserOptions(
                        headless=window_matrix.HEADLESS,
                        window=window_matrix.window_mode(),
                        sandbox=sandbox_flag,
                        seed=seed_name,
                        check_url=login_url,
                    )
                    inst2 = await manager.spawn_browser(opt2)
                    s2_info = await manager.get_session_info(inst2.instance_id)
                    self.assertIsNotNone(s2_info)
                    self.assertIn("clone_ms", s2_info)
                    self.assertIn("launch_ms", s2_info)
                    self.assertIn("total_ms", s2_info)
                    self.assertTrue(s2_info.get("carried_over"))
                    s2_dir = Path(s2_info["user_data_dir"])
                    self.assertTrue(s2_dir.is_dir())
                    self.assertNotEqual(s1_info["session_id"], s2_info["session_id"])

                    # Step 4: Verify both sessions are logged in to the fixture
                    await manager.navigate(inst1.instance_id, login_url, wait_until="load")
                    tab1 = await manager.get_navigation_tab(inst1.instance_id)
                    state1 = await tab1.evaluate("document.getElementById('auth-state').textContent")
                    self.assertEqual(state1, "logged_in")

                    await manager.navigate(inst2.instance_id, login_url, wait_until="load")
                    tab2 = await manager.get_navigation_tab(inst2.instance_id)
                    state2 = await tab2.evaluate("document.getElementById('auth-state').textContent")
                    self.assertEqual(state2, "logged_in")

                    # Step 5: Logout in session 1; leaves session 2 logged in
                    await tab1.evaluate("document.getElementById('logout-button').click()")
                    async def _check_logged_out():
                        text = await tab1.evaluate("document.getElementById('auth-state').textContent")
                        return text == "logged_out"
                    await window_matrix.wait_until_async(_check_logged_out, timeout=5.0)

                    state1_after_logout = await tab1.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(state1_after_logout, "logged_out")

                    # Session 2 must still be logged in!
                    state2_still_in = await tab2.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(state2_still_in, "logged_in")

                    # Step 6: Closing session 1 leaves session 2 running and removes session 1 directory
                    closed_s1 = await manager.close_instance(inst1.instance_id)
                    self.assertTrue(closed_s1)
                    inst1 = None
                    # Verify s1 directory was removed
                    self.assertFalse(s1_dir.is_dir())

                    # Session 2 is still running and responsive
                    state2_check = await tab2.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(state2_check, "logged_in")

                    # Step 7: Close session 2; its directory is removed
                    closed_s2 = await manager.close_instance(inst2.instance_id)
                    self.assertTrue(closed_s2)
                    inst2 = None
                    self.assertFalse(s2_dir.is_dir())

            finally:
                if browser_seed_creator is not None:
                    try:
                        if getattr(browser_seed_creator, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser_seed_creator.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    if getattr(browser_seed_creator, "_process", None) is not None:
                        try:
                            await _stop_process(browser_seed_creator._process)
                        except Exception:
                            pass
                if inst1 is not None:
                    try:
                        await manager.close_instance(inst1.instance_id)
                    except Exception:
                        pass
                if inst2 is not None:
                    try:
                        await manager.close_instance(inst2.instance_id)
                    except Exception:
                        pass
                try:
                    await manager.close_all()
                except Exception:
                    pass
                shutil.rmtree(temp_source_dir, ignore_errors=True)

            elapsed = time.monotonic() - test_start_time
            self.assertLess(elapsed, 60.0, f"Gated acceptance test exceeded 60s: {elapsed:.2f}s")

        asyncio.run(run_acceptance())


if __name__ == "__main__":
    unittest.main()
