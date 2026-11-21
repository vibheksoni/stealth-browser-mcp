"""Per-session download management, wait_for_download, and drag-and-drop file support."""

import asyncio
import base64
import fnmatch
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import nodriver as uc
from nodriver import Tab

from . import input_actions, paths
from .debug_logger import debug_logger
from .file_upload_security import _is_relative_to, get_allowed_upload_roots

DEFAULT_DOWNLOAD_TIMEOUT = 30.0
DOWNLOAD_POLL_INTERVAL = 0.05
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024

DROP_FILES_JS = """function (selector, files) {
  let el = document.querySelector(selector);
  if (!el && (selector.startsWith('//') || selector.toLowerCase().startsWith('xpath='))) {
    const xp = selector.toLowerCase().startsWith('xpath=') ? selector.substring(6).trim() : selector;
    try {
      const res = document.evaluate(xp, document, null, 9, null);
      el = res ? res.singleNodeValue : null;
    } catch (e) {}
  }
  if (!el) return false;

  const dt = new DataTransfer();
  for (const f of files) {
    let blob;
    if (f.base64 && f.base64.length > 0) {
      try {
        const bin = atob(f.base64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        blob = new Blob([arr], { type: f.type });
      } catch (e) {
        blob = new Blob([], { type: f.type });
      }
    } else {
      blob = new Blob([], { type: f.type });
    }
    const file = new File([blob], f.name, { type: f.type, lastModified: f.lastModified });
    dt.items.add(file);
  }

  const enterEvt = new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer: dt });
  const overEvt = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt });
  const dropEvt = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt });

  el.dispatchEvent(enterEvt);
  el.dispatchEvent(overEvt);
  el.dispatchEvent(dropEvt);
  return true;
}"""


