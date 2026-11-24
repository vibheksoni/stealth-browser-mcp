"""Seed snapshots and rollback functionality (SB-38).

This module provides snapshot creation, listing, and restoration for seed
directories. Snapshots are stored under:

    <data_dir>/snapshots/<name>/<timestamp>-<label>/

under the per-seed FileLock from :mod:`file_lock`. Restoring a snapshot restores
the exact file set (files not in the snapshot are removed) atomically via a
temporary directory and rename, refusing if live sessions are using the seed.
"""

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import profiles
from .file_lock import FileLock
from .paths import data_dir

_LABEL_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def pid_alive(pid: Optional[int]) -> bool:
    """Return True when a process with ``pid`` currently exists."""
    return profiles.pid_alive(pid)


def snapshots_base_dir() -> Path:
    """Return the base directory where snapshots for all seeds are stored."""
    return data_dir() / "snapshots"


def seed_snapshots_dir(name: str) -> Path:
    """Return the directory containing snapshots for the named seed."""
    profiles._check_name(name, "seed")
    return snapshots_base_dir() / name


def seed_lock(name: str, timeout: float = 10.0) -> FileLock:
    """Return a FileLock guarding the seed directory."""
    profiles._check_name(name, "seed")
    seed_path = profiles.seed_dir(name)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(seed_path), timeout=timeout)


def _check_label(label: Optional[str]) -> Optional[str]:
    """Validate snapshot label."""
    if label is None:
        return None
    if not isinstance(label, str):
        raise profiles.ProfileError("snapshot label must be a string")
    label = label.strip()
    if not label:
        return None
    if not _LABEL_RE.match(label) or label in (".", ".."):
        raise profiles.ProfileError(f"invalid snapshot label: {label}")
    return label


def _publish_atomic(new_dir: str, dest_dir: str) -> None:
    """Atomically swap ``new_dir`` onto ``dest_dir``, removing the old tree."""
    old_away = dest_dir + ".old"
    if os.path.lexists(old_away):
        shutil.rmtree(old_away, ignore_errors=True)
    if os.path.lexists(dest_dir):
        os.replace(dest_dir, old_away)
    os.replace(new_dir, dest_dir)
    if os.path.lexists(old_away):
        shutil.rmtree(old_away, ignore_errors=True)


def list_seed_snapshots(name: str) -> List[Dict[str, Any]]:
    """Return all snapshots for ``name`` sorted from oldest to newest.

    Args:
        name: Seed name.

    Returns:
        List of dicts describing each snapshot.
    """
    profiles._check_name(name, "seed")
    root = seed_snapshots_dir(name)
    if not root.is_dir():
        return []

    results = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        parts = entry.name.split("-", 1)
        created = None
        label = None
        if len(parts) == 2:
            try:
                created = float(parts[0])
            except ValueError:
                pass
            label = parts[1]
        else:
            try:
                created = float(parts[0])
            except ValueError:
                pass

        if created is None:
            try:
                created = entry.stat().st_mtime
            except OSError:
                created = 0.0

        results.append({
            "snapshot_id": entry.name,
            "id": entry.name,
            "name": name,
            "seed": name,
            "label": label,
            "created": created,
            "path": str(entry),
        })

    results.sort(key=lambda s: (s["created"], s["snapshot_id"]))
    return results


def apply_retention(name: str, max_snapshots: Optional[int]) -> List[str]:
    """Delete oldest snapshots if count exceeds max_snapshots.

    Args:
        name: Seed name.
        max_snapshots: Maximum snapshots to retain. If None or <= 0, no pruning occurs.

    Returns:
        List of deleted snapshot IDs.
    """
    if max_snapshots is None or max_snapshots <= 0:
        return []

    snaps = list_seed_snapshots(name)
    if len(snaps) <= max_snapshots:
        return []

    excess = len(snaps) - max_snapshots
    to_delete = snaps[:excess]
    deleted = []
    for item in to_delete:
        p = Path(item["path"])
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            deleted.append(item["snapshot_id"])
    return deleted


