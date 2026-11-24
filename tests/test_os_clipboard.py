import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import os_clipboard
from stealth_browser_mcp.os_clipboard import ClipboardDisabledError
from stealth_browser_mcp.paths import DATA_DIR_ENV


class RecordingLock:
    """A lock fake that records acquire and release calls in order."""

    def __init__(self):
        self.calls = []
        self.held = False

    def acquire(self, timeout=None):
        self.calls.append("acquire")
        self.held = True
        return self

    def release(self):
        self.calls.append("release")
        self.held = False
        return self


class RecordingBackend:
    """A backend fake that records the current and requested text."""

    def __init__(self, current=""):
        self.current = current
        self.sets = []
        self.gets = 0

    def get_text(self):
        self.gets += 1
        return self.current

    def set_text(self, text):
        self.sets.append(text)
        self.current = text


def _fake_backend_factory(backend):
    def _factory():
        return backend
    return _factory


class FlagTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name
        os.environ.pop(os_clipboard.ENV_FLAG, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_flag_off_raises(self):
        os.environ.pop(os_clipboard.ENV_FLAG, None)
        with self.assertRaises(ClipboardDisabledError) as ctx:
            with os_clipboard.clipboard_paste_session("x"):
                pass
        self.assertIn(os_clipboard.ENV_FLAG, str(ctx.exception))

    def test_flag_wrong_value_raises(self):
        os.environ[os_clipboard.ENV_FLAG] = "0"
        with self.assertRaises(ClipboardDisabledError):
            with os_clipboard.clipboard_paste_session("x"):
                pass

    def test_flag_enabled_ok(self):
        os.environ[os_clipboard.ENV_FLAG] = "1"
        backend = RecordingBackend()
        with unittest.mock.patch.object(os_clipboard, "_get_backend",
                                        _fake_backend_factory(backend)):
            with os_clipboard.clipboard_paste_session("paste", backend=backend):
                pass
        self.assertIn("paste", backend.sets)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[os_clipboard.ENV_FLAG] = "1"
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _run_with(self, backend):
        with unittest.mock.patch.object(
                os_clipboard, "_get_backend", _fake_backend_factory(backend)):
            with os_clipboard.clipboard_paste_session("new"):
                yield backend

    def test_linux_restores_saved_text(self):
        for expected in ("original-", ""):
            with self.subTest(current=expected):
                backend = RecordingBackend(current=expected)
                list(self._run_with(backend))
                self.assertEqual(backend.current, expected)
                self.assertIn(expected, backend.sets)
                self.assertEqual(backend.sets[-1], expected)

    def test_restore_on_exception(self):
        backend = RecordingBackend(current="keepme")
        with self.assertRaises(RuntimeError):
            with unittest.mock.patch.object(
                    os_clipboard, "_get_backend",
                    _fake_backend_factory(backend)):
                with os_clipboard.clipboard_paste_session("temp"):
                    raise RuntimeError("boom")
        self.assertEqual(backend.current, "keepme")
        self.assertEqual(backend.sets[-1], "keepme")

    def test_windows_backend_shape(self):
        # The Windows backend only exists behind ctypes.windll; exercise the
        # factory path indirectly: a Linux test with a fake is sufficient to
        # prove the restore contract. Here we just confirm the class exists.
        self.assertTrue(hasattr(os_clipboard, "WindowsClipboardBackend"))
        self.assertTrue(hasattr(os_clipboard, "LinuxClipboardBackend"))


class LockTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[os_clipboard.ENV_FLAG] = "1"
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_lock_held_across_set_and_restore(self):
        lock = RecordingLock()
        backend = RecordingBackend(current="orig")
        seen = []
        with unittest.mock.patch.object(os_clipboard, "_clipboard_lock",
                                        lambda: lock):
            with unittest.mock.patch.object(
                    os_clipboard, "_get_backend", _fake_backend_factory(backend)):
                with os_clipboard.clipboard_paste_session("paste"):
                    # Lock is held during the paste window.
                    self.assertTrue(lock.held)
                    self.assertEqual(backend.current, "paste")
                    seen.append(lock.calls)
        # Lock released after the body returns.
        self.assertEqual(lock.calls, ["acquire", "release"])
        self.assertFalse(lock.held)
        self.assertEqual(backend.sets[-1], "orig")

    def test_lock_released_even_on_exception(self):
        lock = RecordingLock()
        with unittest.mock.patch.object(os_clipboard, "_clipboard_lock",
                                        lambda: lock):
            with unittest.mock.patch.object(
                    os_clipboard, "_get_backend",
                    _fake_backend_factory(RecordingBackend("x"))):
                with self.assertRaises(RuntimeError):
                    with os_clipboard.clipboard_paste_session("p"):
                        raise RuntimeError("boom")
        self.assertEqual(lock.calls, ["acquire", "release"])
        self.assertFalse(lock.held)

    def test_lock_released_when_backend_get_or_set_raises(self):
        class BrokenBackend:
            def get_text(self):
                raise RuntimeError("clipboard read failed")

            def set_text(self, text):
                raise RuntimeError("clipboard write failed")

        lock = RecordingLock()
        with unittest.mock.patch.object(os_clipboard, "_clipboard_lock", lambda: lock):
            with unittest.mock.patch.object(
                os_clipboard, "_get_backend", _fake_backend_factory(BrokenBackend())
            ):
                with self.assertRaises(RuntimeError):
                    with os_clipboard.clipboard_paste_session("p"):
                        pass
        self.assertEqual(lock.calls, ["acquire", "release"])
        self.assertFalse(lock.held)

    def test_lock_released_when_backend_none(self):
        lock = RecordingLock()
        with unittest.mock.patch.object(os_clipboard, "_clipboard_lock", lambda: lock):
            with unittest.mock.patch.object(os_clipboard, "_get_backend", lambda: None):
                with self.assertRaises(ClipboardDisabledError):
                    with os_clipboard.clipboard_paste_session("p"):
                        pass
        self.assertEqual(lock.calls, ["acquire", "release"])
        self.assertFalse(lock.held)

    def test_clipboard_lock_path_is_always_absolute(self):
        """Clipboard lock path is always absolute and never relative even if data dir is empty."""
        for val in ("", "   "):
            os.environ[DATA_DIR_ENV] = val
            lock = os_clipboard._clipboard_lock()
            self.assertTrue(Path(lock.path).is_absolute())
            self.assertFalse(str(lock.path).startswith("locks"))


if __name__ == "__main__":
    unittest.main()
