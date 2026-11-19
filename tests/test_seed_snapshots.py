"""Tests for seed snapshots and rollback (SB-38).

All tests use synthetic tmp dirs and fakes. No browser is launched and no real
profile is read. Tests verify:
- Snapshot creation under the per-seed lock.
- Exact file set restoration (extra files removed, modified files reverted).
- Refusal to restore while live sessions use the seed (fake pid_alive).
- Snapshot retention pruning older snapshots beyond the limit.
- Listing, deletion, and thin MCP tools in server.py.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import file_lock, paths, profiles, seed_snapshots, server


class _TempDataDir(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(paths.DATA_DIR_ENV)
        self._tmp = tempfile.mkdtemp(prefix="stealth-snapshots-test-")
        os.environ[paths.DATA_DIR_ENV] = self._tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _create_sample_seed(self, name="test_seed"):
        profiles.create_seed(
            name,
            source_browser="chrome",
            source_dir="/tmp/fake_source",
            profile_subdir="Default",
        )
        sdir = profiles.seed_dir(name)
        (sdir / "Preferences").write_text('{"theme": "dark"}', encoding="ascii")
        sub = sdir / "subfolder"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "config.txt").write_text("config_content", encoding="ascii")
        return sdir


class SnapshotSeedLifecycleTests(_TempDataDir):
    def test_snapshot_creates_directory_and_copies_files(self):
        self._create_sample_seed("seed1")
        result = seed_snapshots.snapshot_seed("seed1", label="initial", now=1000)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["snapshot_id"], "1000-initial")
        self.assertEqual(result["id"], "1000-initial")
        self.assertEqual(result["label"], "initial")
        self.assertEqual(result["name"], "seed1")
        self.assertEqual(result["seed"], "seed1")

        snap_path = Path(result["path"])
        self.assertTrue(snap_path.is_dir())
        self.assertTrue((snap_path / profiles.SEED_FILE).is_file())
        self.assertEqual(
            (snap_path / "Preferences").read_text(encoding="ascii"),
            '{"theme": "dark"}',
        )
        self.assertEqual(
            (snap_path / "subfolder" / "config.txt").read_text(encoding="ascii"),
            "config_content",
        )

        snapshots = seed_snapshots.list_seed_snapshots("seed1")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_id"], "1000-initial")
        self.assertEqual(snapshots[0]["label"], "initial")
        self.assertTrue(seed_snapshots.snapshot_exists("seed1", "1000-initial"))

    def test_snapshot_without_label(self):
        self._create_sample_seed("seed_no_label")
        result = seed_snapshots.snapshot_seed("seed_no_label", label=None, now=2000)

        self.assertEqual(result["snapshot_id"], "2000")
        self.assertIsNone(result["label"])
        self.assertTrue(Path(result["path"]).is_dir())

        snapshots = seed_snapshots.list_seed_snapshots("seed_no_label")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["snapshot_id"], "2000")
        self.assertIsNone(snapshots[0]["label"])

    def test_snapshot_seed_not_found(self):
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("nonexistent_seed")

    def test_snapshot_invalid_label(self):
        self._create_sample_seed("seed_bad_label")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("seed_bad_label", label="../escape")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("seed_bad_label", label="invalid/slash")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("seed_bad_label", label=".")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("seed_bad_label", label="..")

    def test_snapshot_invalid_seed_name(self):
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.snapshot_seed("bad/name")


class SnapshotLockTests(_TempDataDir):
    def test_snapshot_under_seed_lock(self):
        self._create_sample_seed("locked_seed")
        lock = seed_snapshots.seed_lock("locked_seed", timeout=0.2)
        lock.acquire()
        try:
            # While the lock is held, a snapshot attempt with timeout=0.05 must fail.
            with self.assertRaises(file_lock.LockTimeout):
                seed_snapshots.snapshot_seed("locked_seed", label="blocked", lock_timeout=0.05)
        finally:
            lock.release()

        # Once released, taking the snapshot succeeds.
        result = seed_snapshots.snapshot_seed("locked_seed", label="unblocked")
        self.assertEqual(result["status"], "ok")

    def test_restore_under_seed_lock(self):
        self._create_sample_seed("locked_restore")
        snap = seed_snapshots.snapshot_seed("locked_restore", label="v1")

        lock = seed_snapshots.seed_lock("locked_restore", timeout=0.2)
        lock.acquire()
        try:
            with self.assertRaises(file_lock.LockTimeout):
                seed_snapshots.restore_seed_snapshot(
                    "locked_restore", snap["snapshot_id"], lock_timeout=0.05
                )
        finally:
            lock.release()

        result = seed_snapshots.restore_seed_snapshot("locked_restore", snap["snapshot_id"])
        self.assertEqual(result["status"], "ok")


class RestoreExactFileSetTests(_TempDataDir):
    def test_restore_reverts_modifications_and_removes_extra_files(self):
        sdir = self._create_sample_seed("restore_seed")
        (sdir / "file1.txt").write_text("orig1", encoding="ascii")
        (sdir / "file2.txt").write_text("orig2", encoding="ascii")

        snap = seed_snapshots.snapshot_seed("restore_seed", label="checkpoint1")

        # Mutate the seed:
        # 1. Modify existing file
        (sdir / "file1.txt").write_text("modified_text", encoding="ascii")
        (sdir / "Preferences").write_text('{"theme": "corrupted"}', encoding="ascii")
        # 2. Delete a file
        (sdir / "file2.txt").unlink()
        # 3. Add an extra file that was not in the snapshot
        (sdir / "extra.txt").write_text("unwanted_extra", encoding="ascii")
        # 4. Add an extra nested directory and file
        extra_dir = sdir / "extra_dir"
        extra_dir.mkdir()
        (extra_dir / "rogue.txt").write_text("rogue_content", encoding="ascii")

        # Confirm mutations happened
        self.assertTrue((sdir / "extra.txt").exists())
        self.assertTrue((extra_dir / "rogue.txt").exists())
        self.assertFalse((sdir / "file2.txt").exists())
        self.assertEqual((sdir / "file1.txt").read_text(encoding="ascii"), "modified_text")

        # Restore from snapshot
        outcome = seed_snapshots.restore_seed_snapshot("restore_seed", snap["snapshot_id"])
        self.assertEqual(outcome["status"], "ok")
        self.assertTrue(outcome["restored"])

        # Confirm exact file set restored
        self.assertTrue((sdir / "file1.txt").exists())
        self.assertEqual((sdir / "file1.txt").read_text(encoding="ascii"), "orig1")
        self.assertTrue((sdir / "file2.txt").exists())
        self.assertEqual((sdir / "file2.txt").read_text(encoding="ascii"), "orig2")
        self.assertEqual(
            (sdir / "Preferences").read_text(encoding="ascii"),
            '{"theme": "dark"}',
        )
        self.assertEqual(
            (sdir / "subfolder" / "config.txt").read_text(encoding="ascii"),
            "config_content",
        )

        # Extra files and directories must be completely removed
        self.assertFalse((sdir / "extra.txt").exists())
        self.assertFalse((sdir / "extra_dir").exists())

    def test_restore_missing_snapshot(self):
        self._create_sample_seed("restore_missing")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.restore_seed_snapshot("restore_missing", "9999-missing")

    def test_restore_invalid_snapshot_id(self):
        self._create_sample_seed("restore_invalid")
        with self.assertRaises(profiles.ProfileError):
            seed_snapshots.restore_seed_snapshot("restore_invalid", "../bad")


class RefusalWithLiveSessionTests(_TempDataDir):
    def test_restore_refused_when_session_is_live(self):
        self._create_sample_seed("seed_live")
        snap = seed_snapshots.snapshot_seed("seed_live", label="v1")

        # Create session associated with seed_live
        profiles.create_session("sess_123", seed="seed_live", pid=54321)

        # Fake pid_alive returning True for pid 54321
        def fake_pid_alive(pid):
            return pid == 54321

        with mock.patch("stealth_browser_mcp.seed_snapshots.pid_alive", side_effect=fake_pid_alive), \
             mock.patch("stealth_browser_mcp.profiles.pid_alive", side_effect=fake_pid_alive):
            with self.assertRaises(profiles.ProfileError) as ctx:
                seed_snapshots.restore_seed_snapshot("seed_live", snap["snapshot_id"])
            self.assertIn("seed_live is in use", str(ctx.exception))
            self.assertIn("54321", str(ctx.exception))

    def test_restore_allowed_when_session_is_dead(self):
        self._create_sample_seed("seed_dead")
        snap = seed_snapshots.snapshot_seed("seed_dead", label="v1")

        profiles.create_session("sess_dead", seed="seed_dead", pid=98765)

        # Fake pid_alive returning False (process is dead / stale session)
        with mock.patch("stealth_browser_mcp.seed_snapshots.pid_alive", return_value=False), \
             mock.patch("stealth_browser_mcp.profiles.pid_alive", return_value=False):
            result = seed_snapshots.restore_seed_snapshot("seed_dead", snap["snapshot_id"])
            self.assertEqual(result["status"], "ok")


class RetentionTests(_TempDataDir):
    def test_retention_prunes_oldest_snapshots(self):
        self._create_sample_seed("seed_retention")

        # Create 7 snapshots with max_snapshots=5
        created_ids = []
        for i in range(7):
            res = seed_snapshots.snapshot_seed(
                "seed_retention",
                label=f"snap{i}",
                max_snapshots=5,
                now=1000 + i,
            )
            created_ids.append(res["snapshot_id"])

        snapshots = seed_snapshots.list_seed_snapshots("seed_retention")
        self.assertEqual(len(snapshots), 5)

        remaining_ids = [s["snapshot_id"] for s in snapshots]
        # The oldest two snapshots must have been pruned
        self.assertEqual(remaining_ids, created_ids[2:])
        self.assertNotIn(created_ids[0], remaining_ids)
        self.assertNotIn(created_ids[1], remaining_ids)

        # Confirm on disk
        base_dir = seed_snapshots.seed_snapshots_dir("seed_retention")
        self.assertFalse((base_dir / created_ids[0]).exists())
        self.assertFalse((base_dir / created_ids[1]).exists())
        self.assertTrue((base_dir / created_ids[2]).exists())
        self.assertTrue((base_dir / created_ids[6]).exists())

    def test_retention_disabled_when_none_or_zero(self):
        self._create_sample_seed("seed_no_ret")
        for i in range(4):
            seed_snapshots.snapshot_seed(
                "seed_no_ret",
                label=f"snap{i}",
                max_snapshots=None,
                now=2000 + i,
            )
        snapshots = seed_snapshots.list_seed_snapshots("seed_no_ret")
        self.assertEqual(len(snapshots), 4)


class DeletionAndListingTests(_TempDataDir):
    def test_list_empty_seed(self):
        self._create_sample_seed("empty_snaps")
        self.assertEqual(seed_snapshots.list_seed_snapshots("empty_snaps"), [])

    def test_delete_snapshot(self):
        self._create_sample_seed("del_seed")
        snap = seed_snapshots.snapshot_seed("del_seed", label="to_delete")
        self.assertTrue(seed_snapshots.snapshot_exists("del_seed", snap["snapshot_id"]))

        deleted = seed_snapshots.delete_seed_snapshot("del_seed", snap["snapshot_id"])
        self.assertTrue(deleted)
        self.assertFalse(seed_snapshots.snapshot_exists("del_seed", snap["snapshot_id"]))
        self.assertEqual(seed_snapshots.list_seed_snapshots("del_seed"), [])

        # Deleting again returns False
        self.assertFalse(seed_snapshots.delete_seed_snapshot("del_seed", snap["snapshot_id"]))


class ServerMCPToolTests(_TempDataDir):
    def test_mcp_tools_flow(self):
        self._create_sample_seed("mcp_seed")

        # Snapshot tool
        snap_res = asyncio.run(
            server.snapshot_seed.fn("mcp_seed", label="mcp_snap")
        )
        self.assertEqual(snap_res["status"], "ok")
        snap_id = snap_res["snapshot_id"]

        # List tool
        list_res = asyncio.run(
            server.list_seed_snapshots.fn("mcp_seed")
        )
        self.assertEqual(len(list_res), 1)
        self.assertEqual(list_res[0]["snapshot_id"], snap_id)

        # Mutate seed
        sdir = profiles.seed_dir("mcp_seed")
        (sdir / "unwanted.txt").write_text("bad", encoding="ascii")
        self.assertTrue((sdir / "unwanted.txt").exists())

        # Restore tool
        restore_res = asyncio.run(
            server.restore_seed_snapshot.fn("mcp_seed", snap_id)
        )
        self.assertEqual(restore_res["status"], "ok")
        self.assertFalse((sdir / "unwanted.txt").exists())


if __name__ == "__main__":
    unittest.main()
