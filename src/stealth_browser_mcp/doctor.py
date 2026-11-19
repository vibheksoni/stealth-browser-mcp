"""Environment health checks for a stealth-browser-mcp deployment.

This module answers "why can't I launch a session?" style questions without
touching a browser, a network, or a real profile's secrets. Each check is a
small function that takes its inputs as injectable dependencies, so the tests
can drive it with fakes and temporary directories instead of real binaries,
keyrings, or live processes.

The six checks are:

* browser binaries per supported family (Chrome, Edge, Brave),
* Linux keyring / password store reachability (``secret-tool`` or a known
  libsecret/KWallet dbus name; skipped on other platforms),
* seed ages, warning when a seed has not refreshed in 30 days,
* live sessions versus the configured cap,
* stale locks left behind by a dead process,
* and whether the data directory is writable.

The result is passed through :func:`secrets_env.redact` so an environment
secret value or a home-directory credential path can never reach the caller.
"""

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import profiles
from . import secrets_env
from . import session_registry
from .browser_sources import (
    BROWSER_FAMILIES,
    binary_candidates,
    normalize_platform,
)
from .file_lock import FileLock
from .paths import data_dir as default_data_dir

# A seed older than this (days since its last refresh) is treated as stale.
STALE_SEED_DAYS = 30

# Dbus well-known names a Linux key store exposes to libsecret clients.
LIBSECRET_DBUS_NAMES = ("org.freedesktop.secrets", "org.freedesktop.KWallet")

# Statuses ranked by how severe they are. ``ok`` is the cleanest, ``skipped``
# is informational, ``warn`` needs attention, and ``fail`` is a hard stop.
STATUS_RANK = {"ok": 0, "skipped": 1, "warn": 2, "fail": 3}

# Seeds are refreshed daily; a seed older than this (days since its refresh)
# is reported as stale by :func:`_default_seed_ages`.
SEED_REFRESH_DAYS = 30


def _check_browser_binaries(
    platform: str,
    which_fn: Callable[[str], Optional[str]],
    families: tuple = BROWSER_FAMILIES,
) -> Dict[str, Any]:
    """Report which browser families have a launchable executable.

    Args:
        platform: A platform string accepted by :func:`normalize_platform`.
        which_fn: Maps an executable name to a found path (or None). Tests
            inject a fake so no real PATH is searched.
        families: The families to probe; defaults to every supported family.

    Returns:
        A single check dict. ``ok`` when at least one family is found, else
        ``fail``. The detail lists the matched candidate per family.
    """
    platform = normalize_platform(platform)
    matched: Dict[str, Optional[str]] = {family: None for family in families}
    for family in families:
        for candidate in binary_candidates(family):
            if which_fn(candidate):
                matched[family] = candidate
                break
    present = [family for family, path in matched.items() if path]
    detail = ", ".join(
        "{}={}".format(family, matched[family] if matched[family] else "none")
        for family in families
    )
    if present:
        return {
            "name": "browser_binaries",
            "status": "ok",
            "detail": "launchable binary for: {}".format(", ".join(present)),
        }
    return {
        "name": "browser_binaries",
        "status": "fail",
        "detail": "no launchable binary for any of {}".format(detail),
    }


def _check_keyring(
    platform: str,
    which_fn: Callable[[str], Optional[str]],
    dbus_names_fn: Callable[[], List[str]],
) -> Dict[str, Any]:
    """Check that a Linux password store is reachable.

    On non-Linux platforms the check is skipped. On Linux it succeeds when
    ``secret-tool`` is on the PATH or a known libsecret/KWallet dbus name is
    present.

    Args:
        platform: A platform string accepted by :func:`normalize_platform`.
        which_fn: Maps ``secret-tool`` to a found path, or None.
        dbus_names_fn: Returns the dbus well-known names currently present.

    Returns:
        A check dict with status ``ok``, ``warn`` (Linux without a store), or
        ``skipped`` (non-Linux).
    """
    platform = normalize_platform(platform)
    if platform != "linux":
        return {"name": "keyring", "status": "skipped", "detail": "skipped on {}".format(platform)}
    if which_fn("secret-tool"):
        return {"name": "keyring", "status": "ok", "detail": "secret-tool found"}
    if any(name in dbus_names_fn() for name in LIBSECRET_DBUS_NAMES):
        return {"name": "keyring", "status": "ok", "detail": "libsecret/kwallet dbus name present"}
    return {
        "name": "keyring",
        "status": "warn",
        "detail": "secret-tool missing and no libsecret/kwallet dbus name found",
    }


