"""Unit tests and gated browser integration tests for list_sources and refresh_seed (SB-46).

Unit tests run with synthetic fixture trees and temporary directories; no real
browser profile is read. Real-browser tests are gated by STEALTH_BROWSER_TESTS=1.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import browser_sources, paths, profiles, seed_copy, seed_tools
from stealth_browser_mcp.file_lock import FileLock
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def sha256(path: str) -> str:
    """Return the SHA-256 hex digest of path."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hashes(root: str) -> Dict[str, str]:
    """Return {rel_path: sha256} for every readable file under root."""
    hashes = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            try:
                hashes[rel] = sha256(abs_path)
            except OSError:
                pass
    return hashes


def make_sqlite_cookies(path: str, rows=None) -> None:
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
                (13300000000000000, "127.0.0.1", "", "session_token", "secret_123", b"", "/", 13300086400000000, 0, 0, 0, 1, 1, 1, 1, 1, 8000, 0),
                (13300000000000000, "127.0.0.1", "", "auth_user", "testadmin", b"", "/", 13300086400000000, 0, 0, 0, 1, 1, 1, 1, 1, 8000, 0),
            ]
        con.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


class _TempDataDir(unittest.TestCase):
    """Sets up an isolated temporary data dir for seed registry tests."""

    def setUp(self):
        self._saved_data_dir = os.environ.get(paths.DATA_DIR_ENV)
        self._data_tmp = tempfile.mkdtemp(prefix="stealth_seed_tools_data_")
        os.environ[paths.DATA_DIR_ENV] = self._data_tmp

    def tearDown(self):
        if self._saved_data_dir is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data_dir
        shutil.rmtree(self._data_tmp, ignore_errors=True)


class TestListSourcesUnit(_TempDataDir):
    """Unit tests for list_sources."""

    def test_list_sources_non_linux(self):
        res = seed_tools.list_sources(platform="windows")
        self.assertEqual(res["status"], "error")
        self.assertIn("only supported on Linux", res["error"])
        self.assertEqual(res["sources"], [])
        self.assertEqual(res["seeds"], [])

    def test_list_sources_discovers_chrome_and_profiles(self):
        tmp_home = tempfile.mkdtemp(prefix="fake_home_")
        self.addCleanup(lambda: shutil.rmtree(tmp_home, ignore_errors=True))

        chrome_root = Path(tmp_home) / ".config" / "google-chrome"
        chrome_root.mkdir(parents=True)

        local_state_payload = {
            "profile": {
                "info_cache": [
                    {"name": "Default"},
                    {"name": "Profile 1"},
                ],
                "last_used": 1,
            }
        }
        (chrome_root / "Local State").write_text(json.dumps(local_state_payload), encoding="ascii")
        (chrome_root / "Default").mkdir()
        (chrome_root / "Profile 1").mkdir()

        res = seed_tools.list_sources(env={"HOME": tmp_home}, platform="linux")
        self.assertEqual(res["status"], "ok")

        sources = res["sources"]
        chrome_sources = [s for s in sources if s["browser"] == "chrome"]
        self.assertEqual(len(chrome_sources), 1)

        chrome = chrome_sources[0]
        self.assertEqual(chrome["user_data_dir"], str(chrome_root))
        self.assertEqual(chrome["profiles"], ["Default", "Profile 1"])
        self.assertEqual(chrome["last_used_profile"], "Profile 1")
        self.assertIn("google-chrome", chrome["binary_candidates"])
        self.assertTrue(chrome["installed"])

    def test_list_sources_reports_existing_seeds_with_age(self):
        fake_now = 1700000000.0
        profiles.create_seed(
            name="test-seed-a",
            source_browser="chrome",
            source_dir="/fake/dir",
            profile_subdir="Default",
            now=fake_now - 150.0,
        )

        tmp_home = tempfile.mkdtemp(prefix="fake_home_empty_")
        self.addCleanup(lambda: shutil.rmtree(tmp_home, ignore_errors=True))

        res = seed_tools.list_sources(env={"HOME": tmp_home}, platform="linux")
        self.assertEqual(res["status"], "ok")

        seeds = res["seeds"]
        self.assertEqual(len(seeds), 1)
        s = seeds[0]
        self.assertEqual(s["name"], "test-seed-a")
        self.assertEqual(s["source_browser"], "chrome")
        self.assertEqual(s["profile_subdir"], "Default")
        self.assertIsNotNone(s["age_seconds"])
        self.assertGreater(s["age_seconds"], 0)


