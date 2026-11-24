"""Seed manifest: include/exclude rules for a browser profile directory.

A seed copy is a curated snapshot of a real browser profile (Alan's profile A)
that is safe to hand to a clone. This module decides, by name, which entries of
a profile directory tree to include and which to skip. It never reads a real
profile's secrets: it only walks names and reports sizes, so it is pure and safe
to unit test against a synthetic tree.

The decision table (see :data:`INCLUDE_RULES`) names the folders Chrome writes
that carry a session a clone wants to keep. Everything else is excluded, and the
exclusion carries a reason so a caller can explain a plan to an operator.

Privacy defaults follow PLAN decision D6: saved passwords and payment data
(:file:`Web Data`) are excluded by default. They are included only when the
caller opts in.
"""

import os
from dataclasses import dataclass
from typing import List, NamedTuple, Optional

# Default per-origin cap for a single IndexedDB subdirectory. Chrome writes one
# directory per origin under IndexedDB/; a heavy site can hold hundreds of MB.
DEFAULT_INDEXEDDB_CAP_BYTES = 50 * 1024 * 1024  # 50 MB

# Actions a plan entry can take.
INCLUDE = "include"
EXCLUDE = "exclude"


@dataclass(frozen=True)
class SeedOptions:
    """Toggles that change which profile entries the manifest includes.

    Opt-ins default off (PLAN decision D6). ``indexeddb_cap_bytes`` bounds how
    large any one IndexedDB subdirectory may be before it is skipped.
    """

    # Include saved login credentials (file "Login Data", "Login Data-Journal").
    include_passwords: bool = False
    # Include saved payment details (file "Web Data", "Web Data-Journal").
    include_cards: bool = False
    # Skip any single IndexedDB subdirectory larger than this many bytes.
    indexeddb_cap_bytes: int = DEFAULT_INDEXEDDB_CAP_BYTES


class PlanEntry(NamedTuple):
    """One decision in a build plan.

    ``rel_path`` is relative to the profile root and uses forward slashes so the
    plan reads the same on every OS. ``action`` is ``include`` or ``exclude``.
    ``reason`` explains the choice (an empty string for an include). ``size`` is
    the entry's size in bytes.
    """

    rel_path: str
    action: str
    reason: str
    size: int
    nested: Optional[List["PlanEntry"]] = None

    @property
    def included(self) -> bool:
        return self.action == INCLUDE


class SeedPlan(NamedTuple):
    """A full plan over a profile directory: entries plus totals.

    ``entries`` lists one decision per top-level profile entry. ``included`` and
    ``excluded`` partition those by action. ``total_size`` is the number of
    bytes the included entries represent, which is the seed's size after the
    manifest runs.
    """

    entries: List[PlanEntry]
    included: List[PlanEntry]
    excluded: List[PlanEntry]
    total_size: int


# Explicitly included folders, matched by their position at the profile root.
# Values are the plan reason attached to each, kept short and stable.
INCLUDE_RULES = {
    "Cookies": ("cookie store",),
    "Cookies-journal": ("cookie store",),
    "Network": ("cookie store",),  # holds Network/Cookies on modern Chrome
    "Local Storage": ("local storage",),
    "IndexedDB": ("indexed storage",),
    "Session Storage": ("session storage",),
    "Preferences": ("browser settings",),
    "Secure Preferences": ("browser settings",),
    "Extensions": ("extensions",),
    "Extension State": ("extension state",),
    "Local Extension Settings": ("extension settings",),
    "Sync Extension Settings": ("extension sync settings",),
}

# Explicitly excluded folders, matched by name or by prefix. Values are the
# plan reason attached to each.
EXCLUDE_RULES = {
    "exact": {
        "Cache": "cache",
        "Code Cache": "cache",
        "GPUCache": "cache",
        "DawnCache": "cache",
        "GrShaderCache": "cache",
        "ShaderCache": "cache",
        "Media Cache": "cache",
        "Service Worker": "cache",
        "Crashpad": "crash dumps",
        "component_crx_cache": "extension cache",
        "blob_storage": "blob storage",
        "OptGuideOnDeviceModel": "on-device models",
    },
    "prefix": {
        "optimization_guide": "optimization cache",
        "BrowserMetrics": "metrics",
    },
}


def _rel(path: str) -> str:
    """Return ``path`` relative to ``profile_root`` using forward slashes."""
    rel = os.path.relpath(path, PROFILE_ROOT)
    return rel.replace(os.sep, "/")


def _entry_size(path: str) -> int:
    """Return the size in bytes of ``path`` (a file, or a tree total)."""
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                continue
    return total


