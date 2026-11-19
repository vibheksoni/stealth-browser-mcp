"""Seed and session registry for profile based browser launches.

A seed is a curated copy of a source browser profile. Each chat clones a seed
into its own session directory and launches a separate Chrome process from it.
Seeds live under ``seeds/`` and sessions under ``sessions/`` inside the shared
data directory so unrelated processes can inspect and clean them up.

Only local files and the current machine are touched. No real profile is ever
read here, and nothing is written back to a source profile.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from .paths import data_dir

# Seed names must stay safe as directory components: short, no separators, no
# traversal. The session id is a random token, but is validated the same way so
# a corrupt id cannot escape the sessions directory.
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_SESSION_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

SEED_FILE = "seed.json"
SESSION_FILE = "session.json"


class ProfileError(Exception):
    """Raised when a seed or session operation cannot be completed."""


def _check_name(name, kind):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ProfileError(
            "invalid {} name: use 1-40 characters from [a-z0-9_-] (not '..', '/', or empty)".format(
                kind
            )
        )
    return name


def check_session_id(session_id):
    if not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
        raise ProfileError(
            "invalid session id: use 1-64 characters from [a-z0-9_-]"
        )
    return session_id


def generate_session_id() -> str:
    """Return a fresh random session id."""
    return os.urandom(16).hex()


def seeds_dir() -> Path:
    """Directory that holds all seed directories."""
    return data_dir() / "seeds"


def sessions_dir() -> Path:
    """Directory that holds all session directories."""
    return data_dir() / "sessions"


def seed_dir(name) -> Path:
    """Absolute path of a seed directory."""
    _check_name(name, "seed")
    return seeds_dir() / name


def session_dir(session_id) -> Path:
    """Absolute path of a session directory."""
    check_session_id(session_id)
    return sessions_dir() / session_id


def seed_path(name) -> Path:
    """Absolute path of a seed's ``seed.json``."""
    return seed_dir(name) / SEED_FILE


def session_path(session_id) -> Path:
    """Absolute path of a session's ``session.json``."""
    return session_dir(session_id) / SESSION_FILE