class TestRefreshSeedUnit(_TempDataDir):
    """Unit tests for refresh_seed with synthetic fixture trees."""

    def setUp(self):
        super().setUp()
        self.source_tmp = tempfile.mkdtemp(prefix="fake_browser_source_")
        self.addCleanup(lambda: shutil.rmtree(self.source_tmp, ignore_errors=True))

    def _create_synthetic_source(self) -> Path:
        source_root = Path(self.source_tmp)
        default_dir = source_root / "Default"
        default_dir.mkdir(parents=True)

        # Network/Cookies SQLite store
        make_sqlite_cookies(str(default_dir / "Network" / "Cookies"))

        # Preferences file
        (default_dir / "Preferences").write_text('{"browser": {"test": true}}', encoding="ascii")

        # Local State in user data root
        ls_data = {
            "profile": {
                "info_cache": [{"name": "Default"}],
                "last_used": 0,
            }
        }
        (source_root / "Local State").write_text(json.dumps(ls_data), encoding="ascii")

        # Caches to verify they are excluded
        cache_dir = default_dir / "Cache"
        cache_dir.mkdir()
        (cache_dir / "data_0").write_bytes(b"dummy cache content 12345")

        code_cache = default_dir / "Code Cache" / "js"
        code_cache.mkdir(parents=True)
        (code_cache / "js_cache_0").write_bytes(b"dummy code cache")

        return source_root

    def test_refresh_seed_non_linux(self):
        res = seed_tools.refresh_seed(
            name="s1",
            source_browser="chrome",
            platform="windows",
        )
        self.assertEqual(res["status"], "error")
        self.assertIn("only supported on Linux", res["error"])

    def test_refresh_seed_invalid_name(self):
        res = seed_tools.refresh_seed(
            name="bad/seed..name",
            source_browser="chrome",
            platform="linux",
        )
        self.assertEqual(res["status"], "error")
        self.assertIn("invalid seed name", res["error"].lower())

    def test_refresh_seed_missing_source_dir(self):
        res = seed_tools.refresh_seed(
            name="s1",
            source_browser="/path/does/not/exist/at/all",
            platform="linux",
        )
        self.assertEqual(res["status"], "error")
        self.assertIn("Source directory does not exist", res["error"])

    def test_refresh_seed_synthetic_source_success(self):
        source_root = self._create_synthetic_source()
        before_hashes = source_hashes(str(source_root))

        res = seed_tools.refresh_seed(
            name="synth-seed",
            source_browser="chrome",
            source_dir=str(source_root),
            platform="linux",
        )

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["seed"], "synth-seed")
        self.assertEqual(res["source_browser"], "chrome")
        self.assertEqual(res["profile"], "Default")
        self.assertGreater(res["copied_count"], 0)
        self.assertGreater(res["bytes"], 0)
        self.assertEqual(res["skipped_busy"], [])

        # Source hash must be unchanged (source directory is never modified)
        after_hashes = source_hashes(str(source_root))
        self.assertEqual(before_hashes, after_hashes, "Source directory must not be modified")

        # Verify seed directory contents
        dest_dir = profiles.seed_dir("synth-seed")
        self.assertTrue(dest_dir.is_dir())

        # Cookies store exists
        cookie_file = dest_dir / "Network" / "Cookies"
        self.assertTrue(cookie_file.is_file())

        # Verify cookie rows in copied SQLite DB
        con = sqlite3.connect(str(cookie_file))
        try:
            cursor = con.cursor()
            cursor.execute("SELECT name, value FROM cookies")
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 2)
            names = [r[0] for r in rows]
            self.assertIn("session_token", names)
            self.assertIn("auth_user", names)
        finally:
            con.close()

        # Preferences exists
        self.assertTrue((dest_dir / "Preferences").is_file())

        # Caches must NOT exist
        self.assertFalse((dest_dir / "Cache").exists())
        self.assertFalse((dest_dir / "Code Cache").exists())

        # seed.json was created
        self.assertTrue(profiles.seed_exists("synth-seed"))
        seed_record = profiles.load_seed("synth-seed")
        self.assertEqual(seed_record["source_browser"], "chrome")
        self.assertEqual(seed_record["source_dir"], str(source_root))
        self.assertEqual(seed_record["profile_subdir"], "Default")
        self.assertIsNotNone(seed_record.get("refreshed_at"))

    def test_refresh_seed_updates_existing_seed(self):
        source_root = self._create_synthetic_source()

        # First refresh
        res1 = seed_tools.refresh_seed(
            name="up-seed",
            source_browser="chrome",
            source_dir=str(source_root),
            platform="linux",
        )
        self.assertEqual(res1["status"], "ok")
        rec1 = profiles.load_seed("up-seed")

        time.sleep(0.05)

        # Second refresh
        res2 = seed_tools.refresh_seed(
            name="up-seed",
            source_browser="chrome",
            source_dir=str(source_root),
            platform="linux",
        )
        self.assertEqual(res2["status"], "ok")
        rec2 = profiles.load_seed("up-seed")

        self.assertGreater(rec2["refreshed_at"], rec1["refreshed_at"])

    def test_refresh_seed_passwords_and_cards_options(self):
        source_root = self._create_synthetic_source()
        default_dir = source_root / "Default"

        # Create dummy Login Data and Web Data SQLite files
        make_sqlite_cookies(str(default_dir / "Login Data"))
        make_sqlite_cookies(str(default_dir / "Web Data"))

        # Default options: passwords and cards excluded
        res_default = seed_tools.refresh_seed(
            name="opt-default",
            source_browser="chrome",
            source_dir=str(source_root),
            platform="linux",
        )
        self.assertEqual(res_default["status"], "ok")
        dest_default = profiles.seed_dir("opt-default")
        self.assertFalse((dest_default / "Login Data").exists())
        self.assertFalse((dest_default / "Web Data").exists())

        # Opted-in: passwords and cards included
        res_optin = seed_tools.refresh_seed(
            name="opt-in",
            source_browser="chrome",
            source_dir=str(source_root),
            include_passwords=True,
            include_cards=True,
            platform="linux",
        )
        self.assertEqual(res_optin["status"], "ok")
        dest_optin = profiles.seed_dir("opt-in")
        self.assertTrue((dest_optin / "Login Data").is_file())
        self.assertTrue((dest_optin / "Web Data").is_file())

    def test_refresh_seed_source_busy_reported(self):
        source_root = self._create_synthetic_source()
        # Mock copy_seed to return source_busy
        original_copy = seed_copy.copy_seed
        try:
            seed_copy.copy_seed = lambda plan, src, dst: seed_copy.CopyResult(
                status="source_busy",
                skipped_busy=["Cookies"],
                errors=["database is locked"],
            )
            res = seed_tools.refresh_seed(
                name="busy-seed",
                source_browser="chrome",
                source_dir=str(source_root),
                platform="linux",
            )
            self.assertEqual(res["status"], "source_busy")
            self.assertEqual(res["copied_count"], 0)
            self.assertEqual(res["skipped_busy"], ["Cookies"])
            self.assertIn("database is locked", res["errors"])
        finally:
            seed_copy.copy_seed = original_copy

    def test_no_secrets_in_summary(self):
        source_root = self._create_synthetic_source()
        res = seed_tools.refresh_seed(
            name="secret-check",
            source_browser="chrome",
            source_dir=str(source_root),
            platform="linux",
        )
        as_str = json.dumps(res)
        self.assertNotIn("secret_123", as_str)
        self.assertNotIn("testadmin", as_str)
        self.assertNotIn("dummy cache", as_str)


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestRefreshSeedGatedRealBrowser(_TempDataDir):
    """Gated integration test for SB-46 acceptance:

    Make a synthetic source by launching Chrome on a temp dir, log in to fixture,
    close; refresh; seed has cookies, no caches; source hash unchanged.
    """

    def test_gated_chrome_fixture_login_and_refresh_seed(self):
        import nodriver as uc
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        async def run_gated_test():
            temp_user_data_dir = tempfile.mkdtemp(prefix="stealth_sb46_source_")
            browser = None
            try:
                with FixtureServer() as fixture_srv:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_user_data_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)

                    # Navigate to login fixture page
                    login_url = fixture_srv.url_for("login.html")
                    tab = await browser.get(login_url)

                    # Trigger login on fixture page
                    await tab.evaluate("document.getElementById('login-button').click()")
                    await asyncio.sleep(0.5)

                    # Also set cookies explicitly with Max-Age to ensure persistent disk storage
                    await tab.evaluate(
                        "document.cookie = 'session_token=secret_fixture_cookie_12345; Path=/; Max-Age=86400; SameSite=Lax';"
                        "document.cookie = 'auth_user=testadmin; Path=/; Max-Age=86400; SameSite=Lax';"
                    )
                    await asyncio.sleep(0.5)

                    # Confirm cookie was registered in the browser session
                    cookies_str = await tab.evaluate("document.cookie")
                    self.assertIn("session_token", cookies_str)

                # Orderly shutdown of Chrome to ensure cookie store is flushed to disk
                if getattr(browser, "connection", None) is not None:
                    try:
                        await asyncio.wait_for(
                            browser.connection.send(uc.cdp.browser.close()),
                            timeout=5.0,
                        )
                    except Exception:
                        pass
                if getattr(browser, "_process", None) is not None:
                    try:
                        await _stop_process(browser._process)
                    except Exception:
                        pass
                browser._process = None
                await asyncio.sleep(0.5)

                # 1. Take hash of the source tree before refresh
                before_hashes = source_hashes(temp_user_data_dir)
                self.assertGreater(len(before_hashes), 0, "Source tree must contain files")

                # 2. Call refresh_seed
                seed_name = "chrome-login-seed"
                refresh_res = seed_tools.refresh_seed(
                    name=seed_name,
                    source_browser="chrome",
                    source_dir=temp_user_data_dir,
                    platform="linux",
                )
                self.assertEqual(refresh_res["status"], "ok")
                self.assertGreater(refresh_res["copied_count"], 0)

                # 3. Assert source hash unchanged
                after_hashes = source_hashes(temp_user_data_dir)
                self.assertEqual(before_hashes, after_hashes, "Source tree hash must be unchanged")

                # 4. Assert seed has no caches
                seed_dir = profiles.seed_dir(seed_name)
                self.assertTrue(seed_dir.is_dir())
                self.assertFalse((seed_dir / "Cache").exists())
                self.assertFalse((seed_dir / "Code Cache").exists())
                self.assertFalse((seed_dir / "GPUCache").exists())

                # 5. Assert seed has Cookies store with cookie row
                cookie_file = seed_dir / "Network" / "Cookies"
                if not cookie_file.is_file():
                    cookie_file = seed_dir / "Cookies"
                self.assertTrue(cookie_file.is_file(), "Seed must have Cookies store file")

                con = sqlite3.connect(str(cookie_file))
                try:
                    cursor = con.cursor()
                    cursor.execute("SELECT name, value, host_key FROM cookies")
                    rows = cursor.fetchall()
                    self.assertGreater(len(rows), 0, "Cookies database must contain cookie rows")
                    names = [r[0] for r in rows]
                    self.assertTrue(
                        any("session_token" in n or "auth_user" in n for n in names),
                        f"Expected session_token or auth_user in cookie rows, found {names}",
                    )
                finally:
                    con.close()

            finally:
                if browser is not None:
                    try:
                        if getattr(browser, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    if getattr(browser, "_process", None) is not None:
                        try:
                            await _stop_process(browser._process)
                        except Exception:
                            pass
                    browser._process = None
                shutil.rmtree(temp_user_data_dir, ignore_errors=True)

        asyncio.run(run_gated_test())


if __name__ == "__main__":
    unittest.main()
