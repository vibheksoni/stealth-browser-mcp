"""Browser instance management with nodriver."""

import asyncio
import sys
import uuid
from typing import Dict, Optional, List
from datetime import datetime, timedelta

import nodriver as uc
from nodriver import Browser, Tab

from debug_logger import debug_logger
from models import BrowserInstance, BrowserState, BrowserOptions, PageState
from persistent_storage import persistent_storage
from dynamic_hook_system import dynamic_hook_system
from platform_utils import get_platform_info, check_browser_executable, merge_browser_args
from process_cleanup import process_cleanup


class BrowserManager:
    """Manages multiple browser instances."""

    def __init__(self):
        self._instances: Dict[str, dict] = {}
        self._lock = asyncio.Lock()

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

        try:
            platform_info = get_platform_info()
            
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
            
            config = uc.Config(
                headless=options.headless,
                user_data_dir=options.user_data_dir,
                sandbox=options.sandbox,
                browser_executable_path=browser_executable,
                browser_args=merge_browser_args()
            )

            browser = await uc.start(config=config)
            tab = browser.main_tab

            if hasattr(browser, '_process') and browser._process:
                process_cleanup.track_browser_process(instance_id, browser._process)
            else:
                debug_logger.log_warning("browser_manager", "spawn_browser", 
                                       f"Browser {instance_id} has no process to track")

            if options.user_agent:
                await tab.send(uc.cdp.emulation.set_user_agent_override(
                    user_agent=options.user_agent
                ))

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
            print(f"[DEBUG] Set viewport to {options.viewport_width}x{options.viewport_height}", file=sys.stderr)

            await self._setup_dynamic_hooks(tab, instance_id)

            async with self._lock:
                self._instances[instance_id] = {
                    'browser': browser,
                    'tab': tab,
                    'instance': instance,
                    'options': options,
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
            instance.state = BrowserState.ERROR
            raise Exception(f"Failed to spawn browser: {str(e)}")

        return instance
    
    async def _setup_dynamic_hooks(self, tab: Tab, instance_id: str):
        """Setup dynamic hook system for browser instance."""
        try:
            dynamic_hook_system.add_instance(instance_id)
            
            await dynamic_hook_system.setup_interception(tab, instance_id)
            
            debug_logger.log_info("browser_manager", "_setup_dynamic_hooks", f"Dynamic hook system setup complete for instance {instance_id}")
            
        except Exception as e:
            debug_logger.log_error("browser_manager", "_setup_dynamic_hooks", f"Failed to setup dynamic hooks for {instance_id}: {e}")

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
                    await browser.stop()
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

                del self._instances[instance_id]

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
                        del self._instances[instance_id]
                        persistent_storage.remove_instance(instance_id)
            except Exception:
                pass
            return True
        except Exception as e:
            debug_logger.log_error("browser_manager", "close_instance", e)
            return False

    async def get_tab(self, instance_id: str) -> Optional[Tab]:
        """
        Get the main tab for a browser instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[Tab]: The main tab if found, else None.
        """
        data = await self.get_instance(instance_id)
        if data:
            return data['tab']
        return None

    async def get_browser(self, instance_id: str) -> Optional[Browser]:
        """
        Get the browser object for an instance.

        Args:
            instance_id (str): The ID of the browser instance.

        Returns:
            Optional[Browser]: The browser object if found, else None.
        """
        data = await self.get_instance(instance_id)
        if data:
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
                instance.update_activity()

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

    async def cleanup_inactive(self, timeout_minutes: int = 30):
        """
        Clean up inactive browser instances.

        Args:
            timeout_minutes (int, optional): Timeout in minutes to consider an instance inactive. Defaults to 30.
        """
        now = datetime.now()
        timeout = timedelta(minutes=timeout_minutes)

        to_close = []
        async with self._lock:
            for instance_id, data in self._instances.items():
                instance = data['instance']
                if now - instance.last_activity > timeout:
                    to_close.append(instance_id)

        for instance_id in to_close:
            await self.close_instance(instance_id)

    async def close_all(self):
        """
        Close all browser instances.

        Closes all currently managed browser instances.
        """
        instance_ids = list(self._instances.keys())
        for instance_id in instance_ids:
            await self.close_instance(instance_id)