"""Browser instance management with nodriver."""

import inspect
import json
import asyncio
import os
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from datetime import datetime

import nodriver as uc
from nodriver import Browser, Tab

from . import cdp_raw, nodriver_registry
from .browser_launcher import launch_browser, _stop_process, clear_profile_locks
from .dialog_manager import DialogManager
from .debug_logger import debug_logger
from .dom_handler import DOMHandler
from .errors import instance_not_found_error
from .models import BrowserInstance, BrowserState, BrowserOptions, PageState
from .network_idle import NetworkIdleTracker, wait_for_network_idle
from .persistent_storage import persistent_storage
from .dynamic_hook_system import dynamic_hook_system
from .platform_utils import get_platform_info, check_browser_executable, merge_browser_args
from .browser_args_policy import validate_browser_args, validate_sandbox, validate_user_data_dir
from .path_policy import reject_blocked_url
from .process_cleanup import process_cleanup
from .proxy_forwarder import AuthenticatedProxyForwarder
from .proxy_utils import (
    ProxyConfig,
    ProxyConfigError,
    merge_proxy_server_arg,
    parse_proxy_config,
    redact_launch_arg,
)
from .window_mode import (
    X11_OZONE_NOTE,
    apply_window_mode,
    effective_window_mode,
    format_window_bounds,
    headed_display_args,
    headed_display_error,
    open_background_tab,
    resolve_window_mode,
    window_launch_args,
)

