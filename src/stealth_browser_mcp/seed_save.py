"""Seed save functionality: save session state back to seed (SB-49).

Saves a running session's profile state (cookies, local storage, settings) back
into its originating seed (or a new seed name) so subsequent sessions start
logged in.

Operations are protected by a per-seed FileLock. If the seed already exists, an
automatic snapshot is taken first via :mod:`seed_snapshots`. The session profile
directory is copied using the same manifest and copy machinery
(:mod:`seed_manifest` and :mod:`seed_copy`), ensuring consistent SQLite snapshots
even when a browser is live. The original source browser profile is never touched.

No print() calls exist in this module (stdio MCP transport). ASCII only.
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import browser_sources
from . import profiles
from . import seed_copy as sc
from . import seed_manifest as sm
from . import seed_snapshots
from .file_lock import FileLock, LockTimeout


class SeedSaveError(profiles.ProfileError):
    """Raised when save_to_seed cannot complete."""


class SeedBusyError(SeedSaveError, LockTimeout):
    """Raised when the target seed is locked or busy."""


def seed_lock(name: str, timeout: float = 10.0) -> FileLock:
    """Return a FileLock guarding save operations for the named seed."""
    profiles._check_name(name, "seed")
    seed_path = profiles.seed_dir(name)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(seed_path) + ".save", timeout=timeout)


seed_save_lock = seed_lock


def _flush_sleep(seconds: float) -> None:
    """Sleep helper for flushing pending profile writes before copying."""
    time.sleep(seconds)


def _copy_entry_recursive(src: Path, dst: Path) -> None:
    """Recursively copy a file or directory tree."""
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_entry_recursive(child, dst / child.name)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))


def find_session_profile_dir(session_id: str) -> Tuple[Path, str, Optional[Path]]:
    """Determine the profile directory and Local State location for a session.

    Args:
        session_id: Validated session id.

    Returns:
        Tuple of (profile_dir_path, profile_subdir_name, local_state_path).
    """
    profiles.check_session_id(session_id)
    if not profiles.session_exists(session_id):
        raise profiles.ProfileError(f"session not found: {session_id}")

    session_path = profiles.session_dir(session_id)
    if not session_path.is_dir():
        raise profiles.ProfileError(f"session directory not found: {session_id}")

    session_data = profiles.load_session(session_id)
    orig_seed = session_data.get("seed")
    profile_subdir = "Default"
    if orig_seed and profiles.seed_exists(orig_seed):
        try:
            orig_data = profiles.load_seed(orig_seed)
            profile_subdir = orig_data.get("profile_subdir") or "Default"
        except Exception:
            profile_subdir = "Default"

    # Find Local State
    ls_path: Optional[Path] = None
    root_ls = session_path / browser_sources.LOCAL_STATE_FILE
    if root_ls.is_file():
        ls_path = root_ls
    elif profile_subdir and (session_path / profile_subdir / browser_sources.LOCAL_STATE_FILE).is_file():
        ls_path = session_path / profile_subdir / browser_sources.LOCAL_STATE_FILE

    profile_indicators = ("Cookies", "Network", "Preferences", "Local Storage", "IndexedDB", "Extensions")

    # Check profile_subdir
    if profile_subdir and (session_path / profile_subdir).is_dir():
        sub_dir = session_path / profile_subdir
        if any((sub_dir / ind).exists() for ind in profile_indicators):
            return sub_dir, profile_subdir, ls_path

    # Check Default
    if (session_path / "Default").is_dir():
        sub_dir = session_path / "Default"
        if any((sub_dir / ind).exists() for ind in profile_indicators):
            return sub_dir, "Default", ls_path

    # Fallback to session root directory
    return session_path, profile_subdir or "Default", ls_path


def close_session_browser(
    session_id: str,
    timeout: float = 15.0,
    browser_manager: Optional[Any] = None,
) -> Any:
    """Gracefully close the browser for a session via CDP Browser.close and wait for exit."""
    from .browser_manager import BrowserManager

    return BrowserManager.find_and_close_session_browser(
        session_id=session_id,
        keep=True,
        timeout=timeout,
        manager=browser_manager,
    )


def save_to_seed(
    session_id: str,
    seed: Optional[str] = None,
    close_first: bool = False,
    flush_wait_seconds: float = 6.0,
    *,
    lock_timeout: float = 10.0,
    max_snapshots: Optional[int] = 5,
    snapshot_label: Optional[str] = "auto-save",
    browser_already_closed: bool = False,
    close_browser_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Save a session's profile state into a seed under the per-seed lock.

    Args:
        session_id: Validated session ID.
        seed: Optional target seed name. If None, saves to the seed the session
            was spawned from.
        close_first: When True, gracefully closes the session's browser first,
            then copies the profile, then removes the session directory.
        flush_wait_seconds: Seconds to wait before copying when close_first is False
            so localStorage has time to commit. Defaults to 6.0 (0 disables).
        lock_timeout: Seconds to wait for seed locks before reporting busy.
        max_snapshots: Snapshot retention count when taking the automatic pre-save
            snapshot.
        snapshot_label: Optional label for the automatic snapshot.
        browser_already_closed: Internal flag indicating the browser was already
            closed asynchronously before calling save_to_seed.
        close_browser_fn: Optional custom close callable for testing with fakes.

    Returns:
        Dict[str, Any] with operation outcome, status, seed, and file counts.
        Includes a warning string when close_first is False.

    Raises:
        SeedBusyError: When locks cannot be acquired within the timeout.
        SeedSaveError: When profile copying fails.
        ProfileError: When session or seed validation fails.
    """
    profiles.check_session_id(session_id)

    orig_seed: Optional[str] = None
    if seed is not None and isinstance(seed, str) and seed.strip():
        target_seed = seed.strip()
        if profiles.session_exists(session_id):
            orig_seed = profiles.load_session(session_id).get("seed")
    else:
        if not profiles.session_exists(session_id):
            raise profiles.ProfileError(f"session not found: {session_id}")
        orig_seed = profiles.load_session(session_id).get("seed")
        target_seed = orig_seed

    if not target_seed:
        raise profiles.ProfileError(f"no seed specified and session {session_id} has no recorded seed")

    profiles._check_name(target_seed, "seed")

    save_guard = seed_lock(target_seed, timeout=lock_timeout)
    try:
        save_guard.acquire(timeout=lock_timeout)
    except LockTimeout as exc:
        raise SeedBusyError(
            f"seed '{target_seed}' is busy: could not acquire save lock within {lock_timeout}s"
        ) from exc

    try:
        if not profiles.session_exists(session_id):
            raise profiles.ProfileError(f"session not found: {session_id}")

        session_data = profiles.load_session(session_id)
        session_path = profiles.session_dir(session_id)
        if not session_path.is_dir():
            raise profiles.ProfileError(f"session directory not found: {session_id}")

        orig_seed = session_data.get("seed")

        if close_first and not browser_already_closed:
            if close_browser_fn is not None:
                close_res = close_browser_fn(session_id)
            else:
                close_res = close_session_browser(session_id)
            if asyncio.iscoroutine(close_res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is None:
                    asyncio.run(close_res)
                elif not loop.is_running():
                    loop.run_until_complete(close_res)
        elif not close_first and flush_wait_seconds > 0:
            _flush_sleep(flush_wait_seconds)

        # Step 1: Automatic snapshot if seed already exists
        snapshot_id: Optional[str] = None
        had_existing_seed = profiles.seed_exists(target_seed)
        existing_seed_data: Dict[str, Any] = {}
        if had_existing_seed:
            try:
                existing_seed_data = profiles.load_seed(target_seed)
            except Exception:
                existing_seed_data = {}
            try:
                snap_res = seed_snapshots.snapshot_seed(
                    name=target_seed,
                    label=snapshot_label,
                    max_snapshots=max_snapshots,
                    lock_timeout=lock_timeout,
                )
                snapshot_id = snap_res.get("snapshot_id")
            except LockTimeout as exc:
                raise SeedBusyError(
                    f"seed '{target_seed}' is busy: could not acquire lock for snapshot within {lock_timeout}s"
                ) from exc
            except Exception as exc:
                raise SeedSaveError(f"failed to snapshot seed '{target_seed}': {exc}") from exc

        # Step 2: Determine session profile directory and build manifest plan
        session_profile_dir, profile_subdir, ls_path = find_session_profile_dir(session_id)
        plan = sm.build_plan(str(session_profile_dir))

        # Step 3: Copy session profile dir into seed dir
        dest_path = profiles.seed_dir(target_seed)
        dest_dir = str(dest_path)

        copy_result = sc.copy_seed(plan, str(session_profile_dir), dest_dir)
        if copy_result.status == "source_busy":
            raise SeedBusyError(
                f"seed '{target_seed}' or session is busy: {copy_result.errors}"
            )
        if copy_result.status != "ok":
            raise SeedSaveError(
                f"failed to copy session to seed: {copy_result.errors}"
            )

        # Step 4: Copy Local State if present
        if ls_path is not None and ls_path.is_file():
            dest_ls = dest_path / browser_sources.LOCAL_STATE_FILE
            if not dest_ls.is_file():
                try:
                    shutil.copy2(str(ls_path), str(dest_ls))
                except Exception:
                    pass

        # Step 5: Mirror to dest_path / profile_subdir if appropriate
        if profile_subdir and profile_subdir not in (".", "", "/"):
            sub_dest = dest_path / profile_subdir
            if sub_dest.is_dir():
                shutil.rmtree(str(sub_dest), ignore_errors=True)
            sub_dest.mkdir(parents=True, exist_ok=True)
            for item in dest_path.iterdir():
                if item.name in (
                    profile_subdir,
                    profiles.SESSION_FILE,
                    profiles.SEED_FILE,
                    browser_sources.LOCAL_STATE_FILE,
                ) or item.name.endswith(".lock") or item.name.endswith(".lock.holder"):
                    continue
                _copy_entry_recursive(item, sub_dest / item.name)

        # Step 6: Update or create seed.json
        now_ts = time.time()
        if had_existing_seed:
            if not existing_seed_data.get("source_browser"):
                existing_seed_data["source_browser"] = "chrome"
            if "source_dir" not in existing_seed_data:
                existing_seed_data["source_dir"] = ""
            existing_seed_data["profile_subdir"] = profile_subdir or existing_seed_data.get("profile_subdir", "Default")
            existing_seed_data["refreshed_at"] = now_ts
            profiles.save_seed(target_seed, existing_seed_data)
        else:
            if orig_seed and profiles.seed_exists(orig_seed):
                try:
                    orig_data = profiles.load_seed(orig_seed)
                except Exception:
                    orig_data = {}
                source_browser = orig_data.get("source_browser", "chrome")
                source_dir = orig_data.get("source_dir", "")
                psubdir = profile_subdir or orig_data.get("profile_subdir", "Default")
                password_store = orig_data.get("password_store")
                defaults = orig_data.get("defaults", {})
            else:
                source_browser = "chrome"
                source_dir = ""
                psubdir = profile_subdir or "Default"
                password_store = None
                defaults = {}

            if (dest_path / profiles.SEED_FILE).is_file():
                profiles.save_seed(
                    target_seed,
                    profiles.seed_defaults(
                        source_browser=source_browser,
                        source_dir=source_dir,
                        profile_subdir=psubdir,
                        password_store=password_store,
                        defaults=defaults,
                        now=now_ts,
                    ),
                )
            else:
                profiles.create_seed(
                    name=target_seed,
                    source_browser=source_browser,
                    source_dir=source_dir,
                    profile_subdir=psubdir,
                    password_store=password_store,
                    defaults=defaults,
                    now=now_ts,
                )

        if close_first:
            try:
                from . import session_spawn
                session_spawn.cleanup_session(session_id, keep=False)
            except Exception:
                pass

        result = {
            "status": "ok",
            "seed": target_seed,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "copied_count": len(copy_result.copied),
            "copied": len(copy_result.copied),
            "skipped_busy": copy_result.skipped_busy,
            "bytes": copy_result.bytes,
            "path": str(dest_path),
        }
        if not close_first:
            result["warning"] = (
                "Cookies written within the last ~30 s may be missing and "
                "that close_first=True guarantees a complete save."
            )
        return result
    finally:
        save_guard.release()
