"""Discover browser profile source roots and their profile lists.

A source browser (Alan's real profile A) is read to learn which profiles it
holds. This module is pure: given a platform string and an environment
mapping it builds the user-data roots and parses ``Local State`` for the
profile subdirectories. It never launches a browser and never reads a real
profile's secrets.

Supported families are Chrome, Edge, and Brave, on Linux and Windows. macOS
roots are listed but optional; the path builder works for them too.
"""

import json
import ntpath
import os
import posixpath
from typing import Dict, List, Mapping, Optional, Tuple

# Browser families this slice knows how to locate and launch. The order is the
# launch preference: Chrome first, then Edge, then Brave.
BROWSER_FAMILIES = ("chrome", "edge", "brave")

# User-data roots per platform and family. Each value is the folder that holds
# the per-profile subdirectories (Default, Profile 1, ...), the Local State
# file, and the encrypted stores.
#
#   | Browser | Linux | Windows |
#   |---|---|---|
#   | Chrome | ~/.config/google-chrome | %LOCALAPPDATA%\Google\Chrome\User Data |
#   | Edge | ~/.config/microsoft-edge | %LOCALAPPDATA%\Microsoft\Edge\User Data |
#   | Brave | ~/.config/BraveSoftware/Brave-Browser | %LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data |
_ROOTS: Dict[str, Dict[str, str]] = {
    "linux": {
        "chrome": ".config/google-chrome",
        "edge": ".config/microsoft-edge",
        "brave": ".config/BraveSoftware/Brave-Browser",
    },
    "windows": {
        "chrome": "Google\\Chrome\\User Data",
        "edge": "Microsoft\\Edge\\User Data",
        "brave": "BraveSoftware\\Brave-Browser\\User Data",
    },
    "darwin": {
        "chrome": "Library/Application Support/Google/Chrome",
        "edge": "Library/Application Support/Microsoft Edge",
        "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
    },
}

# Profile subdirectory names Chrome writes into Local State, in order of use.
DEFAULT_PROFILE = "Default"

# File that lists every profile a browser profile directory contains.
LOCAL_STATE_FILE = "Local State"


def user_data_root(browser_family: str, platform: str, env: Optional[Mapping[str, str]]) -> str:
    """Return the user-data root for ``browser_family`` on ``platform``.

    The env mapping supplies the platform prefix: ``HOME`` on Linux and macOS,
    ``LOCALAPPDATA`` on Windows. Tests pass fake values so no real profile is
    ever touched.

    The per-family relative paths use backslashes for Windows (the OS-native
    separator) and forward slashes elsewhere; they are normalised to the host
    separator so the result can drive real filesystem operations in tests.

    Args:
        browser_family: One of the families in ``BROWSER_FAMILIES``.
        platform: A platform string accepted by :func:`normalize_platform`.
        env: Environment mapping. ``None`` falls back to ``os.environ``.

    Returns:
        The absolute-ish root path of the browser's user-data directory.

    Raises:
        ValueError: If ``browser_family`` is unknown for this platform.
    """
    platform = normalize_platform(platform)
    env = env if env is not None else os.environ
    roots = _ROOTS.get(platform)
    if not roots:
        raise ValueError("unsupported platform: {}".format(platform))
    if browser_family not in roots:
        raise ValueError("browser family not supported on {}: {}".format(platform, browser_family))

    base = env["LOCALAPPDATA"] if platform == "windows" else env["HOME"]
    path_mod = ntpath if platform == "windows" else posixpath
    relative = roots[browser_family].replace("/", "\\") if platform == "windows" else roots[browser_family].replace("\\", "/")
    return path_mod.join(base, relative)


def local_state_path(browser_family: str, platform: str, env: Optional[Mapping[str, str]]) -> str:
    """Return the path of the ``Local State`` file under the user-data root."""
    platform = normalize_platform(platform)
    path_mod = ntpath if platform == "windows" else posixpath
    return path_mod.join(user_data_root(browser_family, platform, env), LOCAL_STATE_FILE)