def _size_of_indexeddb(indexeddb_path: str) -> int:
    """Return the size of a single IndexedDB origin subdirectory.

    Chrome stores one directory per origin under ``IndexedDB/``. The cap applies
    to each origin directory separately.
    """
    if not os.path.isdir(indexeddb_path):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(indexeddb_path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def _match_exact(name: str) -> str:
    for match, reason in EXCLUDE_RULES["exact"].items():
        if name == match:
            return reason
    return ""


def _match_prefix(name: str) -> str:
    for prefix, reason in EXCLUDE_RULES["prefix"].items():
        if name.startswith(prefix + "/") or name == prefix:
            return reason
    return ""


def _include_reason(name: str) -> str:
    """Return the include reason for a top-level entry name, or "" if none."""
    for entry_name, reasons in INCLUDE_RULES.items():
        if name == entry_name:
            return reasons[0]
    return ""


def _plan_entry(path: str, name: str) -> PlanEntry:
    """Classify one top-level ``name`` under ``profile_root``."""
    # Passwords and payment data: excluded by default, included only when opted
    # in. Both carry real credentials, so the default is to skip them.
    if name in ("Login Data", "Login Data-Journal", "Login Data-Flushed",
                "Login Data-Shm"):
        if _options.include_passwords:
            return PlanEntry(_rel(path), INCLUDE, "login credentials", _entry_size(path))
        return PlanEntry(_rel(path), EXCLUDE, "passwords", _entry_size(path))

    if name in ("Web Data", "Web Data-Journal", "Web Data-Flushed", "Web Data-Shm"):
        if _options.include_cards:
            return PlanEntry(_rel(path), INCLUDE, "payment data", _entry_size(path))
        return PlanEntry(_rel(path), EXCLUDE, "payment data", _entry_size(path))

    # An explicit exclude beats an include: caches and model dirs are named for
    # what they are, not for any session value.
    exact = _match_exact(name)
    if exact:
        return PlanEntry(_rel(path), EXCLUDE, exact, _entry_size(path))
    prefix = _match_prefix(name)
    if prefix:
        return PlanEntry(_rel(path), EXCLUDE, prefix, _entry_size(path))

    include = _include_reason(name)
    if include:
        if name == "IndexedDB":
            cap = _options.indexeddb_cap_bytes
            origins = sorted(os.listdir(path))
            nested_entries = []
            total = 0
            skipped = False
            skipped_names = []
            for origin in origins:
                origin_path = os.path.join(path, origin)
                if not os.path.isdir(origin_path):
                    continue
                size = _size_of_indexeddb(origin_path)
                rel = _rel(os.path.join(path, origin))
                if size > cap:
                    nested_entries.append(
                        PlanEntry(rel, EXCLUDE,
                                  "indexeddb_cap ({} bytes)".format(size), size)
                    )
                    skipped = True
                    skipped_names.append(origin)
                else:
                    nested_entries.append(
                        PlanEntry(rel, INCLUDE, include, size)
                    )
                    total += size
            if skipped:
                # List the origins individually so an operator sees exactly
                # which origin(s) were dropped by the cap; only the kept
                # origins count toward the plan total. The parent reason names
                # every skipped origin.
                reason = "indexeddb_cap ({})".format(", ".join(skipped_names))
                return PlanEntry(_rel(path), INCLUDE, reason, total,
                                 nested=nested_entries)
            return PlanEntry(_rel(path), INCLUDE, include, _entry_size(path))
        return PlanEntry(_rel(path), INCLUDE, include, _entry_size(path))

    # Anything else is not part of the curated seed.
    return PlanEntry(_rel(path), EXCLUDE, "not_in_manifest", _entry_size(path))


# Set by build_plan so the name-based helpers above stay free of a module-level
# root binding, which keeps the module importable without side effects.
PROFILE_ROOT = None
_options = SeedOptions()


def build_plan(profile_dir: str, options: Optional[SeedOptions] = None) -> SeedPlan:
    """Build a seed manifest plan over ``profile_dir``.

    Walks the top-level entries of ``profile_dir`` and classifies each by name
    using the include/exclude table. Sub-entries (IndexedDB origins, caches) are
    folded into their parent's reported size rather than listed separately, so
    the plan lists one decision per profile root entry.

    Args:
        profile_dir: Path to a profile directory tree. Never a real user profile
            is required; a synthetic tree is fine and is what tests use.
        options: Manifest toggles (opt-ins and the IndexedDB cap). Defaults to
            :data:`SeedOptions` with opt-ins off.

    Returns:
        A :class:`SeedPlan` with ``entries`` (one per root entry), the
        ``included`` and ``excluded`` partitions, and ``total_size`` bytes of
        the included entries.

    Raises:
        NotADirectoryError: If ``profile_dir`` is not a directory.
    """
    global PROFILE_ROOT, _options
    if not os.path.isdir(profile_dir):
        raise NotADirectoryError("not a directory: {}".format(profile_dir))

    saved_root = PROFILE_ROOT
    saved_options = _options
    PROFILE_ROOT = profile_dir
    _options = options if options is not None else SeedOptions()

    try:
        names = sorted(os.listdir(profile_dir))
        entries = [_plan_entry(os.path.join(profile_dir, name), name) for name in names]
    finally:
        PROFILE_ROOT = saved_root
        _options = saved_options

    included = [e for e in entries if e.included]
    excluded = [e for e in entries if not e.included]
    total = sum(e.size for e in included)
    return SeedPlan(entries, included, excluded, total)