def _default_seed_ages(data_dir: Path, now: float) -> Dict[str, int]:
    """Return ``{seed_name: age_days}`` for every seed under ``data_dir``.

    Age is measured from ``seed.json`` ``refreshed_at``. Seeds that cannot be
    read are skipped rather than aborting the whole check.
    """
    ages: Dict[str, int] = {}
    for name in profiles.list_seeds():
        try:
            seed = profiles.load_seed(name)
        except profiles.ProfileError:
            continue
        refreshed = seed.get("refreshed_at")
        if isinstance(refreshed, (int, float)):
            ages[name] = max(0, int((now - refreshed) // SEED_REFRESH_DAYS))
    return ages


def _check_seed_ages(ages: Dict[str, int]) -> Dict[str, Any]:
    """Warn about seeds older than :data:`SEED_REFRESH_DAYS` days.

    Args:
        ages: Mapping of seed name to age in days.

    Returns:
        A check dict. ``ok`` when there are no seeds or all are fresh,
        ``warn`` when any exceed the threshold.
    """
    if not ages:
        return {"name": "seed_ages", "status": "ok", "detail": "no seeds to check"}
    stale = {name: days for name, days in ages.items() if days > SEED_REFRESH_DAYS}
    if not stale:
        youngest = min(ages, key=lambda name: ages[name])
        return {
            "name": "seed_ages",
            "status": "ok",
            "detail": "all seeds fresh (oldest {}={}d)".format(youngest, ages[youngest]),
        }
    detail = ", ".join("{}={}".format(name, days) for name, days in sorted(stale.items()))
    return {
        "name": "seed_ages",
        "status": "warn",
        "detail": "stale seed(s) older than {}d: {}".format(SEED_REFRESH_DAYS, detail),
    }


def _check_live_sessions(cap: Optional[int], live_count: int) -> Dict[str, Any]:
    """Compare live sessions to the cap.

    Args:
        cap: Maximum concurrent live sessions. ``None`` or non-positive means
            unlimited.
        live_count: Number of currently live sessions.

    Returns:
        A check dict. ``fail`` when the live count reaches the cap, else
        ``ok`` with the count and cap.
    """
    if cap is None or cap <= 0:
        return {"name": "live_sessions", "status": "ok", "detail": "live sessions={} (no cap)".format(live_count)}
    if live_count >= cap:
        return {"name": "live_sessions", "status": "fail", "detail": "live sessions {} reached cap {}".format(live_count, cap)}
    return {
        "name": "live_sessions",
        "status": "ok",
        "detail": "live sessions {} of cap {}".format(live_count, cap),
    }


def _default_lock_files(data_dir: Path) -> List[str]:
    """Return the ``*.lock`` paths under ``data_dir()/locks``.

    Returns an empty list when the locks directory does not exist.
    """
    locks_dir = data_dir / "locks"
    if not locks_dir.is_dir():
        return []
    return sorted(str(path) for path in locks_dir.iterdir() if path.name.endswith(".lock"))


def _default_lock_alive(lock_path: str) -> bool:
    """Return True when the holder recorded in a lock file is still alive."""
    return FileLock(lock_path).holder_alive()


def _check_stale_locks(
    lock_files_fn: Callable[[], List[str]],
    lock_alive_fn: Callable[[str], bool],
) -> Dict[str, Any]:
    """Find locks whose recorded holder process has exited.

    Args:
        lock_files_fn: Returns the list of lock file paths under the data dir.
        lock_alive_fn: Returns True when a lock file is held by a live pid.

    Returns:
        A check dict. ``ok`` when every lock is live or there are none,
        ``warn`` when at least one lock is stale.
    """
    stale = [lock_path for lock_path in lock_files_fn() if not lock_alive_fn(lock_path)]
    if not stale:
        return {"name": "stale_locks", "status": "ok", "detail": "no stale locks"}
    return {
        "name": "stale_locks",
        "status": "warn",
        "detail": "{} stale lock(s) from dead process(es): {}".format(
            len(stale), ", ".join(os.path.basename(path) for path in stale)
        ),
    }


def _default_writable(directory: Path) -> bool:
    """Probe ``directory`` by writing and removing a temporary probe file."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".stealth_doctor_write"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _check_data_dir_writable(
    writable_fn: Callable[[Path], bool],
    data_dir: Path,
) -> Dict[str, Any]:
    """Report whether the data directory can be written to."""
    if writable_fn(data_dir):
        return {"name": "data_dir_writable", "status": "ok", "detail": "data directory is writable"}
    return {"name": "data_dir_writable", "status": "fail", "detail": "data directory is not writable"}


def _reduce(counts: Dict[str, int]) -> Dict[str, Any]:
    """Summarize per-status counts into one overall verdict."""
    worst = "ok"
    for status, rank in STATUS_RANK.items():
        if counts.get(status, 0) > 0 and rank > STATUS_RANK[worst]:
            worst = status
    return {
        "total": sum(counts.values()),
        "ok": counts.get("ok", 0),
        "warn": counts.get("warn", 0),
        "fail": counts.get("fail", 0),
        "status": worst,
    }


def run_checks(
    env: Optional[Dict[str, str]] = None,
    *,
    platform: Optional[str] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    seed_ages_fn: Optional[Callable[[], Dict[str, int]]] = None,
    cap: Optional[int] = None,
    live_session_count: Optional[int] = None,
    lock_files_fn: Optional[Callable[[], List[str]]] = None,
    lock_alive_fn: Optional[Callable[[str], bool]] = None,
    data_dir: Optional[Path] = None,
    writable_fn: Optional[Callable[[Path], bool]] = None,
    redactor: Optional[secrets_env.Redactor] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run the environment health checks and return a redacted summary.

    All dependencies default to reading the live machine (``os.environ``,
    :mod:`session_registry`, :mod:`profiles`, and real filesystem probes), but
    every one can be overridden with a keyword argument so tests can supply
    fakes and temporary directories. No real browser profile is ever read.

    Args:
        env: Optional environment mapping. Only used for platform detection
            when ``platform`` is not given; nothing in it is echoed to output.
        platform: Platform string (``linux``/``windows``/``darwin`` or a
            :func:`platform.system` / :func:`sys.platform` spelling). Defaults
            to the current platform.
        which_fn: Callable mapping an executable name to a path. Defaults to
            :func:`shutil.which`.
        seed_ages_fn: Callable returning ``{seed_name: age_days}``. Defaults to
            reading seeds under the data directory.
        cap: Session cap. Defaults to :func:`session_registry.resolve_cap`.
        live_session_count: Live session count. Defaults to
            :func:`session_registry.live_session_count`.
        lock_files_fn: Callable returning lock file paths. Defaults to listing
            ``data_dir()/locks``.
        lock_alive_fn: Callable returning whether a lock is held live.
        data_dir: Data directory for the default lock/seed/writable probes.
        writable_fn: Callable returning whether the data directory is writable.
        redactor: Redactor to mask secrets from the result. Defaults to the
            global :data:`secrets_env.default_redactor`.
        now: Current time (seconds since epoch) for seed-age math.

    Returns:
        A dict with a ``checks`` list of ``{name, status, detail}`` dicts and a
        ``summary`` dict with counts and an overall ``status``. The output is
        redacted so no secret value reaches it.
    """
    if platform is None:
        candidate = (env or {}).get("OS") or sys.platform
        platform = normalize_platform(candidate)
    if which_fn is None:
        which_fn = shutil.which
    if data_dir is None:
        data_dir = default_data_dir()
    if now is None:
        now = time.time()
    if redactor is None:
        redactor = secrets_env.default_redactor

    if seed_ages_fn is None:
        seed_ages_fn = lambda: _default_seed_ages(data_dir, now)
    if lock_files_fn is None:
        lock_files_fn = lambda: _default_lock_files(data_dir)
    if lock_alive_fn is None:
        lock_alive_fn = _default_lock_alive
    if writable_fn is None:
        writable_fn = lambda directory: _default_writable(directory)

    cap_value = cap if cap is not None else session_registry.resolve_cap()
    live = live_session_count if live_session_count is not None else session_registry.live_session_count()

    dbus_names = lambda: list(LIBSECRET_DBUS_NAMES) if _keyring_dbus_available() else []

    checks = [
        _check_browser_binaries(platform, which_fn),
        _check_keyring(platform, which_fn, dbus_names),
        _check_seed_ages(seed_ages_fn()),
        _check_live_sessions(cap_value, live),
        _check_stale_locks(lock_files_fn, lock_alive_fn),
        _check_data_dir_writable(writable_fn, data_dir),
    ]

    counts: Dict[str, int] = {status: 0 for status in STATUS_RANK}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1

    result = {"checks": checks, "summary": _reduce(counts)}
    return redactor.redact(result)


def _keyring_dbus_available() -> bool:
    """Return True when the dbus module is importable (Linux key store probe).

    The libsecret/KWallet dbus names cannot be probed without the optional
    ``dbus`` package, so the default relies on ``secret-tool``. This hook keeps
    the check importable and testable without that optional dependency.
    """
    try:
        import dbus  # noqa: F401
    except (ImportError, AttributeError):
        return False
    return True
