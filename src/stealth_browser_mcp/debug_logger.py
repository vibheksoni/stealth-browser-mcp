import gzip
import json
import os
import pickle
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from .secrets_env import redact
except ImportError:
    from secrets_env import redact

MAX_LOG_ENTRIES = 2000
MAX_SEEN_ERROR_SIGNATURES = 10000


def _tail(items: List[Dict[str, Any]], count: Optional[int]) -> List[Dict[str, Any]]:
    """
    Return the newest entries of a log list.

    Args:
        items (List[Dict[str, Any]]): Log entries, oldest first.
        count (Optional[int]): Number of entries to keep. None keeps all.

    Returns:
        List[Dict[str, Any]]: The newest entries, oldest first.
    """
    if count is None:
        return list(items)
    if count <= 0:
        return []
    return items[-count:]


def _append_capped(items: List[Dict[str, Any]], entry: Dict[str, Any]) -> None:
    """
    Append a log entry and drop the oldest entries past the cap.

    Args:
        items (List[Dict[str, Any]]): Log list to update in place.
        entry (Dict[str, Any]): Entry to append.

    Returns:
        None
    """
    items.append(entry)
    overflow = len(items) - MAX_LOG_ENTRIES
    if overflow > 0:
        del items[:overflow]


class DebugLogger:
    """Centralized debug logging system for the MCP server."""

    def __init__(self):
        """
        Initializes the DebugLogger.

        Variables:
            self._errors (List[Dict[str, Any]]): Stores error logs.
            self._warnings (List[Dict[str, Any]]): Stores warning logs.
            self._info (List[Dict[str, Any]]): Stores info logs.
            self._stats (Dict[str, int]): Stores statistics for errors, warnings, and calls.
            self._lock (threading.Lock): Ensures thread safety for logging.
            self._enabled (bool): Indicates if logging is enabled.
            self._seen_errors (set): Track error signatures to prevent duplicates.
        """
        self._errors: List[Dict[str, Any]] = []
        self._warnings: List[Dict[str, Any]] = []
        self._info: List[Dict[str, Any]] = []
        self._stats: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._enabled = False
        self._lock_owner = "none"
        self._lock_acquired_time = 0
        self._seen_errors: set = set()

    def _emit_stderr(self, message: str, force: bool = False):
        """
        Emit a debug line to stderr only when logging is enabled unless forced.

        Args:
            message (str): Message to emit.
            force (bool): Whether to emit regardless of current debug state.
        """
        if not force and not self._enabled:
            return
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass

    def log_error(self, component: str, method: str, error: Exception, context: Optional[Dict[str, Any]] = None):
        """
        Log an error with full context.

        Args:
            component (str): Name of the component where the error occurred.
            method (str): Name of the method where the error occurred.
            error (Exception): The exception instance.
            context (Optional[Dict[str, Any]]): Additional context for the error.
        """
        if not self._enabled:
            return

        with self._lock:
            error_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'error_type': type(error).__name__,
                'error_message': str(error),
                'traceback': traceback.format_exc(),
                'context': context or {}
            }
            error_entry = redact(error_entry)

            error_signature = f"{component}.{method}.{error_entry['error_type']}.{error_entry['error_message']}"

            if error_signature in self._seen_errors:
                self._stats[f'{component}.{method}.errors'] += 1
                return

            if len(self._seen_errors) >= MAX_SEEN_ERROR_SIGNATURES:
                self._seen_errors.clear()
            self._seen_errors.add(error_signature)

            _append_capped(self._errors, error_entry)
            self._stats[f'{component}.{method}.errors'] += 1
            self._emit_stderr(f"[DEBUG ERROR] {component}.{method}: {error_entry['error_message']}")

    def log_warning(self, component: str, method: str, message: str, context: Optional[Dict[str, Any]] = None):
        """
        Log a warning.

        Args:
            component (str): Name of the component where the warning occurred.
            method (str): Name of the method where the warning occurred.
            message (str): Warning message.
            context (Optional[Dict[str, Any]]): Additional context for the warning.
        """
        if not self._enabled:
            return

        with self._lock:
            warning_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'message': message,
                'context': context or {}
            }
            warning_entry = redact(warning_entry)
            _append_capped(self._warnings, warning_entry)
            self._stats[f'{component}.{method}.warnings'] += 1
            self._emit_stderr(f"[DEBUG WARN] {component}.{method}: {warning_entry['message']}")

    def log_info(self, component: str, method: str, message: str, data: Optional[Any] = None):
        """
        Log information for debugging.

        Args:
            component (str): Name of the component where the info is logged.
            method (str): Name of the method where the info is logged.
            message (str): Info message.
            data (Optional[Any]): Additional data for the info log.
        """
        if not self._enabled:
            return

        with self._lock:
            info_entry = {
                'timestamp': datetime.now().isoformat(),
                'component': component,
                'method': method,
                'message': message,
                'data': data
            }
            info_entry = redact(info_entry)
            _append_capped(self._info, info_entry)
            self._stats[f'{component}.{method}.calls'] += 1
            self._emit_stderr(f"[DEBUG INFO] {component}.{method}: {info_entry['message']}")
            if info_entry['data']:
                self._emit_stderr(f"  Data: {info_entry['data']}")

    def get_debug_view(self) -> Dict[str, Any]:
        """
        Get comprehensive debug view of all logged data.

        Returns:
            Dict[str, Any]: Dictionary containing summary, recent errors/warnings, all errors/warnings, and component breakdown.
        """
        return self.get_debug_view_paginated()

    def get_debug_view_paginated(
        self,
        max_errors: Optional[int] = None,
        max_warnings: Optional[int] = None,
        max_info: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get paginated debug view of logged data with size limits.

        Args:
            max_errors (Optional[int]): Maximum number of errors to include. None for all.
            max_warnings (Optional[int]): Maximum number of warnings to include. None for all.
            max_info (Optional[int]): Maximum number of info logs to include. None for all.

        Returns:
            Dict[str, Any]: Dictionary containing summary, recent errors/warnings, limited errors/warnings, and component breakdown.
        """
        with self._lock:
            all_errors = _tail(self._errors, max_errors)
            all_warnings = _tail(self._warnings, max_warnings)
            all_info = _tail(self._info, max_info)
            limited_errors = all_errors if max_errors is not None else _tail(self._errors, 10)
            limited_warnings = all_warnings if max_warnings is not None else _tail(self._warnings, 10)
            limited_info = all_info if max_info is not None else _tail(self._info, 10)

            return {
                'summary': {
                    'total_errors': len(self._errors),
                    'total_warnings': len(self._warnings),
                    'total_info': len(self._info),
                    'returned_errors': len(all_errors),
                    'returned_warnings': len(all_warnings),
                    'returned_info': len(all_info),
                    'error_types': self._get_error_summary(),
                    'stats': dict(self._stats)
                },
                'recent_errors': limited_errors,
                'recent_warnings': limited_warnings,
                'recent_info': limited_info,
                'all_errors': all_errors,
                'all_warnings': all_warnings,
                'all_info': all_info,
                'component_breakdown': self._get_component_breakdown()
            }

    def _get_error_summary(self) -> Dict[str, int]:
        """
        Get summary of error types.

        Returns:
            Dict[str, int]: Dictionary mapping error type names to their counts.
        """
        error_types = defaultdict(int)
        for error in self._errors:
            error_types[error['error_type']] += 1
        return dict(error_types)

    def _get_component_breakdown(self) -> Dict[str, Dict[str, int]]:
        """
        Get breakdown by component.

        Returns:
            Dict[str, Dict[str, int]]: Dictionary mapping component names to their error, warning, and call counts.
        """
        breakdown = defaultdict(lambda: {'errors': 0, 'warnings': 0, 'calls': 0})

        for error in self._errors:
            breakdown[error['component']]['errors'] += 1

        for warning in self._warnings:
            breakdown[warning['component']]['warnings'] += 1

        for info in self._info:
            breakdown[info['component']]['calls'] += 1

        return dict(breakdown)

    def clear_debug_view(self):
        """
        Clear all debug logs with timeout protection.

        Variables:
            self._errors (List[Dict[str, Any]]): Cleared.
            self._warnings (List[Dict[str, Any]]): Cleared.
            self._info (List[Dict[str, Any]]): Cleared.
            self._stats (Dict[str, int]): Cleared.
        """
        try:
            if self._lock.acquire(timeout=5.0):
                try:
                    self._errors.clear()
                    self._warnings.clear()
                    self._info.clear()
                    self._stats.clear()
                    self._seen_errors.clear()
                    self._emit_stderr("[DEBUG] Debug logs cleared")
                finally:
                    self._lock.release()
            else:
                self._emit_stderr("[DEBUG] Failed to clear logs - timeout acquiring lock")
        except Exception as e:
            self._emit_stderr(f"[DEBUG] Error clearing logs: {e}")

    def clear_debug_view_safe(self):
        """
        Safe version that recreates data structures if lock fails.
        """
        try:
            self.clear_debug_view()
        except Exception:
            self._errors = []
            self._warnings = []
            self._info = []
            self._stats = defaultdict(int)
            self._seen_errors = set()
            self._emit_stderr("[DEBUG] Debug logs force-cleared (lock bypass)")

    def enable(self):
        """
        Enable debug logging.

        Variables:
            self._enabled (bool): Set to True.
        """
        self._enabled = True
        self._emit_stderr("[DEBUG] Debug logging enabled", force=True)

    def disable(self):
        """
        Disable debug logging.

        Variables:
            self._enabled (bool): Set to False.
        """
        self._emit_stderr("[DEBUG] Debug logging disabled")
        self._enabled = False

    def get_lock_status(self) -> Dict[str, Any]:
        """Get current lock status for debugging."""
        return {
            "lock_owner": self._lock_owner,
            "lock_held_duration": time.time() - self._lock_acquired_time if self._lock_acquired_time > 0 else 0,
            "lock_acquired": self._lock.locked() if hasattr(self._lock, 'locked') else "unknown"
        }

    def export_to_file(self, filepath: str = "debug_log.json"):
        """
        Export debug logs to a JSON file.

        Args:
            filepath (str): Path to the file where logs will be exported.

        Returns:
            str: The filepath where logs were exported.
        """
        return self.export_to_file_paginated(filepath)

    def export_to_file_paginated(
        self,
        filepath: str = "debug_log.json",
        max_errors: Optional[int] = None,
        max_warnings: Optional[int] = None,
        max_info: Optional[int] = None,
        format: str = "auto"
    ):
        """
        Export paginated debug logs to a file using fastest method available.

        Args:
            filepath (str): Path to the file where logs will be exported.
            max_errors (Optional[int]): Maximum number of errors to export. None for all.
            max_warnings (Optional[int]): Maximum number of warnings to export. None for all.
            max_info (Optional[int]): Maximum number of info logs to export. None for all.
            format (str): Export format: 'json', 'pickle', 'gzip-pickle', 'auto' (default: 'auto').

        Returns:
            str: The filepath where logs were exported.
        """
        try:
            debug_data = self.get_debug_view_paginated(
                max_errors=max_errors,
                max_warnings=max_warnings,
                max_info=max_info
            )
        except Exception as e:
            self._emit_stderr(f"[DEBUG] Exception in export: {e}")
            return self._export_lockfree(filepath, max_errors, max_warnings, max_info, format)

        if format == "auto":
            total_items = (debug_data['summary']['returned_errors'] +
                         debug_data['summary']['returned_warnings'] +
                         debug_data['summary']['returned_info'])
            if total_items > 1000:
                format = "gzip-pickle"
            elif total_items > 100:
                format = "pickle"
            else:
                format = "json"

        if format == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        elif format == "pickle":
            return self._export_pickle(debug_data, filepath)
        else:
            return self._export_json(debug_data, filepath)

    def _export_lockfree(self, filepath: str, max_errors: Optional[int], max_warnings: Optional[int], max_info: Optional[int], format: str) -> str:
        """
        Lock-free export method that creates a snapshot without acquiring locks.
        """
        errors_snapshot = _tail(list(self._errors), max_errors)
        warnings_snapshot = _tail(list(self._warnings), max_warnings)
        info_snapshot = _tail(list(self._info), max_info)

        debug_data = {
            'summary': {
                'total_errors': len(self._errors),
                'total_warnings': len(self._warnings),
                'total_info': len(self._info),
                'returned_errors': len(errors_snapshot),
                'returned_warnings': len(warnings_snapshot),
                'returned_info': len(info_snapshot)
            },
            'all_errors': errors_snapshot,
            'all_warnings': warnings_snapshot,
            'all_info': info_snapshot
        }

        if format == "auto":
            total_items = len(errors_snapshot) + len(warnings_snapshot) + len(info_snapshot)
            if total_items > 1000:
                format = "gzip-pickle"
            elif total_items > 100:
                format = "pickle"
            else:
                format = "json"

        if format == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        elif format == "pickle":
            return self._export_pickle(debug_data, filepath)
        else:
            return self._export_json(debug_data, filepath)

    def _export_gzip_pickle(self, debug_data: Dict[str, Any], filepath: str) -> str:
        if not filepath.endswith('.pkl.gz'):
            filepath = filepath.replace('.json', '.pkl.gz')

        with gzip.open(filepath, 'wb') as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)

        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, gzip-pickle format)"
        )
        return filepath

    def _export_pickle(self, debug_data: Dict[str, Any], filepath: str) -> str:
        """Export using pickle (fast for medium data)."""
        if not filepath.endswith('.pkl'):
            filepath = filepath.replace('.json', '.pkl')

        with open(filepath, 'wb') as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)

        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, pickle format)"
        )
        return filepath

    def _export_json(self, debug_data: Dict[str, Any], filepath: str) -> str:
        """Export using JSON (human readable but slower)."""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(debug_data, f, separators=(',', ':'), default=str)

        file_size = os.path.getsize(filepath)
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, JSON format)"
        )
        return filepath


debug_logger = DebugLogger()
