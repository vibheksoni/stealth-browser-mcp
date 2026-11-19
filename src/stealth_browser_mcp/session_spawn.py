"""Per-chat session spawn from a browser profile seed (SB-47).

A session is an isolated, per-chat Chrome instance whose profile is cloned from
a seed profile. The seed is never modified. Clones are disposable and their
directories are cleaned up on close.

Design, from PLAN section 2 and SB-47:
- (1) Session id generation and cap enforcement via session_registry.
- (2) Seed directory cloning (reflink where available, plain copy otherwise)
      under the per-seed lock. The seed is read-only.
- (3) Launch using the same browser family binary as the seed's source_browser
      (browser_sources.binary_candidates), user_data_dir = session profile dir,
      --profile-directory=<subdir>, --disable-sync, and on Linux drop nodriver's
      default --password-store=basic, passing --password-store=<seed.password_store>
      when known.
- (4) Timing recorded in session.json and the spawn result (clone_ms, launch_ms, total_ms).
- (5) Deletion of session dir on close (retried on Windows) and registry entry;
      startup janitor removes dead sessions' dirs.
- (6) Optional carryover check via CDP Network.getCookies verifying cookie count > 0
      for a check_url without logging or printing cookie values.

No print() calls exist in this module (stdio MCP transport). ASCII only.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import browser_sources
from . import cdp_raw
from . import profiles
from . import seed_extensions
from . import session_registry
from .file_lock import FileLock
from .platform_utils import BROWSER_EXECUTABLE_ENV, check_browser_executable


def find_family_binary(source_browser: str) -> str:
    """Find an installed browser executable matching source_browser family.

    Args:
        source_browser: Browser family name (e.g. "chrome", "edge", "brave").

    Returns:
        Path to an existing executable.

    Raises:
        RuntimeError: When no executable for the family is found.
    """
    family = (source_browser or "chrome").strip().lower()
    if os.path.sep in family or (os.path.altsep and os.path.altsep in family):
        for candidate_family in browser_sources.BROWSER_FAMILIES:
            if candidate_family in family:
                family = candidate_family
                break
        else:
            family = "chrome"

    try:
        candidates = browser_sources.binary_candidates(family)
    except ValueError:
        candidates = browser_sources.binary_candidates("chrome")

    override = os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        override_base = os.path.basename(override).lower()
        if any(c.lower() in override_base for c in candidates) or family in override_base:
            return override

    for cand in candidates:
        found = shutil.which(cand)
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return found

    sys_name = sys.platform
    if sys_name == "win32":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        ]
        subpaths: List[str] = []
        if family == "chrome":
            subpaths = [
                r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chromium.exe",
            ]
        elif family == "edge":
            subpaths = [
                r"Microsoft\Edge\Application\msedge.exe",
            ]
        elif family == "brave":
            subpaths = [
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ]
        for root in roots:
            if not root:
                continue
            for sub in subpaths:
                p = os.path.join(root, sub)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    elif sys_name == "darwin":
        apps: List[str] = []
        if family == "chrome":
            apps = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        elif family == "edge":
            apps = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
        elif family == "brave":
            apps = ["/Applications/Brave Software/Brave Browser/Contents/MacOS/Brave Browser"]
        for p in apps:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    else:
        prefixes = ["/usr/bin", "/usr/local/bin", "/snap/bin", "/opt"]
        for cand in candidates:
            for prefix in prefixes:
                p = os.path.join(prefix, cand)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p

    fallback = check_browser_executable()
    if fallback:
        fallback_base = os.path.basename(fallback).lower()
        if any(c.lower() in fallback_base for c in candidates) or family in fallback_base:
            return fallback

    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    if fallback:
        return fallback

    raise RuntimeError(
        "No compatible browser executable found for family '{}' (searched: {})".format(
            family, ", ".join(candidates)
        )
    )


def prepare_session_spawn(
    seed_name: str,
    cap: Optional[int] = None,
) -> Tuple[str, Path, Dict[str, Any]]:
    """Enforce cap and create the session directory with session.json.

    Args:
        seed_name: Validated seed name to spawn from.
        cap: Optional max concurrent sessions override.

    Returns:
        Tuple of (session_id, session_dir_path, seed_data_dict).

    Raises:
        profiles.ProfileError: If the seed does not exist or the cap is reached.
    """
    if not profiles.seed_exists(seed_name):
        raise profiles.ProfileError("seed not found: {}".format(seed_name))

    seed_data = profiles.load_seed(seed_name)
    session_id = profiles.generate_session_id()

    # create_session enforces live session cap under machine-wide lock
    session_registry.create_session(
        session_id=session_id,
        seed=seed_name,
        pid=os.getpid(),
        cap=cap,
    )
    session_dir = profiles.session_dir(session_id)
    return session_id, session_dir, seed_data


def _copy_file_reflink(src: Path, dst: Path) -> None:
    """Copy a file, preferring a reflink on Linux."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "linux":
        try:
            res = subprocess.run(
                ["cp", "--reflink=auto", "--", str(src), str(dst)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if res.returncode == 0 and dst.exists():
                return
        except Exception:
            pass
    shutil.copy2(str(src), str(dst))


def _copy_entry_recursive(src: Path, dst: Path) -> None:
    """Recursively copy a file or directory tree using reflink."""
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_entry_recursive(child, dst / child.name)
    elif src.is_file():
        _copy_file_reflink(src, dst)


def _clone_tree(src: Path, dst: Path) -> None:
    """Copy files from src directory to dst directory, omitting lock and session files."""
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(str(src)):
        dirnames[:] = [d for d in dirnames if not d.endswith(".lock")]
        rel_dir = os.path.relpath(dirpath, str(src))
        target_dir = dst if rel_dir == "." else dst / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            if fname.endswith(".lock") or fname.endswith(".lock.holder") or fname.endswith(".tmp"):
                continue
            if fname == profiles.SESSION_FILE:
                continue
            src_file = os.path.join(dirpath, fname)
            dst_file = target_dir / fname
            _copy_file_reflink(Path(src_file), dst_file)


def clone_seed_to_session(
    seed_name: str,
    session_id: str,
    seed_data: Optional[Dict[str, Any]] = None,
) -> float:
    """Clone seed directory into the session directory under the seed lock.

    The seed is never modified. Reflinks are used where possible on Linux,
    falling back to plain copy. Returns clone time in milliseconds.
    """
    t0 = time.monotonic()
    src_dir = profiles.seed_dir(seed_name)
    dst_dir = profiles.session_dir(session_id)
    if not src_dir.is_dir():
        raise profiles.ProfileError("seed directory not found: {}".format(seed_name))

    if seed_data is None:
        seed_data = profiles.load_seed(seed_name)

    seed_lock = FileLock(str(src_dir), timeout=10.0)
    with seed_lock:
        _clone_tree(src_dir, dst_dir)

        profile_subdir = seed_data.get("profile_subdir")
        if profile_subdir and profile_subdir not in (".", "", "/"):
            sub_target = dst_dir / profile_subdir
            if not sub_target.is_dir():
                sub_target.mkdir(parents=True, exist_ok=True)
                for item in dst_dir.iterdir():
                    if item.name in (
                        profile_subdir,
                        profiles.SESSION_FILE,
                        profiles.SEED_FILE,
                        browser_sources.LOCAL_STATE_FILE,
                    ):
                        continue
                    if item.name.endswith(".lock") or item.name.endswith(".lock.holder"):
                        continue
                    _copy_entry_recursive(item, sub_target / item.name)

        # SB-50: Fix unpacked extension paths only when clone contains extensions
        session_pdir = (
            dst_dir / profile_subdir
            if profile_subdir and profile_subdir not in (".", "", "/")
            else dst_dir
        )
        if seed_extensions.clone_has_extensions(session_pdir):
            source_dir = seed_data.get("source_dir")
            source_pdir = (
                Path(source_dir) / profile_subdir
                if source_dir and profile_subdir and profile_subdir not in (".", "", "/")
                else (Path(source_dir) if source_dir else src_dir)
            )
            report = seed_extensions.fixup_extension_paths(session_pdir, source_pdir)
            if report.load_extension_args:
                seed_data["load_extension_args"] = list(report.load_extension_args)

    clone_ms = round((time.monotonic() - t0) * 1000.0, 2)
    return clone_ms


def build_spawn_args_and_config(
    seed_data: Dict[str, Any],
    caller_args: List[str],
    platform_name: Optional[str] = None,
) -> Tuple[List[str], Optional[Callable[[List[str]], List[str]]]]:
    """Build launch args and optional arg modifier for password store.

    Args:
        seed_data: Parsed seed metadata dict.
        caller_args: Caller-provided browser launch args.
        platform_name: Optional platform override (e.g. for testing).

    Returns:
        Tuple of (modified_args, args_modifier_callable).
    """
    plat = browser_sources.normalize_platform(platform_name or sys.platform)
    args = list(caller_args or [])

    if "--disable-sync" not in args:
        args.append("--disable-sync")

    profile_subdir = seed_data.get("profile_subdir")
    if profile_subdir and not any(a.startswith("--profile-directory=") for a in args):
        args.append(f"--profile-directory={profile_subdir}")

    # SB-50: Pass --load-extension for unpacked extensions found in the clone
    for ext_arg in seed_data.get("load_extension_args") or []:
        if ext_arg not in args:
            args.append(ext_arg)

    args_modifier: Optional[Callable[[List[str]], List[str]]] = None
    if plat == "linux":
        password_store = seed_data.get("password_store")
        args = [a for a in args if not a.startswith("--password-store=")]
        if password_store:
            args.append(f"--password-store={password_store}")

        def _linux_password_store_modifier(cmd_args: List[str]) -> List[str]:
            filtered = [a for a in cmd_args if not a.startswith("--password-store=")]
            if password_store:
                filtered.append(f"--password-store={password_store}")
            return filtered

        args_modifier = _linux_password_store_modifier

    return args, args_modifier


async def finish_session_spawn(
    session_id: str,
    seed_name: str,
    session_dir: Path,
    browser: Any,
    clone_ms: float,
    t_launch_start: float,
    t_spawn_start: float,
    check_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Record timings in session.json, perform carryover check, return session_info."""
    t_now = time.monotonic()
    launch_ms = round((t_now - t_launch_start) * 1000.0, 2)
    total_ms = round((t_now - t_spawn_start) * 1000.0, 2)

    browser_pid = None
    if getattr(browser, "_process", None) is not None:
        browser_pid = getattr(browser._process, "pid", None)
    if browser_pid is None:
        browser_pid = getattr(browser, "_process_pid", None)

    session_file = session_dir / profiles.SESSION_FILE
    if session_file.is_file():
        try:
            data = json.loads(session_file.read_text(encoding="ascii"))
            data["browser_pid"] = browser_pid
            data["clone_ms"] = clone_ms
            data["launch_ms"] = launch_ms
            data["total_ms"] = total_ms
            session_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=True) + "\n",
                encoding="ascii",
            )
        except Exception:
            pass

    timing = {
        "clone_ms": clone_ms,
        "launch_ms": launch_ms,
        "total_ms": total_ms,
    }

    result: Dict[str, Any] = {
        "session_id": session_id,
        "seed": seed_name,
        "user_data_dir": str(session_dir),
        "timing": timing,
        "clone_ms": clone_ms,
        "launch_ms": launch_ms,
        "total_ms": total_ms,
    }

    if check_url:
        carried_over = False
        try:
            main_tab = getattr(browser, "main_tab", None)
            if main_tab is None and getattr(browser, "tabs", None):
                main_tab = browser.tabs[0]
            if main_tab is not None:
                cookies = await cdp_raw.get_cookies(main_tab, urls=[check_url])
                carried_over = len(cookies) > 0
        except Exception:
            carried_over = False
        result["carried_over"] = carried_over

    return result


def _safe_remove_dir(path: Path, max_attempts: int = 10, delay: float = 0.1) -> None:
    """Safely delete directory with retry for Windows process file handle release."""
    if not path.is_dir():
        return
    for _ in range(max_attempts):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError:
            if sys.platform == "win32":
                time.sleep(delay)
            else:
                break
    shutil.rmtree(path, ignore_errors=True)


def cleanup_session(session_id: str, keep: bool = False) -> None:
    """Delete the session directory and the registry entry.

    Retries on Windows while handles release.
    """
    if keep:
        return
    try:
        session_path = profiles.session_dir(session_id)
    except Exception:
        return

    try:
        session_registry.close_session(session_id)
    except Exception:
        pass

    if session_path.is_dir():
        _safe_remove_dir(session_path)


def startup_janitor() -> List[str]:
    """Startup janitor removing dead sessions' directories."""
    try:
        return session_registry.janitor()
    except Exception:
        return []


try:
    startup_janitor()
except Exception:
    pass
