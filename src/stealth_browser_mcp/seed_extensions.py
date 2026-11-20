"""Extension handling and path fixup for cloned browser profiles (SB-50).

When Chrome loads unpacked extensions (location 4), it stores absolute paths
in the profile Preferences and Secure Preferences files under extensions.settings.
When a seed profile is cloned into an isolated session directory, copied unpacked
extensions reside inside the clone, but their recorded paths still point to the
source profile.

Limitation and Design Note:
Chrome validates 'Secure Preferences' using HMAC signatures derived from machine
and profile identity. Editing 'Secure Preferences' directly invalidates its HMAC,
prompting Chrome to discard settings with a corruption warning. Therefore, this
module NEVER edits 'Secure Preferences' (it remains untouched byte-for-byte).
Instead, this module rewrites paths in plain 'Preferences' only, and additionally
passes '--load-extension=<clone path>' for unpacked extensions found so Chrome
reliably loads them from their cloned location.

No print() calls exist in this module (stdio MCP transport). ASCII only.
"""

import json
import os
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union


class ExtensionFixupReport(dict):
    """Report describing extension fixups applied to a cloned profile.

    Supports both dictionary indexing and property attribute access.
    """

    def __init__(
        self,
        fixed_count: int = 0,
        load_extension_args: Optional[List[str]] = None,
        unpacked_extensions: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        args = list(load_extension_args or [])
        exts = list(unpacked_extensions or [])
        super().__init__(
            fixed_count=fixed_count,
            load_extension_args=args,
            unpacked_extensions=exts,
            **kwargs,
        )

    @property
    def fixed_count(self) -> int:
        return int(self.get("fixed_count", 0))

    @property
    def load_extension_args(self) -> List[str]:
        return list(self.get("load_extension_args", []))

    @property
    def unpacked_extensions(self) -> List[str]:
        return list(self.get("unpacked_extensions", []))


def clone_has_extensions(profile_dir: Union[str, Path]) -> bool:
    """Return True if the cloned profile directory contains extension resources.

    Checks for the presence of 'Extensions', 'Local Extension Settings', or
    'Sync Extension Settings' folders, or extension settings entries in
    'Preferences' or 'Secure Preferences'.
    """
    pdir = Path(profile_dir)
    check_dirs = [pdir, pdir / "Default"]

    for d in check_dirs:
        if (d / "Extensions").is_dir():
            return True
        if (d / "Local Extension Settings").is_dir():
            return True
        if (d / "Sync Extension Settings").is_dir():
            return True

        for fname in ("Preferences", "Secure Preferences"):
            pref_path = d / fname
            if pref_path.is_file():
                try:
                    text = pref_path.read_text(encoding="utf-8", errors="replace")
                    data = json.loads(text)
                    if isinstance(data, dict):
                        ext_settings = data.get("extensions", {}).get("settings")
                        if isinstance(ext_settings, dict) and len(ext_settings) > 0:
                            return True
                except Exception:
                    pass

    return False


def _find_relative_subpath(raw_path: str, candidates: List[Path]) -> Optional[str]:
    """Return the relative path if raw_path is inside any base candidate."""
    if not raw_path or not isinstance(raw_path, str):
        return None
    try:
        real_ext = os.path.realpath(os.path.abspath(raw_path))
    except Exception:
        return None

    for base in candidates:
        try:
            real_base = os.path.realpath(os.path.abspath(str(base)))
            rel = os.path.relpath(real_ext, real_base)
            if not rel.startswith("..") and not os.path.isabs(rel) and rel != ".":
                return rel
            norm_ext = os.path.normcase(real_ext)
            norm_base = os.path.normcase(real_base)
            norm_rel = os.path.relpath(norm_ext, norm_base)
            if not norm_rel.startswith("..") and not os.path.isabs(norm_rel) and norm_rel != ".":
                return rel
        except (ValueError, OSError):
            continue
    return None


def fixup_extension_paths(
    session_profile_dir: Union[str, Path],
    source_profile_dir: Optional[Union[str, Path]] = None,
) -> ExtensionFixupReport:
    """Rewrite unpacked extension paths in Preferences and build --load-extension args.

    Parses the clone's Preferences and Secure Preferences 'extensions.settings'
    entries whose 'path' points into the source profile (location 4 = unpacked).
    Rewrites matching paths in plain Preferences to point to the cloned session
    location. Does NOT edit Secure Preferences (byte-for-byte untouched).
    Returns an ExtensionFixupReport containing fixed_count and load_extension_args.

    Args:
        session_profile_dir: Cloned session profile directory.
        source_profile_dir: Source profile directory where extensions originated.

    Returns:
        ExtensionFixupReport: Report with fixed_count and load_extension_args.
    """
    session_base = Path(os.path.abspath(str(session_profile_dir)))

    # Locate effective profile directory containing Preferences
    eff_session_pdir = session_base
    if not (session_base / "Preferences").is_file() and not (session_base / "Secure Preferences").is_file():
        if (session_base / "Default" / "Preferences").is_file() or (session_base / "Default" / "Secure Preferences").is_file():
            eff_session_pdir = session_base / "Default"

    # Build source candidate bases
    src_candidates: List[Path] = []
    if source_profile_dir is not None:
        src_base = Path(os.path.abspath(str(source_profile_dir)))
        src_candidates.append(src_base)
        if (src_base / "Default").is_dir():
            src_candidates.append(src_base / "Default")
        if src_base.parent not in src_candidates:
            src_candidates.append(src_base.parent)

    unpacked_paths: List[str] = []
    fixed_count = 0

    # 1. Inspect and rewrite plain Preferences
    pref_file = eff_session_pdir / "Preferences"
    if pref_file.is_file():
        try:
            pref_text = pref_file.read_text(encoding="utf-8", errors="replace")
            pref_data = json.loads(pref_text)
        except Exception:
            pref_data = None

        if isinstance(pref_data, dict):
            pref_modified = False
            settings = pref_data.get("extensions", {}).get("settings")
            if isinstance(settings, dict):
                for _ext_id, entry in settings.items():
                    if not isinstance(entry, dict):
                        continue
                    raw_path = entry.get("path")
                    loc = entry.get("location")
                    is_unpacked = loc in (4, "4") or (
                        loc is None and isinstance(raw_path, str) and os.path.isabs(raw_path)
                    )
                    if not is_unpacked or not isinstance(raw_path, str):
                        continue

                    rel = _find_relative_subpath(raw_path, src_candidates)
                    if rel is not None:
                        clone_target = os.path.normpath(os.path.join(str(eff_session_pdir), rel))
                        entry["path"] = clone_target
                        pref_modified = True
                        fixed_count += 1
                        if clone_target not in unpacked_paths:
                            unpacked_paths.append(clone_target)
                    elif not os.path.exists(raw_path):
                        # Fallback: check if subpath exists under clone
                        target_name = Path(raw_path).name
                        local_cand = eff_session_pdir / "Extensions" / target_name
                        if local_cand.is_dir():
                            clone_target = os.path.normpath(str(local_cand))
                            entry["path"] = clone_target
                            pref_modified = True
                            fixed_count += 1
                            if clone_target not in unpacked_paths:
                                unpacked_paths.append(clone_target)

            if pref_modified:
                pref_file.write_text(
                    json.dumps(pref_data, indent=2, ensure_ascii=True) + "\n",
                    encoding="ascii",
                )

    # 2. Inspect Secure Preferences WITHOUT modifying it (read-only for HMAC safety)
    sec_pref_file = eff_session_pdir / "Secure Preferences"
    if sec_pref_file.is_file():
        try:
            sec_text = sec_pref_file.read_text(encoding="utf-8", errors="replace")
            sec_data = json.loads(sec_text)
        except Exception:
            sec_data = None

        if isinstance(sec_data, dict):
            sec_settings = sec_data.get("extensions", {}).get("settings")
            if isinstance(sec_settings, dict):
                for _ext_id, entry in sec_settings.items():
                    if not isinstance(entry, dict):
                        continue
                    raw_path = entry.get("path")
                    loc = entry.get("location")
                    is_unpacked = loc in (4, "4") or (
                        loc is None and isinstance(raw_path, str) and os.path.isabs(raw_path)
                    )
                    if not is_unpacked or not isinstance(raw_path, str):
                        continue

                    rel = _find_relative_subpath(raw_path, src_candidates)
                    if rel is not None:
                        clone_target = os.path.normpath(os.path.join(str(eff_session_pdir), rel))
                        if clone_target not in unpacked_paths:
                            unpacked_paths.append(clone_target)
                    elif not os.path.exists(raw_path):
                        target_name = Path(raw_path).name
                        local_cand = eff_session_pdir / "Extensions" / target_name
                        if local_cand.is_dir():
                            clone_target = os.path.normpath(str(local_cand))
                            if clone_target not in unpacked_paths:
                                unpacked_paths.append(clone_target)

    # Build --load-extension argument if unpacked extensions were found
    load_extension_args: List[str] = []
    if unpacked_paths:
        load_extension_args.append(f"--load-extension={','.join(unpacked_paths)}")

    return ExtensionFixupReport(
        fixed_count=fixed_count,
        load_extension_args=load_extension_args,
        unpacked_extensions=unpacked_paths,
    )
