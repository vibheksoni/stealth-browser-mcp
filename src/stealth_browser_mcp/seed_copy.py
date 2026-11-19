"""Seed copy engine.

This module copies a curated seed tree (see :mod:`seed_manifest`) from a source
profile directory into a fresh destination directory. The destination is a
complete, self-contained seed that a clone can launch from.

Design goals, from PLAN section 2 and SB-44:

- The source is NEVER modified. SQLite stores are copied through the SQLite
  backup API against a read-only URI connection, so the copy is a consistent
  snapshot even while Chrome keeps writing. Plain files and whole directories
  (LevelDB, IndexedDB, Extensions) are copied read-only.
- The publish is atomic. The tree is built under ``<dest>.new`` on the same
  filesystem and then ``os.replace``d onto the final path. A crash never leaves a
  half seed; the previous seed stays usable until the switch.
- Locked source files are skipped, not fatal. On Windows a running Chrome holds
  a share lock on some files; a ``PermissionError`` there is a sharing violation
  (``winerror 32``). We record such files in ``skipped_busy`` and keep going. If
  the seed destination lock cannot be taken, or the primary cookie store is busy,
  we report ``source_busy`` and leave the old seed on disk intact.

No ``print`` calls exist here: this runs under the MCP stdio transport.
"""

import errno
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List

from .file_lock import FileLock

SQLITE_TIMEOUT = 1.0


def _default_sqlite_opener(uri, **kwargs):
    kwargs.setdefault("timeout", SQLITE_TIMEOUT)
    return sqlite3.connect(uri, uri=True, **kwargs)


_SQLITE_SRC_OPENER = _default_sqlite_opener

# SQLite writes a 16-byte magic header "SQLite format 3\000".
SQLITE_MAGIC = b"SQLite format 3\x00"

# A busy file is a sharing violation: POSIX EACCES/EPERM/EAGAIN or Windows
# sharing violation winerror 32.
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EAGAIN})
_WIN_SHARING_VIOLATION = 32


def _is_sqlite_file(path):
    """Return True if ``path`` begins with the sqlite magic header."""
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except (PermissionError, OSError) as exc:
        if _busy_error(exc):
            raise
        return False