def pid_alive(pid: Optional[int]) -> bool:
    """Return True when a process with ``pid`` currently exists.

    A ``None`` or non-positive pid is never alive. A pid that has become a
    zombie (exited but not yet reaped) is treated as dead.

    ``psutil.pid_exists`` is used instead of ``os.kill(pid, 0)``: on Windows the
    latter calls ``TerminateProcess`` for every signal other than
    ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT``, which would kill the target instead
    of merely probing it.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def seed_defaults(source_browser, source_dir, profile_subdir, password_store=None, defaults=None, now=None) -> Dict[str, Any]:
    """Build the ``seed.json`` payload for a new seed.

    Args:
        source_browser: Browser family the seed came from (e.g. "chrome").
        source_dir: Absolute path of the source user-data-dir that was read.
        profile_subdir: Profile folder inside the source, passed as
            ``--profile-directory`` when launching a clone.
        password_store: Password store backend for the clone, or None for the
            default.
        defaults: Spawn defaults merged with explicit args at launch time.
        now: Optional ``refreshed_at`` timestamp; defaults to the current time.

    Returns:
        A dict ready to persist as ``seed.json``.
    """
    return {
        "source_browser": source_browser,
        "source_dir": source_dir,
        "profile_subdir": profile_subdir,
        "password_store": password_store,
        "defaults": dict(defaults or {}),
        "refreshed_at": now if now is not None else time.time(),
    }


def create_seed(name, source_browser, source_dir, profile_subdir, password_store=None, defaults=None, now=None) -> Dict[str, Any]:
    """Create a seed directory and write its ``seed.json``.

    Args:
        name: Validated seed name.
        source_browser, source_dir, profile_subdir: See :func:`seed_defaults`.
        password_store: Optional password store backend.
        defaults: Optional spawn defaults dict.
        now: Optional timestamp for ``refreshed_at``.

    Returns:
        The seed payload that was written.

    Raises:
        ProfileError: If the seed already exists or the name is invalid.
    """
    _check_name(name, "seed")
    path = seed_dir(name)
    if (path / SEED_FILE).exists():
        raise ProfileError("seed already exists: {}".format(name))
    data = seed_defaults(
        source_browser, source_dir, profile_subdir, password_store, defaults, now
    )
    path.mkdir(parents=True, exist_ok=True)
    _write_json(path / SEED_FILE, data)
    return data


def load_seed(name) -> Dict[str, Any]:
    """Return the parsed ``seed.json`` for ``name``.

    Raises:
        ProfileError: If the name is invalid or the seed is missing.
    """
    _check_name(name, "seed")
    path = seed_path(name)
    if not path.is_file():
        raise ProfileError("seed not found: {}".format(name))
    return _read_json(path)


def save_seed(name, data) -> Dict[str, Any]:
    """Persist ``data`` as ``seed.json`` for ``name``, returning it.

    Raises:
        ProfileError: If the name is invalid or the seed directory is missing.
    """
    _check_name(name, "seed")
    path = seed_path(name)
    if not path.parent.is_dir():
        raise ProfileError("seed not found: {}".format(name))
    _write_json(path, data)
    return data


def seed_exists(name) -> bool:
    """Return True when ``seed.json`` exists for ``name``."""
    _check_name(name, "seed")
    return seed_path(name).is_file()


def list_seeds() -> List[str]:
    """Return sorted seed names that have a ``seed.json``."""
    root = seeds_dir()
    if not root.is_dir():
        return []
    return sorted(
        parent.name
        for parent in root.iterdir()
        if parent.is_dir() and (parent / SEED_FILE).is_file()
    )


def delete_seed(name) -> None:
    """Remove a seed directory and all of its sessions.

    Refuses to delete while any session from the seed is live, where a session
    is live when the pid recorded in its ``session.json`` still exists.

    Raises:
        ProfileError: If the name is invalid or the seed is still in use.
    """
    _check_name(name, "seed")
    path = seed_dir(name)
    if not path.is_dir():
        raise ProfileError("seed not found: {}".format(name))

    live = [session for session in sessions_for_seed(name) if pid_alive(session.get("pid"))]
    if live:
        pids = sorted(
            int(pid)
            for session in live
            if (pid := session.get("pid"))
        )
        raise ProfileError(
            "seed {} is in use: {} session(s) still live (pid(s) {}); close them before deleting".format(
                name, len(live), pids
            )
        )

    # Drop session directories that belonged to this seed so no orphans remain
    # after the seed itself is gone.
    for session in sessions_for_seed(name):
        _remove_dir(session_dir(session.get("id")))

    import shutil

    shutil.rmtree(path)


def create_session(session_id=None, *, seed, pid, created=None, browser_pid=None, target=None) -> Dict[str, Any]:
    """Create a session directory and write its ``session.json``.

    Args:
        session_id: Optional id; a random id is generated when omitted.
        seed: Seed name this session was cloned from.
        pid: Pid of the launched clone process.
        created: Optional creation timestamp.
        browser_pid: Optional pid of the browser process.
        target: Optional CDP target identifier for the clone.

    Returns:
        The session payload that was written.
    """
    if session_id is None:
        session_id = generate_session_id()
    check_session_id(session_id)
    path = session_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    data = {
        "id": session_id,
        "seed": seed,
        "pid": pid,
        "browser_pid": browser_pid,
        "target": target,
        "created": created if created is not None else time.time(),
    }
    _write_json(path / SESSION_FILE, data)
    return data


def load_session(session_id) -> Dict[str, Any]:
    """Return the parsed ``session.json`` for ``session_id``.

    Raises:
        ProfileError: If the id is invalid or the session is missing.
    """
    check_session_id(session_id)
    path = session_path(session_id)
    if not path.is_file():
        raise ProfileError("session not found: {}".format(session_id))
    return _read_json(path)


def session_exists(session_id) -> bool:
    """Return True when ``session.json`` exists for ``session_id``."""
    check_session_id(session_id)
    return session_path(session_id).is_file()


def list_sessions() -> List[str]:
    """Return sorted session ids that have a ``session.json``."""
    root = sessions_dir()
    if not root.is_dir():
        return []
    return sorted(
        parent.name
        for parent in root.iterdir()
        if parent.is_dir() and (parent / SESSION_FILE).is_file()
    )


def session_id_for_seed(session_id) -> Optional[str]:
    """Return the id of ``session_id`` or None if it is not a valid session id."""
    try:
        check_session_id(session_id)
    except ProfileError:
        return None
    if not session_path(session_id).is_file():
        return None
    return session_id


def sessions_for_seed(name) -> List[Dict[str, Any]]:
    """Return the session payloads under ``name`` that still have a dir.

    A session counts even when its pid is dead (stale), so callers can decide
    whether liveness matters.
    """
    _check_name(name, "seed")
    root = sessions_dir()
    if not root.is_dir():
        return []
    matches = []
    for parent in root.iterdir():
        if not parent.is_dir():
            continue
        meta_file = parent / SESSION_FILE
        if not meta_file.is_file():
            continue
        data = _safe_read_json(meta_file)
        if data.get("seed") == name:
            data.setdefault("id", parent.name)
            matches.append(data)
    return matches


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=True)
    path.write_text(text + "\n", encoding="ascii")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="ascii"))


def _safe_read_json(path: Path) -> Dict[str, Any]:
    try:
        return _read_json(path)
    except (ValueError, OSError):
        return {}


def _remove_dir(path: Path) -> None:
    import shutil

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
