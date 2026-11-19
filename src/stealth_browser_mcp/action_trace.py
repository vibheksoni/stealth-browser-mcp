"""Action trace recording, redaction, and bundle export for stealth browser MCP."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any, Dict, List, Optional, Set, Union
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from .secrets_env import REDACTED_PLACEHOLDER, Redactor, default_redactor
except ImportError:
    from secrets_env import REDACTED_PLACEHOLDER, Redactor, default_redactor

DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
SENSITIVE_KEY_MARKERS = ("password", "token", "secret", "authorization", "cookie")


def is_sensitive_key(key: Any) -> bool:
    """Check if a key name matches sensitive patterns (case-insensitive)."""
    if not isinstance(key, str):
        key = str(key)
    k = key.lower()
    return any(marker in k for marker in SENSITIVE_KEY_MARKERS)


def mask_sensitive_keys(
    obj: Any,
    placeholder: str = REDACTED_PLACEHOLDER,
    seen: Optional[Set[int]] = None,
    redactor: Optional[Redactor] = None,
) -> Any:
    """Recursively mask values for keys named like password/token/secret/authorization/cookie."""
    if isinstance(obj, dict):
        obj_id = id(obj)
        if seen is None:
            seen = set()
        elif obj_id in seen:
            return placeholder
        seen.add(obj_id)
        try:
            res = {}
            for k, v in obj.items():
                if is_sensitive_key(k):
                    res[k] = placeholder
                else:
                    res[k] = mask_sensitive_keys(v, placeholder, seen, redactor)
            return res
        finally:
            seen.remove(obj_id)
    if isinstance(obj, list):
        obj_id = id(obj)
        if seen is None:
            seen = set()
        elif obj_id in seen:
            return placeholder
        seen.add(obj_id)
        try:
            return [mask_sensitive_keys(item, placeholder, seen, redactor) for item in obj]
        finally:
            seen.remove(obj_id)
    if isinstance(obj, tuple):
        obj_id = id(obj)
        if seen is None:
            seen = set()
        elif obj_id in seen:
            return placeholder
        seen.add(obj_id)
        try:
            return tuple(mask_sensitive_keys(item, placeholder, seen, redactor) for item in obj)
        finally:
            seen.remove(obj_id)
    if isinstance(obj, set):
        obj_id = id(obj)
        if seen is None:
            seen = set()
        elif obj_id in seen:
            return placeholder
        seen.add(obj_id)
        try:
            return {mask_sensitive_keys(item, placeholder, seen, redactor) for item in obj}
        finally:
            seen.remove(obj_id)
    if isinstance(obj, str) and ("?" in obj and ("://" in obj or obj.startswith("/"))):
        return redact_url(obj, redactor=redactor, placeholder=placeholder)
    return obj


def redact_url(
    url: str,
    redactor: Optional[Redactor] = None,
    placeholder: Optional[str] = None,
) -> str:
    """Redact sensitive query parameters and registered secrets from a URL string."""
    if not url or not isinstance(url, str):
        return url
    target_redactor = redactor or default_redactor
    ph = placeholder or target_redactor.placeholder
    url = target_redactor.redact(url)
    try:
        parsed = urlparse(url)
        if parsed.query:
            query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
            new_pairs = []
            changed = False
            for k, v in query_pairs:
                if is_sensitive_key(k):
                    new_pairs.append((k, ph))
                    changed = True
                else:
                    new_pairs.append((k, v))
            if changed:
                new_query = urlencode(new_pairs, safe="[]")
                url = urlunparse((
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    new_query,
                    parsed.fragment,
                ))
    except Exception:
        pass
    return url


def redact_sensitive_data(obj: Any, redactor: Optional[Redactor] = None) -> Any:
    """Recursively redact sensitive keys and registered secrets from data."""
    target_redactor = redactor or default_redactor
    masked = mask_sensitive_keys(obj, placeholder=target_redactor.placeholder, redactor=target_redactor)
    return target_redactor.redact(masked)


def _json_default(obj: Any) -> Any:
    """JSON serialization fallback for non-primitive types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return repr(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


@dataclass
class ActionEntry:
    """Recorded browser action entry."""

    timestamp: str
    tool: str
    args: Dict[str, Any]
    result_summary: Any
    url: str = ""
    screenshot_path: Optional[str] = None
    screenshot_omitted: bool = False

    @property
    def tool_name(self) -> str:
        """Alias for tool."""
        return self.tool

    @property
    def result(self) -> Any:
        """Alias for result_summary."""
        return self.result_summary

    def to_dict(self) -> Dict[str, Any]:
        """Return entry as a dictionary."""
        d = {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "tool_name": self.tool,
            "args": self.args,
            "result_summary": self.result_summary,
            "result": self.result_summary,
            "url": self.url,
            "screenshot_path": self.screenshot_path,
        }
        if self.screenshot_omitted:
            d["screenshot_omitted"] = True
        return d

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.to_dict()


class ActionTrace:
    """Bounded, redacted action trace recording for browser tools."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_entries: Optional[int] = None,
        redactor: Optional[Redactor] = None,
    ):
        self._max_bytes = max(1, int(max_bytes))
        self._max_entries = max_entries
        self._redactor = redactor or default_redactor
        self._actions: List[ActionEntry] = []
        self._dropped_count = 0
        self._truncated = False
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        """Configured size cap in bytes."""
        return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        with self._lock:
            self._max_bytes = max(1, int(value))
            self._enforce_size_cap_locked()

    @property
    def max_entries(self) -> Optional[int]:
        """Optional entry count cap."""
        return self._max_entries

    @property
    def redactor(self) -> Redactor:
        """Active redactor instance."""
        return self._redactor

    @property
    def actions(self) -> List[ActionEntry]:
        """Copy of currently recorded actions."""
        with self._lock:
            return list(self._actions)

    @property
    def truncated(self) -> bool:
        """Whether any entries or screenshot files were dropped or omitted."""
        with self._lock:
            return self._truncated

    @property
    def is_truncated(self) -> bool:
        """Alias for truncated."""
        return self.truncated

    @property
    def dropped_count(self) -> int:
        """Number of action entries dropped due to size cap."""
        with self._lock:
            return self._dropped_count

    @property
    def dropped(self) -> int:
        """Alias for dropped_count."""
        return self.dropped_count

    def __len__(self) -> int:
        with self._lock:
            return len(self._actions)

    def __getitem__(self, index: int) -> ActionEntry:
        with self._lock:
            return self._actions[index]

    def __iter__(self):
        with self._lock:
            items = list(self._actions)
        return iter(items)

    def clear(self) -> None:
        """Reset all recorded actions and counters."""
        with self._lock:
            self._actions.clear()
            self._dropped_count = 0
            self._truncated = False

    def record_action(
        self,
        tool: Optional[Union[str, Dict[str, Any], ActionEntry]] = None,
        args: Optional[Dict[str, Any]] = None,
        result: Any = None,
        url: str = "",
        screenshot_path: Optional[Union[str, Path]] = None,
        timestamp: Optional[Union[str, float, datetime]] = None,
        *,
        tool_name: Optional[str] = None,
        result_summary: Any = None,
        **kwargs: Any,
    ) -> ActionEntry:
        """Record a single browser action, redacting secrets and sensitive keys."""
        if isinstance(tool, dict):
            d = tool
            tool = d.get("tool") or d.get("tool_name") or ""
            args = d.get("args") if args is None else args
            result = d.get("result_summary") if result is None else (d.get("result") if result is None else result)
            url = d.get("url", url)
            screenshot_path = d.get("screenshot_path", screenshot_path)
            timestamp = d.get("timestamp", timestamp)
        elif isinstance(tool, ActionEntry):
            entry = tool
            tool = entry.tool
            args = entry.args if args is None else args
            result = entry.result_summary if result is None else result
            url = entry.url or url
            screenshot_path = entry.screenshot_path or screenshot_path
            timestamp = entry.timestamp or timestamp

        actual_tool = str(tool or tool_name or kwargs.get("tool") or kwargs.get("tool_name") or "")
        actual_result = result_summary if result_summary is not None else result
        if actual_result is None and "result_summary" in kwargs:
            actual_result = kwargs["result_summary"]

        if timestamp is None:
            ts_str = datetime.now(timezone.utc).isoformat()
        elif isinstance(timestamp, (int, float)):
            ts_str = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        elif isinstance(timestamp, datetime):
            ts_str = timestamp.isoformat()
        else:
            ts_str = str(timestamp)

        args_input = dict(args) if isinstance(args, dict) else (args if args is not None else {})
        redacted_args = redact_sensitive_data(args_input, self._redactor)
        redacted_result = redact_sensitive_data(actual_result, self._redactor)
        redacted_url = redact_url(url or "", self._redactor)

        shot_str = str(screenshot_path) if screenshot_path is not None else None
        if shot_str is not None:
            shot_str = self._redactor.redact(shot_str)

        entry = ActionEntry(
            timestamp=ts_str,
            tool=actual_tool,
            args=redacted_args,
            result_summary=redacted_result,
            url=redacted_url,
            screenshot_path=shot_str,
        )

        with self._lock:
            self._actions.append(entry)
            if self._max_entries is not None and len(self._actions) > self._max_entries:
                overflow = len(self._actions) - self._max_entries
                del self._actions[:overflow]
                self._dropped_count += overflow
                self._truncated = True
            self._enforce_size_cap_locked()

        return entry

    record = record_action
    add_action = record_action
    append = record_action

    def _measure_bundle_size(self, bundle: Dict[str, Any]) -> int:
        """Measure serialized JSON size of a candidate bundle."""
        json_bytes = json.dumps(bundle, indent=2, default=_json_default, ensure_ascii=True).encode("utf-8")
        return len(json_bytes)

    def _build_bundle_dict_locked(self, include_all_screenshots: bool = False) -> Dict[str, Any]:
        """Build dictionary representation of current trace without mutating state."""
        action_dicts = []
        for entry in self._actions:
            d = entry.to_dict()
            if not include_all_screenshots and entry.screenshot_omitted:
                d["screenshot_path"] = None
                d["screenshot_omitted"] = True
            action_dicts.append(d)

        return {
            "version": "1.0",
            "truncated": self._truncated,
            "dropped_count": self._dropped_count,
            "dropped": self._dropped_count,
            "max_bytes": self._max_bytes,
            "actions": action_dicts,
        }

    def _enforce_size_cap_locked(self) -> None:
        """Drop oldest entries while serialized actions exceed max_bytes."""
        if not self._actions:
            return

        while len(self._actions) > 1:
            candidate = self._build_bundle_dict_locked(include_all_screenshots=False)
            if self._measure_bundle_size(candidate) <= self._max_bytes:
                break
            self._actions.pop(0)
            self._dropped_count += 1
            self._truncated = True

        if len(self._actions) == 1:
            candidate = self._build_bundle_dict_locked(include_all_screenshots=False)
            if self._measure_bundle_size(candidate) > self._max_bytes:
                self._actions.pop(0)
                self._dropped_count += 1
                self._truncated = True

    def export_bundle(self) -> Dict[str, Any]:
        """Export trace bundle as a redacted dictionary."""
        with self._lock:
            self._enforce_size_cap_locked()

            base_bundle = self._build_bundle_dict_locked(include_all_screenshots=False)
            base_size = self._measure_bundle_size(base_bundle)
            remaining = self._max_bytes - base_size

            truncated = self._truncated
            action_dicts = []

            for entry in self._actions:
                d = entry.to_dict()
                if entry.screenshot_path and os.path.isfile(entry.screenshot_path):
                    try:
                        fsize = os.path.getsize(entry.screenshot_path)
                        if fsize <= remaining:
                            remaining -= fsize
                        else:
                            d["screenshot_path"] = None
                            d["screenshot_omitted"] = True
                            truncated = True
                    except OSError:
                        d["screenshot_path"] = None
                        d["screenshot_omitted"] = True
                elif entry.screenshot_path:
                    pass
                action_dicts.append(d)

            bundle = {
                "version": "1.0",
                "truncated": truncated,
                "dropped_count": self._dropped_count,
                "dropped": self._dropped_count,
                "max_bytes": self._max_bytes,
                "actions": action_dicts,
            }
            redacted_bundle = redact_sensitive_data(bundle, self._redactor)
            json_text = json.dumps(redacted_bundle, indent=2, default=_json_default, ensure_ascii=True)
            json_text = self._redactor.redact(json_text)
            return json.loads(json_text)

    to_dict = export_bundle
    bundle = export_bundle

    def write_bundle(self, output_dir: Union[str, Path]) -> Dict[str, Any]:
        """Write trace.json and referenced files to output_dir, enforcing max_bytes."""
        out_path = Path(output_dir).resolve()
        out_path.mkdir(parents=True, exist_ok=True)

        with self._lock:
            self._enforce_size_cap_locked()

            base_bundle = self._build_bundle_dict_locked(include_all_screenshots=False)
            base_size = self._measure_bundle_size(base_bundle)
            remaining = self._max_bytes - base_size

            used_filenames: Set[str] = set()
            action_dicts = []
            truncated = self._truncated

            for idx, entry in enumerate(self._actions):
                d = entry.to_dict()
                if entry.screenshot_path and os.path.isfile(entry.screenshot_path):
                    try:
                        src_path = Path(entry.screenshot_path)
                        fsize = os.path.getsize(src_path)
                        if fsize <= remaining:
                            base_name = src_path.name
                            clean_name = self._redactor.redact(base_name)
                            dest_name = clean_name
                            if dest_name in used_filenames or dest_name == "trace.json":
                                dest_name = f"screenshot_{idx}_{clean_name}"
                            used_filenames.add(dest_name)

                            dest_file = out_path / dest_name
                            shutil.copy2(src_path, dest_file)
                            remaining -= fsize
                            d["screenshot_path"] = dest_name
                        else:
                            d["screenshot_path"] = None
                            d["screenshot_omitted"] = True
                            truncated = True
                    except Exception:
                        d["screenshot_path"] = None
                        d["screenshot_omitted"] = True
                        truncated = True
                elif entry.screenshot_path:
                    pass
                action_dicts.append(d)

            bundle = {
                "version": "1.0",
                "truncated": truncated,
                "dropped_count": self._dropped_count,
                "dropped": self._dropped_count,
                "max_bytes": self._max_bytes,
                "actions": action_dicts,
            }

            bundle = redact_sensitive_data(bundle, self._redactor)

            trace_json_path = out_path / "trace.json"
            json_text = json.dumps(bundle, indent=2, default=_json_default, ensure_ascii=True)
            json_text = self._redactor.redact(json_text)
            with open(trace_json_path, "w", encoding="utf-8") as f:
                f.write(json_text)

            return json.loads(json_text)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "SENSITIVE_KEY_MARKERS",
    "ActionEntry",
    "ActionTrace",
    "is_sensitive_key",
    "mask_sensitive_keys",
    "redact_sensitive_data",
    "redact_url",
]
