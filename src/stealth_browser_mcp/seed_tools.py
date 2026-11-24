"""Seed tools for discovering browser profiles and refreshing seeds.

Provides list_sources and refresh_seed tools on Linux. Discovers installed
Chrome, Edge, and Brave browser profiles and builds curated seed profiles
(cookies, local storage, extensions, settings, no caches) from them.

The source profile directory is opened read-only and is never modified.
No secrets or profile contents are returned in tool results.
"""

import os
import shutil
import sys
import time
from typing import Any, Dict, List, Mapping, Optional

from . import browser_sources
from . import profiles
from . import seed_copy as sc
from . import seed_manifest as sm
from .file_lock import FileLock


def _is_linux(platform: Optional[str] = None) -> bool:
    """Return True if the target platform normalizes to Linux."""
    target = platform if platform is not None else sys.platform
    try:
        return browser_sources.normalize_platform(target) == "linux"
    except ValueError:
        return False


def list_sources(
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """Discover installed browser profile sources and existing seeds with age.

    Args:
        env: Optional environment mapping (e.g. for testing with fake HOME).
        platform: Optional platform override (e.g. for testing).

    Returns:
        Dict[str, Any]: Discovered browser profiles and existing seeds.
    """
    try:
        plat = browser_sources.normalize_platform(platform or sys.platform)
    except ValueError:
        return {
            "status": "error",
            "error": "Unsupported platform: {}".format(platform or sys.platform),
            "sources": [],
            "seeds": [],
        }

    if plat != "linux":
        return {
            "status": "error",
            "error": "list_sources is currently only supported on Linux (platform: {})".format(plat),
            "sources": [],
            "seeds": [],
        }

    sources: List[Dict[str, Any]] = []
    for family in browser_sources.BROWSER_FAMILIES:
        candidates = browser_sources.binary_candidates(family)
        installed_binary = None
        for cand in candidates:
            found = shutil.which(cand)
            if found:
                installed_binary = found
                break

        try:
            root = browser_sources.user_data_root(family, plat, env)
        except Exception:
            root = None

        root_exists = bool(root and os.path.isdir(root))
        if not root_exists and installed_binary is None:
            continue

        profiles_list: List[str] = []
        last_used: Optional[str] = None
        if root_exists and root:
            ls_path = browser_sources.local_state_path(family, plat, env)
            if os.path.isfile(ls_path):
                try:
                    with open(ls_path, "r", encoding="utf-8", errors="replace") as handle:
                        ls_text = handle.read()
                    profiles_list = browser_sources.parse_profiles_from_local_state(ls_text)
                    last_used = browser_sources.last_used_profile(ls_text)
                except Exception:
                    pass

            if not profiles_list:
                if os.path.isdir(os.path.join(root, browser_sources.DEFAULT_PROFILE)):
                    profiles_list.append(browser_sources.DEFAULT_PROFILE)
                try:
                    for entry in sorted(os.listdir(root)):
                        if entry.startswith("Profile ") and os.path.isdir(os.path.join(root, entry)):
                            if entry not in profiles_list:
                                profiles_list.append(entry)
                except Exception:
                    pass

        source_entry: Dict[str, Any] = {
            "browser": family,
            "user_data_dir": root,
            "profiles": profiles_list,
            "last_used_profile": last_used,
            "binary_candidates": candidates,
            "installed": bool(root_exists or installed_binary is not None),
        }
        if installed_binary:
            source_entry["installed_binary"] = installed_binary
        sources.append(source_entry)

    seeds: List[Dict[str, Any]] = []
    now = time.time()
    for seed_name in profiles.list_seeds():
        try:
            seed_data = profiles.load_seed(seed_name)
        except Exception:
            continue
        refreshed_at = seed_data.get("refreshed_at")
        age_seconds: Optional[float] = None
        if refreshed_at is not None:
            try:
                age_seconds = max(0.0, round(now - float(refreshed_at), 1))
            except (ValueError, TypeError):
                pass
        seeds.append({
            "name": seed_name,
            "source_browser": seed_data.get("source_browser"),
            "source_dir": seed_data.get("source_dir"),
            "profile_subdir": seed_data.get("profile_subdir"),
            "refreshed_at": refreshed_at,
            "age_seconds": age_seconds,
            "defaults": seed_data.get("defaults", {}),
        })

    return {
        "status": "ok",
        "sources": sources,
        "seeds": seeds,
    }


def allowed_seed_source_roots(platform=None, env=None):
    """Return known browser user-data roots refresh_seed may copy from."""
    import sys
    plat = browser_sources.normalize_platform(platform or sys.platform)
    env = env if env is not None else os.environ
    roots = []
    for family in browser_sources.BROWSER_FAMILIES:
        try:
            roots.append(os.path.realpath(browser_sources.user_data_root(family, plat, env)))
        except Exception:
            continue
    return roots


def is_allowed_seed_source_dir(path, platform=None, env=None) -> bool:
    """True when path is a known browser profile root or a directory under one."""
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    for root in allowed_seed_source_roots(platform=platform, env=env):
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


GENERIC_PROFILE_ERROR = "Profile directory does not exist or is not allowed"


def is_safe_profile_arg(profile: Any) -> bool:
    """Check if profile argument is non-empty, relative, and has no path separators or '..'."""
    if not isinstance(profile, str) or not profile.strip():
        return False
    if "\x00" in profile:
        return False
    if os.path.isabs(profile) or profile.startswith("/") or profile.startswith("\\"):
        return False
    if len(profile) >= 2 and profile[1] == ":":
        return False
    if "/" in profile or "\\" in profile or os.path.sep in profile:
        return False
    if os.path.altsep and os.path.altsep in profile:
        return False
    if ".." in profile:
        return False
    if profile.strip() in (".", ".."):
        return False
    return True


def is_under_dir(child: str, parent: str) -> bool:
    """Require realpath child to stay strictly under realpath parent."""
    try:
        resolved_child = os.path.realpath(child)
        resolved_parent = os.path.realpath(parent)
        if resolved_child == resolved_parent:
            return False
        rel = os.path.relpath(resolved_child, resolved_parent)
        if rel.startswith("..") or rel == "." or os.path.isabs(rel):
            return False
        return resolved_child.startswith(resolved_parent + os.sep)
    except (ValueError, OSError):
        return False


def resolve_source_root(
    source_browser: str,
    source_dir: Optional[str] = None,
    platform: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve the user-data root for a given source_browser and source_dir."""
    if source_dir is not None:
        return os.path.abspath(source_dir)
    if (
        os.path.isdir(source_browser)
        or (os.path.sep in source_browser)
        or (os.path.altsep and os.path.altsep in source_browser)
    ):
        return os.path.abspath(source_browser)
    plat = browser_sources.normalize_platform(platform or sys.platform)
    family = source_browser.lower() if source_browser else "chrome"
    if family not in browser_sources.BROWSER_FAMILIES:
        return None
    try:
        return browser_sources.user_data_root(family, plat, env)
    except Exception:
        return None


def refresh_seed(
    name: str,
    source_browser: str,
    profile: Optional[str] = None,
    include_passwords: bool = False,
    include_cards: bool = False,
    indexeddb_cap_bytes: int = sm.DEFAULT_INDEXEDDB_CAP_BYTES,
    source_dir: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """Refresh or create a seed directory from a source browser profile.

    Builds a curated manifest plan, copies files under a per-seed FileLock,
    updates or creates the seed record, and returns a summary. The source
    directory is opened read-only and is never modified.

    Args:
        name: Validated seed name.
        source_browser: Browser family ("chrome", "edge", "brave") or directory path.
        profile: Profile subdirectory (e.g. "Default", "Profile 1"), or None to auto-detect.
        include_passwords: Whether to copy saved passwords. Defaults to False.
        include_cards: Whether to copy payment cards. Defaults to False.
        indexeddb_cap_bytes: Max bytes per IndexedDB origin. Defaults to 50 MB.
        source_dir: Optional user data directory override.
        env: Optional environment mapping for path resolution.
        platform: Optional platform override.

    Returns:
        Dict[str, Any]: Operation summary.
    """
    try:
        plat = browser_sources.normalize_platform(platform or sys.platform)
    except ValueError:
        return {
            "status": "error",
            "error": "Unsupported platform: {}".format(platform or sys.platform),
        }

    if plat != "linux":
        return {
            "status": "error",
            "error": "refresh_seed is currently only supported on Linux (platform: {})".format(plat),
        }

    try:
        profiles._check_name(name, "seed")
    except profiles.ProfileError as exc:
        return {
            "status": "error",
            "error": str(exc),
        }

    # Resolve source user-data-dir and browser family
    if source_dir is not None:
        resolved_source_dir = os.path.abspath(source_dir)
        source_family = source_browser.lower() if source_browser else "chrome"
    elif os.path.isdir(source_browser) or (os.path.sep in source_browser) or (os.path.altsep and os.path.altsep in source_browser):
        resolved_source_dir = os.path.abspath(source_browser)
        source_family = "chrome"
        for fam in browser_sources.BROWSER_FAMILIES:
            if fam in source_browser.lower():
                source_family = fam
                break
    else:
        source_family = source_browser.lower()
        if source_family not in browser_sources.BROWSER_FAMILIES:
            return {
                "status": "error",
                "error": "Unknown browser family: {}. Supported families: {}".format(
                    source_browser, ", ".join(browser_sources.BROWSER_FAMILIES)
                ),
            }
        try:
            resolved_source_dir = browser_sources.user_data_root(source_family, plat, env)
        except Exception as exc:
            return {
                "status": "error",
                "error": "Could not determine user data root for {}: {}".format(source_browser, exc),
            }

    if not os.path.isdir(resolved_source_dir):
        return {
            "status": "error",
            "error": "Source directory does not exist: {}".format(resolved_source_dir),
        }

    # Determine profile subdirectory
    profile_dir = None
    if profile is not None:
        if not is_safe_profile_arg(profile):
            return {
                "status": "error",
                "error": GENERIC_PROFILE_ERROR,
            }
        profile_subdir = profile
        profile_dir = os.path.join(resolved_source_dir, profile_subdir)
        resolved_root = os.path.realpath(resolved_source_dir)
        resolved_profile_dir = os.path.realpath(profile_dir)
        if (
            not is_under_dir(resolved_profile_dir, resolved_root)
            or not is_allowed_seed_source_dir(resolved_profile_dir, platform=platform, env=env)
            or not os.path.isdir(profile_dir)
        ):
            return {
                "status": "error",
                "error": GENERIC_PROFILE_ERROR,
            }
    else:
        ls_path = os.path.join(resolved_source_dir, browser_sources.LOCAL_STATE_FILE)
        profile_subdir = None
        if os.path.isfile(ls_path):
            try:
                with open(ls_path, "r", encoding="utf-8", errors="replace") as handle:
                    ls_text = handle.read()
                last_used = browser_sources.last_used_profile(ls_text)
                if last_used and is_safe_profile_arg(last_used) and os.path.isdir(os.path.join(resolved_source_dir, last_used)):
                    profile_subdir = last_used
                else:
                    for candidate in browser_sources.parse_profiles_from_local_state(ls_text):
                        if is_safe_profile_arg(candidate) and os.path.isdir(os.path.join(resolved_source_dir, candidate)):
                            profile_subdir = candidate
                            break
            except Exception:
                pass

        if not profile_subdir:
            if os.path.isdir(os.path.join(resolved_source_dir, browser_sources.DEFAULT_PROFILE)):
                profile_subdir = browser_sources.DEFAULT_PROFILE
            elif (os.path.exists(os.path.join(resolved_source_dir, "Cookies")) or
                  os.path.exists(os.path.join(resolved_source_dir, "Network")) or
                  os.path.exists(os.path.join(resolved_source_dir, "Preferences"))):
                profile_subdir = browser_sources.DEFAULT_PROFILE
                profile_dir = resolved_source_dir

        if profile_subdir and profile_dir is None:
            profile_dir = os.path.join(resolved_source_dir, profile_subdir)

        if not profile_dir or not os.path.isdir(profile_dir):
            return {
                "status": "error",
                "error": GENERIC_PROFILE_ERROR,
            }

        resolved_root = os.path.realpath(resolved_source_dir)
        resolved_profile_dir = os.path.realpath(profile_dir)
        is_under_or_root = (resolved_profile_dir == resolved_root) or is_under_dir(resolved_profile_dir, resolved_root)
        if not is_under_or_root:
            return {
                "status": "error",
                "error": GENERIC_PROFILE_ERROR,
            }
        if is_allowed_seed_source_dir(resolved_root, platform=platform, env=env) and not is_allowed_seed_source_dir(resolved_profile_dir, platform=platform, env=env):
            return {
                "status": "error",
                "error": GENERIC_PROFILE_ERROR,
            }

    options = sm.SeedOptions(
        include_passwords=include_passwords,
        include_cards=include_cards,
        indexeddb_cap_bytes=indexeddb_cap_bytes,
    )
    try:
        plan = sm.build_plan(profile_dir, options)
    except Exception as exc:
        return {
            "status": "error",
            "error": "Failed to build seed plan: {}".format(exc),
        }

    dest_path = profiles.seed_dir(name)
    dest_dir = str(dest_path)

    # Check whether the seed record already exists before copy_seed swaps dest_dir
    had_existing_seed = profiles.seed_exists(name)
    existing_seed_data: Dict[str, Any] = {}
    if had_existing_seed:
        try:
            existing_seed_data = profiles.load_seed(name)
        except Exception:
            existing_seed_data = {}

    copy_result = sc.copy_seed(plan, profile_dir, dest_dir)
    if copy_result.status == "source_busy":
        return {
            "status": "source_busy",
            "seed": name,
            "source_browser": source_family,
            "profile": profile_subdir,
            "copied_count": 0,
            "copied": 0,
            "skipped_busy": copy_result.skipped_busy,
            "errors": copy_result.errors,
            "bytes": 0,
        }

    if copy_result.status != "ok":
        return {
            "status": copy_result.status,
            "seed": name,
            "source_browser": source_family,
            "profile": profile_subdir,
            "copied_count": len(copy_result.copied),
            "copied": len(copy_result.copied),
            "skipped_busy": copy_result.skipped_busy,
            "errors": copy_result.errors,
            "bytes": copy_result.bytes,
        }

    lock = FileLock(dest_dir, timeout=10.0)
    try:
        with lock:
            now_ts = time.time()
            if had_existing_seed:
                existing_seed_data.update({
                    "source_browser": source_family,
                    "source_dir": resolved_source_dir,
                    "profile_subdir": profile_subdir,
                    "refreshed_at": now_ts,
                })
                profiles.save_seed(name, existing_seed_data)
            else:
                profiles.create_seed(
                    name=name,
                    source_browser=source_family,
                    source_dir=resolved_source_dir,
                    profile_subdir=profile_subdir,
                    now=now_ts,
                )

            # Copy Local State from the user-data root if present
            ls_source = os.path.join(resolved_source_dir, browser_sources.LOCAL_STATE_FILE)
            if not os.path.isfile(ls_source) and os.path.isfile(os.path.join(profile_dir, browser_sources.LOCAL_STATE_FILE)):
                ls_source = os.path.join(profile_dir, browser_sources.LOCAL_STATE_FILE)
            ls_dest = os.path.join(dest_dir, browser_sources.LOCAL_STATE_FILE)
            if os.path.isfile(ls_source) and not os.path.exists(ls_dest):
                try:
                    shutil.copy2(ls_source, ls_dest)
                except Exception:
                    pass
    except Exception as exc:
        return {
            "status": "error",
            "error": "Failed to update seed metadata: {}".format(exc),
            "seed": name,
            "copied_count": len(copy_result.copied),
            "copied": len(copy_result.copied),
            "skipped_busy": copy_result.skipped_busy,
            "bytes": copy_result.bytes,
        }

    return {
        "status": "ok",
        "seed": name,
        "source_browser": source_family,
        "profile": profile_subdir,
        "copied_count": len(copy_result.copied),
        "copied": len(copy_result.copied),
        "skipped_busy": copy_result.skipped_busy,
        "bytes": copy_result.bytes,
    }
