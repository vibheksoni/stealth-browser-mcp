import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.process_cleanup import ProcessCleanup


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str = "chrome",
        cmdline: list = None,
        create_time: float = 100.0,
        children: list = None,
    ):
        self.pid = pid
        self._name = name
        self._cmdline = list(cmdline or [])
        self._create_time = create_time
        self._children = list(children or [])
        self.terminated = False
        self.killed = False
        self.waited = False
        self.info = {
            "pid": pid,
            "name": name,
            "cmdline": self._cmdline,
        }
        self._psutil_ref = None

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list:
        return list(self._cmdline)

    def create_time(self) -> float:
        return self._create_time

    def children(self, recursive: bool = True) -> list:
        result = list(self._children)
        if recursive:
            for child in self._children:
                result.extend(child.children(recursive=True))
        return result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float = None) -> None:
        self.waited = True
        if self._psutil_ref is not None and self.pid in self._psutil_ref._processes:
            del self._psutil_ref._processes[self.pid]


class FakePsutil:
    NoSuchProcess = psutil.NoSuchProcess
    AccessDenied = psutil.AccessDenied
    ZombieProcess = psutil.ZombieProcess
    TimeoutExpired = psutil.TimeoutExpired

    def __init__(self, processes: dict = None):
        self._processes = {}
        if processes:
            for proc in processes.values():
                self.add_process(proc)

    def add_process(self, proc: FakeProcess) -> None:
        proc._psutil_ref = self
        self._processes[proc.pid] = proc

    def pid_exists(self, pid: int) -> bool:
        return pid in self._processes

    def Process(self, pid: int):
        if pid not in self._processes:
            raise self.NoSuchProcess(pid)
        return self._processes[pid]

    def process_iter(self, attrs: list = None):
        return list(self._processes.values())


class ProcessCleanupTreeKillTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.tmp_dir = self.tmp_dir_obj.name
        self.pid_file = Path(self.tmp_dir) / "stealth_browser_pids.json"

    def tearDown(self):
        self.tmp_dir_obj.cleanup()

    def _create_cleanup(self, fake_psutil: FakePsutil) -> ProcessCleanup:
        with mock.patch("os.path.expanduser", return_value=str(self.pid_file)):
            with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
                return ProcessCleanup()

    def test_foreign_chrome_on_same_dir_survives_and_tree_killed(self):
        shared_profile = str(Path(self.tmp_dir) / "shared_chrome_dir")

        child_1 = FakeProcess(101, name="renderer", cmdline=["chrome", "--type=renderer"])
        child_2 = FakeProcess(102, name="crashpad_handler", cmdline=["chrome_crashpad_handler"])
        launched_proc = FakeProcess(
            100,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
            create_time=50.0,
            children=[child_1, child_2],
        )

        foreign_child = FakeProcess(201, name="chrome", cmdline=["chrome", "--type=renderer"])
        foreign_proc = FakeProcess(
            200,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
            create_time=40.0,
            children=[foreign_child],
        )

        fake_psutil = FakePsutil(
            {
                100: launched_proc,
                101: child_1,
                102: child_2,
                200: foreign_proc,
                201: foreign_child,
            }
        )

        cleanup = self._create_cleanup(fake_psutil)

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            cleanup.track_browser_process(
                "inst-1",
                launched_proc,
                user_data_dir=shared_profile,
                uses_custom_data_dir=True,
            )

            result = cleanup.kill_browser_process("inst-1")

        self.assertTrue(result)
        self.assertTrue(launched_proc.terminated)
        self.assertTrue(child_1.terminated)
        self.assertTrue(child_2.terminated)

        self.assertFalse(foreign_proc.terminated)
        self.assertFalse(foreign_proc.killed)
        self.assertTrue(fake_psutil.pid_exists(200))

        self.assertFalse(foreign_child.terminated)
        self.assertFalse(foreign_child.killed)
        self.assertTrue(fake_psutil.pid_exists(201))

    def test_already_gone_pid_returns_true(self):
        fake_psutil = FakePsutil({})
        cleanup = self._create_cleanup(fake_psutil)

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            cleanup.browser_processes["dead-inst"] = {
                "pid": 99999,
                "timestamp": 100.0,
                "user_data_dir": "/tmp/nonexistent",
                "uses_custom_data_dir": True,
            }

            self.assertTrue(cleanup.kill_browser_process("dead-inst"))
            self.assertTrue(
                cleanup._kill_processes_for_metadata("dead-inst", {"pid": 99999})
            )
            self.assertTrue(
                cleanup._kill_processes_for_metadata("none-inst", {"pid": None})
            )
            self.assertTrue(
                cleanup._kill_processes_for_metadata("invalid-inst", {"pid": -1})
            )

    def test_reused_pid_is_not_killed(self):
        proc_reused = FakeProcess(300, name="chrome", create_time=200.0)
        fake_psutil = FakePsutil({300: proc_reused})
        cleanup = self._create_cleanup(fake_psutil)

        metadata = {
            "pid": 300,
            "timestamp": 100.0,
            "user_data_dir": "/tmp/custom",
            "uses_custom_data_dir": True,
        }

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            self.assertTrue(cleanup._kill_processes_for_metadata("reused", metadata))

        self.assertFalse(proc_reused.terminated)
        self.assertFalse(proc_reused.killed)
        self.assertTrue(fake_psutil.pid_exists(300))

    def test_orphan_recovery_kills_orphan_tree_and_foreign_chrome_survives(self):
        shared_profile = str(Path(self.tmp_dir) / "shared_recovery_dir")

        orphan_child = FakeProcess(501, name="chrome", cmdline=["chrome", "--type=renderer"])
        orphan_proc = FakeProcess(
            500,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
            create_time=20.0,
            children=[orphan_child],
        )

        foreign_proc = FakeProcess(
            600,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
            create_time=15.0,
        )

        live_server_browser = FakeProcess(
            700,
            name="chrome",
            cmdline=["chrome", "--user-data-dir=/tmp/other_server"],
            create_time=30.0,
        )

        live_server_owner_pid = 8888
        live_server_proc = FakeProcess(live_server_owner_pid, name="python")

        fake_psutil = FakePsutil(
            {
                500: orphan_proc,
                501: orphan_child,
                600: foreign_proc,
                700: live_server_browser,
                live_server_owner_pid: live_server_proc,
            }
        )

        pids_data = {
            "browser_processes": {
                "orphan-inst": {
                    "pid": 500,
                    "user_data_dir": shared_profile,
                    "uses_custom_data_dir": True,
                    "timestamp": 20.0,
                    "owner_pid": 999999,
                },
                "live-server-inst": {
                    "pid": 700,
                    "user_data_dir": "/tmp/other_server",
                    "uses_custom_data_dir": True,
                    "timestamp": 30.0,
                    "owner_pid": live_server_owner_pid,
                },
            },
            "timestamp": time.time(),
        }

        with open(self.pid_file, "w", encoding="utf-8") as f:
            json.dump(pids_data, f)

        cleanup = self._create_cleanup(fake_psutil)

        self.assertTrue(orphan_proc.terminated)
        self.assertTrue(orphan_child.terminated)

        self.assertFalse(foreign_proc.terminated)
        self.assertFalse(foreign_proc.killed)
        self.assertTrue(fake_psutil.pid_exists(600))

        self.assertFalse(live_server_browser.terminated)
        self.assertTrue(fake_psutil.pid_exists(700))

        with open(self.pid_file, "r", encoding="utf-8") as f:
            saved = json.load(f)

        saved_processes = saved.get("browser_processes", {})
        self.assertNotIn("orphan-inst", saved_processes)
        self.assertIn("live-server-inst", saved_processes)

    def test_orphan_recovery_with_dead_orphan_pid(self):
        pids_data = {
            "browser_processes": {
                "dead-orphan": {
                    "pid": 44444,
                    "user_data_dir": "/tmp/gone_dir",
                    "uses_custom_data_dir": True,
                    "timestamp": 10.0,
                    "owner_pid": 33333,
                }
            },
            "timestamp": time.time(),
        }

        with open(self.pid_file, "w", encoding="utf-8") as f:
            json.dump(pids_data, f)

        fake_psutil = FakePsutil({})
        cleanup = self._create_cleanup(fake_psutil)

        self.assertFalse(self.pid_file.exists())

    def test_finalize_browser_process_succeeds_when_foreign_chrome_on_same_dir(self):
        shared_profile = str(Path(self.tmp_dir) / "shared_finalize_dir")

        foreign_proc = FakeProcess(
            900,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
        )

        fake_psutil = FakePsutil({900: foreign_proc})
        cleanup = self._create_cleanup(fake_psutil)

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            cleanup.browser_processes["inst-fin"] = {
                "pid": 800,
                "user_data_dir": shared_profile,
                "uses_custom_data_dir": True,
                "timestamp": 50.0,
            }

            self.assertTrue(cleanup.finalize_browser_process("inst-fin"))
            self.assertNotIn("inst-fin", cleanup.browser_processes)

        self.assertFalse(foreign_proc.terminated)
        self.assertTrue(fake_psutil.pid_exists(900))

    def test_is_process_alive_not_misled_by_foreign_chrome(self):
        shared_profile = str(Path(self.tmp_dir) / "shared_alive_dir")

        foreign_proc = FakeProcess(
            950,
            name="chrome",
            cmdline=["chrome", f"--user-data-dir={shared_profile}"],
        )

        fake_psutil = FakePsutil({950: foreign_proc})
        cleanup = self._create_cleanup(fake_psutil)

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            cleanup.browser_processes["dead-inst"] = {
                "pid": 850,
                "user_data_dir": shared_profile,
                "timestamp": 50.0,
            }

            self.assertFalse(cleanup.is_process_alive("dead-inst"))

            live_proc = FakeProcess(851, name="chrome", create_time=40.0)
            fake_psutil.add_process(live_proc)
            cleanup.browser_processes["live-inst"] = {
                "pid": 851,
                "user_data_dir": shared_profile,
                "timestamp": 50.0,
            }

            self.assertTrue(cleanup.is_process_alive("live-inst"))

    def test_get_process_tree_pids_variations(self):
        gc = FakeProcess(13, name="chrome")
        c1 = FakeProcess(11, name="chrome", children=[gc])
        c2 = FakeProcess(12, name="chrome")
        parent = FakeProcess(10, name="chrome", children=[c1, c2])

        fake_psutil = FakePsutil({10: parent, 11: c1, 12: c2, 13: gc})

        with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
            tree = ProcessCleanup._get_process_tree_pids(10)
            self.assertEqual(tree, {10, 11, 12, 13})

            single = FakeProcess(20, name="chrome")
            fake_psutil.add_process(single)
            self.assertEqual(ProcessCleanup._get_process_tree_pids(20), {20})

            self.assertEqual(ProcessCleanup._get_process_tree_pids(9999), set())
            self.assertEqual(ProcessCleanup._get_process_tree_pids(0), set())
            self.assertEqual(ProcessCleanup._get_process_tree_pids(-5), set())
            self.assertEqual(ProcessCleanup._get_process_tree_pids(None), set())

    def test_startup_sweep_orphaned_temp_profiles(self):
        temp_dir = Path(self.tmp_dir) / "system_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        old_dir = temp_dir / "uc_stale_12345"
        old_dir.mkdir(parents=True, exist_ok=True)
        fresh_dir = temp_dir / "uc_fresh_67890"
        fresh_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        os.utime(str(old_dir), (now - 30000, now - 30000))
        os.utime(str(fresh_dir), (now - 100, now - 100))

        fake_psutil = FakePsutil({})

        # Instantiating ProcessCleanup runs startup orphan recovery which sweeps stale temp profiles
        with mock.patch("tempfile.gettempdir", return_value=str(temp_dir)):
            cleanup = self._create_cleanup(fake_psutil)

        self.assertFalse(old_dir.exists())
        self.assertTrue(fresh_dir.exists())

        # Calling _sweep_orphaned_temp_profiles directly removes newly stale directories
        stale_dir2 = temp_dir / "uc_stale_67891"
        stale_dir2.mkdir(parents=True, exist_ok=True)
        os.utime(str(stale_dir2), (now - 30000, now - 30000))

        with mock.patch("tempfile.gettempdir", return_value=str(temp_dir)):
            with mock.patch("stealth_browser_mcp.process_cleanup.psutil", fake_psutil):
                removed = cleanup._sweep_orphaned_temp_profiles()

        self.assertEqual(removed, 1)
        self.assertFalse(stale_dir2.exists())
        self.assertTrue(fresh_dir.exists())


if __name__ == "__main__":
    unittest.main()
