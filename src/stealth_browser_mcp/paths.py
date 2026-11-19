"""Default locations for files the server reads and writes."""

import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "stealth-browser-mcp"
DATA_DIR_ENV = "STEALTH_BROWSER_MCP_DATA_DIR"
PACKAGE_DIR = Path(__file__).resolve().parent


def source_checkout_root() -> Optional[Path]:
    """
    Return the repository root when running from a source checkout.

    Returns:
        Optional[Path]: Repository root, or None for an installed package.
    """
    root = PACKAGE_DIR.parent.parent
    if PACKAGE_DIR.parent.name == "src" and (root / "pyproject.toml").is_file():
        return root
    return None


def user_data_dir() -> Path:
    """
    Return the per-user data directory for this platform.

    Returns:
        Path: LOCALAPPDATA on Windows, Application Support on macOS, and
        XDG_DATA_HOME or ~/.local/share elsewhere, joined with the app name.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / APP_NAME).resolve()


def data_dir() -> Path:
    """
    Return the directory for generated files such as element clones.

    STEALTH_BROWSER_MCP_DATA_DIR wins when set. A source checkout uses the
    repository root, and an installed package uses the per-user data directory.

    Returns:
        Path: Absolute data directory.
    """
    configured = os.getenv(DATA_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = source_checkout_root()
    if root is not None:
        return root.resolve()
    return user_data_dir().resolve()


def clone_output_dir() -> Path:
    """
    Return the directory for element clone and large response files.

    Returns:
        Path: data_dir() joined with element_clones.
    """
    return data_dir() / "element_clones"


def default_upload_root() -> Path:
    """
    Return the default directory that file_upload may read from.

    A source checkout keeps the repository root. An installed package uses an
    uploads folder in the data directory, created on first use, so the default
    never points at the interpreter's install location.

    Returns:
        Path: Existing upload root directory.
    """
    root = source_checkout_root()
    if root is not None:
        return root
    uploads = data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads
