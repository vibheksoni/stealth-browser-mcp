import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import file_lock

SRC = str(ROOT / "src")

HOLDER_SCRIPT = '''
import os
import sys
import time

sys.path.insert(0, {src!r})
from stealth_browser_mcp import file_lock

path, die_file, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
lock = file_lock.FileLock(path, timeout=1.0)
lock.acquire()
with open(pid_file, "w") as handle:
    handle.write(str(os.getpid()))
while not os.path.exists(die_file):
    time.sleep(0.05)
lock.release()
sys.exit(0)
'''

WAITER_SCRIPT = '''
import sys

sys.path.insert(0, {src!r})
from stealth_browser_mcp import file_lock

path, timeout = sys.argv[1], float(sys.argv[2])
lock = file_lock.FileLock(path, timeout=timeout)
try:
    lock.acquire()
except file_lock.LockTimeout:
    sys.exit(3)
sys.exit(0)
'''


def _spawn(args):
    """Run a python subprocess with the source tree importable."""
    return subprocess.call(
        [sys.executable, "-c", args],
        cwd=str(ROOT),
    )


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "resource")

    @staticmethod
    def _terminate(proc):
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def test_acquire_release_roundtrip(self):
        lock = file_lock.FileLock(self.path, timeout=1.0)
        self.assertIs(lock.acquire(), lock)
        self.assertTrue(lock.holder_alive())
        lock.release()
        # After release the holder is gone and the lock is acquirable again.
        self.assertFalse(lock.holder_alive())
        self.assertTrue(lock.acquire(timeout=1.0))
        lock.release()

    def test_holder_info_recorded_and_cleared(self):
        lock = file_lock.FileLock(self.path, timeout=1.0)
        lock.acquire()
        info = lock.holder()
        self.assertIsNotNone(info)
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(lock.holder_alive())
        # Another instance can read holder info while lock is held
        other = file_lock.FileLock(self.path, timeout=0.1)
        other_info = other.holder()
        self.assertIsNotNone(other_info)
        self.assertEqual(other_info["pid"], os.getpid())
        lock.release()
        self.assertIsNone(lock.holder())

    def test_holder_none_without_lock_file(self):
        lock = file_lock.FileLock(self.path, timeout=1.0)
        self.assertIsNone(lock.holder())
        self.assertFalse(lock.holder_alive())

    def test_context_manager(self):
        lock = file_lock.FileLock(self.path, timeout=1.0)
        with lock:
            self.assertTrue(lock._fd is not None)
            self.assertEqual(lock.holder()["pid"], os.getpid())
        self.assertEqual(lock._fd, None)
        self.assertIsNone(lock.holder())

    def test_is_pid_alive(self):
        self.assertFalse(file_lock.is_pid_alive(0))
        self.assertFalse(file_lock.is_pid_alive(-1))
        self.assertFalse(file_lock.is_pid_alive(None))
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"])
        self.addCleanup(proc.wait)
        self.addCleanup(self._terminate, proc)
        time.sleep(0.2)
        self.assertTrue(file_lock.is_pid_alive(proc.pid))

    @unittest.skipUnless(os.name == "posix", "fork is POSIX only")
    def test_is_pid_alive_dead(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        self.assertFalse(file_lock.is_pid_alive(pid))


class TwoSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = Path(self.directory.name)
        self.path = str(self.dir / "resource")
        self.pid_file = str(self.dir / "holder.pid")
        self.die_file = str(self.dir / "holder.die")

    def _holder(self):
        code = HOLDER_SCRIPT.format(src=SRC)
        self.holder_proc = subprocess.Popen(
            [sys.executable, "-c", code, self.path, self.die_file, self.pid_file],
            cwd=str(ROOT),
        )
        self.addCleanup(self._kill_holder)
        return self.holder_proc

    def _kill_holder(self):
        proc = getattr(self, "holder_proc", None)
        if proc is None:
            return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _waiter(self, timeout):
        code = WAITER_SCRIPT.format(src=SRC)
        return subprocess.call(
            [sys.executable, "-c", code, self.path, str(timeout)],
            cwd=str(ROOT),
        )

    def _wait_for_pid(self, wait=5.0):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if os.path.exists(self.pid_file):
                with open(self.pid_file) as handle:
                    return int(handle.read())
            time.sleep(0.02)
        self.fail("holder did not report its pid in time")

    def test_second_process_times_out_then_becomes_acquirable(self):
        self._holder()
        holder_pid = self._wait_for_pid()
        lock = file_lock.FileLock(self.path, timeout=1.0)
        self.assertEqual(lock.holder()["pid"], holder_pid)
        self.assertTrue(lock.holder_alive())

        start = time.monotonic()
        rc = self._waiter(timeout=0.6)
        elapsed = time.monotonic() - start
        self.assertEqual(rc, 3, "waiter should have timed out")
        self.assertGreaterEqual(elapsed, 0.5)

        # Signal the holder to release, then confirm a new waiter can acquire.
        open(self.die_file, "a").close()
        try:
            self.holder_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.holder_proc.kill()
            self.holder_proc.wait()
        time.sleep(0.2)
        self.assertEqual(
            self._waiter(timeout=1.5), 0, "should acquire after release")

    def test_dead_holder_is_acquirable_and_stale(self):
        self._holder()
        holder_pid = self._wait_for_pid()
        self.holder_proc.terminate()
        try:
            self.holder_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.holder_proc.kill()
            self.holder_proc.wait()

        lock = file_lock.FileLock(self.path, timeout=2.0)
        self.assertFalse(lock.holder_alive())

        start = time.monotonic()
        rc = self._waiter(timeout=2.0)
        elapsed = time.monotonic() - start
        self.assertEqual(rc, 0, "waiter should acquire a stale lock")
        self.assertLess(elapsed, 1.0, "stale acquisition should be fast")


if __name__ == "__main__":
    unittest.main()
