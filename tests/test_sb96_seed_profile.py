"""Unit tests for SB-96 / F36: keep refresh_seed profile under source root.

Replays the live exploit with a fake HOME (no real browser profile):
- Confinement of profile argument under the resolved source root.
- Rejection of path traversal, absolute paths, and '..' in profile.
- Enforcement in both seed_tools.refresh_seed and server.py HTTP wrapper.
- Uniform generic error text for missing and refused profiles.
- Valid profile ('Default', 'Profile 1') under source root succeeds.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import paths, profiles, seed_tools, server


def make_sqlite_cookies(path: str, rows=None, marker: str = "COOKIE_MARKER") -> None:
    """Create a synthetic Cookies SQLite database at path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
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
        if rows is None:
            rows = [
                (
                    13300000000000000,
                    "127.0.0.1",
                    "",
                    "session_token",
                    marker,
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
            ]
        con.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def seed_contains_marker(seed_name: str, marker: str) -> bool:
    """Check if any file in the seed directory contains marker bytes."""
    sdir = profiles.seed_dir(seed_name)
    if not sdir.exists():
        return False
    for root, _dirs, files in os.walk(str(sdir)):
        for f in files:
            p = os.path.join(root, f)
            try:
                with open(p, "rb") as h:
                    if marker.encode("ascii") in h.read():
                        return True
            except OSError:
                pass
    return False


class TestSB96SeedProfileConfinement(unittest.TestCase):
    """Test seed profile confinement under fake HOME."""

    def setUp(self):
        self._saved_home = os.environ.get("HOME")
        self._saved_data_dir = os.environ.get(paths.DATA_DIR_ENV)
        self._saved_allow_refresh = os.environ.get("STEALTH_ALLOW_SEED_REFRESH")
        self._saved_http_mode = getattr(server, "_HTTP_MODE", False)
        self._saved_http_host = getattr(server, "_HTTP_BIND_HOST", None)

        self.fake_home = tempfile.mkdtemp(prefix="fake_home_sb96_")
        self.fake_data = tempfile.mkdtemp(prefix="fake_data_sb96_")

        os.environ["HOME"] = self.fake_home
        os.environ[paths.DATA_DIR_ENV] = self.fake_data
        os.environ["STEALTH_ALLOW_SEED_REFRESH"] = "1"

        server._HTTP_MODE = True
        server._HTTP_BIND_HOST = "127.0.0.1"

        # Create chrome user-data root under fake HOME
        self.chrome_root = Path(self.fake_home) / ".config" / "google-chrome"
        self.chrome_root.mkdir(parents=True, exist_ok=True)

        local_state_payload = {
            "profile": {
                "info_cache": [
                    {"name": "Default"},
                    {"name": "Profile 1"},
                ],
                "last_used": 0,
            }
        }
        (self.chrome_root / "Local State").write_text(
            json.dumps(local_state_payload), encoding="ascii"
        )

        # Valid Default profile under chrome root
        self.chrome_default = self.chrome_root / "Default"
        self.chrome_default.mkdir(parents=True, exist_ok=True)
        (self.chrome_default / "Preferences").write_text(
            '{"browser": {"test": true}}', encoding="ascii"
        )
        make_sqlite_cookies(
            str(self.chrome_default / "Network" / "Cookies"),
            marker="VALIDCOOKIES-chrome",
        )

        # Valid Profile 1 under chrome root
        self.chrome_p1 = self.chrome_root / "Profile 1"
        self.chrome_p1.mkdir(parents=True, exist_ok=True)
        (self.chrome_p1 / "Preferences").write_text(
            '{"browser": {"profile1": true}}', encoding="ascii"
        )
        make_sqlite_cookies(
            str(self.chrome_p1 / "Network" / "Cookies"),
            marker="VALIDCOOKIES-profile1",
        )

        # Outside tree containing marker
        self.outside_dir = Path(self.fake_home) / "outside" / "Default"
        self.outside_dir.mkdir(parents=True, exist_ok=True)
        (self.outside_dir / "Preferences").write_text(
            '{"outside": true}', encoding="ascii"
        )
        make_sqlite_cookies(
            str(self.outside_dir / "Cookies"),
            marker="FAKECOOKIES-outside",
        )
        make_sqlite_cookies(
            str(self.outside_dir / "Network" / "Cookies"),
            marker="FAKECOOKIES-outside",
        )

    def tearDown(self):
        server._HTTP_MODE = self._saved_http_mode
        server._HTTP_BIND_HOST = self._saved_http_host

        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home

        if self._saved_data_dir is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data_dir

        if self._saved_allow_refresh is None:
            os.environ.pop("STEALTH_ALLOW_SEED_REFRESH", None)
        else:
            os.environ["STEALTH_ALLOW_SEED_REFRESH"] = self._saved_allow_refresh

        shutil.rmtree(self.fake_home, ignore_errors=True)
        shutil.rmtree(self.fake_data, ignore_errors=True)

    def test_refresh_seed_escapes_refused_in_http_mode(self):
        # 1. Traversal profile
        res_traversal = asyncio.run(
            server.refresh_seed.fn(
                name="f5",
                source_browser="chrome",
                profile="../../../outside/Default",
            )
        )
        self.assertEqual(res_traversal["status"], "error")
        self.assertFalse(seed_contains_marker("f5", "FAKECOOKIES-outside"))

        # 2. Absolute profile path to outside Default
        res_abs_outside = asyncio.run(
            server.refresh_seed.fn(
                name="f6",
                source_browser="chrome",
                profile=str(self.outside_dir),
            )
        )
        self.assertEqual(res_abs_outside["status"], "error")
        self.assertFalse(seed_contains_marker("f6", "FAKECOOKIES-outside"))

        # 3. profile="../x"
        res_dotdot = asyncio.run(
            server.refresh_seed.fn(
                name="f_dotdot",
                source_browser="chrome",
                profile="../x",
            )
        )
        self.assertEqual(res_dotdot["status"], "error")

        # 4. profile="/abs"
        res_abs = asyncio.run(
            server.refresh_seed.fn(
                name="f_abs",
                source_browser="chrome",
                profile="/abs",
            )
        )
        self.assertEqual(res_abs["status"], "error")

        # 5. Missing profile under chrome root
        res_missing = asyncio.run(
            server.refresh_seed.fn(
                name="f_missing",
                source_browser="chrome",
                profile="nonexistent-probe",
            )
        )
        self.assertEqual(res_missing["status"], "error")

        # 6. Missing and refused profiles must use the same generic error text
        generic_error = res_missing["error"]
        self.assertIn("profile", generic_error.lower())
        self.assertEqual(res_traversal["error"], generic_error)
        self.assertEqual(res_abs_outside["error"], generic_error)
        self.assertEqual(res_dotdot["error"], generic_error)
        self.assertEqual(res_abs["error"], generic_error)

    def test_refresh_seed_valid_profiles_accepted(self):
        with mock.patch("stealth_browser_mcp.browser_sources.normalize_platform", return_value="linux"):
            # Normal profile "Default" under chrome root
            res_default = asyncio.run(
                server.refresh_seed.fn(
                    name="seed-default",
                    source_browser="chrome",
                    profile="Default",
                )
            )
            self.assertEqual(res_default["status"], "ok")
            self.assertEqual(res_default["seed"], "seed-default")
            self.assertEqual(res_default["profile"], "Default")
            self.assertTrue(profiles.seed_exists("seed-default"))
            self.assertTrue(seed_contains_marker("seed-default", "VALIDCOOKIES-chrome"))
            self.assertFalse(seed_contains_marker("seed-default", "FAKECOOKIES-outside"))

            # Normal profile "Profile 1" under chrome root
            res_p1 = asyncio.run(
                server.refresh_seed.fn(
                    name="seed-p1",
                    source_browser="chrome",
                    profile="Profile 1",
                )
            )
            self.assertEqual(res_p1["status"], "ok")
            self.assertEqual(res_p1["seed"], "seed-p1")
            self.assertEqual(res_p1["profile"], "Profile 1")
            self.assertTrue(profiles.seed_exists("seed-p1"))
            self.assertTrue(seed_contains_marker("seed-p1", "VALIDCOOKIES-profile1"))

    def test_seed_tools_direct_enforces_confinement(self):
        # Direct calls to seed_tools.refresh_seed (defense in depth)
        res_direct_traversal = seed_tools.refresh_seed(
            name="dir-f1",
            source_browser="chrome",
            profile="../../../outside/Default",
            platform="linux",
        )
        self.assertEqual(res_direct_traversal["status"], "error")
        self.assertFalse(seed_contains_marker("dir-f1", "FAKECOOKIES-outside"))

        res_direct_abs = seed_tools.refresh_seed(
            name="dir-f2",
            source_browser="chrome",
            profile=str(self.outside_dir),
            platform="linux",
        )
        self.assertEqual(res_direct_abs["status"], "error")
        self.assertFalse(seed_contains_marker("dir-f2", "FAKECOOKIES-outside"))

        res_direct_dotdot = seed_tools.refresh_seed(
            name="dir-f-dotdot",
            source_browser="chrome",
            profile="../x",
            platform="linux",
        )
        self.assertEqual(res_direct_dotdot["status"], "error")

        res_direct_slash_abs = seed_tools.refresh_seed(
            name="dir-f-abs",
            source_browser="chrome",
            profile="/abs",
            platform="linux",
        )
        self.assertEqual(res_direct_slash_abs["status"], "error")

        res_direct_missing = seed_tools.refresh_seed(
            name="dir-f3",
            source_browser="chrome",
            profile="nonexistent-probe",
            platform="linux",
        )
        self.assertEqual(res_direct_missing["status"], "error")

        self.assertEqual(res_direct_traversal["error"], res_direct_missing["error"])
        self.assertEqual(res_direct_abs["error"], res_direct_missing["error"])
        self.assertEqual(res_direct_dotdot["error"], res_direct_missing["error"])
        self.assertEqual(res_direct_slash_abs["error"], res_direct_missing["error"])

        res_direct_valid = seed_tools.refresh_seed(
            name="dir-valid",
            source_browser="chrome",
            profile="Default",
            platform="linux",
        )
        self.assertEqual(res_direct_valid["status"], "ok")
        self.assertTrue(seed_contains_marker("dir-valid", "VALIDCOOKIES-chrome"))


if __name__ == "__main__":
    unittest.main()
