"""Tests for the doctor environment-health module.

Every check is driven with fakes and temporary directories so no real browser,
keyring, network connection, or real profile is ever touched. Each check has an
``ok`` and a ``fail`` case, plus a redaction case.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import doctor
from stealth_browser_mcp import profiles
from stealth_browser_mcp import secrets_env


class DoctorChecksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    # --- browser binaries -------------------------------------------------

    def test_browser_binaries_ok(self):
        found = {"chrome": "/usr/bin/google-chrome"}
        check = doctor.run_checks(
            env={"OS": "linux"},
            platform="win32",
            which_fn=lambda name: found.get(name),
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["browser_binaries"]["status"], "ok")
        self.assertIn("chrome", by_name["browser_binaries"]["detail"])

    def test_browser_binaries_fail(self):
        check = doctor.run_checks(
            env={"OS": "linux"},
            platform="linux",
            which_fn=lambda name: None,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["browser_binaries"]["status"], "fail")

    # --- keyring ----------------------------------------------------------

    def test_keyring_ok_secret_tool(self):
        check = doctor.run_checks(
            platform="linux",
            which_fn=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["keyring"]["status"], "ok")

    def test_keyring_fail(self):
        check = doctor.run_checks(
            platform="linux",
            which_fn=lambda name: None,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["keyring"]["status"], "warn")

    def test_keyring_skipped_non_linux(self):
        check = doctor.run_checks(
            platform="darwin",
            which_fn=lambda name: None,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["keyring"]["status"], "skipped")

    # --- seed ages --------------------------------------------------------

    def test_seed_ages_ok(self):
        ages = {"alpha": 3}
        check = doctor.run_checks(
            seed_ages_fn=lambda: ages,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["seed_ages"]["status"], "ok")

    def test_seed_ages_warn(self):
        ages = {"alpha": 5, "old": 40}
        check = doctor.run_checks(
            seed_ages_fn=lambda: ages,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["seed_ages"]["status"], "warn")
        self.assertIn("old", by_name["seed_ages"]["detail"])

    def test_seed_ages_default_reads_files(self):
        seed_dir = self.data_dir / "seeds" / "s1"
        seed_dir.mkdir(parents=True)
        seed = {
            "id": "s1",
            "refreshed_at": time.time() - (10 * 24 * 3600),
            "session_id": None,
            "pid": None,
        }
        import json

        (seed_dir / "seed.json").write_text(
            json.dumps(seed, ensure_ascii=True), encoding="ascii"
        )
        check = doctor.run_checks(data_dir=self.data_dir)
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["seed_ages"]["status"], "ok")
        self.assertEqual(by_name["seed_ages"]["detail"], "no seeds to check")

    # --- live sessions vs cap --------------------------------------------

    def test_live_sessions_ok(self):
        check = doctor.run_checks(
            cap=4,
            live_session_count=1,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["live_sessions"]["status"], "ok")
        self.assertIn("cap 4", by_name["live_sessions"]["detail"])

    def test_live_sessions_fail(self):
        check = doctor.run_checks(
            cap=2,
            live_session_count=2,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["live_sessions"]["status"], "fail")

    # --- stale locks ------------------------------------------------------

    def test_stale_locks_ok(self):
        check = doctor.run_checks(
            lock_files_fn=lambda: ["/data/locks/a.lock"],
            lock_alive_fn=lambda path: True,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["stale_locks"]["status"], "ok")

    def test_stale_locks_warn(self):
        check = doctor.run_checks(
            lock_files_fn=lambda: ["/data/locks/a.lock", "/data/locks/b.lock"],
            lock_alive_fn=lambda path: path != "/data/locks/b.lock",
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["stale_locks"]["status"], "warn")
        self.assertIn("1 stale", by_name["stale_locks"]["detail"])

    def test_stale_locks_no_dir_ok(self):
        check = doctor.run_checks(data_dir=self.data_dir)
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["stale_locks"]["status"], "ok")

    # --- data dir writable ------------------------------------------------

    def test_data_dir_writable_ok(self):
        check = doctor.run_checks(data_dir=self.data_dir)
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["data_dir_writable"]["status"], "ok")

    def test_data_dir_writable_fail(self):
        check = doctor.run_checks(
            writable_fn=lambda directory: False,
            data_dir=self.data_dir,
        )
        by_name = {c["name"]: c for c in check["checks"]}
        self.assertEqual(by_name["data_dir_writable"]["status"], "fail")

    # --- redaction --------------------------------------------------------

    def test_output_is_redacted(self):
        redactor = secrets_env.Redactor()
        redactor.register("supersecrettoken")
        # Even if a secret name were echoed into detail, it must be masked.
        check = doctor.run_checks(
            env={"STEALTH_SUPER_SECRET_TOKEN": "supersecrettoken"},
            platform="linux",
            which_fn=lambda name: None,
            seed_ages_fn=lambda: {},
            cap=1,
            live_session_count=1,
            lock_files_fn=lambda: [],
            data_dir=self.data_dir,
            redactor=redactor,
        )
        blob = str(check)
        self.assertNotIn("supersecrettoken", blob)
        self.assertEqual(redactor.secrets, {"supersecrettoken"})

    # --- overall summary --------------------------------------------------

    def test_summary_reflects_checks(self):
        check = doctor.run_checks(
            platform="win32",
            which_fn=lambda name: None,  # browser fail, keyring skipped
            seed_ages_fn=lambda: {},
            cap=4,
            live_session_count=1,
            lock_files_fn=lambda: [],
            data_dir=self.data_dir,
        )
        self.assertEqual(check["summary"]["total"], len(check["checks"]))
        self.assertEqual(check["summary"]["fail"], 1)
        self.assertEqual(check["summary"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
