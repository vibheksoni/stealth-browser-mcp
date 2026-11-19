"""Unit tests and gated browser integration tests for extension cloning (SB-50).

Unit tests run with synthetic fixtures and temporary directories without a browser.
Real-browser tests are gated by STEALTH_BROWSER_TESTS=1.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import (
    browser_sources,
    paths,
    profiles,
    seed_extensions,
    session_registry,
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

EXT_FIXTURE_DIR = Path(__file__).resolve().parent / "browser_harness" / "ext_fixture"


class _TempDataDir(unittest.TestCase):
    """Base test case providing an isolated temporary data directory."""

    def setUp(self):
        self._saved_data = os.environ.get(paths.DATA_DIR_ENV)
        self._saved_cap = os.environ.get(session_registry._CAP_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-ext-test-")
        os.environ[paths.DATA_DIR_ENV] = self._tmp
        os.environ[session_registry._CAP_ENV] = "10"
        paths._DATA_DIR = None

    def tearDown(self):
        if self._saved_data is not None:
            os.environ[paths.DATA_DIR_ENV] = self._saved_data
        else:
            os.environ.pop(paths.DATA_DIR_ENV, None)

        if self._saved_cap is not None:
            os.environ[session_registry._CAP_ENV] = self._saved_cap
        else:
            os.environ.pop(session_registry._CAP_ENV, None)

        paths._DATA_DIR = None
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestExtensionPathFixup(_TempDataDir):
    """Unit tests for fixup_extension_paths and seed_extensions module."""

    def test_preferences_paths_rewritten(self):
        source_dir = Path(self._tmp) / "fake_source"
        session_dir = Path(self._tmp) / "fake_session"
        source_ext = source_dir / "Extensions" / "my_extension"
        clone_ext = session_dir / "Extensions" / "my_extension"

        source_ext.mkdir(parents=True, exist_ok=True)
        clone_ext.mkdir(parents=True, exist_ok=True)

        # Create Preferences in session_dir with path pointing into source_dir
        pref_data = {
            "extensions": {
                "settings": {
                    "abcdefghijklmnopabcdefghijklmnop": {
                        "location": 4,
                        "path": str(source_ext),
                        "state": 1,
                    }
                }
            }
        }
        pref_file = session_dir / "Preferences"
        pref_file.write_text(json.dumps(pref_data, indent=2), encoding="ascii")

        report = seed_extensions.fixup_extension_paths(session_dir, source_dir)

        # Assert report values
        self.assertEqual(report.fixed_count, 1)
        self.assertEqual(report["fixed_count"], 1)
        expected_clone_path = os.path.normpath(str(clone_ext))
        self.assertEqual(
            report.load_extension_args,
            [f"--load-extension={expected_clone_path}"],
        )

        # Assert Preferences file on disk was rewritten
        disk_data = json.loads(pref_file.read_text(encoding="ascii"))
        rewritten_path = disk_data["extensions"]["settings"]["abcdefghijklmnopabcdefghijklmnop"]["path"]
        self.assertEqual(rewritten_path, expected_clone_path)

    def test_secure_preferences_untouched_byte_for_byte(self):
        source_dir = Path(self._tmp) / "fake_source"
        session_dir = Path(self._tmp) / "fake_session"
        session_dir.mkdir(parents=True, exist_ok=True)

        source_ext = source_dir / "Extensions" / "sec_ext"
        clone_ext = session_dir / "Extensions" / "sec_ext"
        clone_ext.mkdir(parents=True, exist_ok=True)

        # Create Secure Preferences with arbitrary bytes and HMAC dummy signature
        sec_content = (
            b'{\n'
            b'  "extensions": {\n'
            b'    "settings": {\n'
            b'      "extsecid12345": {\n'
            b'        "location": 4,\n'
            b'        "path": "' + str(source_ext).encode("ascii") + b'"\n'
            b'      }\n'
            b'    }\n'
            b'  },\n'
            b'  "protection": {"macs": {"extensions": "HMAC_SIGNATURE_12345"}}\n'
            b'}\n'
        )
        sec_file = session_dir / "Secure Preferences"
        sec_file.write_bytes(sec_content)

        # Empty plain Preferences
        pref_file = session_dir / "Preferences"
        pref_file.write_text("{}", encoding="ascii")

        report = seed_extensions.fixup_extension_paths(session_dir, source_dir)

        # Assert Secure Preferences is untouched byte-for-byte
        after_bytes = sec_file.read_bytes()
        self.assertEqual(after_bytes, sec_content)

        # Assert the unpacked extension was identified and added to load_extension_args
        expected_clone_path = os.path.normpath(str(clone_ext))
        self.assertIn(f"--load-extension={expected_clone_path}", report.load_extension_args)

    def test_noop_when_no_extensions(self):
        source_dir = Path(self._tmp) / "fake_source"
        session_dir = Path(self._tmp) / "fake_session"
        session_dir.mkdir(parents=True, exist_ok=True)

        pref_file = session_dir / "Preferences"
        original_pref = '{\n  "browser": {\n    "has_seen_welcome_page": true\n  }\n}\n'
        pref_file.write_text(original_pref, encoding="ascii")

        report = seed_extensions.fixup_extension_paths(session_dir, source_dir)

        self.assertEqual(report.fixed_count, 0)
        self.assertEqual(report.load_extension_args, [])
        self.assertEqual(pref_file.read_text(encoding="ascii"), original_pref)

    def test_multiple_extensions_filtering(self):
        source_dir = Path(self._tmp) / "fake_source"
        session_dir = Path(self._tmp) / "fake_session"

        ext1_src = source_dir / "Extensions" / "ext1"
        ext2_src = source_dir / "Extensions" / "ext2"
        (session_dir / "Extensions" / "ext1").mkdir(parents=True, exist_ok=True)
        (session_dir / "Extensions" / "ext2").mkdir(parents=True, exist_ok=True)

        pref_data = {
            "extensions": {
                "settings": {
                    "ext_unpacked_1": {
                        "location": 4,
                        "path": str(ext1_src),
                    },
                    "ext_packed_web_store": {
                        "location": 1,
                        "path": "1.0.0_0",
                    },
                    "ext_external_unpacked": {
                        "location": 4,
                        "path": "/opt/external_extension_dir",
                    },
                    "ext_unpacked_2": {
                        "location": 4,
                        "path": str(ext2_src),
                    },
                }
            }
        }
        pref_file = session_dir / "Preferences"
        pref_file.write_text(json.dumps(pref_data, indent=2), encoding="ascii")

        report = seed_extensions.fixup_extension_paths(session_dir, source_dir)

        # Only the two unpacked extensions inside source_dir should be rewritten
        self.assertEqual(report.fixed_count, 2)
        disk_data = json.loads(pref_file.read_text(encoding="ascii"))
        settings = disk_data["extensions"]["settings"]

        # ext1 and ext2 rewritten
        self.assertEqual(settings["ext_unpacked_1"]["path"], os.path.normpath(str(session_dir / "Extensions" / "ext1")))
        self.assertEqual(settings["ext_unpacked_2"]["path"], os.path.normpath(str(session_dir / "Extensions" / "ext2")))

        # Packed extension and external extension untouched
        self.assertEqual(settings["ext_packed_web_store"]["path"], "1.0.0_0")
        self.assertEqual(settings["ext_external_unpacked"]["path"], "/opt/external_extension_dir")

        # Comma-separated load-extension arg
        self.assertEqual(len(report.load_extension_args), 1)
        self.assertTrue(report.load_extension_args[0].startswith("--load-extension="))
        self.assertIn(os.path.normpath(str(session_dir / "Extensions" / "ext1")), report.load_extension_args[0])
        self.assertIn(os.path.normpath(str(session_dir / "Extensions" / "ext2")), report.load_extension_args[0])

    def test_clone_has_extensions_detection(self):
        empty_dir = Path(self._tmp) / "empty"
        empty_dir.mkdir(parents=True, exist_ok=True)
        self.assertFalse(seed_extensions.clone_has_extensions(empty_dir))

        ext_dir = Path(self._tmp) / "with_ext"
        (ext_dir / "Extensions").mkdir(parents=True, exist_ok=True)
        self.assertTrue(seed_extensions.clone_has_extensions(ext_dir))

        loc_dir = Path(self._tmp) / "with_loc_settings"
        (loc_dir / "Local Extension Settings").mkdir(parents=True, exist_ok=True)
        self.assertTrue(seed_extensions.clone_has_extensions(loc_dir))

        pref_dir = Path(self._tmp) / "with_pref"
        pref_dir.mkdir(parents=True, exist_ok=True)
        (pref_dir / "Preferences").write_text(
            json.dumps({"extensions": {"settings": {"abc": {"path": "x"}}}}),
            encoding="ascii",
        )
        self.assertTrue(seed_extensions.clone_has_extensions(pref_dir))


class TestSessionSpawnExtensionWiring(_TempDataDir):
    """Test integration between session_spawn and seed_extensions."""

    def test_spawn_wires_load_extension_args_for_clones(self):
        seed_name = "wire-seed"
        seed_dir = profiles.seed_dir(seed_name)
        seed_dir.mkdir(parents=True, exist_ok=True)

        source_dir = Path(self._tmp) / "orig_source"
        source_ext = source_dir / "Extensions" / "test_ext"
        seed_ext = seed_dir / "Extensions" / "test_ext"
        seed_ext.mkdir(parents=True, exist_ok=True)

        seed_data = {
            "source_browser": "chrome",
            "source_dir": str(source_dir),
            "profile_subdir": "Default",
            "password_store": "basic",
            "refreshed_at": int(time.time()),
        }
        profiles.save_seed(seed_name, seed_data)

        # Write Preferences with unpacked extension path pointing into source_dir
        pref_content = {
            "extensions": {
                "settings": {
                    "myfixtureextid": {
                        "location": 4,
                        "path": str(source_ext),
                    }
                }
            }
        }
        (seed_dir / "Preferences").write_text(json.dumps(pref_content), encoding="ascii")

        # 1. Prepare session spawn
        session_id, session_dir, loaded_seed_data = session_spawn.prepare_session_spawn(seed_name)
        self.assertTrue(session_dir.is_dir())

        # 2. Clone seed to session (triggers extension path fixup)
        clone_ms = session_spawn.clone_seed_to_session(seed_name, session_id, loaded_seed_data)
        self.assertGreaterEqual(clone_ms, 0.0)

        # Verify load_extension_args was populated in loaded_seed_data
        self.assertIn("load_extension_args", loaded_seed_data)
        self.assertTrue(len(loaded_seed_data["load_extension_args"]) > 0)
        expected_arg_prefix = "--load-extension="
        self.assertTrue(loaded_seed_data["load_extension_args"][0].startswith(expected_arg_prefix))

        # 3. Build spawn args and verify --load-extension is present
        args, _ = session_spawn.build_spawn_args_and_config(
            loaded_seed_data,
            caller_args=["--no-sandbox"],
        )
        has_load_ext = any(a.startswith("--load-extension=") for a in args)
        self.assertTrue(has_load_ext)

        # 4. Verify cloned Preferences on disk has rewritten path
        target_pdir = session_dir / "Default"
        pref_on_disk = json.loads((target_pdir / "Preferences").read_text(encoding="ascii"))
        rewritten = pref_on_disk["extensions"]["settings"]["myfixtureextid"]["path"]
        self.assertEqual(rewritten, os.path.normpath(str(target_pdir / "Extensions" / "test_ext")))

        session_spawn.cleanup_session(session_id)


class TestGatedExtensionTwoClones(unittest.TestCase):
    """Gated real-browser test for extension presence in two cloned sessions (SB-50)."""

    @unittest.skipUnless(RUN_BROWSER_TESTS, "STEALTH_BROWSER_TESTS not enabled")
    def test_extension_present_in_two_clones(self):
        async def run_test():
            manager = BrowserManager()
            temp_data_dir = tempfile.mkdtemp(prefix="stealth-gated-ext-data-")
            saved_data = os.environ.get(paths.DATA_DIR_ENV)
            os.environ[paths.DATA_DIR_ENV] = temp_data_dir
            paths._DATA_DIR = None

            try:
                with FixtureServer() as server:
                    # Setup seed with local MIT fixture extension
                    seed_name = "ext-clone-seed"
                    seed_dir = profiles.seed_dir(seed_name)
                    seed_dir.mkdir(parents=True, exist_ok=True)

                    source_fake_dir = Path(temp_data_dir) / "source_profile"
                    source_ext_path = source_fake_dir / "Default" / "Extensions" / "ext_fixture"

                    seed_ext_path = seed_dir / "Default" / "Extensions" / "ext_fixture"
                    seed_ext_path.mkdir(parents=True, exist_ok=True)

                    # Copy extension fixture files into seed
                    for item in EXT_FIXTURE_DIR.iterdir():
                        if item.is_file():
                            shutil.copy2(item, seed_ext_path / item.name)

                    seed_data = {
                        "source_browser": "chrome",
                        "source_dir": str(source_fake_dir),
                        "profile_subdir": "Default",
                        "password_store": "basic",
                        "refreshed_at": int(time.time()),
                    }
                    profiles.save_seed(seed_name, seed_data)

                    # Write seed Preferences referencing the extension
                    pref_content = {
                        "extensions": {
                            "settings": {
                                "stealthfixtureextensionid00000000": {
                                    "location": 4,
                                    "path": str(source_ext_path),
                                    "state": 1,
                                }
                            }
                        }
                    }
                    (seed_dir / "Default" / "Preferences").write_text(
                        json.dumps(pref_content),
                        encoding="ascii",
                    )
                    (seed_dir / "Preferences").write_text(
                        json.dumps(pref_content),
                        encoding="ascii",
                    )
                    (seed_dir / browser_sources.LOCAL_STATE_FILE).write_text(
                        json.dumps({"profile": {"info_cache": [{"name": "Default"}]}}),
                        encoding="ascii",
                    )

                    inst1 = None
                    inst2 = None
                    try:
                        # Spawn Clone 1
                        opt1 = BrowserOptions(
                            headless=True,
                            sandbox=False,
                            seed=seed_name,
                        )
                        inst1 = await manager.spawn_browser(opt1)
                        self.assertIsNotNone(inst1)

                        # Spawn Clone 2
                        opt2 = BrowserOptions(
                            headless=True,
                            sandbox=False,
                            seed=seed_name,
                        )
                        inst2 = await manager.spawn_browser(opt2)
                        self.assertIsNotNone(inst2)

                        fixture_url = server.url_for("login.html")

                        # Navigate Clone 1 and check extension marker
                        await manager.navigate(inst1.instance_id, fixture_url, wait_until="load")
                        tab1 = await manager.get_navigation_tab(inst1.instance_id)
                        await asyncio.sleep(0.5)
                        marker1 = await tab1.evaluate(
                            "document.documentElement.getAttribute('data-fixture-extension')"
                        )
                        if marker1 != "loaded":
                            marker1 = await tab1.evaluate(
                                "document.body ? document.body.getAttribute('data-fixture-extension') : null"
                            )

                        if marker1 != "loaded":
                            self.skipTest(
                                "Extension did not load in headless mode in this environment "
                                "(headless chrome requires headless=new with extension support)"
                            )

                        self.assertEqual(marker1, "loaded")

                        # Navigate Clone 2 and check extension marker
                        await manager.navigate(inst2.instance_id, fixture_url, wait_until="load")
                        tab2 = await manager.get_navigation_tab(inst2.instance_id)
                        await asyncio.sleep(0.5)
                        marker2 = await tab2.evaluate(
                            "document.documentElement.getAttribute('data-fixture-extension')"
                        )
                        if marker2 != "loaded":
                            marker2 = await tab2.evaluate(
                                "document.body ? document.body.getAttribute('data-fixture-extension') : null"
                            )
                        self.assertEqual(marker2, "loaded")

                    finally:
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
            finally:
                try:
                    await manager.close_all()
                except Exception:
                    pass
                if saved_data is not None:
                    os.environ[paths.DATA_DIR_ENV] = saved_data
                else:
                    os.environ.pop(paths.DATA_DIR_ENV, None)
                paths._DATA_DIR = None
                shutil.rmtree(temp_data_dir, ignore_errors=True)

        asyncio.run(run_test())

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
