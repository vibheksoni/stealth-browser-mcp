"""Cross-process file lock shared across server processes.

This lock guards data shared on disk (seed refresh, session count, clipboard
paste) that in-process threading primitives cannot protect. It uses an OS file
lock: ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows. The holder pid
is recorded in a small side file so a lock left behind by a crashed process can
be detected and stolen.

Usage::

    lock = FileLock(path, timeout=10)
    with lock:            # context manager
        do_work()

    lock.acquire()        # or explicit control
    try:
        do_work()
    finally:
        lock.release()

A second process that cannot take the lock waits until ``timeout`` seconds pass,
then raises :class:`LockTimeout`.
"""

import errno
import json
import os
import sys
import time

import psutil

if sys.platform == "win32":
    import msvcrt
    _HAS_FLOCK = False
else:
    import fcntl
    _HAS_FLOCK = True

HOLDER_SUFFIX = ".lock.holder"


class _WouldBlock(OSError):
    """Raised when a non-blocking lock cannot be taken because it is held."""


class LockTimeout(Exception):
    """Raised when a lock cannot be acquired within the allotted timeout."""


def is_pid_alive(pid):
    """
    Return True if a process with ``pid`` is currently running.

    Args:
        pid (int): Process id to check.

    Returns:
        bool: True if the process exists, False otherwise.
    """
    if pid is None or pid <= 0:
        return False
    # ``psutil.pid_exists`` is used instead of ``os.kill(pid, 0)``: on Windows
    # the latter calls ``TerminateProcess`` for every signal other than
    # ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT``, which would kill the target instead
    # of merely probing it.
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return False


class FileLock:
    """A cross-process advisory file lock with holder tracking.

    The lock is an OS-level lock taken on a dedicated lock file next to
    ``path``. A holder-info file records the pid that holds the lock so a stale
    lock (owner process has exited) can be detected and taken over.

    Attributes:
        path (str): Path the caller wants to guard. The lock is taken on a file
            beside it.
        timeout (float): Seconds to wait when the lock is held by another live
            process. ``0`` means try once and give up.
        lock_path (str): The lock file actually locked.
    """

    def __init__(self, path, timeout=10.0):
        """
        Args:
            path (str): Path the caller wants to guard. The lock file is created
                next to it.
            timeout (float): Seconds to wait for the lock. ``0`` tries once.
        """
        self.path = path
        self.timeout = timeout
        self.lock_path = path + ".lock"
        self.info_path = path + HOLDER_SUFFIX
        self._fd = None

    def holder(self):
        """
        Read the recorded holder info for this lock.

        Returns:
            Optional[dict]: The holder info dict, or None if the lock file does
                not exist or no holder file is present.
        """
        try:
            with open(self.lock_path, "rb") as handle:
                handle.read(1)
        except (OSError, IOError):
            return None
        try:
            with open(self.info_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, IOError, ValueError):
            return None

    def holder_alive(self):
        """
        Return True if the recorded holder process is still running.

        A dead holder means the lock is stale and may be stolen.

        Returns:
            bool: True if a holder is recorded and still alive, False otherwise
                (no holder, or the holder pid is dead).
        """
        info = self.holder()
        if not info:
            return False
        return is_pid_alive(info.get("pid"))

    def acquire(self, timeout=None):
        """
        Take the lock, waiting up to ``timeout`` seconds.

        Waits for a live holder to release the lock. A stale holder (dead pid)
        does not block acquisition.

        Args:
            timeout (float): Overrides the value passed to ``__init__``. None
                keeps the instance value. ``0`` tries once and gives up.

        Raises:
            LockTimeout: If the lock cannot be taken within the timeout.

        Returns:
            FileLock: The lock, so ``lock.acquire()`` can be chained.
        """
        if timeout is None:
            timeout = self.timeout
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._try_lock()
            except _WouldBlock:
                if timeout <= 0 or time.monotonic() >= deadline:
                    raise LockTimeout(
                        "Could not acquire lock within {0:g}s".format(timeout))
                time.sleep(0.02)
                continue
            self._record_holder()
            return self

    def _try_lock(self):
        """
        Attempt a single non-blocking lock acquisition.

        Raises:
            _WouldBlock: If the lock is already held by another process.
        """
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        contention = False
        try:
            if _HAS_FLOCK:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if not _HAS_FLOCK:
                contention = True
            elif exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                contention = True
            else:
                raise
        if contention:
            os.close(fd)
            raise _WouldBlock("lock held")
        self._fd = fd

    def _record_holder(self):
        """
        Write holder pid info next to the lock file.

        Raises:
            OSError: If the holder info file cannot be written.
        """
        payload = json.dumps({"pid": os.getpid()})
        tmp = self.info_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.info_path)

    def release(self):
        """
        Release the lock and clear holder info. Safe to call multiple times.
        """
        if self._fd is not None:
            try:
                if _HAS_FLOCK:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                else:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except (IOError, OSError, ValueError):
                pass
            try:
                os.close(self._fd)
            except (IOError, OSError):
                pass
            self._fd = None
        try:
            os.remove(self.info_path)
        except (OSError, IOError):
            pass

    def __enter__(self):
        """
        Enter the runtime context by acquiring the lock.

        Returns:
            FileLock: The lock.
        """
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Leave the runtime context, releasing the lock.

        Args:
            exc_type: Exception type if the context raised, else None.
            exc_value: Exception value if the context raised, else None.
            traceback: Traceback if the context raised, else None.

        Returns:
            bool: Always False, so any exception propagates.
        """
        self.release()
        return False
