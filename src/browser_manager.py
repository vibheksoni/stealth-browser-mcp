"""Browser instance management with nodriver."""

import json
import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from datetime import datetime

import nodriver as uc
from nodriver import Browser, Tab

import cdp_raw
from debug_logger import debug_logger
from models import BrowserInstance, BrowserState, BrowserOptions, PageState
from persistent_storage import persistent_storage
from dynamic_hook_system import dynamic_hook_system
from platform_utils import get_platform_info, check_browser_executable, merge_browser_args
from process_cleanup import process_cleanup
from proxy_forwarder import AuthenticatedProxyForwarder
from proxy_utils import (
    ProxyConfig,
    ProxyConfigError,
    merge_proxy_server_arg,
    parse_proxy_config,
    redact_launch_arg,
)

HEADLESS_UA_TOKEN = "HeadlessChrome/"
HEADLESS_UA_PROBE_TIMEOUT = 20.0
CLOSE_STEP_TIMEOUT = 2.0
TARGET_UPDATE_EVENTS = (
    uc.cdp.target.TargetInfoChanged,
    uc.cdp.target.TargetCreated,
    uc.cdp.target.TargetDestroyed,
    uc.cdp.target.TargetCrashed,
)
PROCESS_EXIT_TIMEOUT = 3.0


def _parse_nonnegative_int_env(
    name: str,
    default: int,
    minimum: int = 0,
) -> int:
    """
    Parse a non-negative integer environment variable with a fallback default.

    Args:
        name (str): Environment variable name.
        default (int): Fallback value if parsing fails.
        minimum (int): Minimum accepted value.

    Returns:
        int: Parsed integer or the provided default.
    """
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return parsed


