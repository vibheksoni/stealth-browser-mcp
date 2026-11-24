"""Machine-wide session registry built on profiles.py and file_lock.py.

profiles.py already stores each session as a directory under
``data_dir()/sessions/<id>/session.json`` with an id, seed name, pid, and
creation time. This module adds two things those helpers do not:

* a cross-process live-session count and an optional cap. Separate server
  processes each keep their own in-memory state, so the count has to be taken
  under an OS file lock (see :class:`file_lock.FileLock`) or two processes can
  both exceed the cap. No default cap is imposed (Alan's decision, Q9); callers
  pass an explicit ``cap`` or set ``STEALTH_MAX_SESSIONS``.
* a janitor that removes session directories whose recorded pid is dead while
  keeping the live ones, so a crashed clone never blocks a new spawn.

Only local files are touched. No real profile is ever read here.
"""

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .file_lock import FileLock
from .paths import data_dir
from . import profiles
from .profiles import pid_alive

# Path of the machine-wide lock that guards the live-session count and the cap.
# Computed at call time (see _lock_path) so it respects STEALTH_BROWSER_MCP_DATA_DIR.
_LOCK_PATH = data_dir() / "locks" / "sessions.lock"


def _lock_path():
    """Return the current lock path under the active data directory.

    ``data_dir()`` is called here (not once at import) so the lock follows
    STEALTH_BROWSER_MCP_DATA_DIR for tests and for a data dir changed at runtime.
    """
    return data_dir() / "locks" / "sessions.lock"


# Environment variable that sets a default cap when a caller does not pass one.
_CAP_ENV = "STEALTH_MAX_SESSIONS"
_UNSET = object()


def cap_from_env(default=None):
    """Return the cap from ``STEALTH_MAX_SESSIONS``.

    A blank or non-numeric value falls back to ``None`` (unlimited) so an unset
    environment never raises. A value of ``0`` or less is treated as unlimited,
    matching the ``cap=None`` convention.
    """
    text = os.environ.get(_CAP_ENV, "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return None if value <= 0 else value


def resolve_cap(cap=_UNSET):
    """Resolve a final cap from a caller value and the environment.

    An explicit ``cap`` wins (including ``cap=None`` for unlimited);
    the environment is only consulted when ``cap`` is not given.
    ``None`` or a non-positive result means unlimited.
    """
    if cap is _UNSET:
        return cap_from_env()
    if cap is None or cap <= 0:
        return None
    return cap


def lock_for(cap=None):
    """Return the cross-process lock that guards the live-session count.

    ``cap`` is accepted for symmetry with :func:`create_session` and ignored;
    the lock path is fixed so every process guards the same resource. The lock
    directory is created if missing so :class:`file_lock.FileLock` can open it.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=10.0)


def live_session_count(cap=None):
    """Count session directories whose recorded pid is still alive.

    Dead (crashed) sessions are not counted, so they never take up a cap slot.
    The count is taken under the machine-wide lock so concurrent processes see
    a consistent number.

    Args:
        cap: Ignored; accepted for symmetry with :func:`create_session`.

    Returns:
        int: Number of live session directories.
    """
    with lock_for(cap):
        return _count_live()


def _count_live():
    """Count live sessions. Must be called while holding the lock."""
    return sum(
        1
        for session in profiles.list_sessions()
        if pid_alive(profiles.load_session(session).get("pid"))
    )


def create_session(session_id=None, *, seed, pid, created=None, browser_pid=None, target=None, cap=_UNSET):
    """Create a session directory, honoring the live-session cap.

    The session is created under the machine-wide lock only after confirming
    that the current live count is below the cap. With a cap of ``1`` and two
    processes racing, exactly one sees room and creates the session; the other
    sees the slot taken and raises.

    Args:
        session_id: Optional id; a random id is generated when omitted.
        seed: Seed name this session was cloned from.
        pid: Pid of the launched clone process.
        created: Optional creation timestamp.
        browser_pid: Optional pid of the browser process.
        target: Optional CDP target identifier for the clone.
        cap: Optional maximum number of concurrent live sessions. None or a
            non-positive value means unlimited. Overrides ``STEALTH_MAX_SESSIONS``.

    Returns:
        The session payload that was written.

    Raises:
        profiles.ProfileError: If the id is invalid, or the cap is exceeded.
    """
    resolved = resolve_cap(cap)
    with lock_for(cap):
        if resolved is not None and _count_live() >= resolved:
            live = [
                session
                for session in profiles.list_sessions()
                if pid_alive(profiles.load_session(session).get("pid"))
            ]
            raise profiles.ProfileError(
                "session cap of {0} reached: {1} live session(s); close one before launching another".format(
                    resolved, len(live)
                )
            )
        data = profiles.create_session(
            session_id,
            seed=seed,
            pid=pid,
            created=created,
            browser_pid=browser_pid,
            target=target,
        )
    return data


def close_session(session_id):
    """Remove a session directory and free its cap slot across processes.

    The removal is recorded under the machine-wide lock so a concurrent cap
    check in another process does not see the slot as still occupied.

    Args:
        session_id: Session id to close.

    Raises:
        profiles.ProfileError: If the id is invalid or the session is missing.
    """
    with lock_for():
        if not profiles.session_exists(session_id):
            raise profiles.ProfileError("session not found: {}".format(session_id))
        profiles._remove_dir(profiles.session_dir(session_id))


def janitor():
    """Remove session directories whose recorded pid is dead.

    A session is dead when the pid stored in its ``session.json`` no longer
    exists. Live sessions are kept. Returns the ids of the sessions removed.

    Returns:
        List[str]: Session ids whose directories were removed.
    """
    with lock_for():
        removed = []
        for session in profiles.list_sessions():
            data = profiles.load_session(session)
            if not pid_alive(data.get("pid")):
                profiles._remove_dir(profiles.session_dir(session))
                removed.append(session)
        return removed
