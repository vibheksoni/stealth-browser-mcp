"""Downloads Chrome for Testing builds so runs can target a specific milestone."""

import io
import os
import platform
import stat
import zipfile
from pathlib import Path
from typing import Any, Dict, Tuple

import requests

LAST_KNOWN_GOOD_URL = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json"
MILESTONES_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json"
)
EXECUTABLES = {
    "linux64": "chrome-linux64/chrome",
    "win64": "chrome-win64/chrome.exe",
    "win32": "chrome-win32/chrome.exe",
    "mac-arm64": "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "mac-x64": "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
}
REQUEST_TIMEOUT = 60


def cft_platform() -> str:
    """
    Chrome for Testing platform name for this machine.

    Returns:
        str: linux64, win64, win32, mac-arm64, or mac-x64

    Raises:
        RuntimeError: When the platform has no Chrome for Testing build
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in ("x86_64", "amd64"):
        return "linux64"
    if system == "windows":
        return "win64" if machine.endswith("64") else "win32"
    if system == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    raise RuntimeError(f"No Chrome for Testing build for {system} {machine}")


def resolve_milestone(spec: str, stable_version: str) -> int:
    """
    Turn a milestone spec into a milestone number.

    Args:
        spec (str): stable, previous, or a milestone number such as 153
        stable_version (str): Current stable version such as 154.0.7000.0

    Returns:
        int: Milestone number

    Raises:
        ValueError: When the spec is not recognized
    """
    stable = int(stable_version.split(".", 1)[0])
    if spec == "stable":
        return stable
    if spec == "previous":
        return stable - 1
    if spec.isdigit():
        return int(spec)
    raise ValueError(f"Milestone must be stable, previous, or a number, got {spec!r}")


def download_url(milestones: Dict[str, Any], milestone: int, platform_name: str) -> Tuple[str, str]:
    """
    Find the Chrome download for a milestone and platform.

    Args:
        milestones (Dict[str, Any]): Parsed latest-versions-per-milestone-with-downloads.json
        milestone (int): Milestone number
        platform_name (str): Chrome for Testing platform name

    Returns:
        Tuple[str, str]: Full version and zip URL

    Raises:
        LookupError: When the milestone or platform is not published
    """
    entry = (milestones.get("milestones") or {}).get(str(milestone))
    if not entry:
        raise LookupError(f"Chrome for Testing has no milestone {milestone}")
    for download in (entry.get("downloads") or {}).get("chrome", []):
        if download.get("platform") == platform_name:
            return entry["version"], download["url"]
    raise LookupError(f"Chrome {milestone} has no {platform_name} download")


def extract_zip(data: bytes, destination: Path) -> None:
    """
    Extract a zip archive and keep Unix permission bits.

    Args:
        data (bytes): Zip file content
        destination (Path): Directory to extract into
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            target = destination / member.filename
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError(f"Refusing to extract {member.filename} outside {destination}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(archive.read(member).decode("utf-8"), target)
                continue
            archive.extract(member, destination)
            if mode and not member.is_dir():
                os.chmod(target, stat.S_IMODE(mode))


def install_chrome(spec: str, destination: Path) -> Tuple[str, Path]:
    """
    Download and unpack a Chrome for Testing build.

    Args:
        spec (str): stable, previous, or a milestone number
        destination (Path): Directory to unpack into

    Returns:
        Tuple[str, Path]: Installed version and path to the browser executable
    """
    session = requests.Session()
    stable = session.get(LAST_KNOWN_GOOD_URL, timeout=REQUEST_TIMEOUT)
    stable.raise_for_status()
    stable_version = stable.json()["channels"]["Stable"]["version"]
    milestone = resolve_milestone(spec, stable_version)
    milestones = session.get(MILESTONES_URL, timeout=REQUEST_TIMEOUT)
    milestones.raise_for_status()
    platform_name = cft_platform()
    version, url = download_url(milestones.json(), milestone, platform_name)
    archive = session.get(url, timeout=REQUEST_TIMEOUT * 5)
    archive.raise_for_status()
    install_dir = destination / version
    install_dir.mkdir(parents=True, exist_ok=True)
    extract_zip(archive.content, install_dir)
    executable = install_dir / EXECUTABLES[platform_name]
    if not executable.is_file():
        raise RuntimeError(f"Chrome executable missing after extraction: {executable}")
    return version, executable
