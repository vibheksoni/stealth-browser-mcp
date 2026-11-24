import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import paths, profiles


class _TempDataDir(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(paths.DATA_DIR_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-profiles-test-")
        os.environ[paths.DATA_DIR_ENV] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)


class NameValidationTests(_TempDataDir):
    def test_accepts_valid_names(self):
        for name in ["a", "abc-123", "x_y_z", "0", "a" * 40]:
            self.assertTrue(profiles._NAME_RE.match(name))

    def test_rejects_traversal(self):
        for name in ["..", "../x", "x/..", "a/b"]:
            with self.assertRaises(profiles.ProfileError):
                profiles.seed_dir(name)

    def test_rejects_slash(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir("a/b")

    def test_rejects_empty(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir("")

    def test_rejects_spaces_and_upper(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir("a b")
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir("ABC")

    def test_rejects_too_long(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir("a" * 41)

    def test_rejects_none_and_bytes(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir(None)
        with self.assertRaises(profiles.ProfileError):
            profiles.seed_dir(b"abc")


class SeedLifecycleTests(_TempDataDir):
    def test_create_lists_and_loads(self):
        data = profiles.create_seed(
            "a",
            source_browser="chrome",
            source_dir="/root/google-chrome",
            profile_subdir="Profile 1",
            password_store="gnome-libsecret",
            defaults={"headless": False},
            now=1000,
        )
        self.assertEqual(profiles.list_seeds(), ["a"])
        self.assertTrue(profiles.seed_exists("a"))
        loaded = profiles.load_seed("a")
        self.assertEqual(loaded["source_browser"], "chrome")
        self.assertEqual(loaded["source_dir"], "/root/google-chrome")
        self.assertEqual(loaded["profile_subdir"], "Profile 1")
        self.assertEqual(loaded["password_store"], "gnome-libsecret")
        self.assertEqual(loaded["defaults"], {"headless": False})
        self.assertEqual(loaded["refreshed_at"], 1000)

    def test_seed_path_under_seeds(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        self.assertTrue(profiles.seed_path("a").is_file())
        self.assertEqual(
            profiles.seed_path("a").parent.parent,
            profiles.seeds_dir(),
        )

    def test_seed_json_round_trip(self):
        profiles.create_seed("a", "chrome", "/src", "P", now=2000)
        data = profiles.load_seed("a")
        profiles.save_seed("a", {**data, "refreshed_at": 3000})
        reloaded = profiles.load_seed("a")
        self.assertEqual(reloaded["refreshed_at"], 3000)
        self.assertEqual(reloaded["source_browser"], "chrome")

    def test_create_duplicate_fails(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        with self.assertRaises(profiles.ProfileError):
            profiles.create_seed("a", "chrome", "/src", "P")

    def test_missing_seed_raises(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.load_seed("missing")

    def test_defaults_merged_in_defaults(self):
        data = profiles.create_seed("a", "chrome", "/src", "P")
        self.assertEqual(data["defaults"], {})
        self.assertIn("refreshed_at", data)


class SessionLifecycleTests(_TempDataDir):
    def test_create_and_load_session(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        meta = profiles.create_session(seed="a", pid=999999999, created=111, browser_pid=999999998, target="abc")
        self.assertEqual(meta["seed"], "a")
        self.assertEqual(meta["pid"], 999999999)
        self.assertEqual(profiles.session_exists(meta["id"]), True)
        loaded = profiles.load_session(meta["id"])
        self.assertEqual(loaded["seed"], "a")
        self.assertEqual(loaded["target"], "abc")
        self.assertIn("created", loaded)

    def test_session_paths_under_sessions(self):
        meta = profiles.create_session(seed="a", pid=42)
        self.assertEqual(
            profiles.session_path(meta["id"]).parent.parent,
            profiles.sessions_dir(),
        )

    def test_session_id_generated_and_valid(self):
        meta = profiles.create_session(seed="a", pid=1)
        self.assertTrue(profiles._SESSION_RE.match(meta["id"]))

    def test_list_sessions(self):
        a = profiles.create_session(seed="a", pid=1)["id"]
        b = profiles.create_session(seed="a", pid=2)["id"]
        self.assertEqual(profiles.list_sessions(), sorted([a, b]))

    def test_bad_session_id(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.create_session(seed="a", pid=1, session_id="a/b")
        with self.assertRaises(profiles.ProfileError):
            profiles.load_session("nope!")


class LiveSessionTests(_TempDataDir):
    def test_delete_refuses_while_live(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        meta = profiles.create_session(seed="a", pid=os.getpid(), created=1)
        # os.getpid() is alive right now.
        with self.assertRaises(profiles.ProfileError):
            profiles.delete_seed("a")

    def test_delete_refuses_when_session_live_even_if_seed_dir_has_other_sessions(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        profiles.create_session(seed="a", pid=os.getpid(), created=1)
        profiles.create_session(seed="a", pid=2147483646, created=2)
        with self.assertRaises(profiles.ProfileError):
            profiles.delete_seed("a")

    def test_delete_succeeds_when_no_live_sessions(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        profiles.create_session(seed="a", pid=2147483646, created=1)
        profiles.delete_seed("a")
        self.assertFalse(profiles.seed_exists("a"))
        self.assertEqual(profiles.list_seeds(), [])
        # Sessions that belonged to the seed are cleaned up too.
        self.assertEqual(profiles.list_sessions(), [])

    def test_delete_refuses_name_and_missing(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.delete_seed("..")
        with self.assertRaises(profiles.ProfileError):
            profiles.delete_seed("missing")

    def test_delete_reports_live_pid(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        profiles.create_session(seed="a", pid=os.getpid(), created=1)
        with self.assertRaises(profiles.ProfileError) as ctx:
            profiles.delete_seed("a")
        self.assertIn("live", str(ctx.exception).lower())
        self.assertIn(str(os.getpid()), str(ctx.exception))

    def test_pid_alive_helper(self):
        self.assertTrue(profiles.pid_alive(os.getpid()))
        self.assertFalse(profiles.pid_alive(2147483646))
        self.assertFalse(profiles.pid_alive(None))
        self.assertFalse(profiles.pid_alive(0))

    def test_sessions_for_seed_returns_live_and_stale(self):
        profiles.create_seed("a", "chrome", "/src", "P")
        profiles.create_session(seed="a", pid=os.getpid(), created=1)
        profiles.create_session(seed="a", pid=2147483646, created=2)
        matches = profiles.sessions_for_seed("a")
        self.assertEqual(len(matches), 2)
        names = {m["id"] for m in matches}
        # Directory listing should not be affected by order.
        self.assertEqual(len(names), 2)


if __name__ == "__main__":
    unittest.main()