HEADLESS_UA_TOKEN = "HeadlessChrome/"
HEADLESS_UA_PROBE_TIMEOUT = 40.0
CLOSE_STEP_TIMEOUT = 2.0
TARGET_UPDATE_EVENTS = (
    uc.cdp.target.TargetInfoChanged,
    uc.cdp.target.TargetCreated,
    uc.cdp.target.TargetDestroyed,
    uc.cdp.target.TargetCrashed,
)
PROCESS_EXIT_TIMEOUT = 15.0


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
    _active_managers: List["BrowserManager"] = []

    def __init__(self):
        if not hasattr(BrowserManager, "_active_managers"):
            BrowserManager._active_managers = []
        if self not in BrowserManager._active_managers:
            BrowserManager._active_managers.append(self)
        self.dialog_manager = DialogManager()
        self._instances: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._debug_endpoints: Dict[tuple, str] = {}
        self._spawn_diagnostics: Dict[str, Dict[str, Any]] = {}
        self._session_infos: Dict[str, Dict[str, Any]] = {}
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
        try:
            from . import session_spawn
            session_spawn.startup_janitor()
        except Exception:
            pass

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
                launch_browser(uc.Config(
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
    def _endpoint_key(host: Optional[str], port: Optional[int]) -> Optional[tuple]:
        """Canonical (host, port) key. Loopback aliases share one key."""
        if port is None:
            return None
        try:
            port_num = int(port)
        except (TypeError, ValueError):
            return None
        from .http_auth import MixedLoopbackHostError, canonical_connect_host
        try:
            label = canonical_connect_host(host)
        except MixedLoopbackHostError:
            label = (str(host).strip().lower() if host else "mixed")
        return (label, port_num)

    def _register_debug_endpoint(self, instance_id: str, host: Optional[str], port: Optional[int]) -> None:
        key = self._endpoint_key(host, port)
        if key is None:
            return
        self._debug_endpoints[key] = instance_id

    def _unregister_debug_endpoints(self, instance_id: str) -> None:
        stale = [key for key, owner in self._debug_endpoints.items() if owner == instance_id]
        for key in stale:
            self._debug_endpoints.pop(key, None)

    def lookup_instance_by_debug_endpoint(self, host: str, port: int) -> Optional[str]:
        """Return the instance id that owns this DevTools endpoint, if any."""
        key = self._endpoint_key(host, port)
        if key is None:
            return None
        instance_id = self._debug_endpoints.get(key)
        if instance_id and instance_id in self._instances:
            return instance_id
        for candidate_id, data in self._instances.items():
            if self._endpoint_key(data.get("debug_host"), data.get("debug_port")) == key:
                return candidate_id
        return None

    @staticmethod
    def _browser_debug_endpoint(browser: Any, fallback_host: Optional[str] = None, fallback_port: Optional[int] = None) -> tuple:
        config = getattr(browser, "config", None)
        host = getattr(config, "host", None) or fallback_host or "127.0.0.1"
        port = getattr(config, "port", None)
        if port is None:
            port = fallback_port
        return host, port

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

    async def spawn_browser(self, options: Optional[BrowserOptions] = None, **kwargs: Any) -> BrowserInstance:
        """
        Spawn a new browser instance with given options.

        Args:
            options (Optional[BrowserOptions]): Options for browser configuration.
            **kwargs: Additional options if options object is not provided.

        Returns:
            BrowserInstance: The spawned browser instance.
        """
        if options is None:
            options = BrowserOptions(**kwargs)
        elif kwargs:
            options = options.model_copy(update=kwargs)

        options = options.model_copy(update={
            "browser_args": validate_browser_args(options.browser_args or []),
            "user_data_dir": validate_user_data_dir(options.user_data_dir),
            "sandbox": validate_sandbox(bool(options.sandbox), http_mode=True),
        })

        if options.user_data_dir:
            target_profile_path = os.path.abspath(os.path.realpath(options.user_data_dir))
            for existing_id, existing_data in list(self._instances.items()):
                existing_opts = existing_data.get("options")
                existing_dir = getattr(existing_opts, "user_data_dir", None)
                if not existing_dir:
                    existing_diag = existing_data.get("spawn_diagnostics") or {}
                    existing_dir = existing_diag.get("user_data_dir")
                if existing_dir and os.path.abspath(os.path.realpath(existing_dir)) == target_profile_path:
                    raise Exception(f"Failed to spawn browser: profile in use by instance {existing_id}")

        mode = resolve_window_mode(options.window, options.headless)
        mode, window_note = effective_window_mode(mode)
        display_error = headed_display_error(mode)
        if display_error:
            raise Exception(display_error)
        ozone_args = headed_display_args(mode)
        if ozone_args:
            window_note = f"{window_note} {X11_OZONE_NOTE}".strip() if window_note else X11_OZONE_NOTE
        options = options.model_copy(update={"headless": mode == "headless", "window": mode})

        instance_id = str(uuid.uuid4())

        instance = BrowserInstance(
            instance_id=instance_id,
            headless=options.headless,
            window=mode,
            window_note=window_note,
            user_agent=options.user_agent,
            viewport={"width": options.viewport_width, "height": options.viewport_height}
        )

        browser: Optional[Browser] = None
        proxy_forwarder: Optional[AuthenticatedProxyForwarder] = None
        session_id: Optional[str] = None
        session_dir: Optional[Path] = None
        session_info: Optional[Dict[str, Any]] = None
        args_modifier: Optional[Callable[[List[str]], List[str]]] = None
        clone_ms: float = 0.0
        t_spawn_start = time.monotonic()
        t_launch_start: float = 0.0
        spawned_process: Optional[Any] = None

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

            if options.seed:
                from . import session_spawn
                session_id, session_dir, seed_data = session_spawn.prepare_session_spawn(
                    seed_name=options.seed,
                    cap=getattr(options, "cap", None),
                )
                clone_ms = session_spawn.clone_seed_to_session(
                    options.seed,
                    session_id,
                    seed_data,
                )
                options.user_data_dir = str(session_dir)
                source_browser = seed_data.get("source_browser", "chrome")
                browser_executable = session_spawn.find_family_binary(source_browser)

                caller_args, args_modifier = session_spawn.build_spawn_args_and_config(
                    seed_data,
                    list(options.browser_args or []),
                )
                options.browser_args = caller_args
            else:
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
            existing_flags = {arg.split("=", 1)[0].strip() for arg in (options.browser_args or [])}
            for w_arg in list(window_launch_args(mode, options.viewport_width, options.viewport_height)) + ozone_args:
                if w_arg.split("=", 1)[0].strip() not in existing_flags:
                    caller_args.append(w_arg)
                    existing_flags.add(w_arg.split("=", 1)[0].strip())
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

            if args_modifier is not None:
                if hasattr(config, "_default_browser_args"):
                    config._default_browser_args = [
                        arg for arg in config._default_browser_args
                        if not arg.startswith("--password-store=")
                    ]
                config.args_modifier = args_modifier

            t_launch_start = time.monotonic()
            spawn_timeout = getattr(options, "spawn_timeout", None)
            if spawn_timeout is None:
                spawn_timeout = kwargs.get("spawn_timeout")
            if spawn_timeout is None:
                spawn_timeout = float(os.environ.get("STEALTH_SPAWN_TIMEOUT", "60.0"))

            spawned_process = None

            def on_process_started(proc: Any) -> None:
                nonlocal spawned_process
                spawned_process = proc
                try:
                    process_cleanup.track_browser_process(
                        instance_id,
                        proc,
                        user_data_dir=options.user_data_dir,
                        uses_custom_data_dir=bool(options.user_data_dir),
                    )
                except Exception:
                    pass

            launch_kwargs: Dict[str, Any] = {"args_modifier": args_modifier}
            try:
                sig = inspect.signature(launch_browser)
                if "timeout" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    launch_kwargs["timeout"] = spawn_timeout
                if "on_process_started" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    launch_kwargs["on_process_started"] = on_process_started
            except Exception:
                pass

            try:
                browser = await asyncio.wait_for(
                    launch_browser(config, **launch_kwargs),
                    timeout=spawn_timeout,
                )
            except TypeError as te:
                if "on_process_started" in str(te) and "on_process_started" in launch_kwargs:
                    launch_kwargs.pop("on_process_started", None)
                    browser = await asyncio.wait_for(
                        launch_browser(config, **launch_kwargs),
                        timeout=spawn_timeout,
                    )
                else:
                    raise
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

            if hasattr(browser, "connection") and browser.connection:
                async def _on_target_created(event: Any, *args: Any) -> None:
                    try:
                        target_info = getattr(event, "target_info", None)
                        if target_info and getattr(target_info, "type_", None) == "page":
                            await asyncio.sleep(0.05)
                            for candidate in getattr(browser, "tabs", []):
                                cand_tid = getattr(getattr(candidate, "target", None), "target_id", None)
                                if cand_tid == target_info.target_id:
                                    await self._configure_tab(
                                        candidate,
                                        instance_id,
                                        options,
                                        init_scripts,
                                        configured_tabs,
                                    )
                                    break
                    except Exception:
                        pass
                if hasattr(browser.connection, "add_handler"):
                    try:
                        browser.connection.add_handler(uc.cdp.target.TargetCreated, _on_target_created)
                    except Exception:
                        pass

            window_bounds: Optional[Dict[str, Any]] = None
            if mode == "normal":
                await tab.set_window_size(
                    left=0,
                    top=0, 
                    width=options.viewport_width,
                    height=options.viewport_height
                )
                try:
                    w_res = await asyncio.wait_for(
                        tab.send(uc.cdp.browser.get_window_for_target()),
                        timeout=3.0,
                    )
                    if isinstance(w_res, (tuple, list)) and len(w_res) >= 2:
                        window_bounds = format_window_bounds(w_res[1])
                    elif hasattr(w_res, "left") or isinstance(w_res, dict):
                        window_bounds = format_window_bounds(w_res)
                except Exception:
                    pass
            try:
                apply_res = await apply_window_mode(
                    tab,
                    mode,
                    options.viewport_width,
                    options.viewport_height,
                )
                if apply_res.get("window_bounds"):
                    window_bounds = apply_res["window_bounds"]
                if apply_res.get("window_note"):
                    if window_note:
                        window_note = f"{window_note}; {apply_res['window_note']}"
                    else:
                        window_note = apply_res["window_note"]
            except Exception as e:
                debug_logger.log_warning(
                    "browser_manager",
                    "spawn_browser",
                    f"Failed to apply window mode {mode} for {instance_id}: {e}",
                )
            instance.window_note = window_note
            instance.window_bounds = window_bounds
            debug_logger.log_info(
                "browser_manager",
                "spawn_browser",
                f"Set viewport to {options.viewport_width}x{options.viewport_height} (window mode: {mode})",
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

            if session_id and options.seed and session_dir is not None:
                from . import session_spawn
                session_info = await session_spawn.finish_session_spawn(
                    session_id=session_id,
                    seed_name=options.seed,
                    session_dir=session_dir,
                    browser=browser,
                    clone_ms=clone_ms,
                    t_launch_start=t_launch_start,
                    t_spawn_start=t_spawn_start,
                    check_url=options.check_url,
                )
                self._session_infos[instance_id] = session_info
                instance.session_id = session_id

            async with self._lock:
                self._instances[instance_id] = {
                    'browser': browser,
                    'tab': tab,
                    'instance': instance,
                    'options': options,
                    'window': mode,
                    'window_note': window_note,
                    'window_bounds': window_bounds,
                    'navigation_count': 0,
                    'init_scripts': init_scripts,
                    'configured_tabs': configured_tabs,
                    'idle_timeout_seconds': idle_timeout_seconds,
                    'spawn_diagnostics': spawn_diagnostics,
                    'network_data': [],
                    'session_id': session_id,
                    'session_info': session_info,
                    'debug_host': None,
                    'debug_port': None,
                }
                debug_host, debug_port = self._browser_debug_endpoint(browser, "127.0.0.1", None)
                self._instances[instance_id]['debug_host'] = debug_host
                self._instances[instance_id]['debug_port'] = debug_port
                self._register_debug_endpoint(instance_id, debug_host, debug_port)

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
            if session_id:
                try:
                    from . import session_spawn
                    session_spawn.cleanup_session(session_id, keep=False)
                except Exception:
                    pass
            if spawned_process is not None:
                try:
                    await _stop_process(spawned_process)
                except Exception:
                    pass
            try:
                await asyncio.to_thread(process_cleanup.kill_browser_process, instance_id)
            except Exception:
                pass
            target_profile = options.user_data_dir
            err_str = str(e)
            profile_held = ("code 21" in err_str) or ("profile in use" in err_str)
            # Only clear locks this spawn created. If Chrome exited because
            # the profile is already in use, leave the holder's lock alone.
            if target_profile and not profile_held:
                clear_profile_locks(target_profile)
            async with self._lock:
                self._instances.pop(instance_id, None)
            self._spawn_diagnostics.pop(instance_id, None)
            self.dialog_manager.clear_instance(instance_id)
            instance.state = BrowserState.ERROR

            if profile_held:
                holder_id = None
                if target_profile:
                    norm_tp = os.path.abspath(os.path.realpath(target_profile))
                    for eid, edata in list(self._instances.items()):
                        eopts = edata.get("options")
                        edir = getattr(eopts, "user_data_dir", None)
                        if edir and os.path.abspath(os.path.realpath(edir)) == norm_tp:
                            holder_id = eid
                            break
                if holder_id:
                    raise Exception(f"Failed to spawn browser: profile in use by instance {holder_id}") from e
                raise Exception("Failed to spawn browser: profile in use") from e

            if isinstance(e, asyncio.TimeoutError):
                raise Exception(f"Failed to spawn browser: launch timed out after {spawn_timeout}s") from e

            if not isinstance(e, Exception):
                raise
            raise Exception(f"Failed to spawn browser: {str(e)}") from e

        return instance

    async def attach_browser(
        self,
        port: int = 9222,
        host: str = "127.0.0.1",
        window: str = "keep",
    ) -> BrowserInstance:
        """
        Attach to an already running browser instance via its remote debugging port.

        Args:
            port (int): Remote debugging port.
            host (str): Debugging host.
            window (str): Window mode ('keep').

        Returns:
            BrowserInstance: The attached browser instance.
        """
        instance_id = str(uuid.uuid4())
        instance = BrowserInstance(
            instance_id=instance_id,
            headless=False,
            window=window,
            window_note=None,
            viewport={"width": 1920, "height": 1080},
            attached=True,
        )

        browser: Optional[Browser] = None
        try:
            config = uc.Config(
                host=host,
                port=port,
            )

            browser = await uc.Browser.create(config)
            tab = browser.main_tab

            try:
                await tab.send(uc.cdp.emulation.set_focus_emulation_enabled(True))
            except Exception:
                pass

            options = BrowserOptions(
                window=window,
            )

            init_scripts: List[Dict[str, Any]] = []
            configured_tabs: set = set()
            await self._configure_tab(
                tab,
                instance_id,
                options,
                init_scripts,
                configured_tabs,
            )
            await self._setup_dynamic_hooks(tab, instance_id)

            if hasattr(browser, "connection") and browser.connection:
                async def _on_target_created(event: Any, *args: Any) -> None:
                    try:
                        target_info = getattr(event, "target_info", None)
                        if target_info and getattr(target_info, "type_", None) == "page":
                            await asyncio.sleep(0.05)
                            for candidate in getattr(browser, "tabs", []):
                                cand_tid = getattr(getattr(candidate, "target", None), "target_id", None)
                                if cand_tid == target_info.target_id:
                                    await self._configure_tab(
                                        candidate,
                                        instance_id,
                                        options,
                                        init_scripts,
                                        configured_tabs,
                                    )
                                    break
                    except Exception:
                        pass
                if hasattr(browser.connection, "add_handler"):
                    try:
                        browser.connection.add_handler(uc.cdp.target.TargetCreated, _on_target_created)
                    except Exception:
                        pass

            spawn_diagnostics = {
                "attached": True,
                "host": host,
                "port": port,
                "window": window,
            }
            self._spawn_diagnostics[instance_id] = spawn_diagnostics

            async with self._lock:
                self._instances[instance_id] = {
                    'browser': browser,
                    'tab': tab,
                    'instance': instance,
                    'options': options,
                    'window': window,
                    'window_note': None,
                    'navigation_count': 0,
                    'init_scripts': init_scripts,
                    'configured_tabs': configured_tabs,
                    'idle_timeout_seconds': 0,
                    'spawn_diagnostics': spawn_diagnostics,
                    'network_data': [],
                    'session_id': None,
                    'session_info': None,
                    'attached': True,
                    'debug_host': host,
                    'debug_port': port,
                }
                self._register_debug_endpoint(instance_id, host, port)

            instance.state = BrowserState.READY
            instance.update_activity()

            persistent_storage.store_instance(instance_id, {
                'state': instance.state.value,
                'created_at': instance.created_at.isoformat(),
                'current_url': getattr(tab, 'url', ''),
                'title': 'Browser Instance (Attached)',
                'attached': True,
            })

            return instance
        except BaseException as e:
            try:
                await dynamic_hook_system.cleanup_instance(instance_id)
            except Exception:
                pass
            if browser is not None:
                try:
                    await self._disconnect_browser(browser)
                except Exception:
                    pass
            instance.state = BrowserState.ERROR
            if not isinstance(e, Exception):
                raise
            raise Exception(f"Failed to attach to browser: {str(e)}") from e

    
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

    async def close_instance(
        self,
        instance_id: str,
        keep: Optional[bool] = None,
        timeout: float = 15.0,
    ) -> bool:
        """
        Close and remove a browser instance.

        The instance is removed first so other calls stop using it, then
        Chrome is asked to exit over CDP, the connections are dropped, the
        process is waited on (and killed if it lingers), and process, profile,
        proxy, and listener cleanup runs.

        Args:
            instance_id (str): The ID of the browser instance to close.
            keep (Optional[bool]): If True, keep session profile directory on disk.
            timeout (float): Maximum seconds to wait for browser process to exit.

        Returns:
            bool: True if the instance existed and was closed, False otherwise.
        """
        async with self._lock:
            data = self._instances.pop(instance_id, None)
            if data is None:
                for i_id, inst_data in list(self._instances.items()):
                    if inst_data.get("session_id") == instance_id:
                        data = self._instances.pop(i_id, None)
                        instance_id = i_id
                        break
        if data is None:
            return False

        browser: Browser = data["browser"]
        data["instance"].state = BrowserState.CLOSED
        self._spawn_diagnostics.pop(instance_id, None)
        self._unregister_debug_endpoints(instance_id)
        persistent_storage.remove_instance(instance_id)

        try:
            await asyncio.wait_for(dynamic_hook_system.cleanup_instance(instance_id), CLOSE_STEP_TIMEOUT)
        except Exception:
            dynamic_hook_system.cancel_instance(instance_id)

        is_attached = data.get("attached", False) or getattr(data.get("instance"), "attached", False)

        if is_attached:
            await self._disconnect_browser(browser)
        else:
            await self._shutdown_browser(browser, process_exit_timeout=timeout)

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

            session_id = data.get("session_id")
            options = data.get("options")
            effective_keep = keep if keep is not None else (getattr(options, "keep", False) if options else False)
            if session_id:
                try:
                    from . import session_spawn
                    session_spawn.cleanup_session(session_id, keep=effective_keep)
                except Exception as error:
                    debug_logger.log_warning(
                        "browser_manager",
                        "close_instance",
                        f"Session cleanup failed for {session_id}: {error}",
                    )
            self._session_infos.pop(instance_id, None)

        await self._notify_close_listeners(instance_id)
        return True

    async def _disconnect_browser(self, browser: Any) -> None:
        """Disconnect CDP sessions from an attached browser, then release nodriver's registry reference."""
        try:
            await self._disconnect_browser_impl(browser)
        finally:
            nodriver_registry.release_browser(browser)

    async def _disconnect_browser_impl(self, browser: Any) -> None:
        """
        Disconnect CDP sessions from an attached browser without terminating it.
        """
        connection = getattr(browser, "connection", None)
        if connection is not None:
            handlers = getattr(connection, "handlers", {})
            for event_type in TARGET_UPDATE_EVENTS:
                handlers.pop(event_type, None)

        connections = [getattr(tab, "disconnect", None) for tab in list(getattr(browser, "targets", []) or [])]
        if connection is not None and hasattr(connection, "disconnect"):
            connections.append(connection.disconnect)
        disconnect_coros = []
        for disconnect_fn in connections:
            if callable(disconnect_fn):
                try:
                    res = disconnect_fn()
                    if asyncio.iscoroutine(res):
                        disconnect_coros.append(res)
                except Exception:
                    pass
        if disconnect_coros:
            try:
                await asyncio.wait(
                    [asyncio.ensure_future(item) for item in disconnect_coros],
                    timeout=CLOSE_STEP_TIMEOUT,
                )
            except Exception:
                pass

    async def _shutdown_browser(
        self,
        browser: Browser,
        process_exit_timeout: float = 10.0,
    ) -> None:
        """Stop a browser process and its connections, then release nodriver's registry reference."""
        try:
            await self._shutdown_browser_impl(browser, process_exit_timeout)
        finally:
            nodriver_registry.release_browser(browser)

    async def _shutdown_browser_impl(
        self,
        browser: Browser,
        process_exit_timeout: float = 10.0,
    ) -> None:
        """
        Stop a browser process and its CDP connections without hanging.

        Args:
            browser (Browser): Browser to stop.
            process_exit_timeout (float): Maximum seconds to wait for process exit.

        Returns:
            None
        """
        connection = getattr(browser, "connection", None)
        process = getattr(browser, "_process", None)
        if connection is not None:
            handlers = getattr(connection, "handlers", {})
            for event_type in TARGET_UPDATE_EVENTS:
                handlers.pop(event_type, None)
            try:
                await asyncio.wait_for(connection.send(uc.cdp.browser.close()), CLOSE_STEP_TIMEOUT)
                if process is not None and getattr(process, "returncode", None) is None:
                    try:
                        await asyncio.wait_for(process.wait(), process_exit_timeout)
                    except Exception:
                        pass
            except Exception:
                pass

        if process is not None and getattr(process, "returncode", None) is None:
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

    async def close_session(
        self,
        session_id: str,
        keep: bool = True,
        timeout: float = 15.0,
    ) -> bool:
        """Close browser instance associated with session_id."""
        target_id = None
        async with self._lock:
            for i_id, data in self._instances.items():
                if data.get("session_id") == session_id:
                    target_id = i_id
                    break
        if target_id is not None:
            return await self.close_instance(target_id, keep=keep, timeout=timeout)
        return False

    @classmethod
    async def find_and_close_session_browser(
        cls,
        session_id: str,
        keep: bool = True,
        timeout: float = 15.0,
        manager: Optional["BrowserManager"] = None,
    ) -> bool:
        """Find and gracefully close the browser for a session across all managers."""
        managers = []
        if manager is not None:
            managers.append(manager)
        for m in list(getattr(cls, "_active_managers", [])):
            if m not in managers:
                managers.append(m)

        for mgr in managers:
            if await mgr.close_session(session_id, keep=keep, timeout=timeout):
                return True

        # Fallback: if browser process is recorded in session.json, wait or terminate
        try:
            from . import profiles
            if profiles.session_exists(session_id):
                sdata = profiles.load_session(session_id)
                browser_pid = sdata.get("browser_pid")
                if browser_pid and profiles.pid_alive(browser_pid):
                    import psutil
                    try:
                        proc = psutil.Process(browser_pid)
                        proc.terminate()
                        await asyncio.to_thread(proc.wait, timeout)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass

        return False

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

    async def get_session_info(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get session spawn info if spawned from a seed."""
        return self._session_infos.get(instance_id)

    async def find_instance_id_for_session_or_instance(self, session_or_instance_id: str) -> Optional[str]:
        """Resolve a seed session id or instance id to a live instance id."""
        if session_or_instance_id in self._instances:
            return session_or_instance_id
        for instance_id, data in self._instances.items():
            if data.get("session_id") == session_or_instance_id:
                return instance_id
        if session_or_instance_id in self._session_infos:
            return session_or_instance_id
        return None

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
            return False

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
        options: BrowserOptions = data.get("options")
        mode = getattr(options, "window", "normal") if options else "normal"
        try:
            new_tab = await open_background_tab(browser, "about:blank")
        except Exception:
            new_tab = await open_background_tab(browser, "about:blank", new_window=True)
            if options and mode == "normal":
                try:
                    await new_tab.set_window_size(
                        left=0,
                        top=0,
                        width=options.viewport_width,
                        height=options.viewport_height,
                    )
                except Exception:
                    pass

        if options and mode != "normal":
            try:
                await apply_window_mode(
                    new_tab,
                    mode,
                    options.viewport_width,
                    options.viewport_height,
                )
            except Exception as e:
                debug_logger.log_warning(
                    "browser_manager",
                    "_replace_main_tab",
                    f"Failed to apply window mode {mode}: {e}",
                )

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
        idle_tracker: Optional[NetworkIdleTracker] = None,
        tab: Optional[Any] = None,
    ) -> None:
        """
        Wait for a navigation milestone within the remaining timeout budget.

        Part of the budget is kept in reserve so a page that never fires its
        load event still returns its URL and title instead of failing.

        Args:
            wait_until (str): Desired wait condition.
            timeout_seconds (float): Remaining timeout budget in seconds.
            fired (asyncio.Event): Set by the page event handler registered before navigation.
            idle_tracker (Optional[NetworkIdleTracker]): Active tracker if wait_until is networkidle.
            tab (Optional[Any]): Fallback tab instance if no tracker was passed.
        """
        if timeout_seconds <= 0:
            raise asyncio.TimeoutError("Navigation wait budget exhausted")

        wait_budget = timeout_seconds - min(2.0, timeout_seconds * 0.2)
        if wait_until == "networkidle":
            if idle_tracker is not None:
                idle = await idle_tracker.wait(timeout=wait_budget)
                if not idle:
                    debug_logger.log_warning(
                        "browser_manager",
                        "navigate",
                        f"Page did not reach 'networkidle' within {wait_budget:.1f}s, returning current state",
                    )
                return
            if tab is not None:
                idle = await wait_for_network_idle(tab, idle_time=0.5, timeout=wait_budget)
                if not idle:
                    debug_logger.log_warning(
                        "browser_manager",
                        "navigate",
                        f"Page did not reach 'networkidle' within {wait_budget:.1f}s, returning current state",
                    )
                return
            await asyncio.sleep(min(timeout_seconds, 0.5))
            return

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
        url = reject_blocked_url(url)
        timeout_seconds = max(timeout, 1) / 1000
        deadline = time.monotonic() + timeout_seconds
        last_error: Optional[Exception] = None

        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Exception(f"Navigation to {url} timed out after {timeout}ms")

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

            fired = asyncio.Event()
            event_type = self._navigation_event_type(wait_until)
            handler = lambda _event, *args: fired.set()
            tab.add_handler(event_type, handler)

            main_frame_status: Optional[int] = None
            nav_loader_id: Optional[str] = None
            nav_frame_id: Optional[str] = None

            def on_response(event: Any, *args: Any) -> None:
                nonlocal main_frame_status
                try:
                    resp = getattr(event, "response", None)
                    if resp is None:
                        return
                    ev_loader = str(getattr(event, "loader_id", "") or "")
                    ev_frame = str(getattr(event, "frame_id", "") or "")
                    ev_type = getattr(event, "type_", None)
                    type_str = str(getattr(ev_type, "value", ev_type) or "").lower()

                    matched = False
                    if nav_loader_id and ev_loader and ev_loader == nav_loader_id:
                        matched = True
                    elif nav_frame_id and ev_frame and ev_frame == nav_frame_id and (type_str == "document" or not type_str):
                        matched = True
                    elif type_str == "document" and main_frame_status is None:
                        matched = True

                    if matched or (main_frame_status is None and getattr(resp, "status", None)):
                        code = getattr(resp, "status", None)
                        if isinstance(code, int):
                            main_frame_status = code
                except Exception:
                    pass

            if hasattr(tab, "add_handler"):
                try:
                    tab.add_handler(uc.cdp.network.ResponseReceived, on_response)
                except Exception:
                    pass

            idle_tracker: Optional[NetworkIdleTracker] = None
            if wait_until == "networkidle":
                idle_tracker = NetworkIdleTracker(tab, idle_time=0.5)
                await idle_tracker.start()

            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Navigation deadline exhausted")

                nav_result = await asyncio.wait_for(
                    tab.send(uc.cdp.page.navigate(url, referrer=referrer)),
                    timeout=remaining,
                )

                error_text: Optional[str] = None
                if isinstance(nav_result, (tuple, list)):
                    if len(nav_result) > 0 and nav_result[0] is not None:
                        nav_frame_id = str(nav_result[0])
                    if len(nav_result) > 1 and nav_result[1] is not None:
                        nav_loader_id = str(nav_result[1])
                    if len(nav_result) > 2 and nav_result[2]:
                        error_text = str(nav_result[2])
                elif nav_result is not None:
                    fid = getattr(nav_result, "frame_id", getattr(nav_result, "frameId", None))
                    if fid:
                        nav_frame_id = str(fid)
                    lid = getattr(nav_result, "loader_id", getattr(nav_result, "loaderId", None))
                    if lid:
                        nav_loader_id = str(lid)
                    err = getattr(nav_result, "error_text", getattr(nav_result, "errorText", None))
                    if err:
                        error_text = str(err)

                if error_text:
                    fail_result: Dict[str, Any] = {
                        "url": url,
                        "title": "",
                        "success": False,
                        "error": error_text,
                        "error_text": error_text,
                    }
                    if main_frame_status is not None:
                        fail_result["status"] = main_frame_status
                    return fail_result

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Navigation deadline exhausted")

                await self._wait_for_navigation_condition(
                    wait_until,
                    remaining,
                    fired,
                    idle_tracker=idle_tracker,
                    tab=tab,
                )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Navigation result budget exhausted")

                final_url = await asyncio.wait_for(
                    tab.evaluate("window.location.href"),
                    timeout=remaining,
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Navigation result budget exhausted")

                title = await asyncio.wait_for(
                    tab.evaluate("document.title"),
                    timeout=remaining,
                )

                is_chrome_error = False
                if final_url:
                    low = str(final_url).lower()
                    if low.startswith(("chrome-error://", "data:text/html,chromewebdata")) or "chromewebdata" in low:
                        is_chrome_error = True

                if is_chrome_error:
                    err_msg = error_text or "Navigation failed: Chrome error page"
                    fail_result = {
                        "url": final_url,
                        "title": title,
                        "success": False,
                        "error": err_msg,
                        "error_text": err_msg,
                    }
                    if main_frame_status is not None:
                        fail_result["status"] = main_frame_status
                    return fail_result

                await self.update_instance_state(instance_id, final_url, title)

                async with self._lock:
                    if instance_id in self._instances:
                        self._instances[instance_id]["tab"] = tab
                        self._instances[instance_id]["navigation_count"] = (
                            self._instances[instance_id].get("navigation_count", 0) + 1
                        )

                succ_result: Dict[str, Any] = {
                    "url": final_url,
                    "title": title,
                    "success": True,
                }
                if main_frame_status is not None:
                    succ_result["status"] = main_frame_status
                return succ_result
            except Exception as error:
                last_error = error
                if isinstance(error, asyncio.TimeoutError):
                    try:
                        stop_cmd = getattr(uc.cdp.page, "stop_loading", None) or getattr(uc.cdp.page, "stopLoading", None)
                        if stop_cmd and hasattr(tab, "send"):
                            await asyncio.wait_for(tab.send(stop_cmd()), timeout=0.5)
                    except Exception:
                        pass
                    raise Exception(
                        f"Navigation to {url} timed out after {timeout}ms"
                    ) from error

                debug_logger.log_warning(
                    "browser_manager",
                    "navigate",
                    f"Navigation attempt {attempt + 1} failed for {instance_id}: {error}",
                    {"url": url, "attempt": attempt + 1},
                )
                remaining = deadline - time.monotonic()
                if attempt == 1 or remaining <= 0 or not self._is_recoverable_navigation_error(error):
                    raise
            finally:
                self._remove_tab_handler(tab, event_type, handler)
                self._remove_tab_handler(tab, uc.cdp.network.ResponseReceived, on_response)
                if idle_tracker is not None:
                    await idle_tracker.stop()

    async def wait_for_network_idle(
        self,
        instance_id: str,
        timeout: int = 30000,
        idle_time: float = 0.5,
    ) -> bool:
        """
        Wait until network traffic on the instance tab is idle.

        Args:
            instance_id (str): Browser instance ID.
            timeout (int): Timeout in milliseconds.
            idle_time (float): Idle window duration in seconds (default: 0.5).

        Returns:
            bool: True if idle was reached, False on timeout.
        """
        tab = await self.get_tab(instance_id)
        if not tab:
            raise Exception(f"Instance not found: {instance_id}")
        await self.touch_instance(instance_id)
        timeout_seconds = max(timeout, 0) / 1000
        return await wait_for_network_idle(tab, idle_time=idle_time, timeout=timeout_seconds)

    async def wait_for(
        self,
        instance_id: str,
        selector: Optional[str] = None,
        visible: bool = True,
        present: bool = False,
        state: Optional[str] = None,
        text: Optional[str] = None,
        url: Optional[str] = None,
        networkidle: bool = False,
        timeout: int = 30000,
        raise_on_timeout: bool = True,
    ) -> Dict[str, Any]:
        """
        Wait for page or element conditions.

        Supports waiting for:
        - Element visibility or presence (selector + visible/present/state)
        - Text content (in selector element or on page)
        - URL navigation / match (exact, substring, or regex)
        - Network idle (~500 ms with no in-flight requests)

        Args:
            instance_id (str): Browser instance ID.
            selector (Optional[str]): CSS selector or XPath.
            visible (bool): Wait for element to be visible (default: True).
            present (bool): Wait for element to be present in DOM (not necessarily visible).
            state (Optional[str]): State to wait for ('visible' or 'present'/'attached').
            text (Optional[str]): Text content to wait for.
            url (Optional[str]): URL or URL substring/regex to wait for.
            networkidle (bool): Wait for network to become idle (~500 ms with no in-flight requests).
            timeout (int): Timeout in milliseconds (default: 30000).
            raise_on_timeout (bool): If True, raise TimeoutError on timeout.

        Returns:
            Dict[str, Any]: Result dictionary with success, conditions met, and elapsed time.
        """
        tab = await self.get_tab(instance_id)
        if not tab:
            raise Exception(f"Instance not found: {instance_id}")

        await self.touch_instance(instance_id)

        timeout_ms = max(int(timeout), 0) if timeout is not None else 30000
        timeout_seconds = timeout_ms / 1000
        start_time = time.monotonic()
        deadline = start_time + timeout_seconds

        if present or (state and state.lower() in ("present", "attached")):
            wait_visible = False
        elif state and state.lower() in ("visible",):
            wait_visible = True
        else:
            wait_visible = visible

        conditions_met: List[str] = []

        if networkidle:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if raise_on_timeout:
                    raise TimeoutError(f"Wait for networkidle timed out after {timeout_ms}ms")
                return {
                    "success": False,
                    "error": f"Wait for networkidle timed out after {timeout_ms}ms",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            idle = await wait_for_network_idle(tab, idle_time=0.5, timeout=remaining)
            if not idle:
                if raise_on_timeout:
                    raise TimeoutError(f"Wait for networkidle timed out after {timeout_ms}ms")
                return {
                    "success": False,
                    "error": f"Wait for networkidle timed out after {timeout_ms}ms",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            conditions_met.append("networkidle")

        if selector:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if raise_on_timeout:
                    raise TimeoutError(f"Wait for selector '{selector}' timed out after {timeout_ms}ms")
                return {
                    "success": False,
                    "error": f"Wait for selector '{selector}' timed out after {timeout_ms}ms",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            found = await DOMHandler.wait_for_element(
                tab,
                selector=selector,
                timeout=int(remaining * 1000),
                visible=wait_visible,
                text_content=text,
            )
            if not found:
                state_str = "visible" if wait_visible else "present"
                if raise_on_timeout:
                    raise TimeoutError(
                        f"Wait for selector '{selector}' ({state_str}) timed out after {timeout_ms}ms"
                    )
                return {
                    "success": False,
                    "error": f"Wait for selector '{selector}' ({state_str}) timed out after {timeout_ms}ms",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            conditions_met.append(f"selector:{selector}")

        elif text:
            escaped_text = json.dumps(text)
            matched = False
            while time.monotonic() < deadline:
                try:
                    matched = await tab.evaluate(
                        f"(document.body ? (document.body.innerText || document.body.textContent || '') : '').includes({escaped_text})"
                    )
                    if matched:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            if not matched:
                if raise_on_timeout:
                    raise TimeoutError(f"Wait for text '{text}' timed out after {timeout_ms}ms")
                return {
                    "success": False,
                    "error": f"Wait for text '{text}' timed out after {timeout_ms}ms",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            conditions_met.append(f"text:{text}")

        if url:
            matched = False
            current_url = ""
            while time.monotonic() < deadline:
                try:
                    current_url = getattr(tab, "url", None)
                    if not current_url:
                        current_url = await tab.evaluate("window.location.href")
                    if current_url:
                        if url == current_url or url in current_url:
                            matched = True
                            break
                        try:
                            if re.search(url, current_url):
                                matched = True
                                break
                        except re.error:
                            pass
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            if not matched:
                if raise_on_timeout:
                    raise TimeoutError(
                        f"Wait for url '{url}' timed out after {timeout_ms}ms (current: '{current_url}')"
                    )
                return {
                    "success": False,
                    "error": f"Wait for url '{url}' timed out after {timeout_ms}ms (current: '{current_url}')",
                    "elapsed_ms": round((time.monotonic() - start_time) * 1000, 1),
                }
            conditions_met.append(f"url:{url}")

        if not conditions_met:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            conditions_met.append(f"delay:{timeout_ms}ms")

        elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
        return {
            "success": True,
            "instance_id": instance_id,
            "conditions": conditions_met,
            "elapsed_ms": elapsed_ms,
        }

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
        await self.dialog_manager.configure_tab(tab, instance_id)
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
        url = reject_blocked_url(url)
        data = await self.get_instance(instance_id)
        if not data:
            raise instance_not_found_error(instance_id)
        tab = await open_background_tab(data["browser"], "about:blank")
        options: BrowserOptions = data.get("options")
        if options is not None:
            mode = getattr(options, "window", "normal") or "normal"
            if mode != "normal":
                try:
                    await apply_window_mode(
                        tab,
                        mode,
                        options.viewport_width,
                        options.viewport_height,
                    )
                except Exception as e:
                    debug_logger.log_warning(
                        "browser_manager",
                        "open_tab",
                        f"Failed to apply window mode {mode}: {e}",
                    )
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
            raise instance_not_found_error(instance_id)
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

    def get_dialogs(self, instance_id: str) -> List[Dict[str, Any]]:
        """List pending and recently handled dialogs for an instance."""
        return self.dialog_manager.get_dialogs(instance_id)

    async def handle_dialog(
        self,
        instance_id: str,
        accept: bool = True,
        prompt_text: Optional[str] = None,
        dialog_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Accept or dismiss a pending dialog for an instance."""
        return await self.dialog_manager.handle_dialog(
            instance_id=instance_id,
            accept=accept,
            prompt_text=prompt_text,
            dialog_id=dialog_id,
            action=action,
        )

    def clear_dialog_instance(self, instance_id: str) -> None:
        """Clear dialog state for a closed instance."""
        self.dialog_manager.clear_instance(instance_id)

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

    async def switch_tab(self, instance_id: str, tab_id: str) -> bool:
        """
        Switch to a specific tab.

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
            data = await self.get_instance(instance_id)
            options = data.get("options") if data else None
            mode = getattr(options, "window", "normal") if options else "normal"
            if mode == "normal":
                await target_tab.bring_to_front()
            await self._set_current_tab(instance_id, target_tab)

            return True
        except Exception:
            return False

    switch_to_tab = switch_tab

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

            inst_data = await self.get_instance(instance_id)
            inst = inst_data.get("instance") if inst_data else None
            inst_window = getattr(inst, "window", None) if inst else None
            inst_headless = getattr(inst, "headless", None) if inst else None
            inst_note = getattr(inst, "window_note", None) if inst else None
            inst_bounds = getattr(inst, "window_bounds", None) if inst else None
            inst_attached = getattr(inst, "attached", False) if inst else False

            return PageState(
                instance_id=instance_id,
                url=url,
                title=title,
                ready_state=ready_state,
                cookies=cookies,
                local_storage=local_storage,
                session_storage=session_storage,
                viewport=viewport or {},
                headless=inst_headless,
                window=inst_window,
                window_note=inst_note,
                window_bounds=inst_bounds,
                attached=inst_attached,
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
