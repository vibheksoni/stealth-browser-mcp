"""Browser instance management with nodriver."""

import asyncio
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional, List
from datetime import datetime, timedelta

import nodriver as uc
from nodriver import Browser, Tab

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

    NAVIGATION_RECYCLE_THRESHOLD = 25
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
    async def _stop_browser(browser: Browser) -> None:
        """Stop a nodriver browser regardless of sync or async stop semantics."""
        stop_result = browser.stop()
        if asyncio.iscoroutine(stop_result):
            await stop_result

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
                    finalized_profiles = process_cleanup.cleanup_deferred_profiles()
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

            if options.extra_headers:
                await tab.send(uc.cdp.network.set_extra_http_headers(
                    headers=options.extra_headers
                ))

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

            applied_timezone_id = await self._apply_timezone_override(
                tab=tab,
                timezone_id=options.timezone_id,
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

        except Exception as e:
            if browser is not None:
                try:
                    await self._stop_browser(browser)
                except Exception:
                    pass
            if proxy_forwarder is not None:
                try:
                    await proxy_forwarder.close()
                except Exception:
                    pass
            try:
                process_cleanup.kill_browser_process(instance_id)
            except Exception:
                pass
            instance.state = BrowserState.ERROR
            raise Exception(f"Failed to spawn browser: {str(e)}")

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

        Args:
            instance_id (str): The ID of the browser instance to close.

        Returns:
            bool: True if closed successfully, False otherwise.
        """
        import asyncio
        
        async def _do_close():
            async with self._lock:
                if instance_id not in self._instances:
                    return False

                data = self._instances[instance_id]
                browser = data['browser']
                instance = data['instance']

                try:
                    if hasattr(browser, 'tabs') and browser.tabs:
                        for tab in browser.tabs[:]:
                            try:
                                await tab.close()
                            except Exception:
                                pass
                except Exception:
                    pass

                try:
                    import asyncio
                    if hasattr(browser, 'connection') and browser.connection:
                        asyncio.get_event_loop().create_task(browser.connection.disconnect())
                        debug_logger.log_info("browser_manager", "close_connection", "closed connection using get_event_loop().create_task()")
                except RuntimeError:
                    try:
                        import asyncio
                        if hasattr(browser, 'connection') and browser.connection:
                            await asyncio.wait_for(browser.connection.disconnect(), timeout=2.0)
                            debug_logger.log_info("browser_manager", "close_connection", "closed connection with direct await and timeout")
                    except (asyncio.TimeoutError, Exception) as e:
                        debug_logger.log_info("browser_manager", "close_connection", f"connection disconnect failed or timed out: {e}")
                        pass
                except Exception as e:
                    debug_logger.log_info("browser_manager", "close_connection", f"connection disconnect failed: {e}")
                    pass

                try:
                    import nodriver.cdp.browser as cdp_browser
                    if hasattr(browser, 'connection') and browser.connection:
                        await browser.connection.send(cdp_browser.close())
                except Exception:
                    pass

                try:
                    process_cleanup.kill_browser_process(instance_id)
                except Exception as e:
                    debug_logger.log_warning("browser_manager", "close_instance", 
                                           f"Process cleanup failed for {instance_id}: {e}")

                try:
                    await self._stop_browser(browser)
                except Exception:
                    pass

                try:
                    await self._close_proxy_forwarder(instance_id)
                except Exception:
                    pass

                if hasattr(browser, '_process') and browser._process and browser._process.returncode is None:
                    import os

                    for attempt in range(3):
                        try:
                            browser._process.terminate()
                            debug_logger.log_info("browser_manager", "terminate_process", f"terminated browser with pid {browser._process.pid} successfully on attempt {attempt + 1}")
                            break
                        except Exception:
                            try:
                                browser._process.kill()
                                debug_logger.log_info("browser_manager", "kill_process", f"killed browser with pid {browser._process.pid} successfully on attempt {attempt + 1}")
                                break
                            except Exception:
                                try:
                                    if hasattr(browser, '_process_pid') and browser._process_pid:
                                        os.kill(browser._process_pid, 15)
                                        debug_logger.log_info("browser_manager", "kill_process", f"killed browser with pid {browser._process_pid} using signal 15 successfully on attempt {attempt + 1}")
                                        break
                                except (PermissionError, ProcessLookupError) as e:
                                    debug_logger.log_info("browser_manager", "kill_process", f"browser already stopped or no permission to kill: {e}")
                                    break
                                except Exception as e:
                                    if attempt == 2:
                                        debug_logger.log_error("browser_manager", "kill_process", e)

                try:
                    if hasattr(browser, '_process'):
                        browser._process = None
                    if hasattr(browser, '_process_pid'):
                        browser._process_pid = None

                    instance.state = BrowserState.CLOSED
                except Exception:
                    pass

                try:
                    process_cleanup.finalize_browser_process(instance_id)
                    process_cleanup.cleanup_deferred_profiles()
                except Exception as e:
                    debug_logger.log_warning(
                        "browser_manager",
                        "close_instance",
                        f"Post-stop cleanup failed for {instance_id}: {e}",
                    )

                del self._instances[instance_id]
                self._spawn_diagnostics.pop(instance_id, None)

                persistent_storage.remove_instance(instance_id)

                return True
        
        try:
            return await asyncio.wait_for(_do_close(), timeout=5.0)
        except asyncio.TimeoutError:
            debug_logger.log_info("browser_manager", "close_instance", f"Close timeout for {instance_id}, forcing cleanup")
            try:
                async with self._lock:
                    if instance_id in self._instances:
                        data = self._instances[instance_id]
                        data['instance'].state = BrowserState.CLOSED
                        process_cleanup.kill_browser_process(instance_id)
                        process_cleanup.finalize_browser_process(instance_id)
                        process_cleanup.cleanup_deferred_profiles()
                        del self._instances[instance_id]
                        self._spawn_diagnostics.pop(instance_id, None)
                        proxy_forwarder = self._proxy_forwarders.pop(instance_id, None)
                        if proxy_forwarder is not None:
                            asyncio.create_task(proxy_forwarder.close())
                        persistent_storage.remove_instance(instance_id)
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_error("browser_manager", "close_instance", e)
            return False

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
        new_tab = await browser.get("about:blank", new_tab=True)
        await new_tab

        if close_existing and previous_tab:
            previous_target_id = self._get_tab_target_id(previous_tab)
            new_target_id = self._get_tab_target_id(new_tab)
            if previous_target_id and previous_target_id != new_target_id:
                try:
                    await previous_tab.close()
                except Exception:
                    pass

        async with self._lock:
            if instance_id in self._instances:
                self._instances[instance_id]["tab"] = new_tab
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
            self.NAVIGATION_RECYCLE_THRESHOLD > 0
            and navigation_count >= self.NAVIGATION_RECYCLE_THRESHOLD
        ):
            return await self._replace_main_tab(
                instance_id,
                reason=f"navigation recycle threshold {self.NAVIGATION_RECYCLE_THRESHOLD} reached",
            )

        try:
            await browser.update_targets()
            tracked_target_id = self._get_tab_target_id(tracked_tab)
            if tracked_target_id:
                for candidate_tab in browser.tabs:
                    if self._get_tab_target_id(candidate_tab) == tracked_target_id:
                        await candidate_tab
                        return candidate_tab

            if browser.tabs:
                fallback_tab = browser.tabs[0]
                await fallback_tab
                async with self._lock:
                    if instance_id in self._instances:
                        self._instances[instance_id]["tab"] = fallback_tab
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
    async def _wait_for_navigation_condition(
        tab: Tab,
        wait_until: str,
        timeout_seconds: float,
    ) -> None:
        """
        Wait for a navigation milestone within the remaining timeout budget.

        Args:
            tab (Tab): Browser tab.
            wait_until (str): Desired wait condition.
            timeout_seconds (float): Remaining timeout budget in seconds.
        """
        if timeout_seconds <= 0:
            raise asyncio.TimeoutError("Navigation wait budget exhausted")

        if wait_until == "domcontentloaded":
            await asyncio.wait_for(
                tab.wait(uc.cdp.page.DomContentEventFired),
                timeout=timeout_seconds,
            )
            return

        if wait_until == "networkidle":
            await asyncio.sleep(min(timeout_seconds, 2.0))
            return

        await asyncio.wait_for(
            tab.wait(uc.cdp.page.LoadEventFired),
            timeout=timeout_seconds,
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

            try:
                if referrer:
                    await tab.send(
                        uc.cdp.network.set_extra_http_headers(
                            headers={"Referer": referrer}
                        )
                    )

                await asyncio.wait_for(tab.get(url), timeout=timeout_seconds)

                elapsed = time.monotonic() - start_time
                await self._wait_for_navigation_condition(
                    tab,
                    wait_until,
                    timeout_seconds - elapsed,
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

        Returns:
            Optional[Tab]: The main tab if found, else None.
        """
        data = await self.get_instance(instance_id)
        if data:
            if touch_activity:
                await self.touch_instance(instance_id)
            return data['tab']
        return None

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
            await tab
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
            async with self._lock:
                if instance_id in self._instances:
                    self._instances[instance_id]['tab'] = target_tab

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

        Args:
            instance_id (str): The ID of the browser instance.
            tab_id (str): The target ID of the tab to close.

        Returns:
            bool: True if closed successfully, False otherwise.
        """
        browser = await self.get_browser(instance_id)
        if not browser:
            return False

        target_tab = None
        for tab in browser.tabs:
            if str(tab.target.target_id) == tab_id:
                target_tab = tab
                break

        if not target_tab:
            return False

        try:
            await target_tab.close()
            return True
        except Exception:
            return False

    async def update_instance_state(self, instance_id: str, url: str = None, title: str = None):
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

            cookies = await tab.send(uc.cdp.network.get_cookies())

            local_storage = {}
            session_storage = {}

            try:
                local_storage_keys = await tab.evaluate("Object.keys(localStorage)")
                for key in local_storage_keys:
                    value = await tab.evaluate(f"localStorage.getItem('{key}')")
                    local_storage[key] = value

                session_storage_keys = await tab.evaluate("Object.keys(sessionStorage)")
                for key in session_storage_keys:
                    value = await tab.evaluate(f"sessionStorage.getItem('{key}')")
                    session_storage[key] = value
            except Exception:
                pass

            viewport = await tab.evaluate("""
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
                cookies=cookies.get('cookies', []),
                local_storage=local_storage,
                session_storage=session_storage,
                viewport=viewport
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

        for instance_id in to_close:
            await self.close_instance(instance_id)

        return len(to_close)

    async def close_all(self):
        """
        Close all browser instances.

        Closes all currently managed browser instances.
        """
        instance_ids = list(self._instances.keys())
        for instance_id in instance_ids:
            await self.close_instance(instance_id)
