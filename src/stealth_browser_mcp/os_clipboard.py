"""Machine-wide OS clipboard helper for trusted paste operations.

Opt-in and guarded by an environment flag so the server never touches the
operator's real clipboard unless it is explicitly enabled. When enabled, a
``clipboard_paste_session`` takes the machine-wide file lock (see
:mod:`file_lock`), saves the current clipboard text, replaces it with the text
to paste, yields so the caller can perform the paste, and then restores the
original text in a ``finally`` block even when the body raises.

Two backends implement a small interface so tests can supply fakes instead of
touching a real clipboard:

* Linux: ``wl-paste`` / ``wl-copy`` on Wayland or ``xclip`` on X11, driven by
  ``subprocess`` with a list argv (no shell).
* Windows: the Win32 clipboard (``OpenClipboard`` / ``GetClipboardData`` /
  ``SetClipboardData`` with ``CF_UNICODETEXT``) via ``ctypes``.

Nothing here reads a browser profile or talks to the network.
"""

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Protocol

from .file_lock import FileLock
from .paths import data_dir, user_data_dir

# Opt-in flag. Clipboard access is disabled unless this is exactly "1".
ENV_FLAG = "STEALTH_ALLOW_OS_CLIPBOARD"

# Lock guarding the clipboard across server processes and chats, all of which
# share the one OS clipboard.
_CLIPBOARD_LOCK_NAME = "clipboard.lock"


class ClipboardDisabledError(Exception):
    """Raised when a clipboard operation runs with the opt-in flag unset.

    The message names the environment variable that must be set to enable it.
    """


class ClipboardBackend(Protocol):
    """Interface a backend must implement to read and write the clipboard."""

    def get_text(self) -> str:
        """Return the current clipboard text, or empty string when empty."""

    def set_text(self, text: str) -> None:
        """Replace the clipboard text with ``text``."""


def _clipboard_lock():
    """Return the machine-wide lock guarding the OS clipboard.

    The lock path is computed at call time so the operation follows
    ``STEALTH_BROWSER_MCP_DATA_DIR`` for tests and for a data dir changed at
    runtime. The parent directory is created if missing so
    :class:`file_lock.FileLock` can open the lock file.
    """
    base = data_dir()
    if not base or str(base).strip() in ("", "."):
        base = user_data_dir()
    base = Path(base).resolve()
    path = (base / "locks" / _CLIPBOARD_LOCK_NAME).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=10.0)


def _flag_enabled() -> bool:
    """Return True only when the opt-in flag is set to exactly "1"."""
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def _is_wl_clip_present() -> bool:
    """Return True when the Wayland clipboard tools are on the PATH."""
    return bool(shutil.which("wl-copy")) and bool(shutil.which("wl-paste"))


def _is_xclip_present() -> bool:
    """Return True when xclip is on the PATH."""
    return bool(shutil.which("xclip"))


def _linux_get_text() -> str:
    """Return the Wayland or X11 clipboard text, or empty string on failure."""
    try:
        if _is_wl_clip_present():
            result = subprocess.run(
                ["wl-paste", "--no-watcher", "-i"],
                check=False,
                capture_output=True,
            )
            return result.stdout.decode("utf-16-le", errors="replace")
        if _is_xclip_present():
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                check=False,
                capture_output=True,
            )
            return result.stdout.decode("utf-16-le", errors="replace")
        return ""
    except (OSError, FileNotFoundError):
        return ""


def _linux_set_text(text: str) -> None:
    """Write ``text`` to the Wayland or X11 clipboard."""
    try:
        if _is_wl_clip_present():
            subprocess.run(
                ["wl-copy", "--no-watcher"],
                input=text.encode("utf-16-le"),
                check=False,
            )
        elif _is_xclip_present():
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-16-le"),
                check=False,
            )
    except (OSError, FileNotFoundError):
        pass


class LinuxClipboardBackend:
    """Read and write the clipboard on Linux via clipboard tools."""

    def get_text(self) -> str:
        return _linux_get_text()

    def set_text(self, text: str) -> None:
        _linux_set_text(text)


class WindowsClipboardBackend:
    """Read and write the clipboard on Windows via the Win32 API."""

    def __init__(self):
        self._windll = ctypes.windll

    def get_text(self) -> str:
        windll = self._windll
        if windll.OpenClipboard(0) == 0:
            return ""
        try:
            handle = windll.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            data = ctypes.cast(handle, ctypes.c_wchar_p)
            return data.value or ""
        finally:
            windll.CloseClipboard()

    def set_text(self, text: str) -> None:
        windll = self._windll
        if windll.OpenClipboard(0) == 0:
            return
        try:
            windll.EmptyClipboard()
            encoded = (text + "\0").encode("utf-16-le")
            handle = windll.GlobalAlloc(2, len(encoded))
            if not handle:
                return
            lock = windll.GlobalLock(handle)
            if lock:
                ctypes.memmove(lock, encoded, len(encoded))
                windll.GlobalUnlock(handle)
                windll.SetClipboardData(13, handle)  # CF_UNICODETEXT
        finally:
            windll.CloseClipboard()


def _get_backend() -> Optional[ClipboardBackend]:
    """Return the platform clipboard backend, or None on Windows without ctypes.

    Called at session start (not import) so tests can patch it.
    """
    if sys.platform == "win32":
        if not hasattr(ctypes, "windll"):
            return None
        return WindowsClipboardBackend()
    if _is_wl_clip_present() or _is_xclip_present():
        return LinuxClipboardBackend()
    return None


class clipboard_paste_session:
    """Context manager that sets the clipboard for the duration of a paste.

    The machine-wide lock is held across the whole set+paste window so no other
    process can read a half-written clipboard. The previous clipboard text is
    saved, the new text is set, the body runs, and the saved text is restored in
    a ``finally`` block even when the body raises.

    Args:
        text: The text to place on the clipboard while the body runs.
        backend: Optional clipboard backend to use instead of the platform default.

    Yields:
        None.

    Raises:
        ClipboardDisabledError: If the opt-in flag is not set to "1".
    """

    def __init__(
        self, text: str, backend: Optional[ClipboardBackend] = None
    ) -> None:
        if not _flag_enabled():
            raise ClipboardDisabledError(
                "OS clipboard access is disabled; set {0}=1 to enable clipboard paste"
                .format(ENV_FLAG)
            )
        self._text = text
        self._lock = None
        self._backend = backend
        self._saved = ""

    def __enter__(self):
        """Take the lock, save the current clipboard, and set the new text."""
        self._lock = _clipboard_lock()
        self._lock.acquire()
        try:
            backend = self._backend or _get_backend()
            if backend is None:
                raise ClipboardDisabledError(
                    "OS clipboard access is unavailable on this platform"
                )
            self._backend = backend
            self._saved = backend.get_text()
            backend.set_text(self._text)
            return self
        except BaseException:
            if self._lock is not None:
                try:
                    self._lock.release()
                finally:
                    self._lock = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        """Restore the saved clipboard text and release the lock."""
        try:
            if self._backend is not None:
                self._backend.set_text(self._saved)
        finally:
            if self._lock is not None:
                try:
                    self._lock.release()
                finally:
                    self._lock = None
        return False