def _busy_error(exc):
    """Return True if ``exc`` looks like a locked source (sharing violation)."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == _WIN_SHARING_VIOLATION:
            return True
        if exc.errno in _BUSY_ERRNOS:
            return True
    if isinstance(exc, sqlite3.Error):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return True
    msg = str(exc).lower()
    if "database is locked" in msg or "database is busy" in msg:
        return True
    if "sharing violation" in msg:
        return True
    return False


def _is_primary_store(rel):
    """Return True if rel is the primary cookie store."""
    norm = rel.replace("\\", "/")
    return norm == "Cookies" or norm == "Network/Cookies" or norm.endswith("/Cookies")


@dataclass
class CopyResult:
    """Outcome of a :func:`copy_seed`.

    ``status`` is ``ok`` when the copy completed, or ``source_busy`` when the
    seed could not be produced and the previous seed must be kept. ``copied`` and
    ``skipped_busy`` list relative paths (forward slashes). ``errors`` lists
    messages for failures that were not a locked file. ``bytes`` is the total
    size of successfully copied files.
    """

    status: str = "ok"
    copied: List[str] = field(default_factory=list)
    skipped_busy: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    bytes: int = 0


class SeedBusy(Exception):
    """Raised when the seed copy cannot be produced because the source is busy."""


def _uri_quote(path):
    """Percent-quote a path for a ``file:`` URI (no external-quote chars)."""
    norm = os.path.abspath(path).replace(os.sep, "/")
    out = []
    for ch in norm:
        if ch in ":?#&=%+ \t\n":
            out.append("%{:02X}".format(ord(ch)))
        else:
            out.append(ch)
    return "".join(out)


def _copy_sqlite_file(src, dst):
    """Copy a single sqlite database from ``src`` to ``dst`` via the backup API."""
    dest_dir = os.path.dirname(dst) or "."
    if dest_dir and not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    src_uri = "file:{}?mode=ro".format(_uri_quote(src))
    dest_con = None
    src_con = None
    timeout = max(0.0, float(SQLITE_TIMEOUT))
    try:
        dest_con = sqlite3.connect(dst, timeout=timeout)
        try:
            src_con = _SQLITE_SRC_OPENER(src_uri, check_same_thread=False, timeout=timeout)
        except TypeError:
            src_con = _SQLITE_SRC_OPENER(src_uri, check_same_thread=False)

        cursor = src_con.cursor()
        try:
            cursor.execute("PRAGMA schema_version")
            cursor.fetchone()
        finally:
            cursor.close()

        start_time = time.monotonic()
        last_progress_time = start_time
        last_remaining = None

        def _backup_progress(status, remaining, total):
            nonlocal last_progress_time, last_remaining
            now = time.monotonic()
            if status in (5, 6):  # SQLITE_BUSY (5), SQLITE_LOCKED (6)
                if timeout <= 0.0 or (now - last_progress_time) >= timeout:
                    raise sqlite3.OperationalError("database is locked")
                return

            if last_remaining is None or remaining < last_remaining:
                last_remaining = remaining
                last_progress_time = now
            elif timeout <= 0.0:
                if remaining > 0 and (last_remaining is not None and remaining == last_remaining):
                    raise sqlite3.OperationalError("database is locked")
            elif (now - last_progress_time) >= timeout:
                if remaining > 0:
                    raise sqlite3.OperationalError("database is locked")

        sleep_interval = min(0.05, max(0.005, timeout)) if timeout > 0 else 0.01
        src_con.backup(dest_con, pages=-1, progress=_backup_progress, sleep=sleep_interval)
    except Exception:
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        raise
    finally:
        if dest_con is not None:
            try:
                dest_con.close()
            except sqlite3.Error:
                pass
        if src_con is not None:
            try:
                src_con.close()
            except sqlite3.Error:
                pass


def _copy_regular(src, dst):
    """Copy a single file with metadata; try a reflink first when available."""
    dest_dir = os.path.dirname(dst) or "."
    if dest_dir and not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    if _try_reflink(src, dst):
        return
    shutil.copy2(src, dst)


def _try_reflink(src, dst):
    """Best-effort reflink copy; return True on success, False on any failure."""
    if sys.platform != "linux":
        return False
    try:
        proc = subprocess.run(
            ["cp", "--reflink=auto", "--", src, dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        return False
    return os.path.exists(dst)


def copy_seed(plan, source_dir, dest_dir):
    """Copy the entries in ``plan`` from ``source_dir`` into ``dest_dir``.

    The copy runs under the destination lock so two refreshes cannot interleave.
    SQLite stores are copied through the backup API against a read-only source
    connection; directories and plain files are copied file by file, preferring a
    reflink on Linux. Locked source files are recorded in ``skipped_busy`` and
    skipped (best effort); the copy continues.

    The destination tree is built atomically: everything is written under
    ``<dest>.new`` on the same filesystem and then ``os.replace``d onto
    ``dest_dir``. If the destination lock cannot be taken, or the primary cookie
    store cannot be read, the previous seed is left untouched and the result
    status is ``source_busy``.
    """
    dest_parent = os.path.dirname(dest_dir) or "."
    if dest_parent and not os.path.isdir(dest_parent):
        os.makedirs(dest_parent, exist_ok=True)

    lock = FileLock(dest_dir, timeout=10)
    try:
        lock.acquire(timeout=10)
    except Exception as exc:  # noqa: BLE001 - lock held elsewhere -> source busy
        result = CopyResult(status="source_busy")
        result.errors.append("could not lock destination: {}".format(exc))
        return result

    new_dir = dest_dir + ".new"
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir, ignore_errors=True)
    os.makedirs(new_dir, exist_ok=True)

    result = CopyResult(status="ok")
    try:
        _copy_tree_into(plan, source_dir, new_dir, result)
        _publish_atomic(new_dir, dest_dir)
    except SeedBusy as exc:
        result.status = "source_busy"
        result.errors.append(str(exc))
        result.copied.clear()
        result.bytes = 0
        shutil.rmtree(new_dir, ignore_errors=True)
    finally:
        lock.release()

    return result


def _copy_tree_into(plan, source_dir, new_dir, result):
    """Populate ``new_dir`` with the entries selected by ``plan``."""
    for entry in plan.included:
        if entry.nested:
            for sub in entry.nested:
                if sub.included:
                    _copy_entry(sub.rel_path, source_dir, new_dir, result)
        else:
            _copy_entry(entry.rel_path, source_dir, new_dir, result)


def _copy_entry(rel, source_dir, new_dir, result):
    """Copy a single plan entry (file or directory) into new_dir."""
    src_path = os.path.join(source_dir, rel)
    if not os.path.exists(src_path):
        return
    if os.path.isdir(src_path):
        _copy_dir(src_path, source_dir, new_dir, result)
    else:
        _copy_one(src_path, source_dir, new_dir, rel, result)


def _copy_dir(src_dir, source_dir, new_dir, result):
    """Copy a directory tree file by file into new_dir, recording busy files."""
    rel_dir = os.path.relpath(src_dir, source_dir).replace(os.sep, "/")
    dst_dir = os.path.join(new_dir, rel_dir)
    os.makedirs(dst_dir, exist_ok=True)

    for dirpath, _dirnames, filenames in os.walk(src_dir):
        for name in sorted(filenames):
            abs_src = os.path.join(dirpath, name)
            rel_file = os.path.relpath(abs_src, source_dir).replace(os.sep, "/")
            _copy_one(abs_src, source_dir, new_dir, rel_file, result)


def _copy_one(abs_src, source_dir, new_dir, rel, result):
    """Copy a single file, routing SQLite through the backup API."""
    abs_dst = os.path.join(new_dir, rel)
    dest_dir = os.path.dirname(abs_dst) or "."
    if dest_dir and not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    try:
        is_sqlite = _is_sqlite_file(abs_src)
    except (PermissionError, OSError, sqlite3.Error) as exc:
        if _busy_error(exc):
            if _is_primary_store(rel):
                raise SeedBusy("{} store is busy: {}".format(rel, abs_src))
            result.skipped_busy.append(rel)
            return
        result.errors.append("cannot read {}: {}".format(rel, exc))
        return

    if is_sqlite:
        _safe_sqlite(abs_src, abs_dst, rel, result)
    else:
        _safe_regular(abs_src, abs_dst, rel, result)


def _safe_sqlite(src, dst, rel, result):
    """Copy one sqlite file, recording busy/errors and reporting the seed busy."""
    try:
        _copy_sqlite_file(src, dst)
    except (PermissionError, OSError, sqlite3.Error) as exc:
        if _busy_error(exc):
            if _is_primary_store(rel):
                raise SeedBusy("{} store is busy: {}".format(rel, src))
            result.skipped_busy.append(rel)
            return
        result.errors.append("sqlite copy failed for {}: {}".format(rel, exc))
        return
    _record(result, rel, dst)


def _safe_regular(src, dst, rel, result):
    """Copy one plain file, recording busy/errors."""
    try:
        _copy_regular(src, dst)
    except (PermissionError, OSError) as exc:
        if _busy_error(exc):
            if _is_primary_store(rel):
                raise SeedBusy("{} store is busy: {}".format(rel, src))
            result.skipped_busy.append(rel)
            return
        result.errors.append("copy failed for {}: {}".format(rel, exc))
        return
    _record(result, rel, dst)


def _record(result, rel, dst):
    """Add a copied file to the result tallies."""
    result.copied.append(rel)
    try:
        result.bytes += os.path.getsize(dst)
    except OSError:
        pass


def _publish_atomic(new_dir, dest_dir):
    """Atomically swap ``new_dir`` onto ``dest_dir``, removing the old tree."""
    old_away = dest_dir + ".old"
    if os.path.lexists(old_away):
        shutil.rmtree(old_away, ignore_errors=True)
    if os.path.lexists(dest_dir):
        os.replace(dest_dir, old_away)
    os.replace(new_dir, dest_dir)
    if os.path.lexists(old_away):
        shutil.rmtree(old_away, ignore_errors=True)
