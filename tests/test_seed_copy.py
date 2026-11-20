"""Tests for the seed copy engine (SB-44).

All tests use synthetic tmp dirs and fakes. No browser is launched and no real
profile is read. A writer thread inserts rows while a copy runs to prove the
sqlite backup path yields a consistent snapshot. A simulated busy source (the
source opener raising a sharing violation) yields a busy result with the old
seed left intact.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import seed_copy as sc
from stealth_browser_mcp import seed_manifest as sm


def sha256(path):
    """Return the SHA-256 hex digest of ``path`` (None if unreadable)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hashes(root):
    """Return {rel_path: sha256} for every file under ``root``."""
    hashes = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            hashes[rel] = sha256(abs_path)
    return hashes


def make_sqlite_db(path, table="rows", seed_rows=50):
    """Create a real sqlite database at ``path`` with ``seed_rows`` rows."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE {} (id INTEGER PRIMARY KEY, value TEXT)".format(table))
        con.executemany(
            "INSERT INTO {} (value) VALUES (?)".format(table),
            [("row-{}".format(i),) for i in range(seed_rows)],
        )
        con.commit()
    finally:
        con.close()


class CopyEngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "profile"
        self.dest = self.root / "seed"
        self.source.mkdir()
        self.dest.mkdir()

    def build_plan(self, source=None, options=None):
        """Build a seed plan over the given source directory."""
        return sm.build_plan(str(source or self.source), options)


class ConsistencyTests(CopyEngineBase):
    def test_sqlite_copy_stays_consistent_while_writer_inserts(self):
        """A writer thread inserting rows while the copy runs yields a consistent
        snapshot that opens and passes PRAGMA integrity_check."""
        max_attempts = 3
        last_error = None
        for attempt in range(max_attempts):
            shutil.rmtree(self.source, ignore_errors=True)
            shutil.rmtree(self.dest, ignore_errors=True)
            self.source.mkdir(exist_ok=True)
            self.dest.mkdir(exist_ok=True)

            cookies = self.source / "Cookies"
            make_sqlite_db(str(cookies), seed_rows=100)
            plan = self.build_plan()

            stop = threading.Event()
            insertions = [0]
            lock = threading.Lock()

            def writer():
                con = sqlite3.connect(str(cookies))
                try:
                    counter = 100
                    while not stop.is_set():
                        with lock:
                            for i in range(20):
                                counter += 1
                                con.execute(
                                    "INSERT INTO rows (value) VALUES (?)",
                                    ("live-{}".format(counter),),
                                )
                            con.commit()
                            insertions[0] += 20
                        time.sleep(0.001)
                finally:
                    con.close()

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            try:
                result = sc.copy_seed(plan, str(self.source), str(self.dest))
            finally:
                stop.set()
                writer_thread.join()

            self.assertEqual(result.status, "ok", result.errors)
            self.assertIn("Cookies", result.copied)

            # The destination opens, rows are present, and the database is clean.
            dest_db = self.dest / "Cookies"
            self.assertTrue(dest_db.is_file())
            con = sqlite3.connect(str(dest_db))
            try:
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                row_count = con.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
            finally:
                con.close()

            self.assertEqual(integrity, "ok")
            self.assertGreaterEqual(row_count, 100)
            if insertions[0] >= 20:
                return
            last_error = f"Only {insertions[0]} rows inserted during copy on attempt {attempt + 1}"

        self.fail(last_error or "Consistency check failed after bounded retries")

    def test_source_is_never_modified(self):
        """Every source file hashes identically before and after the copy."""
        cookies = self.source / "Cookies"
        make_sqlite_db(str(cookies), seed_rows=100)
        prefs = self.source / "Preferences"
        prefs.write_bytes(b"\x00\x01\x02\x03" * 64)
        indexeddb = self.source / "IndexedDB"
        indexeddb.mkdir()
        origin = indexeddb / "https___a_example_com_0"
        origin.mkdir()
        make_sqlite_db(str(origin / "data.db"), table="store", seed_rows=30)
        # A non-sqlite file inside a copied directory (simulates a leveldb lock
        # file), plus a plain file to exercise the dir copy path.
        (origin / "LOCK").write_bytes(b"\x00" * 8)

        before = source_hashes(self.source)
        plan = self.build_plan()
        result = sc.copy_seed(plan, str(self.source), str(self.dest))

        after = source_hashes(self.source)
        self.assertEqual(result.status, "ok", result.errors)
        self.assertEqual(before, after, "source files changed during copy")

        # The destination tree contains the copied entries.
        self.assertTrue((self.dest / "Cookies").is_file())
        self.assertTrue((self.dest / "Preferences").is_file())
        self.assertTrue((self.dest / "IndexedDB" /
                         "https___a_example_com_0" / "data.db").is_file())


class AtomicPublishTests(CopyEngineBase):
    def test_repeated_copy_replaces_previous_seed(self):
        """A second copy replaces the first seed; both are valid and complete."""
        for value, table in (("one", "one_rows"), ("two", "two_rows")):
            cookies = self.source / "Cookies"
            make_sqlite_db(str(cookies), table=table, seed_rows=10)
            (self.source / "Preferences").write_text(value)
            result = sc.copy_seed(self.build_plan(), str(self.source),
                                  str(self.dest))
            self.assertEqual(result.status, "ok", result.errors)

        # The final seed is the second copy, and no half/leftover dirs remain.
        self.assertEqual((self.dest / "Preferences").read_text(), "two")
        self.assertFalse(os.path.exists(str(self.dest) + ".old"))
        self.assertFalse(os.path.exists(str(self.dest) + ".new"))


class BusyTests(CopyEngineBase):
    def _patch_busy(self):
        """Make the source sqlite open fail as a locked (busy) file."""
        return self._patch_busy_for(None)

    def _patch_busy_for(self, name=None):
        """Patch the opener to busy a single named store (or all stores)."""
        self._busy_for = name

        def busy_opener(uri, **kwargs):
            if name is None:
                raise PermissionError(21, "Permission denied (locked)")
            clean_uri = uri.split("?")[0].replace("%20", " ")
            if name in clean_uri or clean_uri.endswith(name):
                raise PermissionError(21, "Permission denied (locked)")
            return saved(uri, **kwargs)

        saved = sc._SQLITE_SRC_OPENER
        self._saved_opener = saved
        sc._SQLITE_SRC_OPENER = busy_opener

    def _restore_opener(self):
        sc._SQLITE_SRC_OPENER = self._saved_opener

    def test_busy_primary_store_reports_source_busy_and_keeps_old_seed(self):
        """A locked Cookies store -> status source_busy, and the old seed on disk
        is untouched (copied list empty, old content intact)."""
        old_cookies = self.source / "Cookies"
        make_sqlite_db(str(old_cookies), seed_rows=5)
        (self.source / "Preferences").write_text("seed-v1")

        # The previously published seed is an older, distinct tree.
        old_seed = self.dest / "Cookies"
        make_sqlite_db(str(old_seed), seed_rows=1)
        (self.dest / "Preferences").write_text("OLD")
        old_seed_before = sha256(str(old_seed))
        old_prefs_before = (self.dest / "Preferences").read_text()

        self._patch_busy()
        try:
            result = sc.copy_seed(self.build_plan(), str(self.source),
                                  str(self.dest))
        finally:
            self._restore_opener()

        self.assertEqual(result.status, "source_busy")
        self.assertEqual(result.copied, [])
        self.assertFalse(os.path.exists(str(self.dest) + ".new"))

        # The old seed is intact: same bytes, same content.
        self.assertEqual(sha256(str(old_seed)), old_seed_before)
        self.assertEqual((self.dest / "Preferences").read_text(), "OLD")

    def test_busy_non_primary_store_is_skipped_and_copy_continues(self):
        """A locked non-primary sqlite file is recorded in skipped_busy and the
        copy completes with the other files present."""
        cookies = self.source / "Cookies"
        make_sqlite_db(str(cookies), seed_rows=5)
        login = self.source / "Login Data"
        make_sqlite_db(str(login), table="logins", seed_rows=5)
        (self.source / "Preferences").write_text("ok")

        self._patch_busy_for("Login Data")
        try:
            plan = self.build_plan(options=sm.SeedOptions(include_passwords=True))
            result = sc.copy_seed(plan, str(self.source), str(self.dest))
        finally:
            self._restore_opener()

        # Login Data (non-primary) is busy and skipped; the copy still completes
        # because the primary Cookies store was healthy.
        self.assertEqual(result.status, "ok", result.errors)
        self.assertIn("Cookies", result.copied)
        self.assertIn("Login Data", result.skipped_busy)
        self.assertIn("Preferences", result.copied)
        self.assertFalse(os.path.exists(str(self.dest) + ".new"))

    def test_sqlite_exclusive_lock_detected_as_busy(self):
        """A store held under an exclusive transaction is detected as busy."""
        cookies = self.source / "Cookies"
        make_sqlite_db(str(cookies), seed_rows=5)
        (self.source / "Preferences").write_text("seed-v1")

        old_seed = self.dest / "Cookies"
        make_sqlite_db(str(old_seed), seed_rows=1)
        (self.dest / "Preferences").write_text("OLD")

        con = sqlite3.connect(str(cookies))
        try:
            con.execute("BEGIN EXCLUSIVE")
            saved_timeout = sc.SQLITE_TIMEOUT
            sc.SQLITE_TIMEOUT = 0.0
            try:
                result = sc.copy_seed(self.build_plan(), str(self.source),
                                      str(self.dest))
            finally:
                sc.SQLITE_TIMEOUT = saved_timeout
        finally:
            con.close()

        self.assertEqual(result.status, "source_busy")
        self.assertEqual(result.copied, [])
        self.assertEqual((self.dest / "Preferences").read_text(), "OLD")

    def test_sqlite_exclusive_lock_on_non_primary_is_skipped(self):
        """A non-primary store held under exclusive transaction is skipped."""
        cookies = self.source / "Cookies"
        make_sqlite_db(str(cookies), seed_rows=5)
        login = self.source / "Login Data"
        make_sqlite_db(str(login), table="logins", seed_rows=5)
        (self.source / "Preferences").write_text("ok")

        con = sqlite3.connect(str(login))
        try:
            con.execute("BEGIN EXCLUSIVE")
            saved_timeout = sc.SQLITE_TIMEOUT
            sc.SQLITE_TIMEOUT = 0.0
            try:
                plan = self.build_plan(options=sm.SeedOptions(include_passwords=True))
                result = sc.copy_seed(plan, str(self.source), str(self.dest))
            finally:
                sc.SQLITE_TIMEOUT = saved_timeout
        finally:
            con.close()

        self.assertEqual(result.status, "ok", result.errors)
        self.assertIn("Cookies", result.copied)
        self.assertIn("Login Data", result.skipped_busy)
        self.assertIn("Preferences", result.copied)


class ReflinkTests(CopyEngineBase):
    def test_plain_file_copy_via_dir_copy(self):
        """A LevelDB-style directory with non-sqlite files is copied file by file."""
        leveldb = self.source / "Local Storage"
        leveldb.mkdir()
        leveldb.joinpath("Preferences").write_bytes(b"\x00" * 256)
        leveldb.joinpath("LOCK").write_bytes(b"\x01" * 32)
        plan = self.build_plan()
        result = sc.copy_seed(plan, str(self.source), str(self.dest))
        self.assertEqual(result.status, "ok", result.errors)
        # Local Storage is a directory; its files are recorded with relative
        # paths under the directory. Assert the directory's files made it.
        self.assertTrue(any(
            c.startswith("Local Storage/") for c in result.copied))
        self.assertTrue((self.dest / "Local Storage" / "Preferences").is_file())
        self.assertEqual((self.dest / "Local Storage" / "LOCK").read_bytes(),
                         b"\x01" * 32)


if __name__ == "__main__":
    unittest.main()
