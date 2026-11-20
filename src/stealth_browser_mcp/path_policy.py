"""Confine caller file paths to data_dir and block dangerous URL schemes (SB-83 / F7, F12)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Union
from urllib.parse import urlparse

from .file_upload_security import FILE_UPLOAD_ALLOWED_DIRS_ENV, _split_configured_roots
from .paths import data_dir

ALLOW_FILE_URLS_ENV = "STEALTH_ALLOW_FILE_URLS"
BLOCKED_URL_SCHEMES = frozenset({
    "file",
    "chrome",
    "chrome-untrusted",
    "chrome-extension",
    "devtools",
    "view-source",
})


class PathPolicyError(ValueError):
    """Raised when a caller path or URL is not allowed."""


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def allowed_file_roots() -> List[Path]:
    """Return directories that caller file paths may resolve under."""
    roots = [data_dir().resolve()]
    configured = _split_configured_roots(os.getenv(FILE_UPLOAD_ALLOWED_DIRS_ENV))
    if configured:
        for raw in configured:
            try:
                roots.append(Path(raw).expanduser().resolve())
            except OSError:
                continue
    return roots


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_under_root(path: Union[str, Path], root: Union[str, Path]) -> Path:
    """Resolve path and require it to stay under root, including symlink escapes."""
    root_path = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    try:
        if candidate.exists() or candidate.is_symlink():
            resolved = candidate.resolve()
        else:
            resolved = candidate.resolve()
    except OSError as exc:
        raise PathPolicyError("rejected path {!r}: {}".format(path, exc)) from exc
    if not _is_under(resolved, root_path):
        raise PathPolicyError(
            "rejected path {!r}: must stay under {}".format(path, root_path)
        )
    return resolved


def resolve_managed_path(
    path: Union[str, Path],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a caller path under data_dir() or configured upload roots."""
    last_error: Optional[PathPolicyError] = None
    for root in allowed_file_roots():
        try:
            resolved = resolve_under_root(path, root)
        except PathPolicyError as exc:
            last_error = exc
            continue
        if must_exist and not resolved.is_file():
            raise PathPolicyError("rejected path {!r}: file not found".format(path))
        return resolved
    if last_error is not None:
        raise last_error
    raise PathPolicyError("rejected path {!r}: no allowed roots".format(path))


def write_private_bytes(path: Union[str, Path], data: bytes) -> Path:
    """Write data to a confined path with mode 0600, refusing symlink escapes."""
    resolved = resolve_managed_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved = resolve_managed_path(resolved)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(resolved), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.chmod(str(resolved), 0o600)
        except OSError:
            pass
    return resolved


def write_private_text(path: Union[str, Path], text: str) -> Path:
    """Write text to a confined path with mode 0600."""
    return write_private_bytes(path, text.encode("utf-8"))


def read_managed_text(path: Union[str, Path]) -> str:
    """Read text from a confined existing file."""
    resolved = resolve_managed_path(path, must_exist=True)
    return resolved.read_text(encoding="utf-8")


def file_urls_allowed() -> bool:
    """True when STEALTH_ALLOW_FILE_URLS=1 permits file:/chrome:/devtools: navigation."""
    return _truthy_env(ALLOW_FILE_URLS_ENV)


def reject_blocked_url(url: str) -> str:
    """Return the URL, or raise PathPolicyError for blocked schemes."""
    if url is None:
        raise PathPolicyError("url is required")
    parsed = urlparse(str(url).strip())
    scheme = (parsed.scheme or "").lower()
    if scheme in BLOCKED_URL_SCHEMES and not file_urls_allowed():
        raise PathPolicyError(
            "navigation to {} URLs is blocked. Set STEALTH_ALLOW_FILE_URLS=1 "
            "only if you intentionally need host-file or chrome: access.".format(scheme)
        )
    return str(url)
