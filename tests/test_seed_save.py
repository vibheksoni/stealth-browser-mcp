"""Unit tests and gated browser integration tests for save_to_seed (SB-49).

Ungated unit tests use synthetic fixtures, temporary directories, and fakes.
No real browser is launched and no network is used in unit tests.
Real-browser tests are gated by STEALTH_BROWSER_TESTS=1.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
    file_lock,
    paths,
    profiles,
    seed_save,
    seed_snapshots,
    server,
    session_spawn,
)
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

SRC = str(ROOT / "src")

HOLDER_SCRIPT = """
import os
import sys
import time

sys.path.insert(0, {src!r})
from stealth_browser_mcp import seed_save

name, die_file, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
lock = seed_save.seed_lock(name, timeout=2.0)
lock.acquire()
with open(pid_file, "w") as handle:
    handle.write(str(os.getpid()))
while not os.path.exists(die_file):
    time.sleep(0.05)
lock.release()
sys.exit(0)
"""


def sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of file at path."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hashes(root: Path) -> Dict[str, str]:
    """Return {rel_path: sha256} for every file under root."""
    hashes: Dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            if name.startswith("Singleton"):
                continue
            abs_path = Path(dirpath) / name
            try:
                if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                    continue
                rel = abs_path.relative_to(root).as_posix()
                hashes[rel] = sha256(abs_path)
            except FileNotFoundError:
                continue
    return hashes


def make_sqlite_cookies(path: Path, cookie_rows: Optional[List[tuple]] = None) -> None:
    """Create a synthetic Cookies sqlite database at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
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
        if cookie_rows:
            con.executemany(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                cookie_rows,
            )
        con.commit()
    finally:
        con.close()


class _TempDataDir(unittest.TestCase):
    """Sets up an isolated temporary data dir for tests."""

    def setUp(self):
        self._saved_data = os.environ.get(paths.DATA_DIR_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-seed-save-test-")
        os.environ[paths.DATA_DIR_ENV] = self._tmp

    def tearDown(self):
        if self._saved_data is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _create_sample_seed(
        self,
        name: str = "test_seed",
        source_dir: Optional[str] = None,
        profile_subdir: str = "Default",
    ) -> Path:
        """Create a seed directory with seed.json, Cookies, and Preferences."""
        sdir = profiles.seed_dir(name)
        sdir.mkdir(parents=True, exist_ok=True)
        if source_dir is None:
            source_dir = str(self._tmp + "/fake_source")
            os.makedirs(source_dir, exist_ok=True)

        profiles.create_seed(
            name=name,
            source_browser="chrome",
            source_dir=source_dir,
            profile_subdir=profile_subdir,
            password_store="gnome-libsecret",
            defaults={"headless": True},
        )
        (sdir / "Preferences").write_text('{"theme": "dark"}', encoding="ascii")
        cookie_file = sdir / "Network" / "Cookies"
        make_sqlite_cookies(
            cookie_file,
            [
                (
                    13300000000000000,
                    "127.0.0.1",
                    "",
                    "init_cookie",
                    "orig_val_111",
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
            ],
        )
        return sdir

    def _create_sample_session(
        self,
        session_id: str = "test_session",
        seed: str = "test_seed",
        profile_subdir: str = "Default",
    ) -> Path:
        """Create a session directory cloned from seed."""
        sess_dir = profiles.session_dir(session_id)
        sess_dir.mkdir(parents=True, exist_ok=True)
        profiles.create_session(
            session_id=session_id,
            seed=seed,
            pid=os.getpid(),
        )
        # Session profile data under profile_subdir
        sub_dir = sess_dir / profile_subdir
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "Preferences").write_text('{"theme": "light_mutated"}', encoding="ascii")
        cookie_file = sub_dir / "Network" / "Cookies"
        make_sqlite_cookies(
            cookie_file,
            [
                (
                    13300000000000000,
                    "127.0.0.1",
                    "",
                    "init_cookie",
                    "orig_val_111",
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
                ),
                (
                    13300000001000000,
                    "127.0.0.1",
                    "",
                    "session_token",
                    "saved_new_login_222",
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
                ),
            ],
        )
        (sess_dir / browser_sources.LOCAL_STATE_FILE).write_text("{}", encoding="ascii")
        return sess_dir


class SeedSaveUnitTests(_TempDataDir):
    """Unit tests for seed_save (SB-49)."""

    def test_save_to_existing_seed_updates_data_and_takes_snapshot(self):
        sdir = self._create_sample_seed("seed_orig")
        self._create_sample_session("sess_1", seed="seed_orig")

        # Prior to save: 0 snapshots
        self.assertEqual(len(seed_snapshots.list_seed_snapshots("seed_orig")), 0)

        # Call save_to_seed
        res = seed_save.save_to_seed("sess_1")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["seed"], "seed_orig")
        self.assertEqual(res["session_id"], "sess_1")
        self.assertIsNotNone(res["snapshot_id"])
        self.assertGreater(res["copied_count"], 0)

        # 1. Automatic snapshot was taken first
        snapshots = seed_snapshots.list_seed_snapshots("seed_orig")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_id"], res["snapshot_id"])
        snap_path = Path(snapshots[0]["path"])

        # Snapshot has OLD preferences
        self.assertEqual(
            (snap_path / "Preferences").read_text(encoding="ascii"),
            '{"theme": "dark"}',
        )

        # Seed directory now has the NEW preferences and cookies from session
        self.assertEqual(
            (sdir / "Preferences").read_text(encoding="ascii"),
            '{"theme": "light_mutated"}',
        )
        con = sqlite3.connect(str(sdir / "Network" / "Cookies"))
        try:
            count = con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
            self.assertEqual(count, 2)
            names = [r[0] for r in con.execute("SELECT name FROM cookies").fetchall()]
            self.assertIn("session_token", names)
            self.assertIn("init_cookie", names)
        finally:
            con.close()

    def test_save_to_new_seed_creates_seed_without_modifying_original(self):
        orig_sdir = self._create_sample_seed("seed_a")
        self._create_sample_session("sess_2", seed="seed_a")

        # Save to new seed name "seed_b"
        res = seed_save.save_to_seed("sess_2", seed="seed_b")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["seed"], "seed_b")
        self.assertIsNone(res["snapshot_id"])  # Brand new seed: no pre-existing snapshot

        self.assertTrue(profiles.seed_exists("seed_b"))
        new_data = profiles.load_seed("seed_b")
        self.assertEqual(new_data["source_browser"], "chrome")
        self.assertEqual(new_data["password_store"], "gnome-libsecret")
        self.assertEqual(new_data["defaults"], {"headless": True})

        # New seed has the updated preferences and cookies
        new_sdir = profiles.seed_dir("seed_b")
        self.assertEqual(
            (new_sdir / "Preferences").read_text(encoding="ascii"),
            '{"theme": "light_mutated"}',
        )

        # Original seed_a was untouched
        self.assertEqual(
            (orig_sdir / "Preferences").read_text(encoding="ascii"),
            '{"theme": "dark"}',
        )

    def test_snapshot_taken_first_preserves_old_state(self):
        sdir = self._create_sample_seed("seed_snap")
        (sdir / "checkpoint_v1.txt").write_text("v1_data", encoding="ascii")

        sess_dir = profiles.session_dir("sess_snap")
        sess_dir.mkdir(parents=True, exist_ok=True)
        profiles.create_session("sess_snap", seed="seed_snap", pid=os.getpid())
        sub = sess_dir / "Default"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "Preferences").write_text("{}", encoding="ascii")
        # Do not include checkpoint_v1.txt in session, but add new_v2.txt
        (sub / "new_v2.txt").write_text("v2_data", encoding="ascii")

        res = seed_save.save_to_seed("sess_snap")
        self.assertEqual(res["status"], "ok")

        snap_path = Path(seed_snapshots.seed_snapshots_dir("seed_snap") / res["snapshot_id"])
        # Snapshot has v1 file, not v2
        self.assertTrue((snap_path / "checkpoint_v1.txt").exists())
        self.assertEqual((snap_path / "checkpoint_v1.txt").read_text(encoding="ascii"), "v1_data")
        self.assertFalse((snap_path / "new_v2.txt").exists())

        # Seed has been replaced by session manifest
        self.assertTrue((sdir / "Preferences").exists())

    def test_source_profile_is_never_modified_via_hash(self):
        source_dir = Path(self._tmp) / "alan_real_chrome"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "Preferences").write_text('{"real_user": "alan"}', encoding="ascii")
        make_sqlite_cookies(source_dir / "Network" / "Cookies")
        (source_dir / "Bookmarks").write_text("alan bookmarks", encoding="ascii")

        before_hashes = source_hashes(source_dir)
        self.assertGreater(len(before_hashes), 0)

        self._create_sample_seed("seed_safe", source_dir=str(source_dir))
        self._create_sample_session("sess_safe", seed="seed_safe")

        # Mutate session
        sess_sub = profiles.session_dir("sess_safe") / "Default"
        (sess_sub / "Preferences").write_text('{"session": "modified"}', encoding="ascii")

        # Save session back to seed
        seed_save.save_to_seed("sess_safe")

        # Re-check source hashes: must be 100% identical
        after_hashes = source_hashes(source_dir)
        self.assertEqual(
            before_hashes,
            after_hashes,
            "Original source browser profile was modified during save_to_seed!",
        )

    def test_concurrent_save_blocked_by_lock_thread(self):
        self._create_sample_seed("seed_contend")
        self._create_sample_session("sess_contend", seed="seed_contend")

        lock = seed_save.seed_lock("seed_contend", timeout=2.0)
        lock.acquire()

        blocked_raised = False
        exception_caught = None

        def runner():
            nonlocal blocked_raised, exception_caught
            try:
                seed_save.save_to_seed("sess_contend", seed="seed_contend", lock_timeout=0.1)
            except seed_save.SeedBusyError as e:
                blocked_raised = True
                exception_caught = e

        t = threading.Thread(target=runner)
        t.start()
        t.join(timeout=2.0)

        self.assertTrue(blocked_raised, "Concurrent save must be blocked by lock")
        self.assertIn("busy", str(exception_caught).lower())

        # Release lock; now save succeeds
        lock.release()
        result = seed_save.save_to_seed("sess_contend", seed="seed_contend")
        self.assertEqual(result["status"], "ok")

    def test_concurrent_save_blocked_by_lock_process(self):
        self._create_sample_seed("seed_proc_contend")
        self._create_sample_session("sess_proc", seed="seed_proc_contend")

        pid_file = os.path.join(self._tmp, "proc.pid")
        die_file = os.path.join(self._tmp, "proc.die")

        code = HOLDER_SCRIPT.format(src=SRC)
        proc = subprocess.Popen(
            [sys.executable, "-c", code, "seed_proc_contend", die_file, pid_file],
            cwd=str(ROOT),
        )

        try:
            # Wait for subprocess holder to report pid
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if os.path.exists(pid_file):
                    break
                time.sleep(0.02)
            self.assertTrue(os.path.exists(pid_file), "Holder process failed to start")

            # Main process attempt with short timeout must fail with SeedBusyError
            with self.assertRaises(seed_save.SeedBusyError) as ctx:
                seed_save.save_to_seed("sess_proc", seed="seed_proc_contend", lock_timeout=0.15)
            self.assertIn("busy", str(ctx.exception).lower())

            # Signal holder process to release
            open(die_file, "a").close()
            proc.wait(timeout=3.0)

            # Main process attempt now succeeds
            res = seed_save.save_to_seed("sess_proc", seed="seed_proc_contend")
            self.assertEqual(res["status"], "ok")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def test_invalid_session_or_seed_names(self):
        self._create_sample_seed("valid_seed")
        # Nonexistent session
        with self.assertRaises(profiles.ProfileError):
            seed_save.save_to_seed("nonexistent_session")

        # Invalid session id
        with self.assertRaises(profiles.ProfileError):
            seed_save.save_to_seed("../bad_session_id")

        # Valid session, invalid seed name
        self._create_sample_session("valid_sess", seed="valid_seed")
        with self.assertRaises(profiles.ProfileError):
            seed_save.save_to_seed("valid_sess", seed="../escape_seed")
        with self.assertRaises(profiles.ProfileError):
            seed_save.save_to_seed("valid_sess", seed="has/slash")

    def test_mcp_tool_save_to_seed(self):
        self._create_sample_seed("tool_seed")
        self._create_sample_session("tool_sess", seed="tool_seed")

        # Call MCP tool function directly
        res = asyncio.run(server.save_to_seed.fn("tool_sess"))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["seed"], "tool_seed")

        # Call MCP tool function with instance_id resolution
        with mock.patch.object(
            server.browser_manager,
            "get_session_info",
            return_value={"session_id": "tool_sess"},
        ):
            res2 = asyncio.run(server.save_to_seed.fn("instance_12345"))
            self.assertEqual(res2["status"], "ok")
            self.assertEqual(res2["session_id"], "tool_sess")

    def test_save_to_seed_close_first_calls_close_before_copy(self):
        """Unit test with fake verifying close_first calls close before copy, and cleans session dir."""
        self._create_sample_seed("seed_cf")
        self._create_sample_session("sess_cf", seed="seed_cf")

        events = []

        def fake_close(sess_id):
            events.append(("close", sess_id))

        original_copy_seed = seed_save.sc.copy_seed

        def tracked_copy_seed(*args, **kwargs):
            events.append(("copy", args[1]))
            return original_copy_seed(*args, **kwargs)

        with mock.patch.object(seed_save, "close_session_browser", side_effect=fake_close), \
             mock.patch.object(seed_save.sc, "copy_seed", side_effect=tracked_copy_seed):
            res = seed_save.save_to_seed("sess_cf", close_first=True)

        self.assertEqual(res["status"], "ok")
        self.assertNotIn("warning", res)
        event_names = [e[0] for e in events]
        self.assertIn("close", event_names)
        self.assertIn("copy", event_names)
        self.assertLess(event_names.index("close"), event_names.index("copy"))
        self.assertEqual(events[0], ("close", "sess_cf"))
        self.assertFalse(profiles.session_exists("sess_cf"))

    def test_save_to_seed_flush_wait_and_warning_when_not_close_first(self):
        """Unit test verifying flush wait is used (patched) and warning string is present."""
        self._create_sample_seed("seed_fw")
        self._create_sample_session("sess_fw", seed="seed_fw")

        # 1. Default flush_wait_seconds=6.0
        with mock.patch("stealth_browser_mcp.seed_save._flush_sleep") as mock_sleep:
            res = seed_save.save_to_seed("sess_fw", close_first=False)
            mock_sleep.assert_called_once_with(6.0)

        self.assertEqual(res["status"], "ok")
        self.assertIn("warning", res)
        self.assertIn("cookies written within the last ~30 s may be missing", res["warning"].lower())
        self.assertIn("close_first=true guarantees a complete save", res["warning"].lower())
        self.assertTrue(profiles.session_exists("sess_fw"))

        # 2. Configurable flush_wait_seconds (e.g. 2.0)
        with mock.patch("stealth_browser_mcp.seed_save._flush_sleep") as mock_sleep:
            res2 = seed_save.save_to_seed("sess_fw", close_first=False, flush_wait_seconds=2.0)
            mock_sleep.assert_called_once_with(2.0)
        self.assertIn("warning", res2)

        # 3. flush_wait_seconds=0 disables wait
        with mock.patch("stealth_browser_mcp.seed_save._flush_sleep") as mock_sleep:
            res3 = seed_save.save_to_seed("sess_fw", close_first=False, flush_wait_seconds=0)
            mock_sleep.assert_not_called()
        self.assertIn("warning", res3)

    def test_mcp_tool_save_to_seed_close_first_and_flush_wait(self):
        """Test server.save_to_seed tool handles close_first and flush_wait."""
        self._create_sample_seed("tool_seed3")
        self._create_sample_session("tool_sess3", seed="tool_seed3")

        # Default close_first=False calls time.sleep and returns warning
        with mock.patch("stealth_browser_mcp.seed_save._flush_sleep") as mock_sleep:
            res = asyncio.run(server.save_to_seed.fn("tool_sess3"))
            mock_sleep.assert_called_once_with(6.0)
        self.assertEqual(res["status"], "ok")
        self.assertIn("warning", res)

        # close_first=True calls fake close and does not return warning
        with mock.patch.object(seed_save, "close_session_browser") as mock_close, \
             mock.patch("stealth_browser_mcp.seed_save._flush_sleep") as mock_sleep:
            res2 = asyncio.run(server.save_to_seed.fn("tool_sess3", close_first=True))
            mock_sleep.assert_not_called()
            mock_close.assert_called_once()
        self.assertEqual(res2["status"], "ok")
        self.assertNotIn("warning", res2)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestSaveToSeedGatedRealBrowser(_TempDataDir):
    """Gated real-browser acceptance test for SB-49.

    Acceptance criteria:
    - Spawn session S1 from a synthetic seed.
    - Log in inside session S1 on the fixture server (sets cookies and localStorage).
    - Call save_to_seed(session_id).
    - Spawn a NEW session S2 from the seed.
    - S2 starts logged in.
    - Concurrent save is blocked by lock.
    - Original source profile remains untouched.
    - Completes within 60s runtime cap.
    """

    def test_gated_login_in_session_save_new_session_logged_in(self):
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
            temp_source_dir = tempfile.mkdtemp(prefix="stealth_sb49_gated_source_")
            browser_source = None
            manager = BrowserManager()
            inst1 = None
            inst2 = None
            seed_name = "sb49-gated-seed"

            try:
                with FixtureServer() as fixture_srv:
                    login_url = fixture_srv.url_for("login.html")
                    sandbox_flag = False if sandbox_must_be_disabled() else True

                    # Step 1: Create clean source directory with unauthenticated browser
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_source_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=sandbox_flag,
                    )
                    browser_source = await launch_browser(config, timeout=30.0)
                    tab0 = await browser_source.get(login_url)
                    await asyncio.sleep(0.5)

                    # Close source browser cleanly
                    if getattr(browser_source, "connection", None) is not None:
                        try:
                            await asyncio.wait_for(
                                browser_source.connection.send(uc.cdp.browser.close()),
                                timeout=5.0,
                            )
                        except Exception:
                            pass
                    if getattr(browser_source, "_process", None) is not None:
                        try:
                            await _stop_process(browser_source._process)
                        except Exception:
                            pass
                    browser_source = None
                    await asyncio.sleep(0.5)

                    # Step 2: Refresh seed from clean source
                    ref_res = seed_tools.refresh_seed(
                        name=seed_name,
                        source_browser="chrome",
                        source_dir=temp_source_dir,
                        platform="linux",
                    )
                    self.assertEqual(ref_res["status"], "ok")
                    self.assertTrue(profiles.seed_exists(seed_name))

                    # Record hashes of source profile before any session work
                    initial_source_hashes = source_hashes(Path(temp_source_dir))
                    self.assertGreater(len(initial_source_hashes), 0)

                    # Step 3: Spawn session 1 from seed
                    opt1 = BrowserOptions(
                        headless=True,
                        sandbox=sandbox_flag,
                        seed=seed_name,
                    )
                    inst1 = await manager.spawn_browser(opt1)
                    s1_info = await manager.get_session_info(inst1.instance_id)
                    self.assertIsNotNone(s1_info)
                    s1_session_id = s1_info["session_id"]

                    # Navigate to login page; initially logged out
                    await manager.navigate(inst1.instance_id, login_url, wait_until="load")
                    tab1 = await manager.get_navigation_tab(inst1.instance_id)
                    initial_state1 = await tab1.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(initial_state1, "logged_out")

                    # Step 4: Perform login in session 1
                    await tab1.evaluate("document.getElementById('login-button').click()")
                    await asyncio.sleep(0.5)
                    await tab1.evaluate(
                        "document.cookie = 'session_token=sb49_secret_token_777; Path=/; Max-Age=86400; SameSite=Lax';"
                        "document.cookie = 'auth_user=sb49_user; Path=/; Max-Age=86400; SameSite=Lax';"
                        "localStorage.setItem('session_token', 'sb49_secret_token_777');"
                        "localStorage.setItem('auth_user', 'sb49_user');"
                        "localStorage.setItem('is_authenticated', 'true');"
                    )
                    await asyncio.sleep(0.5)

                    state1_after_login = await tab1.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(state1_after_login, "logged_in")

                    # Step 5: Save session 1 state back to seed
                    save_res = await server.save_to_seed.fn(s1_session_id, close_first=True)
                    self.assertEqual(save_res["status"], "ok")
                    self.assertEqual(save_res["seed"], seed_name)
                    self.assertGreater(save_res["copied_count"], 0)

                    # Step 6: Spawn NEW session 2 from the updated seed
                    opt2 = BrowserOptions(
                        headless=True,
                        sandbox=sandbox_flag,
                        seed=seed_name,
                        check_url=login_url,
                    )
                    inst2 = await manager.spawn_browser(opt2)
                    s2_info = await manager.get_session_info(inst2.instance_id)
                    self.assertIsNotNone(s2_info)

                    # Navigate session 2 to login page -> must start LOGGED IN!
                    await manager.navigate(inst2.instance_id, login_url, wait_until="load")
                    tab2 = await manager.get_navigation_tab(inst2.instance_id)
                    state2 = await tab2.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(
                        state2,
                        "logged_in",
                        "New session from seed must start logged in after save_to_seed!",
                    )
                    user2 = await tab2.evaluate(
                        "document.getElementById('user-display').textContent"
                    )
                    self.assertEqual(user2, "sb49_user")

                    # Step 7: Verify source browser profile was completely untouched
                    final_source_hashes = source_hashes(Path(temp_source_dir))
                    self.assertEqual(
                        initial_source_hashes,
                        final_source_hashes,
                        "Original source browser profile was touched during save_to_seed!",
                    )

                    # Step 8: Verify concurrent save blocked by lock
                    test_lock = seed_save.seed_lock(seed_name, timeout=2.0)
                    test_lock.acquire()
                    try:
                        with self.assertRaises(seed_save.SeedBusyError):
                            seed_save.save_to_seed(s1_session_id, seed=seed_name, lock_timeout=0.1)
                    finally:
                        test_lock.release()

                    # Clean up instances
                    await manager.close_instance(inst1.instance_id)
                    inst1 = None
                    await manager.close_instance(inst2.instance_id)
                    inst2 = None

            finally:
                if browser_source is not None:
                    try:
                        if getattr(browser_source, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser_source.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    if getattr(browser_source, "_process", None) is not None:
                        try:
                            await _stop_process(browser_source._process)
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
                for remaining_id in list(getattr(manager, "_instances", {}).keys()):
                    try:
                        await manager.close_instance(remaining_id)
                    except Exception:
                        pass
                shutil.rmtree(temp_source_dir, ignore_errors=True)

            elapsed = time.monotonic() - test_start_time
            self.assertLess(elapsed, 60.0, f"Gated acceptance test exceeded 60s: {elapsed:.2f}s")

        asyncio.run(run_acceptance())


if __name__ == "__main__":
    unittest.main()
