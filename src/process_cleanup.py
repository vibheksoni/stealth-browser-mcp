"""Robust process and temp-profile cleanup for browser instances."""

import atexit
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

import psutil

from debug_logger import debug_logger


class ProcessCleanup:
    """Manage tracked browser process cleanup and orphan profile recovery."""

    DEFAULT_ORPHAN_PROFILE_MAX_AGE_SECONDS = 21600
    PROFILE_SWEEP_PREFIX = "uc_"

    def __init__(self):
        """
        Initialize process cleanup state and run startup orphan recovery.

        Returns:
            None
        """
        self.pid_file = Path(os.path.expanduser("~/.stealth_browser_pids.json"))
        self.tracked_pids: Set[int] = set()
        self.browser_processes: Dict[str, Dict[str, Any]] = {}
        self.orphan_profile_max_age_seconds = self._parse_nonnegative_int_env(
            "BROWSER_ORPHAN_PROFILE_MAX_AGE",
            self.DEFAULT_ORPHAN_PROFILE_MAX_AGE_SECONDS,
        )
        self._setup_cleanup_handlers()
        self._recover_orphaned_processes()

    @staticmethod
    def _parse_nonnegative_int_env(name: str, default: int) -> int:
        """
        Parse a non-negative integer environment variable.

        Args:
            name (str): Environment variable name.
            default (int): Fallback value if parsing fails.

        Returns:
            int: Parsed value or fallback default.
        """
        value = os.getenv(name)
        if value is None:
            return default
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return parsed

    @staticmethod
    def _normalize_path(path: Optional[str]) -> Optional[str]:
        """
        Normalize a filesystem path for safe comparison.

        Args:
            path (Optional[str]): Path to normalize.

        Returns:
            Optional[str]: Normalized path string or None.
        """
        if not path:
            return None
        return os.path.normcase(os.path.normpath(str(path)))

    @staticmethod
    def _is_browser_process_name(process_name: str) -> bool:
        """
        Determine whether a process name belongs to a supported browser.

        Args:
            process_name (str): Process executable name.

        Returns:
            bool: True when the process looks like a Chromium-family browser.
        """
        normalized_name = (process_name or "").lower()
        return any(
            marker in normalized_name
            for marker in ("chrome", "chromium", "msedge", "edge", "brave")
        )

    @classmethod
    def _extract_profile_dir_from_cmdline(
        cls,
        cmdline: list[str],
    ) -> Optional[str]:
        """
        Extract the user-data-dir argument from a browser command line.

        Args:
            cmdline (list[str]): Process command line.

        Returns:
            Optional[str]: Normalized user-data-dir path if present.
        """
        for index, arg in enumerate(cmdline):
            if arg.startswith("--user-data-dir="):
                return cls._normalize_path(arg.split("=", 1)[1])
            if arg == "--user-data-dir" and index + 1 < len(cmdline):
                return cls._normalize_path(cmdline[index + 1])
        return None

    @classmethod
    def _normalize_process_metadata(
        cls,
        raw_processes: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """
        Normalize old and new PID-file formats into the current metadata shape.

        Args:
            raw_processes (Dict[str, Any]): Raw metadata loaded from disk.

        Returns:
            Dict[str, Dict[str, Any]]: Normalized process metadata keyed by instance id.
        """
        normalized: Dict[str, Dict[str, Any]] = {}
        for instance_id, raw_value in raw_processes.items():
            if isinstance(raw_value, int):
                metadata = {
                    "pid": raw_value,
                    "user_data_dir": None,
                    "uses_custom_data_dir": None,
                    "timestamp": 0,
                }
            elif isinstance(raw_value, dict):
                pid = raw_value.get("pid")
                if not isinstance(pid, int):
                    continue
                metadata = {
                    "pid": pid,
                    "user_data_dir": raw_value.get("user_data_dir"),
                    "uses_custom_data_dir": raw_value.get("uses_custom_data_dir"),
                    "timestamp": raw_value.get("timestamp", 0),
                }
            else:
                continue

            metadata["user_data_dir"] = cls._normalize_path(metadata["user_data_dir"])
            normalized[instance_id] = metadata

        return normalized

    def _setup_cleanup_handlers(self):
        """
        Register process cleanup hooks for normal interpreter shutdown and signals.

        Returns:
            None
        """
        atexit.register(self._cleanup_all_tracked)

        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._signal_handler)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, self._signal_handler)

        if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """
        Handle interpreter termination signals by cleaning tracked browser resources.

        Args:
            signum: Signal number.
            frame: Current stack frame.

        Returns:
            None
        """
        debug_logger.log_info(
            "process_cleanup",
            "signal_handler",
            f"Received signal {signum}, initiating cleanup...",
        )
        self._cleanup_all_tracked()
        sys.exit(0)

    def _load_tracked_pids(self) -> Dict[str, Dict[str, Any]]:
        """
        Load tracked browser metadata from disk.

        Returns:
            Dict[str, Dict[str, Any]]: Tracked browser metadata keyed by instance id.
        """
        try:
            if not self.pid_file.exists():
                return {}
            with open(self.pid_file, "r") as file_handle:
                data = json.load(file_handle)
            return self._normalize_process_metadata(data.get("browser_processes", {}))
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                "load_pids",
                f"Failed to load PID file: {error}",
            )
            return {}

    def _save_tracked_pids(self):
        """
        Persist tracked browser metadata to disk.

        Returns:
            None
        """
        try:
            data = {
                "browser_processes": self.browser_processes,
                "timestamp": time.time(),
            }
            with open(self.pid_file, "w") as file_handle:
                json.dump(data, file_handle)
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                "save_pids",
                f"Failed to save PID file: {error}",
            )

    def _get_active_browser_profile_dirs(self) -> Set[str]:
        """
        Collect browser profile directories used by currently running browser processes.

        Returns:
            Set[str]: Normalized active browser profile directories.
        """
        active_profile_dirs: Set[str] = set()
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                process_name = process.info.get("name") or ""
                if not self._is_browser_process_name(process_name):
                    continue
                cmdline = process.info.get("cmdline") or []
                profile_dir = self._extract_profile_dir_from_cmdline(cmdline)
                if profile_dir:
                    active_profile_dirs.add(profile_dir)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "active_profiles",
                    f"Failed inspecting process {getattr(process, 'pid', 'unknown')}: {error}",
                )
        return active_profile_dirs

    def _get_browser_pids_for_profile(self, user_data_dir: Optional[str]) -> Set[int]:
        """
        Collect all live browser PIDs currently using a specific profile directory.

        Args:
            user_data_dir (Optional[str]): Browser profile directory to match.

        Returns:
            Set[int]: Matching browser process ids.
        """
        normalized_profile_dir = self._normalize_path(user_data_dir)
        if normalized_profile_dir is None:
            return set()

        matching_pids: Set[int] = set()
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                process_name = process.info.get("name") or ""
                if not self._is_browser_process_name(process_name):
                    continue
                cmdline = process.info.get("cmdline") or []
                profile_dir = self._extract_profile_dir_from_cmdline(cmdline)
                if profile_dir == normalized_profile_dir:
                    matching_pids.add(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "profile_pids",
                    f"Failed inspecting process {getattr(process, 'pid', 'unknown')}: {error}",
                )

        return matching_pids

    def _kill_processes_for_metadata(
        self,
        instance_id: str,
        metadata: Dict[str, Any],
    ) -> bool:
        """
        Kill all browser processes associated with tracked metadata.

        Args:
            instance_id (str): Browser instance id.
            metadata (Dict[str, Any]): Tracked process metadata.

        Returns:
            bool: True if all associated browser processes were killed or already absent.
        """
        pids_to_kill = self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        fallback_pid = metadata.get("pid")
        if not pids_to_kill and isinstance(fallback_pid, int):
            pids_to_kill = {fallback_pid}
        if not pids_to_kill:
            return True

        success = True
        for pid in sorted(pids_to_kill):
            if not self._kill_process_by_pid(pid, instance_id):
                success = False
        return success

    def _cleanup_profile_dir(
        self,
        profile_dir: str,
        instance_id: str,
        active_profile_dirs: Optional[Set[str]] = None,
    ) -> bool:
        """
        Remove a browser temp profile directory when it is safe to do so.

        Args:
            profile_dir (str): Profile directory to remove.
            instance_id (str): Browser instance id for diagnostics.
            active_profile_dirs (Optional[Set[str]]): Active profile set used to avoid deleting in-use directories.

        Returns:
            bool: True if the directory was removed or already absent.
        """
        normalized_profile_dir = self._normalize_path(profile_dir)
        if normalized_profile_dir is None:
            return False

        path = Path(profile_dir)
        if not path.exists():
            return True

        for attempt in range(5):
            current_active_profiles = (
                active_profile_dirs
                if active_profile_dirs is not None and attempt == 0
                else self._get_active_browser_profile_dirs()
            )
            if normalized_profile_dir in current_active_profiles:
                if attempt == 4:
                    debug_logger.log_info(
                        "process_cleanup",
                        "cleanup_profile",
                        f"Skipping active profile directory for {instance_id}: {profile_dir}",
                    )
                    return False
                time.sleep(0.15)
                continue
            try:
                shutil.rmtree(path, ignore_errors=False)
                debug_logger.log_info(
                    "process_cleanup",
                    "cleanup_profile",
                    f"Removed temp profile for {instance_id}: {profile_dir}",
                )
                return True
            except FileNotFoundError:
                return True
            except (PermissionError, OSError) as error:
                if attempt == 4:
                    debug_logger.log_warning(
                        "process_cleanup",
                        "cleanup_profile",
                        f"Failed to remove temp profile for {instance_id}: {error}",
                    )
                    return False
                time.sleep(0.15)

        return False

    def _cleanup_profile_for_metadata(
        self,
        instance_id: str,
        metadata: Dict[str, Any],
        active_profile_dirs: Optional[Set[str]] = None,
    ) -> bool:
        """
        Remove an auto-generated profile directory described by tracked metadata.

        Args:
            instance_id (str): Browser instance id.
            metadata (Dict[str, Any]): Persisted process metadata.
            active_profile_dirs (Optional[Set[str]]): Active profile set used to avoid deleting live directories.

        Returns:
            bool: True if cleanup succeeded or nothing needed to be removed.
        """
        if metadata.get("uses_custom_data_dir") is True:
            return False

        profile_dir = metadata.get("user_data_dir")
        if not profile_dir:
            return False

        return self._cleanup_profile_dir(profile_dir, instance_id, active_profile_dirs)

    def _sweep_orphaned_temp_profiles(self) -> int:
        """
        Sweep stale nodriver temp profiles from the system temp directory on startup.

        Returns:
            int: Number of stale temp profile directories removed.
        """
        if self.orphan_profile_max_age_seconds == 0:
            return 0

        temp_root = Path(tempfile.gettempdir())
        if not temp_root.exists():
            return 0

        active_profile_dirs = self._get_active_browser_profile_dirs()
        removed_count = 0
        now = time.time()

        try:
            candidates = list(temp_root.glob(f"{self.PROFILE_SWEEP_PREFIX}*"))
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                "sweep_profiles",
                f"Failed to enumerate temp profiles: {error}",
            )
            return 0

        for candidate in candidates:
            try:
                if not candidate.is_dir():
                    continue
                normalized_candidate = self._normalize_path(str(candidate))
                if normalized_candidate in active_profile_dirs:
                    continue
                age_seconds = now - candidate.stat().st_mtime
                if age_seconds < self.orphan_profile_max_age_seconds:
                    continue
                if self._cleanup_profile_dir(
                    str(candidate),
                    "startup-sweep",
                    active_profile_dirs=active_profile_dirs,
                ):
                    removed_count += 1
            except FileNotFoundError:
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "sweep_profiles",
                    f"Failed processing {candidate}: {error}",
                )

        if removed_count:
            debug_logger.log_info(
                "process_cleanup",
                "sweep_profiles",
                f"Removed {removed_count} stale temp profile directories",
            )

        return removed_count

    def _recover_orphaned_processes(self):
        """
        Recover from previous-run orphan browsers and abandoned temp profiles.

        Returns:
            None
        """
        saved_processes = self._load_tracked_pids()
        recovered_count = 0

        for instance_id, metadata in saved_processes.items():
            try:
                if self._kill_processes_for_metadata(instance_id, metadata):
                    recovered_count += 1
                self._cleanup_profile_for_metadata(instance_id, metadata)
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "recovery",
                    f"Failed recovering {instance_id}: {error}",
                )

        if recovered_count:
            debug_logger.log_info(
                "process_cleanup",
                "recovery",
                f"Killed {recovered_count} orphaned browser processes",
            )

        self._clear_pid_file()
        self._sweep_orphaned_temp_profiles()

    def track_browser_process(
        self,
        instance_id: str,
        browser_process,
        user_data_dir: Optional[str] = None,
        uses_custom_data_dir: Optional[bool] = None,
    ) -> bool:
        """
        Track a browser process and its profile metadata for future cleanup.

        Args:
            instance_id: Browser instance identifier.
            browser_process: Browser process object with `.pid`.
            user_data_dir: Browser profile directory.
            uses_custom_data_dir: Whether the profile directory was explicitly provided by the user.

        Returns:
            bool: True if tracking was successful.
        """
        try:
            if not hasattr(browser_process, "pid") or not browser_process.pid:
                debug_logger.log_warning(
                    "process_cleanup",
                    "track_process",
                    f"Browser process for {instance_id} has no PID",
                )
                return False

            pid = browser_process.pid
            metadata = {
                "pid": pid,
                "user_data_dir": self._normalize_path(user_data_dir),
                "uses_custom_data_dir": uses_custom_data_dir,
                "timestamp": time.time(),
            }
            self.browser_processes[instance_id] = metadata
            self.tracked_pids.add(pid)
            self._save_tracked_pids()

            debug_logger.log_info(
                "process_cleanup",
                "track_process",
                f"Tracking browser process {pid} for instance {instance_id}",
                metadata,
            )
            return True
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "track_process",
                error,
            )
            return False

    def untrack_browser_process(self, instance_id: str) -> bool:
        """
        Stop tracking a browser process and persist the updated metadata file.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if untracking was successful.
        """
        try:
            metadata = self.browser_processes.get(instance_id)
            if metadata is None:
                return False

            pid = metadata.get("pid")
            if isinstance(pid, int):
                self.tracked_pids.discard(pid)
            del self.browser_processes[instance_id]

            if self.browser_processes:
                self._save_tracked_pids()
            else:
                self._clear_pid_file()

            debug_logger.log_info(
                "process_cleanup",
                "untrack_process",
                f"Stopped tracking process {pid} for instance {instance_id}",
            )
            return True
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "untrack_process",
                error,
            )
            return False

    def kill_browser_process(self, instance_id: str) -> bool:
        """
        Kill a specific tracked browser process and clean its temp profile when appropriate.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if the process was killed or already gone.
        """
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        success = self._kill_processes_for_metadata(instance_id, metadata)
        if success:
            active_profile_dirs = self._get_active_browser_profile_dirs()
            cleaned = self._cleanup_profile_for_metadata(
                instance_id,
                metadata,
                active_profile_dirs=active_profile_dirs,
            )
            if (
                cleaned
                or metadata.get("uses_custom_data_dir") is True
                or not metadata.get("user_data_dir")
            ):
                self.untrack_browser_process(instance_id)
            else:
                metadata["pid"] = None
                self.browser_processes[instance_id] = metadata
                self._save_tracked_pids()
        return success

    def finalize_browser_process(self, instance_id: str) -> bool:
        """
        Finalize tracked metadata after a browser was stopped elsewhere.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if the tracked process was fully finalized.
        """
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        pid = metadata.get("pid")
        profile_pids = self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        if profile_pids:
            return False
        if isinstance(pid, int) and psutil.pid_exists(pid):
            return False

        active_profile_dirs = self._get_active_browser_profile_dirs()
        cleaned = self._cleanup_profile_for_metadata(
            instance_id,
            metadata,
            active_profile_dirs=active_profile_dirs,
        )
        if (
            cleaned
            or metadata.get("uses_custom_data_dir") is True
            or not metadata.get("user_data_dir")
        ):
            self.untrack_browser_process(instance_id)
            return True

        metadata["pid"] = None
        self.browser_processes[instance_id] = metadata
        self._save_tracked_pids()
        return False

    def cleanup_deferred_profiles(self) -> int:
        """
        Retry cleanup for tracked temp profiles whose browser process is already gone.

        Returns:
            int: Number of deferred profile entries fully finalized.
        """
        finalized_count = 0
        active_profile_dirs = self._get_active_browser_profile_dirs()

        for instance_id in list(self.browser_processes.keys()):
            metadata = self.browser_processes.get(instance_id)
            if metadata is None:
                continue

            pid = metadata.get("pid")
            if isinstance(pid, int) and psutil.pid_exists(pid):
                continue

            cleaned = self._cleanup_profile_for_metadata(
                instance_id,
                metadata,
                active_profile_dirs=active_profile_dirs,
            )
            if (
                cleaned
                or metadata.get("uses_custom_data_dir") is True
                or not metadata.get("user_data_dir")
            ):
                if self.untrack_browser_process(instance_id):
                    finalized_count += 1

        if finalized_count:
            debug_logger.log_info(
                "process_cleanup",
                "cleanup_deferred_profiles",
                f"Finalized {finalized_count} deferred browser profile cleanup entrie(s)",
            )

        return finalized_count

    def _kill_process_by_pid(self, pid: int, instance_id: str = "unknown") -> bool:
        """
        Kill a browser process by PID using escalating termination methods.

        Args:
            pid: Process ID to kill.
            instance_id: Instance identifier for diagnostics.

        Returns:
            bool: True if the process was killed or already absent.
        """
        try:
            if not psutil.pid_exists(pid):
                debug_logger.log_info(
                    "process_cleanup",
                    "kill_process",
                    f"Process {pid} for {instance_id} already terminated",
                )
                return True

            try:
                process = psutil.Process(pid)
                process_name = process.name()
                if not self._is_browser_process_name(process_name):
                    debug_logger.log_warning(
                        "process_cleanup",
                        "kill_process",
                        f"PID {pid} is not a browser process ({process_name}), skipping",
                    )
                    return False
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "kill_process",
                    f"Could not verify process {pid}: {error}",
                )

            try:
                process = psutil.Process(pid)
                process.terminate()
                try:
                    process.wait(timeout=3)
                    debug_logger.log_info(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} terminated gracefully",
                    )
                    return True
                except psutil.TimeoutExpired:
                    pass
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "kill_process",
                    f"Failed to terminate process {pid} gracefully: {error}",
                )

            try:
                process = psutil.Process(pid)
                process.kill()
                try:
                    process.wait(timeout=2)
                    debug_logger.log_info(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} force killed",
                    )
                    return True
                except psutil.TimeoutExpired:
                    debug_logger.log_warning(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} did not die after force kill",
                    )
                    return False
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_error(
                    "process_cleanup",
                    "kill_process",
                    error,
                )
                return False
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "kill_process",
                error,
            )
            return False

    def _cleanup_all_tracked(self):
        """
        Clean up all tracked browser processes and temp profiles for the current run.

        Returns:
            None
        """
        if not self.browser_processes:
            debug_logger.log_info(
                "process_cleanup",
                "cleanup_all",
                "No browser processes to clean up",
            )
            return

        debug_logger.log_info(
            "process_cleanup",
            "cleanup_all",
            f"Cleaning up {len(self.browser_processes)} browser processes...",
        )

        cleaned_count = 0
        for instance_id in list(self.browser_processes.keys()):
            if self.kill_browser_process(instance_id) or self.finalize_browser_process(instance_id):
                cleaned_count += 1

        debug_logger.log_info(
            "process_cleanup",
            "cleanup_all",
            f"Cleaned up {cleaned_count} tracked browser process entries",
        )

        if self.browser_processes:
            self._save_tracked_pids()
        else:
            self._clear_pid_file()

    def _clear_pid_file(self):
        """
        Remove the persisted PID metadata file.

        Returns:
            None
        """
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                "clear_pid_file",
                f"Failed to clear PID file: {error}",
            )

    def get_tracked_processes(self) -> Dict[str, int]:
        """
        Return currently tracked browser PIDs keyed by instance id.

        Returns:
            Dict[str, int]: Mapping of instance id to process id.
        """
        return {
            instance_id: metadata["pid"]
            for instance_id, metadata in self.browser_processes.items()
            if isinstance(metadata.get("pid"), int)
        }

    def is_process_alive(self, instance_id: str) -> bool:
        """
        Check whether a tracked process is still alive.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if the tracked process still exists.
        """
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        pid = metadata.get("pid")
        if not isinstance(pid, int):
            return bool(self._get_browser_pids_for_profile(metadata.get("user_data_dir")))

        return psutil.pid_exists(pid) or bool(
            self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        )


process_cleanup = ProcessCleanup()