def snapshot_seed(
    name: str,
    label: Optional[str] = None,
    max_snapshots: Optional[int] = 5,
    *,
    lock_timeout: float = 10.0,
    now: Optional[Union[int, float]] = None,
    timestamp: Optional[Union[int, float, str]] = None,
) -> Dict[str, Any]:
    """Take a snapshot of a seed directory.

    Args:
        name: Validated seed name.
        label: Optional label for the snapshot.
        max_snapshots: Retention limit. Keeps last N snapshots (default: 5).
        lock_timeout: Seconds to wait for seed lock.
        now: Optional numeric timestamp override for testing.
        timestamp: Optional timestamp string or number override for testing.

    Returns:
        Dict describing the created snapshot.

    Raises:
        ProfileError: If the seed does not exist or label is invalid.
    """
    profiles._check_name(name, "seed")
    if not profiles.seed_exists(name):
        raise profiles.ProfileError(f"seed not found: {name}")

    clean_label = _check_label(label)

    if timestamp is not None:
        ts_str = str(timestamp)
        try:
            created_val = float(timestamp)
        except ValueError:
            created_val = time.time()
    elif now is not None:
        ts_str = str(int(now))
        created_val = float(now)
    else:
        created_val = time.time()
        ts_str = str(int(created_val))

    if clean_label:
        snapshot_id = f"{ts_str}-{clean_label}"
    else:
        snapshot_id = ts_str

    target_snapshots_dir = seed_snapshots_dir(name)
    target_dir = target_snapshots_dir / snapshot_id

    if target_dir.exists() and now is None and timestamp is None:
        counter = 1
        while target_dir.exists():
            if clean_label:
                snapshot_id = f"{ts_str}_{counter}-{clean_label}"
            else:
                snapshot_id = f"{ts_str}_{counter}"
            target_dir = target_snapshots_dir / snapshot_id
            counter += 1

    lock = seed_lock(name, timeout=lock_timeout)
    with lock:
        src = profiles.seed_dir(name)
        if not src.is_dir():
            raise profiles.ProfileError(f"seed directory not found: {name}")

        target_snapshots_dir.mkdir(parents=True, exist_ok=True)
        tmp_target = target_snapshots_dir / f".tmp_{snapshot_id}"
        if tmp_target.exists():
            shutil.rmtree(tmp_target, ignore_errors=True)

        try:
            shutil.copytree(str(src), str(tmp_target))
            _publish_atomic(str(tmp_target), str(target_dir))
        finally:
            if tmp_target.exists():
                shutil.rmtree(tmp_target, ignore_errors=True)

        deleted = apply_retention(name, max_snapshots)

    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "id": snapshot_id,
        "name": name,
        "seed": name,
        "label": clean_label,
        "path": str(target_dir),
        "created": created_val,
        "deleted_by_retention": deleted,
    }


def restore_seed_snapshot(
    name: str,
    snapshot_id: str,
    *,
    lock_timeout: float = 10.0,
) -> Dict[str, Any]:
    """Restore a seed directory from a snapshot.

    Restores the exact file set under the per-seed lock and refuses if live
    sessions use the seed.

    Args:
        name: Validated seed name.
        snapshot_id: Identifier of the snapshot to restore.
        lock_timeout: Seconds to wait for seed lock.

    Returns:
        Dict describing the restoration outcome.

    Raises:
        ProfileError: If the seed or snapshot is missing, or sessions are live.
    """
    profiles._check_name(name, "seed")
    if not isinstance(snapshot_id, str) or not snapshot_id or ".." in snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
        raise profiles.ProfileError(f"invalid snapshot id: {snapshot_id}")

    snapshot_path = seed_snapshots_dir(name) / snapshot_id
    if not snapshot_path.is_dir():
        raise profiles.ProfileError(f"snapshot not found: {snapshot_id}")

    _check_live_sessions(name)

    lock = seed_lock(name, timeout=lock_timeout)
    with lock:
        _check_live_sessions(name)

        if not snapshot_path.is_dir():
            raise profiles.ProfileError(f"snapshot not found: {snapshot_id}")

        dest = profiles.seed_dir(name)
        dest.parent.mkdir(parents=True, exist_ok=True)

        new_dir = str(dest) + ".new"
        if os.path.exists(new_dir):
            shutil.rmtree(new_dir, ignore_errors=True)

        try:
            shutil.copytree(str(snapshot_path), new_dir)
            _publish_atomic(new_dir, str(dest))
        finally:
            if os.path.exists(new_dir):
                shutil.rmtree(new_dir, ignore_errors=True)

    return {
        "status": "ok",
        "name": name,
        "seed": name,
        "snapshot_id": snapshot_id,
        "id": snapshot_id,
        "restored": True,
        "path": str(dest),
    }


def _check_live_sessions(name: str) -> None:
    live = []
    for session in profiles.sessions_for_seed(name):
        pid = session.get("pid")
        browser_pid = session.get("browser_pid")
        if pid_alive(pid) or pid_alive(browser_pid):
            live.append(session)
    if live:
        pids = sorted(
            set(
                int(p)
                for session in live
                for p in (session.get("pid"), session.get("browser_pid"))
                if p is not None and pid_alive(p)
            )
        )
        raise profiles.ProfileError(
            f"seed {name} is in use: {len(live)} session(s) still live (pid(s) {pids}); close them before restoring"
        )


def delete_seed_snapshot(
    name: str,
    snapshot_id: str,
    *,
    lock_timeout: float = 10.0,
) -> bool:
    """Delete a single snapshot directory.

    Args:
        name: Validated seed name.
        snapshot_id: Identifier of the snapshot to delete.
        lock_timeout: Seconds to wait for seed lock.

    Returns:
        True if the snapshot existed and was deleted, False otherwise.
    """
    profiles._check_name(name, "seed")
    if not isinstance(snapshot_id, str) or not snapshot_id or ".." in snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
        return False

    snapshot_path = seed_snapshots_dir(name) / snapshot_id
    if not snapshot_path.is_dir():
        return False

    lock = seed_lock(name, timeout=lock_timeout)
    with lock:
        if snapshot_path.is_dir():
            shutil.rmtree(snapshot_path, ignore_errors=True)
            return True
    return False


def snapshot_exists(name: str, snapshot_id: str) -> bool:
    """Return True when a snapshot directory exists for ``snapshot_id``."""
    profiles._check_name(name, "seed")
    if not isinstance(snapshot_id, str) or not snapshot_id or ".." in snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
        return False
    return (seed_snapshots_dir(name) / snapshot_id).is_dir()