class BrowserManager:
    """Manages multiple browser instances."""

    DEFAULT_IDLE_TIMEOUT_SECONDS = 600
    DEFAULT_IDLE_REAPER_INTERVAL_SECONDS = 60

    def __init__(self):
        self._instances: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._spawn_diagnostics: Dict[str, Dict[str, Any]] = {}
        self._proxy_forwarders: Dict[str, AuthenticatedProxyForwarder] = {}
        self._idle_timeout_seconds_default = _parse_nonnegative_int_env(
            "BROWSER_IDLE_TIMEOUT",
            self.DEFAULT_IDLE_TIMEOUT_SECONDS,
        )
        self._idle_reaper_interval_seconds = _parse_nonnegative_int_env(
            "BROWSER_IDLE_REAPER_INTERVAL",
            self.DEFAULT_IDLE_REAPER_INTERVAL_SECONDS,
            minimum=1,
        )
        self._idle_reaper_task: Optional[asyncio.Task] = None
        self._headless_user_agents: Dict[Tuple[str, int], Optional[str]] = {}
        self._close_listeners: List[Callable[[str], Awaitable[Any]]] = []
        self._tab_listeners: List[Callable[[Tab, str, BrowserOptions], Awaitable[Any]]] = []
        self._tab_recycle_navigations = _parse_nonnegative_int_env(
            "BROWSER_TAB_RECYCLE_NAVIGATIONS",
            0,
        )

    @staticmethod
    def _append_user_agent_arg(args: List[str], user_agent: Optional[str]) -> List[str]:
        """Merge a user agent override into launch arguments."""
        if not user_agent:
            return args
        ua_prefix = "--user-agent="
        filtered = [arg for arg in args if not arg.startswith(ua_prefix)]
        filtered.append(f"{ua_prefix}{user_agent}")
        return filtered

    @staticmethod
    def _has_user_agent_arg(args: List[str]) -> bool:
        """
        Check whether launch arguments already set a user agent.

        Args:
            args (List[str]): Browser launch arguments.

        Returns:
            bool: True when a --user-agent argument is present.
        """
        return any(arg.startswith("--user-agent=") for arg in args)

    @staticmethod
    def _headful_user_agent(user_agent: str) -> Optional[str]:
        """
        Remove the headless marker from a Chrome user agent.

        Args:
            user_agent (str): User agent reported by the browser.

        Returns:
            Optional[str]: User agent with HeadlessChrome replaced by Chrome, or None when no marker is present.
        """
        if HEADLESS_UA_TOKEN not in user_agent:
            return None
        return user_agent.replace(HEADLESS_UA_TOKEN, "Chrome/")

    async def _resolve_headless_user_agent(
        self,
        browser_executable: str,
        sandbox: bool,
        launch_args: List[str],
    ) -> Optional[str]:
        """
        Find the user agent a headless browser should launch with.

        Headless Chrome reports HeadlessChrome in navigator.userAgent, workers,
        and the User-Agent header. A short probe launch reads the real string,
        and the cleaned value is cached per executable build so later spawns
        skip the probe.

        Args:
            browser_executable (str): Browser executable path.
            sandbox (bool): Whether the sandbox is enabled.
            launch_args (List[str]): Launch arguments for the real spawn.

        Returns:
            Optional[str]: Cleaned user agent, or None when the probe fails or no marker is present.
        """
        try:
            build = os.stat(browser_executable).st_mtime_ns
        except OSError:
            build = 0
        cache_key = (browser_executable, build)
        if cache_key in self._headless_user_agents:
            return self._headless_user_agents[cache_key]

        probe: Optional[Browser] = None
        probe_id = f"ua-probe-{uuid.uuid4()}"
        user_agent: Optional[str] = None
        try:
            probe = await asyncio.wait_for(
                uc.start(config=uc.Config(
                    headless=True,
                    sandbox=sandbox,
                    browser_executable_path=browser_executable,
                    browser_args=list(launch_args),
                )),
                timeout=HEADLESS_UA_PROBE_TIMEOUT,
            )
            if getattr(probe, "_process", None):
                process_cleanup.track_browser_process(
                    probe_id,
                    probe._process,
                    user_data_dir=getattr(probe.config, "user_data_dir", None),
                    uses_custom_data_dir=False,
                )
            version = await asyncio.wait_for(
                probe.main_tab.send(uc.cdp.browser.get_version()),
                timeout=HEADLESS_UA_PROBE_TIMEOUT,
            )
            user_agent = self._headful_user_agent(version[3])
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager",
                "resolve_headless_user_agent",
                f"Could not read the headless user agent: {error}",
            )
            return None
        finally:
            if probe is not None:
                try:
                    await self._shutdown_browser(probe)
                except Exception:
                    pass
                try:
                    await asyncio.to_thread(process_cleanup.kill_browser_process, probe_id)
                except Exception:
                    pass
        self._headless_user_agents[cache_key] = user_agent
        return user_agent

    @staticmethod
    def _build_spawn_diagnostics(
        *,
        launch_args: List[str],
        proxy_server: Optional[str],
        launch_proxy_server: Optional[str],
        timezone_id: Optional[str],
        idle_timeout_seconds: int,
        sandbox: bool,
        headless: bool,
        user_data_dir: Optional[str],
    ) -> Dict[str, Any]:
        """Build redacted diagnostics for a spawned browser instance."""
        return {
            "effective_browser_args": [redact_launch_arg(arg) for arg in launch_args],
            "proxy_server": proxy_server,
            "launch_proxy_server": launch_proxy_server,
            "timezone_id": timezone_id,
            "idle_timeout_seconds": idle_timeout_seconds,
            "sandbox": sandbox,
            "headless": headless,
            "user_data_dir": user_data_dir,
        }

    @staticmethod
    async def _apply_timezone_override(
        *,
        tab: Tab,
        timezone_id: Optional[str],
    ) -> Optional[str]:
        """Apply a CDP timezone override to a browser tab."""
        if not timezone_id:
            return None

        trimmed_timezone = timezone_id.strip()
        if not trimmed_timezone:
            return None

        await tab.send(uc.cdp.emulation.set_timezone_override(timezone_id=trimmed_timezone))
        return trimmed_timezone

    @staticmethod
    def _finalize_process_cleanup(instance_id: str, kill_first: bool = False) -> None:
        """
        Finish process and profile cleanup for a closed instance.

        Blocking, meant to run in a worker thread.

        Args:
            instance_id (str): Browser instance ID.
            kill_first (bool): Kill the tracked process before finalizing.

        Returns:
            None
        """
        if kill_first:
            process_cleanup.kill_browser_process(instance_id)
        process_cleanup.finalize_browser_process(instance_id)
        process_cleanup.cleanup_deferred_profiles()

    async def _close_proxy_forwarder(self, instance_id: str) -> None:
        """Close and forget any authenticated proxy forwarder for an instance."""
        proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)
        if proxy_forwarder is None:
            return
        await proxy_forwarder.close()

    def _resolve_idle_timeout_seconds(
        self,
        override: Optional[int],
    ) -> int:
        """
        Resolve the effective idle timeout for a browser instance.

        Args:
            override (Optional[int]): Optional per-instance override.

        Returns:
            int: Effective idle timeout in seconds. Zero disables reaping.
        """
        if self._idle_timeout_seconds_default == 0:
            return 0
        if override is None:
            return self._idle_timeout_seconds_default
        return max(int(override), 0)

    async def touch_instance(self, instance_id: str) -> bool:
        """
        Update the last-activity timestamp for a browser instance.

        Args:
            instance_id (str): Browser instance id.

        Returns:
            bool: True if the instance exists and was touched.
        """
        async with self._lock:
            if instance_id not in self._instances:
                return False
            self._instances[instance_id]["instance"].update_activity()
            return True

    async def _run_idle_reaper(self) -> None:
        """Periodically close idle browser instances until cancelled."""
        try:
            while True:
                await asyncio.sleep(self._idle_reaper_interval_seconds)
                try:
                    closed_count = await self.cleanup_inactive()
                    finalized_profiles = await asyncio.to_thread(process_cleanup.cleanup_deferred_profiles)
                    if closed_count:
                        debug_logger.log_info(
                            "browser_manager",
                            "idle_reaper",
                            f"Closed {closed_count} idle browser instance(s)",
                        )
                    if finalized_profiles:
                        debug_logger.log_info(
                            "browser_manager",
                            "idle_reaper",
                            f"Finalized {finalized_profiles} deferred temp profile cleanup entrie(s)",
                        )
                except Exception as error:
                    debug_logger.log_error(
                        "browser_manager",
                        "idle_reaper",
                        error,
                    )
        except asyncio.CancelledError:
            debug_logger.log_info(
                "browser_manager",
                "idle_reaper",
                "Idle reaper task cancelled",
            )
            raise

    async def start_idle_reaper(self) -> None:
        """
        Start the background idle reaper task when globally enabled.

        Returns:
            None
        """
        if self._idle_timeout_seconds_default == 0:
            debug_logger.log_info(
                "browser_manager",
                "start_idle_reaper",
                "Idle reaper disabled by BROWSER_IDLE_TIMEOUT=0",
            )
            return
        if self._idle_reaper_task and not self._idle_reaper_task.done():
            return
        self._idle_reaper_task = asyncio.create_task(self._run_idle_reaper())
        debug_logger.log_info(
            "browser_manager",
            "start_idle_reaper",
            f"Idle reaper started with timeout={self._idle_timeout_seconds_default}s interval={self._idle_reaper_interval_seconds}s",
        )

    async def stop_idle_reaper(self) -> None:
        """
        Stop the background idle reaper task if it is running.

        Returns:
            None
        """
        if not self._idle_reaper_task:
            return
        if self._idle_reaper_task.done():
            self._idle_reaper_task = None
            return
        self._idle_reaper_task.cancel()
        try:
            await self._idle_reaper_task
        except asyncio.CancelledError:
            pass
        self._idle_reaper_task = None

    async def spawn_browser(self, options: BrowserOptions) -> BrowserInstance:
        """
        Spawn a new browser instance with given options.

        Args:
            options (BrowserOptions): Options for browser configuration.

        Returns:
            BrowserInstance: The spawned browser instance.
        """
        instance_id = str(uuid.uuid4())

        instance = BrowserInstance(
            instance_id=instance_id,
            headless=options.headless,
            user_agent=options.user_agent,
            viewport={"width": options.viewport_width, "height": options.viewport_height}
        )

        browser: Optional[Browser] = None
        proxy_forwarder: Optional[AuthenticatedProxyForwarder] = None
        try:
            platform_info = get_platform_info()
            proxy_config: Optional[ProxyConfig] = None
            launch_proxy_server: Optional[str] = None
            idle_timeout_seconds = self._resolve_idle_timeout_seconds(
                options.idle_timeout_seconds,
            )
            if options.proxy:
                try:
                    proxy_config = parse_proxy_config(options.proxy)
                except ProxyConfigError as error:
                    raise Exception(str(error))
                if proxy_config.username is not None:
                    proxy_forwarder = AuthenticatedProxyForwarder(options.proxy)
                    await proxy_forwarder.start()
                    launch_proxy_server = proxy_forwarder.proxy_server
                else:
                    launch_proxy_server = proxy_config.server
            
            # Detect the best available browser executable (Chrome, Chromium, or Edge)
            browser_executable = check_browser_executable()
            if not browser_executable:
                raise Exception("No compatible browser found (Chrome, Chromium, or Microsoft Edge)")
            
            # Identify browser type for logging
            browser_type = "Unknown"
            if 'edge' in browser_executable.lower() or 'msedge' in browser_executable.lower():
                browser_type = "Microsoft Edge"
            elif 'chromium' in browser_executable.lower():
                browser_type = "Chromium"
            elif 'chrome' in browser_executable.lower():
                browser_type = "Google Chrome"
            
            debug_logger.log_info(
                "browser_manager",
                "spawn_browser",
                f"Platform: {platform_info['system']} | Root: {platform_info['is_root']} | Container: {platform_info['is_container']} | Sandbox: {options.sandbox} | Browser: {browser_type} ({browser_executable})"
            )

            caller_args = list(options.browser_args or [])
            caller_args = self._append_user_agent_arg(caller_args, options.user_agent)
            caller_args = merge_proxy_server_arg(
                caller_args,
                launch_proxy_server,
            )
            launch_args = merge_browser_args(caller_args)
            if options.headless and not self._has_user_agent_arg(launch_args):
                headful_user_agent = await self._resolve_headless_user_agent(
                    browser_executable,
                    options.sandbox,
                    launch_args,
                )
                launch_args = self._append_user_agent_arg(launch_args, headful_user_agent)

            config = uc.Config(
                headless=options.headless,
                user_data_dir=options.user_data_dir,
                sandbox=options.sandbox,
                browser_executable_path=browser_executable,
                browser_args=launch_args
            )

            browser = await uc.start(config=config)
            tab = browser.main_tab
            config_obj = getattr(browser, "config", None)
            actual_user_data_dir = getattr(config_obj, "user_data_dir", options.user_data_dir)
            uses_custom_data_dir = getattr(
                config_obj,
                "uses_custom_data_dir",
                bool(options.user_data_dir),
            )

            if hasattr(browser, '_process') and browser._process:
                process_cleanup.track_browser_process(
                    instance_id,
                    browser._process,
                    user_data_dir=actual_user_data_dir,
                    uses_custom_data_dir=uses_custom_data_dir,
                )
            else:
                debug_logger.log_warning("browser_manager", "spawn_browser", 
                                       f"Browser {instance_id} has no process to track")

            await tab.set_window_size(
                left=0,
                top=0, 
                width=options.viewport_width,
                height=options.viewport_height
            )
            debug_logger.log_info(
                "browser_manager",
                "spawn_browser",
                f"Set viewport to {options.viewport_width}x{options.viewport_height}",
            )

            init_scripts: List[Dict[str, Any]] = []
            configured_tabs: set = set()
            applied_timezone_id = await self._configure_tab(
                tab,
                instance_id,
                options,
                init_scripts,
                configured_tabs,
            )
            await self._setup_dynamic_hooks(tab, instance_id)

            spawn_diagnostics = self._build_spawn_diagnostics(
                launch_args=launch_args,
                proxy_server=proxy_config.server if proxy_config else None,
                launch_proxy_server=launch_proxy_server,
                timezone_id=applied_timezone_id,
                idle_timeout_seconds=idle_timeout_seconds,
                sandbox=options.sandbox,
                headless=options.headless,
                user_data_dir=actual_user_data_dir,
            )
            self._spawn_diagnostics[instance_id] = spawn_diagnostics
            if proxy_forwarder is not None:
                self._proxy_forwarders[instance_id] = proxy_forwarder

            async with self._lock:
                self._instances[instance_id] = {
                    'browser': browser,
                    'tab': tab,
                    'instance': instance,
                    'options': options,
                    'navigation_count': 0,
                    'init_scripts': init_scripts,
                    'configured_tabs': configured_tabs,
                    'idle_timeout_seconds': idle_timeout_seconds,
                    'spawn_diagnostics': spawn_diagnostics,
                    'network_data': []
                }

            instance.state = BrowserState.READY
            instance.update_activity()

            persistent_storage.store_instance(instance_id, {
                'state': instance.state.value,
                'created_at': instance.created_at.isoformat(),
                'current_url': getattr(tab, 'url', ''),
                'title': 'Browser Instance'
            })

        except BaseException as e:
            try:
                await dynamic_hook_system.cleanup_instance(instance_id)
            except Exception:
                pass
            if browser is not None:
                try:
                    await self._shutdown_browser(browser)
                except Exception:
                    pass
            if proxy_forwarder is not None:
                try:
                    await proxy_forwarder.close()
                except Exception:
                    pass
            try:
                await asyncio.to_thread(process_cleanup.kill_browser_process, instance_id)
            except Exception:
                pass
            instance.state = BrowserState.ERROR
            if not isinstance(e, Exception):
                raise
            raise Exception(f"Failed to spawn browser: {str(e)}") from e

        return instance
    
    async def _setup_dynamic_hooks(self, tab: Tab, instance_id: str) -> bool:
        """Setup dynamic hook system for browser instance."""
        try:
            dynamic_hook_system.add_instance(instance_id)

            await dynamic_hook_system.setup_interception(tab, instance_id)

            debug_logger.log_info(
                "browser_manager",
                "_setup_dynamic_hooks",
                f"Dynamic hook system setup complete for instance {instance_id}",
            )

            return True

        except Exception as e:
            debug_logger.log_error(
                "browser_manager",
                "_setup_dynamic_hooks",
                f"Failed to setup dynamic hooks for {instance_id}: {e}",
            )
            return False

    async def get_instance(self, instance_id: str) -> Optional[dict]:
        """
        Get browser instance by ID.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[dict]: The browser instance data if found, else None.
        """
        async with self._lock:
            return self._instances.get(instance_id)

    async def list_instances(self) -> List[BrowserInstance]:
        """
        List all browser instances.

        Returns:
            List[BrowserInstance]: List of all browser instances.
        """
        async with self._lock:
            return [data['instance'] for data in self._instances.values()]

    async def close_instance(self, instance_id: str) -> bool:
        """
        Close and remove a browser instance.

        The instance is removed first so other calls stop using it, then
        Chrome is asked to exit over CDP, the connections are dropped, the
        process is waited on (and killed if it lingers), and process, profile,
        proxy, and listener cleanup runs.

        Args:
            instance_id (str): The ID of the browser instance to close.

        Returns:
            bool: True if the instance existed and was closed, False otherwise.
        """
        async with self._lock:
            data = self._instances.pop(instance_id, None)
        if data is None:
            return False

        browser: Browser = data["browser"]
        data["instance"].state = BrowserState.CLOSED
        self._spawn_diagnostics.pop(instance_id, None)
        persistent_storage.remove_instance(instance_id)

        try:
            await asyncio.wait_for(dynamic_hook_system.cleanup_instance(instance_id), CLOSE_STEP_TIMEOUT)
        except Exception:
            dynamic_hook_system.cancel_instance(instance_id)

        await self._shutdown_browser(browser)

        try:
            await asyncio.to_thread(self._finalize_process_cleanup, instance_id, True)
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager",
                "close_instance",
                f"Process cleanup failed for {instance_id}: {error}",
            )

        try:
            await self._close_proxy_forwarder(instance_id)
        except Exception:
            pass

        await self._notify_close_listeners(instance_id)
        return True

    async def _shutdown_browser(self, browser: Browser) -> None:
        """
        Stop a browser process and its CDP connections without hanging.

        Args:
            browser (Browser): Browser to stop.

        Returns:
            None
        """
        connection = getattr(browser, "connection", None)
        if connection is not None:
            handlers = getattr(connection, "handlers", {})
            for event_type in TARGET_UPDATE_EVENTS:
                handlers.pop(event_type, None)
            try:
                await asyncio.wait_for(connection.send(uc.cdp.browser.close()), CLOSE_STEP_TIMEOUT)
            except Exception:
                pass

        process = getattr(browser, "_process", None)
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), PROCESS_EXIT_TIMEOUT)
            except Exception:
                for signal_process in (process.terminate, process.kill):
                    try:
                        signal_process()
                        await asyncio.wait_for(process.wait(), PROCESS_EXIT_TIMEOUT)
                        break
                    except ProcessLookupError:
                        break
                    except Exception:
                        continue

        connections = [getattr(tab, "disconnect", None) for tab in list(getattr(browser, "targets", []) or [])]
        if connection is not None:
            connections.append(connection.disconnect)
        disconnects = [disconnect() for disconnect in connections if disconnect is not None]
        if disconnects:
            await asyncio.wait(
                [asyncio.ensure_future(item) for item in disconnects],
                timeout=CLOSE_STEP_TIMEOUT,
            )
        browser._process = None
        browser._process_pid = None

    def add_close_listener(self, listener: Callable[[str], Awaitable[Any]]) -> None:
        """
        Register a coroutine called with the instance id after an instance closes.

        Covers every close path: the close tool, idle reaping, and shutdown.

        Args:
            listener (Callable[[str], Awaitable[Any]]): Cleanup coroutine function.

        Returns:
            None
        """
        if listener not in self._close_listeners:
            self._close_listeners.append(listener)

    async def _notify_close_listeners(self, instance_id: str) -> None:
        """
        Run close listeners, logging failures instead of raising.

        Args:
            instance_id (str): Closed instance ID.

        Returns:
            None
        """
        for listener in list(self._close_listeners):
            try:
                await asyncio.wait_for(listener(instance_id), CLOSE_STEP_TIMEOUT)
            except Exception as error:
                debug_logger.log_warning(
                    "browser_manager",
                    "close_listener",
                    f"Close listener failed for {instance_id}: {error}",
                )

    async def get_spawn_diagnostics(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get spawn diagnostics for an instance."""
        return self._spawn_diagnostics.get(instance_id)

    @staticmethod
    def _get_tab_target_id(tab: Optional[Tab]) -> Optional[str]:
        """Get a stable target id string for a tab when available."""
        if tab is None:
            return None
        target = getattr(tab, "target", None)
        target_id = getattr(target, "target_id", None)
        if target_id is None:
            return None
        return str(target_id)

    @staticmethod
    def _is_recoverable_navigation_error(error: Exception) -> bool:
        """Return whether a navigation error should trigger one stale-tab recovery attempt."""
        if isinstance(error, asyncio.TimeoutError):
            return True

        message = f"{type(error).__name__}: {error}".lower()
        recoverable_markers = (
            "connection dropped",
            "connection closed",
            "connection lost",
            "websocket",
            "target closed",
            "target crashed",
            "session closed",
            "invalid state",
            "not attached",
        )
        return any(marker in message for marker in recoverable_markers)

    async def _replace_main_tab(
        self,
        instance_id: str,
        reason: str,
        close_existing: bool = True,
    ) -> Optional[Tab]:
        """
        Replace the tracked main tab for an instance with a fresh about:blank tab.

        Args:
            instance_id (str): Browser instance id.
            reason (str): Diagnostic reason for replacement.
            close_existing (bool): Whether to close the previously tracked tab.

        Returns:
            Optional[Tab]: The fresh tab, or None if the instance was missing.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None

        browser = data["browser"]
        previous_tab = data.get("tab")
        try:
            new_tab = await browser.get("about:blank", new_tab=True)
        except Exception:
            new_tab = await browser.get("about:blank", new_window=True)
            options: BrowserOptions = data["options"]
            try:
                await new_tab.set_window_size(
                    left=0,
                    top=0,
                    width=options.viewport_width,
                    height=options.viewport_height,
                )
            except Exception:
                pass

        if close_existing and previous_tab:
            previous_target_id = self._get_tab_target_id(previous_tab)
            new_target_id = self._get_tab_target_id(new_tab)
            if previous_target_id and previous_target_id != new_target_id:
                try:
                    await previous_tab.close()
                except Exception:
                    pass

        await self._set_current_tab(instance_id, new_tab)
        async with self._lock:
            if instance_id in self._instances:
                self._instances[instance_id]["navigation_count"] = 0

        debug_logger.log_info(
            "browser_manager",
            "_replace_main_tab",
            f"Replaced main tab for {instance_id}: {reason}",
        )
        return new_tab

    async def get_navigation_tab(self, instance_id: str) -> Optional[Tab]:
        """
        Get a healthy tab for navigation, recovering from stale tracked tabs when needed.

        Args:
            instance_id (str): Browser instance id.

        Returns:
            Optional[Tab]: A valid navigation tab, or None if the instance does not exist.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None

        browser = data["browser"]
        tracked_tab = data.get("tab")
        navigation_count = data.get("navigation_count", 0)

        if (
            self._tab_recycle_navigations > 0
            and navigation_count >= self._tab_recycle_navigations
        ):
            return await self._replace_main_tab(
                instance_id,
                reason=f"navigation recycle threshold {self._tab_recycle_navigations} reached",
            )

        try:
            await browser.update_targets()
            tracked_target_id = self._get_tab_target_id(tracked_tab)
            if tracked_target_id:
                for candidate_tab in browser.tabs:
                    if self._get_tab_target_id(candidate_tab) == tracked_target_id:
                        return candidate_tab

            if browser.tabs:
                fallback_tab = browser.tabs[0]
                await self._set_current_tab(instance_id, fallback_tab)
                return fallback_tab
        except Exception as error:
            debug_logger.log_warning(
                "browser_manager",
                "get_navigation_tab",
                f"Tab health check failed for {instance_id}: {error}",
            )

        return await self._replace_main_tab(
            instance_id,
            reason="tracked tab missing or invalid",
            close_existing=False,
        )

    @staticmethod
    def _navigation_event_type(wait_until: str) -> type:
        """
        Map a wait condition to the CDP page event that signals it.

        Args:
            wait_until (str): Desired wait condition.

        Returns:
            type: CDP event class to listen for.
        """
        if wait_until == "domcontentloaded":
            return uc.cdp.page.DomContentEventFired
        return uc.cdp.page.LoadEventFired

    @staticmethod
    def _remove_tab_handler(tab: Tab, event_type: type, handler: Any) -> None:
        """
        Remove one event handler from a tab.

        nodriver's remove_handler deletes every handler registered for the
        event type, so the callback is removed from the handler list directly.

        Args:
            tab (Tab): Browser tab.
            event_type (type): CDP event class the handler was registered for.
            handler (Any): Callback to remove.
        """
        callbacks = tab.handlers.get(event_type)
        if callbacks and handler in callbacks:
            callbacks.remove(handler)

    @staticmethod
    async def _wait_for_navigation_condition(
        wait_until: str,
        timeout_seconds: float,
        fired: asyncio.Event,
    ) -> None:
        """
        Wait for a navigation milestone within the remaining timeout budget.

        Part of the budget is kept in reserve so a page that never fires its
        load event still returns its URL and title instead of failing.

        Args:
            wait_until (str): Desired wait condition.
            timeout_seconds (float): Remaining timeout budget in seconds.
            fired (asyncio.Event): Set by the page event handler registered before navigation.
        """
        if timeout_seconds <= 0:
            raise asyncio.TimeoutError("Navigation wait budget exhausted")

        if wait_until == "networkidle":
            await asyncio.sleep(min(timeout_seconds, 2.0))
            return

        wait_budget = timeout_seconds - min(2.0, timeout_seconds * 0.2)
        try:
            await asyncio.wait_for(fired.wait(), timeout=wait_budget)
        except asyncio.TimeoutError:
            debug_logger.log_warning(
                "browser_manager",
                "navigate",
                f"Page did not reach '{wait_until}' within {wait_budget:.1f}s, returning current state",
            )

    async def navigate(
        self,
        instance_id: str,
        url: str,
        wait_until: str = "load",
        timeout: int = 30000,
        referrer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Navigate with timeout enforcement and one automatic tab-recovery retry.

        Args:
            instance_id (str): Browser instance id.
            url (str): Target URL.
            wait_until (str): Wait condition after navigation.
            timeout (int): Timeout in milliseconds.
            referrer (Optional[str]): Optional referrer header.

        Returns:
            Dict[str, Any]: Navigation result payload.
        """
        timeout_seconds = max(timeout, 1) / 1000
        last_error: Optional[Exception] = None

        for attempt in range(2):
            await self.touch_instance(instance_id)
            if attempt == 0:
                tab = await self.get_navigation_tab(instance_id)
            else:
                tab = await self._replace_main_tab(
                    instance_id,
                    reason=f"recovering after navigation failure: {type(last_error).__name__ if last_error else 'unknown'}",
                )

            if not tab:
                raise Exception(f"Instance not found: {instance_id}")

            start_time = time.monotonic()
            fired = asyncio.Event()
            event_type = self._navigation_event_type(wait_until)
            handler = lambda _event: fired.set()
            tab.add_handler(event_type, handler)

            try:
                await asyncio.wait_for(
                    tab.send(uc.cdp.page.navigate(url, referrer=referrer)),
                    timeout=timeout_seconds,
                )

                elapsed = time.monotonic() - start_time
                await self._wait_for_navigation_condition(
                    wait_until,
                    timeout_seconds - elapsed,
                    fired,
                )

                elapsed = time.monotonic() - start_time
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError("Navigation result budget exhausted")

                final_url = await asyncio.wait_for(
                    tab.evaluate("window.location.href"),
                    timeout=remaining,
                )
                title = await asyncio.wait_for(
                    tab.evaluate("document.title"),
                    timeout=remaining,
                )

                await self.update_instance_state(instance_id, final_url, title)

                async with self._lock:
                    if instance_id in self._instances:
                        self._instances[instance_id]["tab"] = tab
                        self._instances[instance_id]["navigation_count"] = (
                            self._instances[instance_id].get("navigation_count", 0) + 1
                        )

                return {
                    "url": final_url,
                    "title": title,
                    "success": True,
                }
            except Exception as error:
                last_error = error
                debug_logger.log_warning(
                    "browser_manager",
                    "navigate",
                    f"Navigation attempt {attempt + 1} failed for {instance_id}: {error}",
                    {"url": url, "attempt": attempt + 1},
                )
                if attempt == 1 or not self._is_recoverable_navigation_error(error):
                    if isinstance(error, asyncio.TimeoutError):
                        raise Exception(
                            f"Navigation to {url} timed out after {timeout}ms"
                        ) from error
                    raise
            finally:
                self._remove_tab_handler(tab, event_type, handler)

    async def get_tab(
        self,
        instance_id: str,
        touch_activity: bool = True,
    ) -> Optional[Tab]:
        """
        Get the main tab for a browser instance.

        Args:
            instance_id (str): The ID of the browser instance.
            touch_activity (bool): Whether retrieving the tab should refresh last activity.

        If the tracked tab was closed or crashed, another open tab (or a fresh
        configured tab) takes its place.

        Returns:
            Optional[Tab]: The main tab if found, else None.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return None
        if touch_activity:
            await self.touch_instance(instance_id)
        tab = data['tab']
        if not self._tab_is_open(data['browser'], tab):
            return await self.get_navigation_tab(instance_id) or tab
        return tab

    def _tab_is_open(self, browser: Browser, tab: Optional[Tab]) -> bool:
        """
        Check whether a tab is still among the browser's live targets.

        nodriver removes targets from browser.targets when Chrome reports them
        destroyed, so this needs no CDP round trip.

        Args:
            browser (Browser): Owning browser.
            tab (Optional[Tab]): Tab to check.

        Returns:
            bool: False only when the browser knows its targets and the tab is not among them.
        """
        targets = getattr(browser, "targets", None)
        target_id = self._get_tab_target_id(tab)
        if not targets or target_id is None:
            return True
        return any(self._get_tab_target_id(target) == target_id for target in targets)

    async def get_browser(
        self,
        instance_id: str,
        touch_activity: bool = True,
    ) -> Optional[Browser]:
        """
        Get the browser object for an instance.

        Args:
            instance_id (str): The ID of the browser instance.
            touch_activity (bool): Whether retrieving the browser should refresh last activity.

        Returns:
            Optional[Browser]: The browser object if found, else None.
        """
        data = await self.get_instance(instance_id)
        if data:
            if touch_activity:
                await self.touch_instance(instance_id)
            return data['browser']
        return None

    def add_tab_listener(self, listener: Callable[[Tab, str, BrowserOptions], Awaitable[Any]]) -> None:
        """
        Register a coroutine that prepares every tab an instance uses.

        Listeners run once per tab, on spawn and whenever a tab is opened,
        switched to, or replaced, so per-tab CDP state is never lost.

        Args:
            listener (Callable[[Tab, str, BrowserOptions], Awaitable[Any]]): Coroutine taking (tab, instance_id, options).

        Returns:
            None
        """
        if listener not in self._tab_listeners:
            self._tab_listeners.append(listener)

    async def _configure_tab(
        self,
        tab: Tab,
        instance_id: str,
        options: BrowserOptions,
        init_scripts: List[Dict[str, Any]],
        configured_tabs: set,
    ) -> Optional[str]:
        """
        Apply per-tab state: extra headers, timezone, init scripts, and tab listeners.

        Each tab is configured once. CDP overrides such as the timezone and
        extra headers belong to a tab session, so a tab that skipped this
        would leak the real timezone or miss headers.

        Args:
            tab (Tab): Tab to configure.
            instance_id (str): Owning instance ID.
            options (BrowserOptions): Spawn options.
            init_scripts (List[Dict[str, Any]]): Init scripts installed for the instance.
            configured_tabs (set): Target ids already configured for the instance.

        Returns:
            Optional[str]: Applied timezone id, or None when no timezone override is set.
        """
        target_id = self._get_tab_target_id(tab)
        if target_id and target_id in configured_tabs:
            return options.timezone_id
        if options.extra_headers:
            await tab.send(uc.cdp.network.set_extra_http_headers(
                headers=uc.cdp.network.Headers(options.extra_headers)
            ))
        applied_timezone_id = await self._apply_timezone_override(
            tab=tab,
            timezone_id=options.timezone_id,
        )
        for script in init_scripts:
            await self._install_init_script(tab, **script)
        for listener in list(self._tab_listeners):
            try:
                await listener(tab, instance_id, options)
            except Exception as error:
                debug_logger.log_warning(
                    "browser_manager",
                    "configure_tab",
                    f"Tab listener failed for {instance_id}: {error}",
                )
        if target_id:
            configured_tabs.add(target_id)
        return applied_timezone_id

    async def _ensure_tab_configured(self, instance_id: str, tab: Tab) -> None:
        """
        Configure a tab of a registered instance if it has not been configured.

        Args:
            instance_id (str): Browser instance ID.
            tab (Tab): Tab to configure.

        Returns:
            None
        """
        data = self._instances.get(instance_id)
        if data is None:
            return
        await self._configure_tab(
            tab,
            instance_id,
            data["options"],
            data.setdefault("init_scripts", []),
            data.setdefault("configured_tabs", set()),
        )

    async def _set_current_tab(self, instance_id: str, tab: Tab) -> None:
        """
        Make a tab the instance's current tab.

        The tab is configured if needed and dynamic hook interception moves to it.

        Args:
            instance_id (str): Browser instance ID.
            tab (Tab): New current tab.

        Returns:
            None
        """
        await self._ensure_tab_configured(instance_id, tab)
        await self._setup_dynamic_hooks(tab, instance_id)
        async with self._lock:
            if instance_id in self._instances:
                self._instances[instance_id]["tab"] = tab

    async def open_tab(self, instance_id: str, url: str = "about:blank") -> Tab:
        """
        Open a configured tab and load a URL in it.

        The tab starts blank and is configured before the URL loads, so the
        first page already sees the timezone, headers, and init scripts.

        Args:
            instance_id (str): Browser instance ID.
            url (str): URL to load.

        Returns:
            Tab: The new tab.
        """
        data = await self.get_instance(instance_id)
        if not data:
            raise Exception(f"Instance not found: {instance_id}")
        tab = await data["browser"].get("about:blank", new_tab=True)
        await self._ensure_tab_configured(instance_id, tab)
        if url and url != "about:blank":
            await tab.send(uc.cdp.page.navigate(url))
        return tab

    @staticmethod
    async def _install_init_script(
        tab: Tab,
        source: str,
        world_name: Optional[str] = None,
        include_command_line_api: Optional[bool] = None,
        run_immediately: bool = True,
    ) -> str:
        """
        Install an init script on one tab.

        Page.enable is sent first because Chrome silently ignores
        Page.addScriptToEvaluateOnNewDocument without it.

        Args:
            tab (Tab): Target tab.
            source (str): JavaScript source.
            world_name (Optional[str]): Isolated world name, or None for the main world.
            include_command_line_api (Optional[bool]): Whether command line API is available.
            run_immediately (bool): Whether to run the script in existing contexts too.

        Returns:
            str: Script identifier.
        """
        await tab.send(uc.cdp.page.enable())
        identifier = await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(
            source=source,
            world_name=world_name,
            include_command_line_api=include_command_line_api,
            run_immediately=run_immediately,
        ))
        return str(identifier)

    async def add_init_script(
        self,
        instance_id: str,
        source: str,
        world_name: Optional[str] = None,
        include_command_line_api: Optional[bool] = None,
        run_immediately: bool = True,
    ) -> str:
        """
        Install JavaScript that runs before page scripts on each new document.

        The script is remembered and installed on every tab the instance
        uses later, including replacement and newly opened tabs.

        Args:
            instance_id (str): Browser instance ID.
            source (str): JavaScript source to evaluate before page scripts.
            world_name (Optional[str]): Isolated world name, or None for the main world.
            include_command_line_api (Optional[bool]): Whether command line API is available.
            run_immediately (bool): Whether to run the script in existing contexts too.

        Returns:
            str: Identifier of the installed script.
        """
        data = await self.get_instance(instance_id)
        if not data:
            raise Exception(f"Instance not found: {instance_id}")
        await self.touch_instance(instance_id)
        script = {
            "source": source,
            "world_name": world_name,
            "include_command_line_api": include_command_line_api,
            "run_immediately": run_immediately,
        }
        identifier = await self._install_init_script(data["tab"], **script)
        data.setdefault("init_scripts", []).append(script)
        return identifier

    async def list_tabs(self, instance_id: str) -> List[Dict[str, str]]:
        """
        List all tabs for a browser instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            List[Dict[str, str]]: List of tab information dictionaries.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return []

        await browser.update_targets()

        tabs = []
        for tab in browser.tabs:
            tabs.append({
                'tab_id': str(tab.target.target_id),
                'url': getattr(tab, 'url', '') or '',
                'title': getattr(tab.target, 'title', '') or 'Untitled',
                'type': getattr(tab.target, 'type_', 'page')
            })

        return tabs

    async def switch_to_tab(self, instance_id: str, tab_id: str) -> bool:
        """
        Switch to a specific tab by bringing it to front.

        Args:
            instance_id (str): The ID of the browser instance.
            tab_id (str): The target ID of the tab to switch to.

        Returns:
            bool: True if switched successfully, False otherwise.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        await browser.update_targets()

        target_tab = None
        for tab in browser.tabs:
            if str(tab.target.target_id) == tab_id:
                target_tab = tab
                break

        if not target_tab:
            return False

        try:
            await target_tab.bring_to_front()
            await self._set_current_tab(instance_id, target_tab)

            return True
        except Exception:
            return False

    async def get_active_tab(self, instance_id: str) -> Optional[Tab]:
        """
        Get the currently active tab.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[Tab]: The active tab if found, else None.
        """
        return await self.get_tab(instance_id)

    async def close_tab(self, instance_id: str, tab_id: str) -> bool:
        """
        Close a specific tab.

        When the closed tab is the instance's current tab, another open tab
        becomes current. Closing the last tab opens a blank tab first so the
        browser stays alive.

        Args:
            instance_id (str): The ID of the browser instance.
            tab_id (str): The target ID of the tab to close.

        Returns:
            bool: True if closed successfully, False otherwise.
        """
        data = await self.get_instance(instance_id)
        if not data:
            return False
        browser = data["browser"]
        try:
            await browser.update_targets()
        except Exception:
            pass

        target_tab = next(
            (tab for tab in browser.tabs if self._get_tab_target_id(tab) == tab_id),
            None,
        )
        if not target_tab:
            return False

        is_current = self._get_tab_target_id(data.get("tab")) == tab_id
        remaining = [tab for tab in browser.tabs if self._get_tab_target_id(tab) != tab_id]
        if not remaining:
            await self._replace_main_tab(
                instance_id,
                reason="closing the last open tab",
                close_existing=False,
            )

        try:
            await target_tab.close()
        except Exception:
            return False

        configured_tabs = data.get("configured_tabs")
        if configured_tabs is not None:
            configured_tabs.discard(tab_id)
        if is_current and remaining:
            await self._set_current_tab(instance_id, remaining[0])
        return True

    async def update_instance_state(self, instance_id: str, url: Optional[str] = None, title: Optional[str] = None):
        """
        Update instance state after navigation or action.

        Args:
            instance_id (str): The ID of the browser instance.
            url (str, optional): The current URL to update.
            title (str, optional): The title to update.
        """
        async with self._lock:
            if instance_id in self._instances:
                instance = self._instances[instance_id]['instance']
                if url:
                    instance.current_url = url
                if title:
                    instance.title = title
        await self.touch_instance(instance_id)

    @staticmethod
    async def _evaluate_json(tab: Any, expression: str) -> Any:
        """
        Evaluate an expression and return its value as plain Python data.

        ``Tab.evaluate`` always sends CDP serialization options, so Chrome ignores
        ``returnByValue`` and objects come back as deep-serialized property lists.
        Stringifying inside the page and parsing here sidesteps that.

        Args:
            tab (Any): The nodriver tab.
            expression (str): A JavaScript expression producing a JSON-serializable value.

        Returns:
            Any: The decoded value, or None if the page returned nothing.
        """
        raw = await tab.evaluate(f"JSON.stringify(({expression}))")
        if raw is None or not isinstance(raw, str):
            return None
        return json.loads(raw)

    @staticmethod
    def _cookies_to_dicts(raw: Any) -> List[Dict[str, Any]]:
        """
        Normalize the result of ``Network.getCookies`` into plain dicts.

        nodriver parses the CDP response into ``cdp.network.Cookie`` dataclasses
        and returns them as a list. Older code expected the raw JSON dict with a
        ``cookies`` key, so both shapes are accepted here.

        Args:
            raw (Any): A list of Cookie objects or dicts, or a dict with a
                ``cookies`` key, or None.

        Returns:
            List[Dict[str, Any]]: Cookies as JSON-serializable dicts.
        """
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get('cookies', [])
        cookies: List[Dict[str, Any]] = []
        for cookie in raw:
            if isinstance(cookie, dict):
                cookies.append(cookie)
            elif hasattr(cookie, 'to_json'):
                cookies.append(cookie.to_json())
            else:
                cookies.append(dict(vars(cookie)))
        return cookies

    async def get_page_state(self, instance_id: str) -> Optional[PageState]:
        """
        Get complete page state for an instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[PageState]: The page state if available, else None.
        """
        tab = await self.get_tab(instance_id)
        if not tab:
            return None

        try:
            url = await tab.evaluate("window.location.href")
            title = await tab.evaluate("document.title")
            ready_state = await tab.evaluate("document.readyState")

            cookies = self._cookies_to_dicts(await cdp_raw.get_cookies(tab))

            local_storage = {}
            session_storage = {}

            try:
                local_storage_keys = await self._evaluate_json(tab, "Object.keys(localStorage)")
                for key in local_storage_keys or []:
                    value = await tab.evaluate(
                        f"localStorage.getItem({json.dumps(key)})"
                    )
                    local_storage[key] = value

                session_storage_keys = await self._evaluate_json(tab, "Object.keys(sessionStorage)")
                for key in session_storage_keys or []:
                    value = await tab.evaluate(
                        f"sessionStorage.getItem({json.dumps(key)})"
                    )
                    session_storage[key] = value
            except Exception:
                pass

            viewport = await self._evaluate_json(tab, """
                ({
                    width: window.innerWidth,
                    height: window.innerHeight,
                    devicePixelRatio: window.devicePixelRatio
                })
            """)

            return PageState(
                instance_id=instance_id,
                url=url,
                title=title,
                ready_state=ready_state,
                cookies=cookies,
                local_storage=local_storage,
                session_storage=session_storage,
                viewport=viewport or {}
            )

        except Exception as e:
            raise Exception(f"Failed to get page state: {str(e)}")

    async def cleanup_inactive(self, timeout_seconds: Optional[int] = None) -> int:
        """
        Clean up inactive browser instances.

        Args:
            timeout_seconds (Optional[int]): Override timeout in seconds for all instances. Uses per-instance values when None.

        Returns:
            int: Number of instances selected for idle cleanup.
        """
        now = datetime.now()

        to_close = []
        async with self._lock:
            for instance_id, data in self._instances.items():
                instance = data['instance']
                effective_timeout = (
                    timeout_seconds
                    if timeout_seconds is not None
                    else data.get('idle_timeout_seconds', self._idle_timeout_seconds_default)
                )
                if effective_timeout <= 0:
                    continue
                if (now - instance.last_activity).total_seconds() > effective_timeout:
                    to_close.append(instance_id)

        if to_close:
            await asyncio.gather(
                *(self.close_instance(instance_id) for instance_id in to_close),
                return_exceptions=True,
            )

        return len(to_close)

    async def close_all(self):
        """
        Close all browser instances.

        Closes all currently managed browser instances.
        """
        instance_ids = list(self._instances.keys())
        if instance_ids:
            await asyncio.gather(
                *(self.close_instance(instance_id) for instance_id in instance_ids),
                return_exceptions=True,
            )