def source_path(browser_family: str, profile_subdir: str, platform: str,
                env: Optional[Mapping[str, str]]) -> str:
    """Return the full path of one profile directory inside the user-data root.

    ``profile_subdir`` is a name found in ``Local State`` such as ``Default`` or
    ``Profile 1``. On Windows the OS separator is backslash, but Chrome stores
    subdirectory names with forward slashes, so they are normalised here to stay
    consistent when listed later.

    Args:
        browser_family: One of the families in ``BROWSER_FAMILIES``.
        profile_subdir: Profile folder name (e.g. ``Default``).
        platform: A platform string accepted by :func:`normalize_platform`.
        env: Environment mapping. ``None`` falls back to ``os.environ``.

    Returns:
        The absolute-ish path of the profile directory.
    """
    platform = normalize_platform(platform)
    path_mod = ntpath if platform == "windows" else posixpath
    root = user_data_root(browser_family, platform, env)
    if platform == "windows":
        normalized = profile_subdir.replace("/", "\\")
    else:
        normalized = profile_subdir.replace("\\", "/")
    return path_mod.join(root, normalized)


def normalize_platform(platform: str) -> str:
    """Normalise a platform string to ``linux``, ``windows``, or ``darwin``.

    Accepts the values :func:`platform.system` may return (e.g. ``Linux``,
    ``Windows``, ``Darwin``) as well as the ``sys.platform`` spellings
    (``linux``, ``win32``, ``darwin``) so tests can pass either.
    """
    key = (platform or "").strip().lower()
    if key == "win32" or key.startswith("windows"):
        return "windows"
    if key in ("linux", "linux2"):
        return "linux"
    if key in ("darwin", "macos", "macosx"):
        return "darwin"
    raise ValueError("unsupported platform: {}".format(platform))


def parse_profiles_from_local_state(text: Optional[str]) -> List[str]:
    """Return the profile subdirectory names listed in ``Local State`` JSON.

    A browser writes the directory name of every profile into
    ``profile.info_cache[].name``. This returns those names in order. It is
    tolerant: malformed or missing JSON yields an empty list instead of raising.

    Args:
        text: The raw contents of the ``Local State`` file.

    Returns:
        A list of profile subdirectory names (e.g. ``["Default", "Profile 1"]``).
    """
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    info_cache = data.get("profile", {}).get("info_cache")
    if not isinstance(info_cache, list):
        return []
    names = []
    for entry in info_cache:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def last_used_profile(local_state_text: Optional[str]) -> Optional[str]:
    """Return the profile name most recently used, or None when unknown.

    Chrome records ``profile.last_used`` as an integer index into
    ``profile.info_cache``; the corresponding entry's ``name`` field is the
    profile directory. This resolves that index to a name, tolerating out of
    range or malformed JSON.
    """
    if not local_state_text:
        return None
    try:
        data = json.loads(local_state_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    profile = data.get("profile")
    if not isinstance(profile, dict):
        return None
    info_cache = profile.get("info_cache")
    if not isinstance(info_cache, list) or not info_cache:
        return None
    last_used = profile.get("last_used")
    if not isinstance(last_used, int) or isinstance(last_used, bool):
        return None
    if last_used < 0 or last_used >= len(info_cache):
        return None
    entry = info_cache[last_used]
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def binary_candidates(browser_family: str) -> List[str]:
    """Return the executable candidates for ``browser_family`` in order.

    The list is the launch preference order for the family. Brave is added
    alongside Chrome and Edge; see :func:`platform_utils.check_browser_executable`
    for the same candidates plus system search.

    Args:
        browser_family: One of the families in ``BROWSER_FAMILIES``.

    Returns:
        A list of executable file names to search for.
    """
    if browser_family == "chrome":
        return ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    if browser_family == "edge":
        return ["microsoft-edge-stable", "microsoft-edge", "msedge"]
    if browser_family == "brave":
        return ["brave-browser", "brave"]
    raise ValueError("unknown browser family: {}".format(browser_family))
