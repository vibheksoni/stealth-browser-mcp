"""File upload path validation for browser form automation."""

import os
from pathlib import Path
from typing import List, Optional, Sequence


FILE_UPLOAD_ALLOWED_DIRS_ENV = "BROWSER_FILE_UPLOAD_ALLOWED_DIRS"
DEFAULT_FILE_UPLOAD_ROOT = Path(__file__).resolve().parent.parent


def _split_configured_roots(config_value: Optional[str]) -> Optional[List[str]]:
    """
    Split configured upload roots from the environment value.

    Args:
        config_value (Optional[str]): Raw environment variable value.

    Returns:
        Optional[List[str]]: Configured path strings, or None when the default should be used.
    """
    if config_value is None or not config_value.strip():
        return None
    return [item.strip().strip('"') for item in config_value.split(os.pathsep) if item.strip()]


def _resolve_upload_root(root_value: str) -> Path:
    """
    Resolve and validate one upload root directory.

    Args:
        root_value (str): Configured upload root path.

    Returns:
        Path: Resolved upload root path.
    """
    root = Path(root_value).expanduser()
    if not root.is_absolute():
        raise ValueError(f"Upload root must be absolute: {root_value}")
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError(f"Upload root is not a directory: {root_value}")
    return resolved_root


def get_allowed_upload_roots() -> List[Path]:
    """
    Return upload roots allowed for local file uploads.

    Returns:
        List[Path]: Resolved directories that file_upload may read from.
    """
    configured_roots = _split_configured_roots(os.getenv(FILE_UPLOAD_ALLOWED_DIRS_ENV))
    root_values = configured_roots if configured_roots is not None else [str(DEFAULT_FILE_UPLOAD_ROOT)]
    return [_resolve_upload_root(root_value) for root_value in root_values]


def _is_relative_to(path: Path, root: Path) -> bool:
    """
    Check whether a path is contained inside a root directory.

    Args:
        path (Path): Resolved file path.
        root (Path): Resolved root directory.

    Returns:
        bool: True when the file is inside the root.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _format_allowed_roots(roots: Sequence[Path]) -> str:
    """
    Format upload roots for validation error messages.

    Args:
        roots (Sequence[Path]): Resolved upload root paths.

    Returns:
        str: Human-readable list of roots.
    """
    return ", ".join(str(root) for root in roots)


def validate_upload_paths(paths: Sequence[str]) -> List[str]:
    """
    Validate local files before passing them to nodriver send_file.

    Args:
        paths (Sequence[str]): Absolute local file paths requested for upload.

    Returns:
        List[str]: Resolved file paths safe to pass to nodriver.
    """
    if not paths or isinstance(paths, (str, bytes)):
        raise ValueError("paths must contain at least one file")

    allowed_roots = get_allowed_upload_roots()
    if not allowed_roots:
        raise ValueError(f"No upload roots configured in {FILE_UPLOAD_ALLOWED_DIRS_ENV}")

    resolved_paths = []
    for path_value in paths:
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"Upload path must be a non-empty string: {path_value!r}")

        path = Path(path_value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Path must be absolute: {path_value}")

        try:
            resolved_path = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"File does not exist: {path_value}") from exc

        if not resolved_path.is_file():
            raise ValueError(f"Upload path is not a regular file: {path_value}")

        if not any(_is_relative_to(resolved_path, root) for root in allowed_roots):
            roots = _format_allowed_roots(allowed_roots)
            raise ValueError(
                f"Upload path is outside allowed roots: {resolved_path}. "
                f"Set {FILE_UPLOAD_ALLOWED_DIRS_ENV} to include its directory. "
                f"Allowed roots: {roots}"
            )

        resolved_paths.append(str(resolved_path))

    return resolved_paths
