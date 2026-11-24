import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import paths, profiles, session_registry


class _TempDataDir(unittest.TestCase):
    def setUp(self):
        self._saved_data = os.environ.get(paths.DATA_DIR_ENV)
        self._saved_cap = os.environ.get(session_registry._CAP_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-session-reg-")
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
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


def _patch_liveness(live_pids):
    """Patch liveness so the given pids are alive and everything else dead."""
    def fake_pid_alive(pid):
        if not isinstance(pid, int) or pid <= 0:
            return False
        return pid in live_pids

    class _Patcher:
        def __init__(self):
            self._p1 = mock.patch.object(session_registry, "pid_alive", side_effect=fake_pid_alive)
            self._p2 = mock.patch.object(profiles, "pid_alive", side_effect=fake_pid_alive)

        def __enter__(self):
            self._p1.start()
            self._p2.start()
            return self

        def __exit__(self, *args):
            self._p2.stop()
            self._p1.stop()

    return _Patcher()


class CapResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(session_registry._CAP_ENV)
        os.environ.pop(session_registry._CAP_ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(session_registry._CAP_ENV, None)
        else:
            os.environ[session_registry._CAP_ENV] = self._saved

    def test_env_reads_int(self):
        os.environ[session_registry._CAP_ENV] = "3"
        self.assertEqual(session_registry.cap_from_env(), 3)

    def test_env_blank_falls_back(self):
        os.environ[session_registry._CAP_ENV] = ""
        self.assertIsNone(session_registry.cap_from_env(9))
        self.assertIsNone(session_registry.cap_from_env())
        os.environ[session_registry._CAP_ENV] = "  "
        self.assertIsNone(session_registry.cap_from_env(9))
        self.assertIsNone(session_registry.cap_from_env())

    def test_env_non_numeric_falls_back(self):
        os.environ[session_registry._CAP_ENV] = "soon"
        self.assertIsNone(session_registry.cap_from_env(9))
        self.assertIsNone(session_registry.cap_from_env())

    def test_env_zero_is_unlimited(self):
        os.environ[session_registry._CAP_ENV] = "0"
        self.assertIsNone(session_registry.cap_from_env())

    def test_explicit_cap_wins_over_env(self):
        os.environ[session_registry._CAP_ENV] = "3"
        self.assertEqual(session_registry.resolve_cap(), 3)
        self.assertEqual(session_registry.resolve_cap(cap=7), 7)
        self.assertIsNone(session_registry.resolve_cap(cap=None))


class SessionLifecycleTests(_TempDataDir):
    def test_create_writes_session_json(self):
        with _patch_liveness({42}):
            data = session_registry.create_session(seed="a", pid=42, created=100)
        self.assertEqual(data["seed"], "a")
        self.assertEqual(data["pid"], 42)
        self.assertEqual(data["created"], 100)
        self.assertTrue(profiles.session_exists(data["id"]))
        self.assertTrue(profiles.session_path(data["id"]).is_file())

    def test_create_generates_valid_id(self):
        with _patch_liveness({42}):
            data = session_registry.create_session(seed="a", pid=42)
        self.assertTrue(profiles._SESSION_RE.match(data["id"]))

    def test_create_under_lock_path(self):
        session_registry.lock_for()  # creates the lock file
        lock_path = paths.data_dir() / "locks" / "sessions.lock"
        self.assertTrue(lock_path.parent.is_dir())

    def test_bad_id_rejected(self):
        with self.assertRaises(profiles.ProfileError):
            session_registry.create_session(seed="a", pid=42, session_id="bad/id")

    def test_close_removes_dir_and_reuses_slot(self):
        with _patch_liveness({42}):
            meta = session_registry.create_session(seed="a", pid=42)
        self.assertTrue(profiles.session_exists(meta["id"]))
        session_registry.close_session(meta["id"])
        self.assertFalse(profiles.session_exists(meta["id"]))
        # Slot is free again: a new live session is created without a cap.
        with _patch_liveness({43}):
            session_registry.create_session(seed="a", pid=43)

    def test_close_missing_raises(self):
        with self.assertRaises(profiles.ProfileError):
            session_registry.close_session("nope")

    def test_close_bad_id_raises(self):
        with self.assertRaises(profiles.ProfileError):
            session_registry.close_session("bad/id")


class LiveCountTests(_TempDataDir):
    def test_counts_only_live(self):
        with _patch_liveness({42}):
            live_id = session_registry.create_session(seed="a", pid=42)["id"]
            stale_id = session_registry.create_session(seed="a", pid=2147483646)["id"]
            self.assertEqual(session_registry.live_session_count(), 1)
            # Removing the dead session drops the stale dir.
            removed = session_registry.janitor()
            self.assertIn(stale_id, removed)
            self.assertNotIn(live_id, removed)
            self.assertEqual(profiles.list_sessions(), [live_id])


class CapEnforcementTests(_TempDataDir):
    def test_cap_blocks_when_full(self):
        with _patch_liveness({42, 43}):
            session_registry.create_session(seed="a", pid=42, cap=1)
            with self.assertRaises(profiles.ProfileError) as ctx:
                session_registry.create_session(seed="a", pid=43, cap=1)
            self.assertIn("cap", str(ctx.exception).lower())

    def test_cap_allows_one_below_limit(self):
        with _patch_liveness({42, 43, 44}):
            session_registry.create_session(seed="a", pid=42, cap=2)
            session_registry.create_session(seed="a", pid=43, cap=2)
            # Third live session exceeds cap of 2.
            with self.assertRaises(profiles.ProfileError):
                session_registry.create_session(seed="a", pid=44, cap=2)

    def test_unlimited_when_cap_none(self):
        with _patch_liveness({42, 43, 44, 45}):
            for pid in (42, 43, 44, 45):
                session_registry.create_session(seed="a", pid=pid, cap=None)
            self.assertEqual(session_registry.live_session_count(cap=None), 4)

    def test_env_cap_enforced(self):
        os.environ[session_registry._CAP_ENV] = "1"
        with _patch_liveness({42, 43}):
            session_registry.create_session(seed="a", pid=42)
            with self.assertRaises(profiles.ProfileError):
                session_registry.create_session(seed="a", pid=43)


class JanitorTests(_TempDataDir):
    def test_removes_dead_keeps_live(self):
        with _patch_liveness({42}):
            live = session_registry.create_session(seed="a", pid=42)
            stale = session_registry.create_session(seed="a", pid=2147483646)
        # Now nothing is alive.
        with _patch_liveness(set()):
            removed = session_registry.janitor()
        self.assertIn(live["id"], removed)
        self.assertIn(stale["id"], removed)
        self.assertEqual(profiles.list_sessions(), [])

    def test_keeps_live_session(self):
        with _patch_liveness({42}):
            live = session_registry.create_session(seed="a", pid=42)
            stale = session_registry.create_session(seed="a", pid=2147483646)
        # Only 42 is alive: remove the stale one, keep the live.
        with _patch_liveness({42}):
            removed = session_registry.janitor()
        self.assertEqual(removed, [stale["id"]])
        self.assertEqual(profiles.list_sessions(), [live["id"]])

    def test_empty_when_all_dead(self):
        with _patch_liveness(set()):
            removed = session_registry.janitor()
        self.assertEqual(removed, [])


# --- Two-subprocess race for the last slot under a cap of 1 -----------------

_RACE_SCRIPT = '''
import os
import sys

sys.path.insert(0, {src!r})
os.environ["STEALTH_BROWSER_MCP_DATA_DIR"] = {datadir!r}
os.environ["STEALTH_MAX_SESSIONS"] = "1"

from stealth_browser_mcp import profiles, session_registry

session_registry._LOCK_PATH = os.path.join(
    os.environ["STEALTH_BROWSER_MCP_DATA_DIR"], "locks", "sessions.lock"
)

# Pretend our own pid is alive so the session counts as live for the loser.
live_pid = {pid!r}

import psutil

_orig_exists = psutil.pid_exists

def _fake_exists(pid):
    if isinstance(pid, int) and pid > 0:
        return pid == live_pid
    return False

psutil.pid_exists = _fake_exists
profiles.psutil.pid_exists = _fake_exists

try:
    session_registry.create_session(seed="a", pid=live_pid)
except profiles.ProfileError:
    sys.exit(1)
sys.exit(0)
'''


class TwoSubprocessRaceTests(unittest.TestCase):
    def setUp(self):
        self._saved_data = os.environ.get(paths.DATA_DIR_ENV)
        self._saved_cap = os.environ.get(session_registry._CAP_ENV)
        os.environ.pop(session_registry._CAP_ENV, None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.datadir = self.directory.name
        self.pid_file = os.path.join(self.directory.name, "pid")
        # A real, currently alive pid (the test runner) stands in for the clone.
        self.alive_pid = os.getpid()

    def tearDown(self):
        if self._saved_data is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data
        if self._saved_cap is None:
            os.environ.pop(session_registry._CAP_ENV, None)
        else:
            os.environ[session_registry._CAP_ENV] = self._saved_cap

    def _race_script(self):
        return _RACE_SCRIPT.format(
            src=str(ROOT / "src"),
            datadir=self.datadir,
            pid=self.alive_pid,
        )

    def test_exactly_one_wins_last_slot(self):
        code = self._race_script()
        env = dict(os.environ)
        env.pop("STEALTH_BROWSER_MCP_DATA_DIR", None)
        env.pop("STEALTH_MAX_SESSIONS", None)
        proc1 = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), env=env)
        proc2 = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), env=env)
        rc1, rc2 = proc1.wait(timeout=30), proc2.wait(timeout=30)
        winners = sorted(rc for rc in (rc1, rc2) if rc == 0)
        # Exactly one process took the last slot; the other was blocked by the cap.
        self.assertEqual(winners, [0])
        self.assertIn(1, (rc1, rc2))


if __name__ == "__main__":
    unittest.main()