def ensure_download_directory(dir_path: Union[str, Path]) -> Path:
    """
    Ensure directory exists with private permissions (mode 0700).

    Args:
        dir_path: Directory path to create.

    Returns:
        Path: Resolved directory path.
    """
    p = Path(dir_path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def get_session_download_dir(
    instance_id: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Return the default per-instance download directory (mode 0700).

    Args:
        instance_id: Unique browser instance ID.
        base_dir: Optional base directory (e.g. session user_data_dir).

    Returns:
        Path: Resolved download directory with mode 0700.
    """
    try:
        from .path_policy import session_workspace_dir
        ws = session_workspace_dir()
    except Exception:
        ws = paths.data_dir()

    try:
        from .mcp_sessions import get_current_mcp_session_id
        has_session = bool(get_current_mcp_session_id())
    except Exception:
        has_session = False

    if base_dir is not None and not has_session:
        candidate = Path(base_dir).resolve() / "downloads"
    elif base_dir is not None and has_session:
        resolved_base = Path(base_dir).resolve()
        try:
            resolved_base.relative_to(ws.resolve())
            candidate = resolved_base / "downloads"
        except ValueError:
            candidate = ws / "downloads" / instance_id
    else:
        candidate = ws / "downloads" / instance_id

    if has_session:
        try:
            candidate.resolve().relative_to(ws.resolve())
        except ValueError:
            safe_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(instance_id)) or "default"
            candidate = ws / "downloads" / safe_id

    return ensure_download_directory(candidate)


def validate_pattern(pattern: Optional[str]) -> Optional[str]:
    """
    Validate filename pattern against directory traversal attempts.

    Args:
        pattern: Optional filename or glob pattern.

    Returns:
        Optional[str]: Validated pattern, or None if omitted.

    Raises:
        ValueError: If pattern attempts directory traversal or path escape.
    """
    if not pattern:
        return pattern
    if ".." in pattern:
        raise ValueError(f"Path escape rejected: filename pattern contains '..': {pattern}")
    if pattern.startswith("/") or pattern.startswith("\\"):
        raise ValueError(f"Path escape rejected: filename pattern cannot be an absolute path: {pattern}")
    if ":" in pattern and len(pattern) > 1 and pattern[1] == ":":
        raise ValueError(f"Path escape rejected: filename pattern contains drive letter: {pattern}")
    parts = Path(pattern).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"Path escape rejected: filename pattern contains '..': {pattern}")
    return pattern


def validate_download_target(
    target: Union[str, Path],
    session_dir: Union[str, Path],
) -> Path:
    """
    Validate that a download target path resides strictly inside the session download directory.

    Args:
        target: Target filename or path.
        session_dir: Session download directory.

    Returns:
        Path: Resolved target path.

    Raises:
        ValueError: If target attempts directory traversal or resolves outside session_dir.
    """
    target_str = str(target)
    if ".." in target_str:
        raise ValueError(f"Path escape rejected: target contains '..': {target_str}")
    session_resolved = Path(session_dir).resolve()
    target_path = Path(target)
    if not target_path.is_absolute():
        resolved = (session_resolved / target_path).resolve()
    else:
        resolved = target_path.resolve()
    if not _is_relative_to(resolved, session_resolved):
        raise ValueError(
            f"Path escape rejected: target {target_str!r} resolves to {resolved!r} "
            f"which is outside session download directory {session_resolved!r}"
        )
    return resolved


def validate_drop_paths(
    paths_list: Sequence[str],
    session_dir: Optional[Path] = None,
) -> List[str]:
    """
    Validate local file paths for drag-and-drop operations.

    Args:
        paths_list: Sequence of file paths to drop.
        session_dir: Optional session download directory.

    Returns:
        List[str]: Resolved file path strings.

    Raises:
        ValueError: If paths are empty, contain '..', do not exist, or are outside allowed roots.
    """
    if not paths_list or isinstance(paths_list, (str, bytes)):
        raise ValueError("paths must contain at least one file")

    allowed_roots: List[Path] = []
    try:
        allowed_roots.extend(get_allowed_upload_roots())
    except Exception:
        pass
    if session_dir is not None:
        allowed_roots.append(Path(session_dir).resolve())
    allowed_roots.append(Path(tempfile.gettempdir()).resolve())

    resolved_paths: List[str] = []
    for p in paths_list:
        p_str = str(p)
        if ".." in p_str:
            raise ValueError(f"Path escape rejected in drop_files: {p_str}")
        path_obj = Path(p_str).expanduser()
        if not path_obj.is_absolute():
            raise ValueError(f"Drop path must be absolute: {p_str}")
        resolved = path_obj.resolve()
        if not resolved.is_file():
            raise ValueError(f"Drop file does not exist: {p_str}")
        if allowed_roots and not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise ValueError(f"Drop path {p_str} is not in allowed directories")
        resolved_paths.append(str(resolved))
    return resolved_paths


def _target_key(target: Any) -> Union[str, int]:
    """Return a hashable identifier for a Tab, Connection, or Browser object."""
    target_id = getattr(target, "target_id", None)
    if target_id is not None:
        return str(target_id)
    target_obj = getattr(target, "target", None)
    if target_obj is not None:
        target_id = getattr(target_obj, "target_id", None)
        if target_id is not None:
            return str(target_id)
    return id(target)


class DownloadManager:
    """Manages per-session download directories, CDP download behavior, and events."""

    def __init__(self) -> None:
        self._download_dirs: Dict[str, Path] = {}
        self._completed_downloads: Dict[str, List[Dict[str, Any]]] = {}
        self._configured_targets: set = set()

    def get_download_dir(self, instance_id: str) -> Optional[Path]:
        """Get the configured download directory for an instance."""
        return self._download_dirs.get(instance_id)

    def set_download_dir(self, instance_id: str, download_dir: Union[str, Path]) -> Path:
        """Explicitly set and ensure the download directory for an instance."""
        ensured = ensure_download_directory(download_dir)
        self._download_dirs[instance_id] = ensured
        return ensured

    async def configure_download_directory(
        self,
        target: Any,
        instance_id: str,
        download_dir: Optional[Union[str, Path]] = None,
        behavior: str = "allow",
        events_enabled: bool = True,
    ) -> Path:
        """
        Configure CDP Browser.setDownloadBehavior on a tab or browser connection.

        Args:
            target: Tab or Browser object.
            instance_id: Browser instance ID.
            download_dir: Optional target directory; defaults to per-instance dir (mode 0700).
            behavior: CDP download behavior ('allow', 'deny', 'default').
            events_enabled: Whether to emit CDP download events.

        Returns:
            Path: The configured download directory.
        """
        if download_dir is None:
            download_dir = self.get_download_dir(instance_id)
        if download_dir is None:
            user_data_dir = None
            if hasattr(target, "browser") and hasattr(target.browser, "config"):
                user_data_dir = getattr(target.browser.config, "user_data_dir", None)
            elif hasattr(target, "config"):
                user_data_dir = getattr(target.config, "user_data_dir", None)
            download_dir = get_session_download_dir(instance_id, user_data_dir)

        ensured_dir = ensure_download_directory(download_dir)
        self._download_dirs[instance_id] = ensured_dir

        command = uc.cdp.browser.set_download_behavior(
            behavior=behavior,
            download_path=str(ensured_dir),
            events_enabled=events_enabled,
        )

        sent = False
        browser = getattr(target, "browser", None) or (target if hasattr(target, "connection") else None)
        if browser is not None and getattr(browser, "connection", None) is not None:
            try:
                await browser.connection.send(command)
                sent = True
            except Exception as e:
                debug_logger.log_warning("downloads", "configure_download_directory", f"Connection send: {e}")

        if not sent and hasattr(target, "send"):
            try:
                await target.send(command)
                sent = True
            except Exception as e:
                debug_logger.log_warning("downloads", "configure_download_directory", f"Tab send: {e}")

        def on_will_begin(event: uc.cdp.browser.DownloadWillBegin) -> None:
            self.on_download_will_begin(instance_id, event)

        def on_progress(event: uc.cdp.browser.DownloadProgress) -> None:
            self.on_download_progress(instance_id, event)

        target_key = _target_key(target)
        if hasattr(target, "add_handler") and target_key not in self._configured_targets:
            self._configured_targets.add(target_key)
            try:
                target.add_handler(uc.cdp.browser.DownloadWillBegin, on_will_begin)
                target.add_handler(uc.cdp.browser.DownloadProgress, on_progress)
            except Exception:
                pass

        if browser is not None and getattr(browser, "connection", None) is not None:
            conn = browser.connection
            conn_key = f"conn_{_target_key(conn)}"
            if hasattr(conn, "add_handler") and conn_key not in self._configured_targets:
                self._configured_targets.add(conn_key)
                try:
                    conn.add_handler(uc.cdp.browser.DownloadWillBegin, on_will_begin)
                    conn.add_handler(uc.cdp.browser.DownloadProgress, on_progress)
                except Exception:
                    pass

        return ensured_dir

    async def prepare_tab(self, tab: Any, instance_id: str, options: Any) -> None:
        """Tab preparation listener: automatically configure download behavior."""
        user_data_dir = getattr(options, "user_data_dir", None) if options else None
        current_dir = self.get_download_dir(instance_id) or get_session_download_dir(instance_id, user_data_dir)
        await self.configure_download_directory(tab, instance_id, download_dir=current_dir)

    async def forget_instance(self, instance_id: str) -> None:
        """Close cleanup listener: remove instance tracking."""
        self._download_dirs.pop(instance_id, None)
        self._completed_downloads.pop(instance_id, None)

    def on_download_will_begin(self, instance_id: str, event: Any) -> None:
        """Handle CDP Browser.downloadWillBegin event."""
        debug_logger.log_info(
            "downloads",
            "download_will_begin",
            f"Instance {instance_id}: guid={getattr(event, 'guid', '')} url={getattr(event, 'url', '')}",
        )

    def on_download_progress(self, instance_id: str, event: Any) -> None:
        """Handle CDP Browser.downloadProgress event."""
        state = getattr(event, "state", "")
        if state == "completed":
            file_path = getattr(event, "file_path", None)
            record = {
                "guid": getattr(event, "guid", ""),
                "file_path": file_path,
                "received_bytes": getattr(event, "received_bytes", 0),
                "total_bytes": getattr(event, "total_bytes", 0),
                "timestamp": time.monotonic(),
            }
            self._completed_downloads.setdefault(instance_id, []).append(record)

    def get_completed_downloads(self, instance_id: str) -> List[Dict[str, Any]]:
        """Return list of recorded completed download events for an instance."""
        return list(self._completed_downloads.get(instance_id, []))


download_manager = DownloadManager()


async def wait_for_download(
    instance: Optional[Union[str, Any]] = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    filename_pattern: Optional[str] = None,
    target_path: Optional[str] = None,
    instance_id: Optional[str] = None,
    browser_manager: Optional[Any] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Wait for a file download to complete in the session download directory.

    Args:
        instance: Browser instance ID string, Tab, or Browser object.
        timeout: Maximum seconds to wait (default: 30.0).
        filename_pattern: Optional glob pattern or filename (e.g. '*.csv' or 'data.pdf').
        target_path: Optional expected target file path within download directory.
        instance_id: Alias for instance ID parameter.
        browser_manager: Optional BrowserManager to look up instances.

    Returns:
        Dict[str, Any]: Download result including file_path, filename, and file_size.

    Raises:
        ValueError: If filename_pattern or target_path violates path escape rules.
        TimeoutError: If no matching download completes within timeout.
    """
    validate_pattern(filename_pattern)

    raw_id = instance_id or (instance if isinstance(instance, str) else None) or kwargs.get("instance_name")

    download_dir: Optional[Path] = None
    if raw_id:
        download_dir = download_manager.get_download_dir(raw_id)

    if download_dir is None and browser_manager is not None:
        if raw_id:
            inst_data = await browser_manager.get_instance(raw_id)
            if inst_data:
                opts = inst_data.get("options")
                user_data_dir = getattr(opts, "user_data_dir", None)
                download_dir = get_session_download_dir(raw_id, user_data_dir)
        else:
            instances = await browser_manager.list_instances()
            if len(instances) == 1:
                raw_id = instances[0].instance_id
                inst_data = await browser_manager.get_instance(raw_id)
                if inst_data:
                    opts = inst_data.get("options")
                    user_data_dir = getattr(opts, "user_data_dir", None)
                    download_dir = get_session_download_dir(raw_id, user_data_dir)

    if download_dir is None and instance is not None and not isinstance(instance, str):
        browser = getattr(instance, "browser", None) or instance
        config = getattr(browser, "config", None)
        user_data_dir = getattr(config, "user_data_dir", None)
        ident = getattr(instance, "target_id", None) or "browser_session"
        download_dir = get_session_download_dir(str(ident), user_data_dir)

    if download_dir is None:
        ident = raw_id or "default"
        download_dir = get_session_download_dir(ident)

    ensured_dir = ensure_download_directory(download_dir)

    if target_path:
        validate_download_target(target_path, ensured_dir)

    start_time = time.monotonic()
    deadline = start_time + max(float(timeout), 0.0)

    while True:
        # Check CDP completed download events
        if raw_id:
            for event in download_manager.get_completed_downloads(raw_id):
                fp = event.get("file_path")
                if fp:
                    try:
                        validated = validate_download_target(fp, ensured_dir)
                        if validated.is_file():
                            if not filename_pattern or fnmatch.fnmatch(validated.name, filename_pattern):
                                return {
                                    "success": True,
                                    "file_path": str(validated),
                                    "filename": validated.name,
                                    "file_size": validated.stat().st_size,
                                    "download_dir": str(ensured_dir),
                                }
                    except ValueError:
                        raise

        # Check directory files
        candidates: List[Path] = []
        for p in ensured_dir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".crdownload") or name.endswith(".tmp"):
                continue
            if (ensured_dir / f"{name}.crdownload").exists():
                continue
            if filename_pattern and not fnmatch.fnmatch(name, filename_pattern):
                continue
            validate_download_target(p, ensured_dir)
            candidates.append(p)

        if candidates:
            candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            chosen = candidates[0]
            return {
                "success": True,
                "file_path": str(chosen),
                "filename": chosen.name,
                "file_size": chosen.stat().st_size,
                "download_dir": str(ensured_dir),
            }

        if time.monotonic() >= deadline:
            pattern_info = f" matching pattern {filename_pattern!r}" if filename_pattern else ""
            raise TimeoutError(
                f"Download timed out after {timeout} seconds waiting for file{pattern_info} in {ensured_dir}"
            )

        await asyncio.sleep(DOWNLOAD_POLL_INTERVAL)


async def drop_files(
    selector: Optional[str] = None,
    paths: Optional[List[str]] = None,
    instance_id: Optional[str] = None,
    tab: Optional[Any] = None,
    browser_manager: Optional[Any] = None,
    *args: Any,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Drop files onto a drag-and-drop zone element via CDP Input.dispatchDragEvent and DataTransfer events.

    Args:
        selector: CSS selector, deep selector, or XPath for the drop target element.
        paths: List of absolute file paths to drop.
        instance_id: Optional browser instance ID.
        tab: Optional Tab instance.
        browser_manager: Optional BrowserManager.

    Returns:
        Dict[str, Any]: {
            "success": True,
            "count": int,
            "files": List[str],
            "paths": List[str],
            "selector": str,
        }
    """
    if selector is None:
        selector = kwargs.get("selector_or_instance")
    if paths is None:
        raw_paths = kwargs.get("paths_or_selector")
        if isinstance(raw_paths, list):
            paths = raw_paths
        elif isinstance(raw_paths, str) and selector is None:
            selector = raw_paths

    if isinstance(selector, str) and isinstance(paths, str) and (args or "paths" in kwargs):
        resolved_instance_id = selector
        resolved_selector = paths
        resolved_paths = args[0] if args else kwargs.get("paths", [])
    elif isinstance(selector, str) and isinstance(paths, list):
        resolved_selector = selector
        resolved_paths = paths
        resolved_instance_id = instance_id or kwargs.get("instance")
    elif selector is not None and paths is not None:
        resolved_selector = str(selector)
        resolved_paths = list(paths)
        resolved_instance_id = instance_id or kwargs.get("instance")
    else:
        resolved_selector = selector or kwargs.get("selector", "")
        resolved_paths = paths or kwargs.get("paths", [])
        resolved_instance_id = instance_id or kwargs.get("instance")

    session_dir = None
    if resolved_instance_id:
        session_dir = download_manager.get_download_dir(resolved_instance_id)

    validated_paths = validate_drop_paths(resolved_paths, session_dir=session_dir)

    target_tab = tab
    if target_tab is None and browser_manager is not None:
        if resolved_instance_id:
            target_tab = await browser_manager.get_tab(resolved_instance_id)
        else:
            instances = await browser_manager.list_instances()
            if len(instances) == 1:
                target_tab = await browser_manager.get_tab(instances[0].instance_id)

    if target_tab is None:
        target_tab = kwargs.get("target_tab")

    if target_tab is None:
        raise Exception(
            f"No active tab found for drop_files (instance_id={resolved_instance_id!r}). "
            f"Ensure a browser instance is running."
        )

    x, y = 100.0, 100.0
    try:
        backend_node_id = await input_actions.find_node(target_tab, resolved_selector)
        if backend_node_id is not None:
            center = await input_actions.node_center(target_tab, backend_node_id)
            if center is not None:
                x, y = center
    except Exception:
        pass

    drag_items = []
    files_payload = []
    for p_str in validated_paths:
        p = Path(p_str)
        mime_type = mimetypes.guess_type(p_str)[0] or "application/octet-stream"
        drag_items.append(uc.cdp.input_.DragDataItem(mime_type=mime_type, data=""))
        f_stat = p.stat()
        content_b64 = ""
        if f_stat.st_size <= MAX_PAYLOAD_BYTES:
            try:
                content_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            except Exception:
                pass
        files_payload.append({
            "name": p.name,
            "size": f_stat.st_size,
            "type": mime_type,
            "base64": content_b64,
            "lastModified": int(f_stat.st_mtime * 1000),
        })

    drag_data = uc.cdp.input_.DragData(
        items=drag_items,
        drag_operations_mask=1 | 2 | 16,
        files=validated_paths,
    )

    try:
        await target_tab.send(uc.cdp.input_.dispatch_drag_event("dragEnter", x=x, y=y, data=drag_data))
        await target_tab.send(uc.cdp.input_.dispatch_drag_event("dragOver", x=x, y=y, data=drag_data))
        await target_tab.send(uc.cdp.input_.dispatch_drag_event("drop", x=x, y=y, data=drag_data))
    except Exception as cdp_err:
        debug_logger.log_warning("downloads", "drop_files_cdp", f"CDP dispatchDragEvent: {cdp_err}")

    try:
        serialized_args = f"{json.dumps(resolved_selector)}, {json.dumps(files_payload)}"
        await target_tab.evaluate(f"({DROP_FILES_JS})({serialized_args})")
    except Exception as js_err:
        debug_logger.log_warning("downloads", "drop_files_js", f"JS dispatchEvent: {js_err}")

    return {
        "success": True,
        "count": len(validated_paths),
        "files": [Path(p).name for p in validated_paths],
        "paths": validated_paths,
        "selector": resolved_selector,
    }
