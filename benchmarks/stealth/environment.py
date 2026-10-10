"""Runner labels and environment metadata for benchmark runs."""

import os
import platform
import re
import sys
from importlib import metadata
from typing import Any, Dict, Optional

OS_NAMES = {"windows": "windows", "darwin": "macos", "linux": "linux"}
ARCH_NAMES = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
LABEL_SAFE = re.compile(r"[^a-z0-9.-]+")


def os_name() -> str:
    """
    Short operating system name.

    Returns:
        str: windows, macos, linux, or the lowercase platform name
    """
    system = platform.system().lower()
    return OS_NAMES.get(system, system)


def arch_name() -> str:
    """
    Short CPU architecture name.

    Returns:
        str: x64, arm64, or the lowercase machine name
    """
    machine = platform.machine().lower()
    return ARCH_NAMES.get(machine, machine)


def runner_label(mode: str, channel: str, label: Optional[str] = None) -> str:
    """
    Build the runner label used in result file names.

    Args:
        mode (str): headless or headed
        channel (str): Chrome channel label, for example stable or previous
        label (Optional[str]): Explicit label that overrides the generated one

    Returns:
        str: Lowercase label such as linux-x64-headless-stable
    """
    raw = label or f"{os_name()}-{arch_name()}-{mode}-{channel}"
    return LABEL_SAFE.sub("-", raw.lower()).strip("-")


def package_version(name: str) -> Optional[str]:
    """
    Installed version of a Python package.

    Args:
        name (str): Distribution name

    Returns:
        Optional[str]: Version string, or None when not installed
    """
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def ci_metadata() -> Dict[str, Any]:
    """
    GitHub Actions run metadata, when running in Actions.

    Returns:
        Dict[str, Any]: Run id, URL, and commit, or an empty dict outside Actions
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return {}
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return {
        "provider": "github-actions",
        "run_id": run_id,
        "run_url": f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else None,
        "commit": os.environ.get("GITHUB_SHA"),
        "event": os.environ.get("GITHUB_EVENT_NAME"),
        "runner_image": os.environ.get("ImageOS"),
    }


def base_environment(mode: str, channel: str) -> Dict[str, Any]:
    """
    Environment metadata known before the browser starts.

    Args:
        mode (str): headless or headed
        channel (str): Chrome channel label

    Returns:
        Dict[str, Any]: Platform, Python, package, and CI details
    """
    return {
        "os": os_name(),
        "os_release": platform.platform(),
        "arch": arch_name(),
        "python": sys.version.split()[0],
        "nodriver": package_version("nodriver"),
        "mode": mode,
        "chrome_channel": channel,
        "ci": ci_metadata(),
    }
