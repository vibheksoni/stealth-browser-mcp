"""Main MCP server for browser automation."""

import argparse
import asyncio
import base64
import importlib
import json
import ipaddress
import os
import sys
import tempfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import nodriver as uc
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic_core import to_json

from .browser_manager import BrowserManager
from .cdp_element_cloner import CDPElementCloner
from .cdp_function_executor import CDPFunctionExecutor
from .comprehensive_element_cloner import comprehensive_element_cloner
from .debug_logger import debug_logger
from .dom_handler import DOMHandler
from .element_cloner import element_cloner
from .file_based_element_cloner import file_based_element_cloner
from .http_security import (
    create_http_auth_provider,
    get_http_auth_token,
)
from .models import BrowserOptions
from .network_interceptor import NetworkInterceptor
from .page_bindings import page_bindings
from .dynamic_hook_ai_interface import dynamic_hook_ai
from .downloads import (
    download_manager,
    wait_for_download as wait_for_download_impl,
    drop_files as drop_files_impl,
)
from .persistent_storage import persistent_storage
from .progressive_element_cloner import progressive_element_cloner
from .response_handler import response_handler
from .screenshot_encoder import DEFAULT_QUALITY, fit_screenshot, validate_options as validate_screenshot_options
from .stdio_guard import install_stdio_guard
from .platform_utils import validate_browser_environment, get_platform_info
from .process_cleanup import process_cleanup
from .a11y_snapshot import a11y_manager
from . import fill_tool
from . import session_state
from .har_export import build_har, export_to_json
from . import seed_snapshots
from . import seed_tools

DISABLED_SECTIONS = set()
SECTION_TOOLS: Dict[str, List[str]] = defaultdict(list)
SECTION_DESCRIPTIONS = {
    "browser-management": "Core browser operations",
    "element-interaction": "Page interaction and element manipulation",
    "element-extraction": "Element cloning and extraction",
    "file-extraction": "File-based extraction tools",
    "network-debugging": "Network monitoring and interception",
    "cdp-functions": "Chrome DevTools Protocol function execution",
    "progressive-cloning": "Advanced element cloning system",
    "cookies-storage": "Cookie and storage management",
    "tabs": "Tab management",
    "debugging": "Debug and system tools",
    "dynamic-hooks": "AI-powered network hook system",
}


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


DEBUG_LOGGING_ENABLED = (
    parse_bool_env("STEALTH_BROWSER_DEBUG", default=False)
    or parse_bool_env("DEBUG", default=False)
)
HTTP_AUTH_TOKEN = get_http_auth_token()

def is_loopback_host(host: str) -> bool:
    """
    Check whether an HTTP bind host only accepts local connections.

    Args:
        host (str): Host name or IP address passed to --host.

    Returns:
        bool: True for localhost and loopback addresses.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def section_tool(section: str):
    """Decorator that registers tools and tracks section membership."""
    def decorator(func):
        SECTION_TOOLS[section].append(func.__name__)
        return mcp.tool(func)
    return decorator


def apply_disabled_sections() -> None:
    """Apply section disable rules by unregistering tools from FastMCP."""
    for section in sorted(DISABLED_SECTIONS):
        for tool_name in SECTION_TOOLS.get(section, []):
            try:
                mcp.remove_tool(tool_name)
            except Exception:
                # Tool may already be removed by another section policy.
                continue

@asynccontextmanager
async def app_lifespan(server):
    """
    Manage application lifecycle with proper cleanup.

    Args:
        server (Any): The server instance for which the lifespan is being managed.
    """
    debug_logger.log_info("server", "startup", "Starting Browser Automation MCP Server...")
    try:
        await browser_manager.start_idle_reaper()
        yield
    finally:
        debug_logger.log_info("server", "shutdown", "Shutting down Browser Automation MCP Server...")
        try:
            await browser_manager.stop_idle_reaper()
        except Exception as e:
            debug_logger.log_error("server", "cleanup", e)
        try:
            await browser_manager.close_all()
            debug_logger.log_info("server", "cleanup", "All browser instances closed")
        except Exception as e:
            debug_logger.log_error("server", "cleanup", e)
        
        try:
            process_cleanup._cleanup_all_tracked()
            debug_logger.log_info("server", "cleanup", "Process cleanup complete")
        except Exception as e:
            debug_logger.log_error("server", "cleanup", f"Process cleanup failed: {e}")
        try:
            persistent_instances = persistent_storage.list_instances()
            if persistent_instances.get("instances"):
                debug_logger.log_info(
                    "server",
                    "storage_cleanup",
                    f"Clearing in-memory storage with {len(persistent_instances['instances'])} instances...",
                )
                persistent_storage.clear_all()
                debug_logger.log_info("server", "storage_cleanup", "In-memory storage cleared")
        except Exception as e:
            debug_logger.log_error("server", "storage_cleanup", e)
        debug_logger.log_info("server", "shutdown", "Browser Automation MCP Server shutdown complete")

mcp = FastMCP(
    name="Browser Automation MCP",
    instructions="""
    This MCP server provides undetectable browser automation using nodriver (CDP-based).
    
    Key features:
    - Spawn and manage multiple browser instances
    - Navigate and interact with web pages
    - Query and manipulate DOM elements
    - Intercept and analyze network traffic
    - Execute JavaScript in page context
    - Manage cookies and storage
    
    Instances are built to avoid common bot detection, with trusted input
    events and a clean headless fingerprint. Measured results against public
    detection pages are published in STEALTH_TESTS.md.
    """,
    auth=create_http_auth_provider(HTTP_AUTH_TOKEN),
    lifespan=app_lifespan,
)

browser_manager = BrowserManager()
network_interceptor = NetworkInterceptor()
dom_handler = DOMHandler()
cdp_function_executor = CDPFunctionExecutor()


async def _prepare_tab(tab, instance_id: str, options: BrowserOptions) -> None:
    """
    Attach network capture to a tab an instance uses.

    Args:
        tab (Any): Browser tab.
        instance_id (str): Browser instance ID.
        options (BrowserOptions): Spawn options.
    """
    await network_interceptor.setup_interception(tab, instance_id, options.block_resources)


async def _forget_instance(instance_id: str) -> None:
    """
    Drop per-instance state held outside the browser manager.

    Args:
        instance_id (str): Closed browser instance ID.
    """
    await network_interceptor.clear_instance_data(instance_id)
    cdp_function_executor.forget_instance(instance_id)
    a11y_manager.forget_instance(instance_id)


def _register_manager_listeners() -> None:
    """Connect tab preparation and close cleanup to the current browser manager."""
    browser_manager.add_tab_listener(_prepare_tab)
    browser_manager.add_tab_listener(page_bindings.prepare_tab)
    browser_manager.add_tab_listener(download_manager.prepare_tab)
    browser_manager.add_close_listener(_forget_instance)
    browser_manager.add_close_listener(page_bindings.forget_instance)
    browser_manager.add_close_listener(download_manager.forget_instance)


_register_manager_listeners()

if DEBUG_LOGGING_ENABLED:
    debug_logger.enable()

@section_tool("browser-management")
async def spawn_browser(
    headless: bool = False,
    user_agent: Optional[str] = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    proxy: Optional[str] = None,
    browser_args: Optional[List[str]] = None,
    timezone_id: Optional[str] = None,
    idle_timeout_seconds: Optional[int] = None,
    block_resources: Optional[List[str]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    user_data_dir: Optional[str] = None,
    sandbox: Optional[Any] = None,
    seed: Optional[str] = None,
    check_url: Optional[str] = None,
    keep: bool = False,
    cap: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Spawn a new browser instance.

    Args:
        headless (bool): Run in headless mode.
        user_agent (Optional[str]): Custom user agent string.
        viewport_width (int): Viewport width in pixels.
        viewport_height (int): Viewport height in pixels.
        proxy (Optional[str]): Proxy server URL.
        browser_args (List[str]): Additional browser launch args.
        timezone_id (Optional[str]): IANA timezone ID applied via CDP timezone override.
        idle_timeout_seconds (Optional[int]): Idle timeout override in seconds for automatic instance cleanup.
        block_resources (List[str]): List of resource types to block (e.g., ['image', 'font', 'stylesheet']).
        extra_headers (Dict[str, str]): Additional HTTP headers.
        user_data_dir (Optional[str]): Path to user data directory for persistent sessions.
        sandbox (Optional[Any]): Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect.
        seed (Optional[str]): Seed profile name to clone a disposable session from.
        check_url (Optional[str]): Optional URL to verify cookie carryover via CDP Network.getCookies.
        keep (bool): Keep session directory on close instead of deleting it.
        cap (Optional[int]): Maximum concurrent sessions cap.

    Returns:
        Dict[str, Any]: Instance information including instance_id.
    """
    try:
        from .platform_utils import sandbox_must_be_disabled

        if sandbox is None:
            sandbox = not sandbox_must_be_disabled()
        elif isinstance(sandbox, str):
            sandbox = sandbox.lower() in ('true', '1', 'yes', 'on', 'enabled')
        elif isinstance(sandbox, int):
            sandbox = bool(sandbox)
        elif not isinstance(sandbox, bool):
            sandbox = bool(sandbox)
        
        options = BrowserOptions(
            headless=headless,
            user_agent=user_agent,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            proxy=proxy,
            browser_args=browser_args or [],
            timezone_id=timezone_id,
            idle_timeout_seconds=idle_timeout_seconds,
            block_resources=block_resources or [],
            extra_headers=extra_headers or {},
            user_data_dir=user_data_dir,
            sandbox=sandbox,
            seed=seed,
            check_url=check_url,
            keep=keep,
            cap=cap,
        )
        instance = await browser_manager.spawn_browser(options)
        spawn_diagnostics = await browser_manager.get_spawn_diagnostics(instance.instance_id)
        session_info = await browser_manager.get_session_info(instance.instance_id)
        result: Dict[str, Any] = {
            "instance_id": instance.instance_id,
            "state": instance.state,
            "headless": instance.headless,
            "viewport": instance.viewport,
            "spawn_diagnostics": spawn_diagnostics or {},
        }
        if session_info:
            result.update(session_info)
        return result
    except Exception as e:
        message = str(e)
        if not message.startswith("Failed to spawn browser"):
            message = f"Failed to spawn browser: {message}"
        raise Exception(message) from e

@section_tool("browser-management")
async def list_instances() -> List[Dict[str, Any]]:
    """
    List all active browser instances.

    Returns:
        List[Dict[str, Any]]: List of browser instances with their current state.
    """
    memory_instances = await browser_manager.list_instances()
    storage_instances = persistent_storage.list_instances()
    result = []
    for inst in memory_instances:
        result.append({
            "instance_id": inst.instance_id,
            "state": inst.state,
            "current_url": inst.current_url,
            "title": inst.title,
            "source": "active"
        })
    memory_ids = {inst.instance_id for inst in memory_instances}
    for instance_id, inst_data in storage_instances.get("instances", {}).items():
        if instance_id not in memory_ids:
            result.append({
                "instance_id": inst_data["instance_id"],
                "state": inst_data["state"] + " (stored)",
                "current_url": inst_data["current_url"],
                "title": inst_data["title"],
                "source": "stored"
            })
    return result

@section_tool("browser-management")
async def close_instance(instance_id: str) -> bool:
    """
    Close a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if closed successfully.
    """
    return await browser_manager.close_instance(instance_id)

@section_tool("browser-management")
async def get_instance_state(instance_id: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Optional[Dict[str, Any]]: Complete state information.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return state.dict()
    return None

@section_tool("browser-management")
async def navigate(
    instance_id: str,
    url: str,
    wait_until: str = "load",
    timeout: int = 30000,
    referrer: Optional[str] = None
) -> Dict[str, Any]:
    """
    Navigate to a URL.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to navigate to.
        wait_until (str): Wait condition - 'load', 'domcontentloaded', or 'networkidle'.
        timeout (int): Navigation timeout in milliseconds.
        referrer (Optional[str]): Referrer URL.

    Returns:
        Dict[str, Any]: Navigation result with final URL and title.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    return await browser_manager.navigate(
        instance_id=instance_id,
        url=url,
        wait_until=wait_until,
        timeout=timeout,
        referrer=referrer,
    )

@section_tool("browser-management")
async def wait_for(
    instance_id: str,
    selector: Optional[str] = None,
    visible: bool = True,
    present: bool = False,
    state: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    networkidle: bool = False,
    timeout: int = 30000,
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

    Returns:
        Dict[str, Any]: Result indicating success and elapsed time.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    return await browser_manager.wait_for(
        instance_id=instance_id,
        selector=selector,
        visible=visible,
        present=present,
        state=state,
        text=text,
        url=url,
        networkidle=networkidle,
        timeout=timeout,
    )

@section_tool("browser-management")
async def go_back(instance_id: str) -> bool:
    """
    Navigate back in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if navigation was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await tab.back()
    return True

@section_tool("browser-management")
async def go_forward(instance_id: str) -> bool:
    """
    Navigate forward in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if navigation was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await tab.forward()
    return True

@section_tool("browser-management")
async def reload_page(instance_id: str, ignore_cache: bool = False) -> bool:
    """
    Reload the current page.

    Args:
        instance_id (str): Browser instance ID.
        ignore_cache (bool): Whether to ignore cache when reloading.

    Returns:
        bool: True if reload was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await tab.reload(ignore_cache=ignore_cache)
    return True

@section_tool("browser-management")
async def wait_for_download(
    instance_id: Optional[str] = None,
    timeout: float = 30.0,
    filename_pattern: Optional[str] = None,
    instance: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Wait for a file download to complete in the session download directory.

    Args:
        instance_id (Optional[str]): Browser instance ID.
        timeout (float): Maximum seconds to wait (default: 30.0).
        filename_pattern (Optional[str]): Glob pattern or filename to match.
        instance (Optional[str]): Alias for instance_id.

    Returns:
        Dict[str, Any]: Details of downloaded file including file_path, filename, and file_size.
    """
    target_id = instance_id or instance
    return await wait_for_download_impl(
        instance=target_id,
        timeout=timeout,
        filename_pattern=filename_pattern,
        browser_manager=browser_manager,
    )

@section_tool("browser-management")
async def snapshot_seed(
    name: str,
    label: Optional[str] = None,
    max_snapshots: Optional[int] = 5,
) -> Dict[str, Any]:
    """
    Take a snapshot of a seed directory.

    Copies the seed directory to data_dir/snapshots/<name>/<timestamp>-<label>/
    under the per-seed lock.

    Args:
        name (str): Validated seed name.
        label (Optional[str]): Optional descriptive label for the snapshot.
        max_snapshots (Optional[int]): Number of recent snapshots to retain (default: 5).

    Returns:
        Dict[str, Any]: Information about the created snapshot.
    """
    return seed_snapshots.snapshot_seed(name=name, label=label, max_snapshots=max_snapshots)

@section_tool("browser-management")
async def list_seed_snapshots(
    name: str,
) -> List[Dict[str, Any]]:
    """
    List snapshots for a seed directory.

    Args:
        name (str): Validated seed name.

    Returns:
        List[Dict[str, Any]]: List of snapshot descriptions.
    """
    return seed_snapshots.list_seed_snapshots(name=name)

@section_tool("browser-management")
async def restore_seed_snapshot(
    name: str,
    snapshot_id: str,
) -> Dict[str, Any]:
    """
    Restore a seed directory from a snapshot.

    Restores the exact file set under the per-seed lock and refuses if live
    sessions use the seed.

    Args:
        name (str): Validated seed name.
        snapshot_id (str): Identifier of the snapshot to restore.

    Returns:
        Dict[str, Any]: Result indicating restoration status.
    """
    return seed_snapshots.restore_seed_snapshot(name=name, snapshot_id=snapshot_id)

@section_tool("element-interaction")
async def query_elements(
    instance_id: str,
    selector: str,
    text_filter: Optional[str] = None,
    visible_only: bool = True,
    limit: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """
    Query DOM elements.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath (starts with '//').
        text_filter (Optional[str]): Filter by text content.
        visible_only (bool): Only return visible elements.
        limit (Optional[Any]): Maximum number of elements to return.

    Returns:
        List[Dict[str, Any]]: List of matching elements with their properties.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    debug_logger.log_info('Server', 'query_elements', f'Received limit parameter: {limit} (type: {type(limit)})')
    elements = await dom_handler.query_elements(
        tab, selector, text_filter, visible_only, limit
    )
    debug_logger.log_info('Server', 'query_elements', f'DOM handler returned {len(elements)} elements')
    result = []
    for i, elem in enumerate(elements):
        try:
            if hasattr(elem, 'model_dump'):
                elem_dict = elem.model_dump()
            else:
                elem_dict = elem.dict()
            result.append(elem_dict)
            debug_logger.log_info('Server', 'query_elements', f'Converted element {i+1} to dict: {list(elem_dict.keys())}')
        except Exception as e:
            debug_logger.log_error('Server', 'query_elements', e, {'element_index': i})
    debug_logger.log_info('Server', 'query_elements', f'Returning {len(result)} results to MCP client')
    return result if result else []

@section_tool("element-interaction")
async def click_element(
    instance_id: str,
    selector: str,
    text_match: Optional[str] = None,
    timeout: int = 10000,
    frame: Optional[str] = None
) -> bool:
    """
    Click an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector (host >>> button), or XPath.
        text_match (Optional[str]): Click element with matching text.
        timeout (int): Timeout in milliseconds.
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).

    Returns:
        bool: True if clicked successfully.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.click_element(
        tab, selector, text_match, timeout, frame=frame, instance_id=instance_id
    )

@section_tool("element-interaction")
async def type_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 50,
    parse_newlines: bool = False,
    shift_enter: bool = False,
    frame: Optional[str] = None
) -> bool:
    """
    Type text into an input field with trusted keyboard events.

    Every character produces keydown, keypress, input, and keyup events.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector (host >>> input), XPath, or ref=<id>.
        text (str): Text to type.
        clear_first (bool): Clear field before typing.
        delay_ms (int): Delay between keystrokes in milliseconds.
        parse_newlines (bool): If True, press Enter for each \\n instead of inserting a line break.
        shift_enter (bool): If True, press Shift+Enter for each \\n (for chat apps).
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).

    Returns:
        bool: True if typed successfully.
    """
    if isinstance(delay_ms, str):
        delay_ms = int(delay_ms)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.type_text(
        tab, selector, text, clear_first, delay_ms, parse_newlines, shift_enter, frame=frame, instance_id=instance_id
    )

@section_tool("element-interaction")
async def fill(
    instance_id: str,
    selector: str,
    value: Optional[str] = None,
    value_env: Optional[str] = None,
    strategy: str = "auto",
    verify: bool = True,
    frame: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fill an input, textarea, select, combobox, or contenteditable element with verification.

    Classifies the element kind, picks an appropriate input strategy (native setter,
    synthetic paste, custom combobox choose_option, or formatted input), and
    verifies the result when verify is True. Sensitive values (passwords or value_env)
    are never echoed back in plain text.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector, XPath, or ref=<id>.
        value (Optional[str]): Text or value to fill into the input.
        value_env (Optional[str]): Name of an environment variable holding the secret
            (must start with STEALTH_SECRET_).
        strategy (str): Input strategy: 'auto', 'native_setter', 'synthetic_paste',
            'os_paste', 'typing', or 'choose_option'. Defaults to 'auto'.
        verify (bool): Whether to verify the filled value by reading it back. Defaults to True.
        frame (Optional[str]): Target element within an iframe (selector, name, or id).

    Returns:
        Dict[str, Any]: Result with strategy used, verified status, and safe preview.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await fill_tool.fill(
        tab,
        selector,
        value=value,
        value_env=value_env,
        strategy=strategy,
        verify=verify,
        frame=frame,
        instance_id=instance_id,
    )

@section_tool("element-interaction")
async def fill_element(
    instance_id: str,
    selector: str,
    value: Optional[str] = None,
    value_env: Optional[str] = None,
    strategy: str = "auto",
    verify: bool = True,
    frame: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fill an input field with text (alias for fill).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, XPath, deep selector, or ref=<id> (e.g. ref=e1).
        value (Optional[str]): Text or value to fill into the input.
        value_env (Optional[str]): Secret environment variable name (STEALTH_SECRET_ prefix).
        strategy (str): Strategy override ('auto', 'native_setter', etc.).
        verify (bool): Whether to verify read-back value.
        frame (Optional[str]): Target element within an iframe (selector, name, or id).

    Returns:
        Dict[str, Any]: Result with strategy used, verified status, and safe preview.
    """
    return await fill(
        instance_id=instance_id,
        selector=selector,
        value=value,
        value_env=value_env,
        strategy=strategy,
        verify=verify,
        frame=frame,
    )

@section_tool("element-interaction")
async def paste_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    frame: Optional[str] = None,
    mode: Optional[str] = None,
) -> bool:
    """
    Paste text instantly into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector, XPath, or ref=<id>.
        text (str): Text to paste.
        clear_first (bool): Clear field before pasting.
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).
        mode (Optional[str]): Paste mode in {'synthetic', 'type', 'native', 'clipboard', 'os_paste'}. Defaults to None (keeps current behaviour).

    Returns:
        bool: True if pasted successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.paste_text(
        tab, selector, text, clear_first, frame=frame, instance_id=instance_id, mode=mode
    )

@section_tool("element-interaction")
async def press_key(
    instance_id: str,
    key: str,
    modifiers: Optional[List[str]] = None,
    selector: Optional[str] = None,
    count: int = 1,
    delay_ms: int = 50,
    frame: Optional[str] = None
) -> Dict[str, Any]:
    """
    Press a real key using trusted CDP key events.

    Sends Input.dispatchKeyEvent keyDown/keyUp pairs, so the page sees trusted
    events. Use this for control keys such as Enter, Tab, Escape, and the
    arrow keys in React-select, typeahead, and combobox widgets.

    Args:
        instance_id (str): Browser instance ID.
        key (str): DOM KeyboardEvent.key name (Enter, Tab, Escape, ArrowDown, ArrowUp, ArrowLeft, ArrowRight, Backspace, Delete, Home, End, PageUp, PageDown, Space, F1-F12) or a single printable character.
        modifiers (Optional[List[str]]): Modifier keys to hold: Shift, Control (Ctrl), Alt, Meta (Cmd).
        selector (Optional[str]): CSS selector, deep selector, XPath, or ref=<id> to focus before pressing. Presses on the focused element when omitted.
        count (int): Number of times to press the key.
        delay_ms (int): Delay between presses in milliseconds.
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).

    Returns:
        Dict[str, Any]: { success: True, key: str, count: int, modifiers: [str, ...] }
    """
    if isinstance(count, str):
        count = int(count)
    if isinstance(delay_ms, str):
        delay_ms = int(delay_ms)
    if isinstance(modifiers, str):
        modifiers = [modifiers]
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.press_key(
        tab, key, modifiers, selector, count, delay_ms, frame=frame, instance_id=instance_id
    )

@section_tool("element-interaction")
async def file_upload(
    instance_id: str,
    selector: str,
    paths: List[str]
) -> Dict[str, Any]:
    """
    Upload allowed local files to a <input type="file"> element via CDP DOM.setFileInputFiles.

    Resolves the actual file input even if the selector matches a wrapper element
    (Workday/Ashby/LinkedIn often hide the real input behind a styled button).
    Refuses multi-file upload when the input lacks the `multiple` attribute.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the file input or any ancestor that contains it.
        paths (List[str]): Absolute allowlisted local file paths to upload.

    Returns:
        Dict[str, Any]: { success: True, count: N, files: [basename, ...] }
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.file_upload(tab, selector, paths)

@section_tool("element-interaction")
async def drop_files(
    selector: str,
    paths: List[str],
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Drop files onto a drag-and-drop zone element via CDP Input.dispatchDragEvent / DataTransfer events.

    Args:
        selector (str): CSS selector, deep selector, or XPath for the drop target element.
        paths (List[str]): List of absolute file paths to drop.
        instance_id (Optional[str]): Browser instance ID.

    Returns:
        Dict[str, Any]: { success: True, count: N, files: [basename, ...] }
    """
    return await drop_files_impl(
        selector=selector,
        paths=paths,
        instance_id=instance_id,
        browser_manager=browser_manager,
    )

@section_tool("element-interaction")
async def select_option(
    instance_id: str,
    selector: str,
    value: Optional[str] = None,
    text: Optional[str] = None,
    index: Optional[Any] = None
) -> bool:
    """
    Select an option from a dropdown.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the select element.
        value (Optional[str]): Option value attribute.
        text (Optional[str]): Option text content.
        index (Optional[Any]): Option index (0-based). Can be string or int.

    Returns:
        bool: True if selected successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    
    converted_index = None
    if index is not None:
        try:
            converted_index = int(index)
        except (ValueError, TypeError):
            raise Exception(f"Invalid index value: {index}. Must be a number.")
    
    return await dom_handler.select_option(tab, selector, value, text, converted_index)

@section_tool("element-interaction")
async def choose_option(
    instance_id: str,
    selector: str,
    option_text: str,
    filter_text: Optional[str] = None,
    timeout: int = 10000,
    frame: Optional[str] = None
) -> Dict[str, Any]:
    """
    Select an option from a custom combobox, listbox, or React-select widget.

    Clicks the trigger element to open the dropdown, optionally types filter text,
    waits for the listbox and options to appear (matching role=listbox/option or
    React-select style class names), clicks the matching option by visible text,
    and verifies that the selection was applied.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector, or XPath for the combobox or trigger element.
        option_text (str): Visible text of the option to pick.
        filter_text (Optional[str]): Optional text to type into the combobox to filter options.
        timeout (int): Timeout in milliseconds to wait for elements. Defaults to 10000.
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).

    Returns:
        Dict[str, Any]: { "success": True, "selected": str, "option": str, "verification": str }
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.choose_option(
        tab,
        selector,
        option_text,
        filter_text=filter_text,
        timeout=timeout,
        frame=frame
    )

@section_tool("element-interaction")
async def get_element_state(
    instance_id: str,
    selector: str
) -> Dict[str, Any]:
    """
    Get complete state of an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.

    Returns:
        Dict[str, Any]: Element state including attributes, style, position, etc.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.get_element_state(tab, selector)

@section_tool("element-interaction")
async def wait_for_element(
    instance_id: str,
    selector: str,
    timeout: int = 30000,
    visible: bool = True,
    text_content: Optional[str] = None,
    frame: Optional[str] = None
) -> bool:
    """
    Wait for an element to appear.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector, deep selector, or XPath.
        timeout (int): Timeout in milliseconds.
        visible (bool): Wait for element to be visible.
        text_content (Optional[str]): Wait for specific text content.
        frame (Optional[str]): Target element within an iframe (same-origin or cross-origin; selector, name, or id).

    Returns:
        bool: True if element found.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.wait_for_element(tab, selector, timeout, visible, text_content, frame=frame)

@section_tool("element-interaction")
async def scroll_page(
    instance_id: str,
    direction: str = "down",
    amount: int = 500,
    smooth: bool = True
) -> bool:
    """
    Scroll the page.

    Args:
        instance_id (str): Browser instance ID.
        direction (str): 'down', 'up', 'left', 'right', 'top', or 'bottom'.
        amount (int): Pixels to scroll (ignored for 'top' and 'bottom').
        smooth (bool): Use smooth scrolling.

    Returns:
        bool: True if scrolled successfully.
    """
    if isinstance(amount, str):
        amount = int(amount)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.scroll_page(tab, direction, amount, smooth)

@section_tool("element-interaction")
async def execute_script(
    instance_id: str,
    script: str,
    args: Optional[List[Any]] = None
) -> Dict[str, Any]:
    """
    Execute JavaScript in page context.

    Args:
        instance_id (str): Browser instance ID.
        script (str): JavaScript code to execute.
        args (Optional[List[Any]]): Arguments to pass to the script.

    Returns:
        Dict[str, Any]: Script execution result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    try:
        result = await dom_handler.execute_script(tab, script, args)
        bounded = response_handler.handle_response(result, "script_result")
        return {
            "success": True,
            "result": bounded,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "result": None,
            "error": str(e)
        }

@section_tool("element-interaction")
async def get_page_content(
    instance_id: str,
    include_frames: bool = False
) -> Dict[str, Any]:
    """
    Get page HTML and text content.

    Args:
        instance_id (str): Browser instance ID.
        include_frames (bool): Include iframe information.

    Returns:
        Dict[str, Any]: Page content including HTML, text, and metadata.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    content = await dom_handler.get_page_content(tab, include_frames)
    
    return response_handler.handle_response(
        content, 
        "page_content", 
        {"instance_id": instance_id, "include_frames": include_frames}
    )

@section_tool("element-interaction")
async def take_screenshot(
    instance_id: str,
    full_page: bool = False,
    format: str = "png",
    file_path: Optional[str] = None,
    quality: int = DEFAULT_QUALITY,
    max_width: Optional[int] = None
) -> Union[str, ToolResult]:
    """
    Take a screenshot of the page.

    The image is returned as MCP image content with metadata. It is scaled
    down, keeping its aspect ratio, when it is wider than max_width, longer
    than 8000 pixels on a side, or too large to send inline. The metadata
    reports the original size and the scale used.

    Args:
        instance_id (str): Browser instance ID.
        full_page (bool): Capture full page (not just viewport).
        format (str): Image format ('png' or 'jpeg'). Use jpeg with a lower quality for smaller images.
        file_path (Optional[str]): Save the full-resolution screenshot to this path on the server instead.
        quality (int): JPEG quality from 1 to 100. PNG is lossless and ignores it.
        max_width (Optional[int]): Largest width in pixels to return, for a smaller image.

    Returns:
        Union[str, ToolResult]: Server file location when file_path is given, otherwise
            an MCP image with format, width, height, original size, scale, quality,
            byte count, and SHA-256 metadata.
    """
    format = validate_screenshot_options(format, quality, max_width)

    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    if file_path:
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await tab.save_screenshot(save_path, format=format, full_page=full_page)
        return f"Screenshot saved on the server: {str(save_path.absolute())}"

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        await tab.save_screenshot(tmp_path, format="png", full_page=full_page)
        image_bytes, metadata = fit_screenshot(tmp_path, format, quality, max_width)
        return ToolResult(
            content=[ImageContent(
                type="image",
                mimeType=f"image/{format}",
                data=base64.b64encode(image_bytes).decode("ascii"),
            ), TextContent(type="text", text=json.dumps(metadata))],
            structured_content=metadata,
        )
    finally:
        if tmp_path.exists():
            os.unlink(tmp_path)


@section_tool("element-interaction")
async def get_accessibility_snapshot(
    instance_id: str,
    max_nodes: int = 500,
    max_chars: int = 50000,
    interactive_only: bool = False,
    frame: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get an accessibility snapshot of the page.

    Returns a compact text and JSON tree with stable short refs (e.g. ref=e12)
    for interactive nodes, with hard size caps (max_nodes and max_chars) and
    a truncated flag. Stored refs can be targeted with click_element, fill,
    and type_text using selector="ref=<id>".

    Args:
        instance_id (str): Browser instance ID.
        max_nodes (int): Maximum number of nodes to return in the snapshot. Defaults to 500.
        max_chars (int): Maximum characters for the text tree. Defaults to 50000.
        interactive_only (bool): When True, only include interactive elements. Defaults to False.
        frame (Optional[str]): Target frame selector, name, or id. Defaults to None (root document).

    Returns:
        Dict[str, Any]: { "tree": str, "text": str, "nodes": dict, "truncated": bool, "node_count": int, "refs": dict }
    """
    if isinstance(max_nodes, str):
        max_nodes = int(max_nodes)
    if isinstance(max_chars, str):
        max_chars = int(max_chars)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.get_accessibility_snapshot(
        tab,
        instance_id=instance_id,
        max_nodes=max_nodes,
        max_chars=max_chars,
        interactive_only=interactive_only,
        frame=frame,
    )


@section_tool("element-interaction")
async def take_accessibility_snapshot(
    instance_id: str,
    max_nodes: int = 500,
    max_chars: int = 50000,
    interactive_only: bool = False,
    frame: Optional[str] = None
) -> Dict[str, Any]:
    """
    Take an accessibility snapshot of the page (alias for get_accessibility_snapshot).

    Args:
        instance_id (str): Browser instance ID.
        max_nodes (int): Maximum number of nodes to return in the snapshot. Defaults to 500.
        max_chars (int): Maximum characters for the text tree. Defaults to 50000.
        interactive_only (bool): When True, only include interactive elements. Defaults to False.
        frame (Optional[str]): Target frame selector, name, or id. Defaults to None.

    Returns:
        Dict[str, Any]: { "tree": str, "text": str, "nodes": dict, "truncated": bool, "node_count": int, "refs": dict }
    """
    return await get_accessibility_snapshot(
        instance_id=instance_id,
        max_nodes=max_nodes,
        max_chars=max_chars,
        interactive_only=interactive_only,
        frame=frame,
    )


@section_tool("network-debugging")
async def list_network_requests(
    instance_id: str,
    filter_type: Optional[str] = None
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """
    List captured network requests.

    Args:
        instance_id (str): Browser instance ID.
        filter_type (Optional[str]): Filter by resource type (e.g., 'image', 'script', 'xhr').

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: List of network requests, or file metadata if response too large.
    """
    requests = await network_interceptor.list_requests(instance_id, filter_type)
    formatted_requests = [
        {
            "request_id": req.request_id,
            "url": req.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "timestamp": req.timestamp.isoformat()
        }
        for req in requests
    ]
    
    return response_handler.handle_response(formatted_requests, "network_requests")


@section_tool("network-debugging")
async def get_request_details(
    request_id: str
) -> Optional[Dict[str, Any]]:
    """
    Get detailed information about a network request.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Request details including headers, cookies, and body.
    """
    request = await network_interceptor.get_request(request_id)
    if request:
        return request.dict()
    return None


@section_tool("network-debugging")
async def get_response_details(
    request_id: str
) -> Optional[Dict[str, Any]]:
    """
    Get response details for a network request.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Response details including status, headers, and metadata.
    """
    response = await network_interceptor.get_response(request_id)
    if response:
        return response.dict()
    return None


@section_tool("network-debugging")
async def get_response_content(
    instance_id: str,
    request_id: str
) -> Optional[str]:
    """
    Get response body content.

    Args:
        instance_id (str): Browser instance ID.
        request_id (str): Network request ID.

    Returns:
        Optional[str]: Response body as text (base64 encoded for binary).
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    body = await network_interceptor.get_response_body(tab, request_id)
    if body:
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError:
            return base64.b64encode(body).decode('utf-8')
    return None


@section_tool("network-debugging")
async def search_network_requests(
    instance_id: str,
    url_pattern: Optional[str] = None,
    method: Optional[str] = None,
    status_code: Optional[int] = None,
    response_contains: Optional[str] = None,
    payload_contains: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Search network requests with advanced filters and pagination.

    Args:
        instance_id (str): Browser instance ID.
        url_pattern (Optional[str]): Filter by URL pattern (substring match).
        method (Optional[str]): Filter by HTTP method.
        status_code (Optional[int]): Filter by response status code.
        response_contains (Optional[str]): Search in response body.
        payload_contains (Optional[str]): Search in request payload.
        resource_type (Optional[str]): Filter by resource type.
        limit (int): Max results per page.
        offset (int): Starting index for pagination.

    Returns:
        Dict[str, Any]: Paginated results with metadata.
    """
    return await network_interceptor.search_requests(
        instance_id,
        url_pattern,
        method,
        status_code,
        response_contains,
        payload_contains,
        resource_type,
        limit,
        offset,
    )


@section_tool("network-debugging")
async def export_network_data(
    instance_id: str,
    filepath: str
) -> bool:
    """
    Export network data to JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to save JSON file.

    Returns:
        bool: True if successful.
    """
    return await network_interceptor.export_to_json(instance_id, filepath)


@section_tool("network-debugging")
async def export_har(
    instance_id: str,
    path: Optional[str] = None,
    include_sensitive: bool = False,
) -> Union[Dict[str, Any], str]:
    """
    Export captured network traffic for an instance as a HAR 1.2 document.

    Authorization, Cookie, and Set-Cookie header values are redacted by
    default; pass include_sensitive to include them.

    Args:
        instance_id (str): Browser instance ID.
        path (Optional[str]): Optional file path to write the HAR document.
        include_sensitive (bool): Include redacted (Authorization/Cookie) values.

    Returns:
        Union[Dict[str, Any], str]: The HAR 1.2 document, or the written path.
    """
    requests = await network_interceptor.list_requests(instance_id)
    entries = []
    for request in requests:
        response = await network_interceptor.get_response(request.request_id)
        req_ts = request.timestamp.isoformat() if hasattr(request.timestamp, "isoformat") else str(request.timestamp)
        entry: Dict[str, Any] = {
            "request": {
                "request_id": request.request_id,
                "url": request.url,
                "method": request.method,
                "headers": dict(request.headers),
                "cookies": dict(request.cookies),
                "post_data": request.post_data,
                "timestamp": req_ts,
            }
        }
        if response is not None:
            resp_ts = response.timestamp.isoformat() if hasattr(response.timestamp, "isoformat") else str(response.timestamp)
            resp_dict: Dict[str, Any] = {
                "status": response.status,
                "content_type": response.content_type,
                "headers": dict(response.headers),
                "timestamp": resp_ts,
            }
            if response.body is not None:
                try:
                    resp_dict["text"] = response.body.decode("utf-8")
                except UnicodeDecodeError:
                    resp_dict["body"] = response.body
            entry["response"] = resp_dict
        entries.append(entry)

    har = build_har(entries, include_sensitive=include_sensitive)

    if path:
        export_to_json(har, path)
        return path
    return har


@section_tool("network-debugging")
async def import_network_data(
    instance_id: str,
    filepath: str
) -> bool:
    """
    Import network data from JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to JSON file.

    Returns:
        bool: True if successful.
    """
    return await network_interceptor.import_from_json(instance_id, filepath)


@section_tool("network-debugging")
async def set_network_capture_filters(
    instance_id: str,
    include_types: Optional[List[str]] = None,
    exclude_types: Optional[List[str]] = None,
) -> bool:
    """
    Set resource type filters for network capture to reduce memory usage.

    Args:
        instance_id (str): Browser instance ID.
        include_types (Optional[List[str]]): Only capture these types (e.g., ['XHR', 'Fetch', 'Document']).
        exclude_types (Optional[List[str]]): Exclude these types (e.g., ['Image', 'Stylesheet', 'Font', 'Script']).

    Common resource types: Document, Stylesheet, Image, Media, Font, Script, XHR, Fetch, WebSocket, Manifest, Other

    Returns:
        bool: True if successful.
    """
    await network_interceptor.set_capture_filters(instance_id, include_types, exclude_types)
    return True


@section_tool("network-debugging")
async def get_network_capture_filters(
    instance_id: str
) -> Dict[str, List[str]]:
    """
    Get current network capture filters.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, List[str]]: Current filters with 'include' and 'exclude' lists.
    """
    return await network_interceptor.get_capture_filters(instance_id)


@section_tool("network-debugging")
async def modify_headers(
    instance_id: str,
    headers: Dict[str, str]
) -> bool:
    """
    Modify request headers for future requests.

    Args:
        instance_id (str): Browser instance ID.
        headers (Dict[str, str]): Headers to add/modify.

    Returns:
        bool: True if modified successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await network_interceptor.modify_headers(tab, headers)


@section_tool("cookies-storage")
async def get_cookies(
    instance_id: str,
    urls: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Get cookies for current page or specific URLs.

    Args:
        instance_id (str): Browser instance ID.
        urls (Optional[List[str]]): Optional list of URLs to get cookies for.

    Returns:
        List[Dict[str, Any]]: List of cookies.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await network_interceptor.get_cookies(tab, urls)


@section_tool("cookies-storage")
async def set_cookie(
    instance_id: str,
    name: str,
    value: str,
    url: Optional[str] = None,
    domain: Optional[str] = None,
    path: str = "/",
    secure: bool = False,
    http_only: bool = False,
    same_site: Optional[str] = None
) -> bool:
    """
    Set a cookie.

    Args:
        instance_id (str): Browser instance ID.
        name (str): Cookie name.
        value (str): Cookie value.
        url (Optional[str]): The request-URI to associate with the cookie.
        domain (Optional[str]): Cookie domain.
        path (str): Cookie path.
        secure (bool): Secure flag.
        http_only (bool): HttpOnly flag.
        same_site (Optional[str]): SameSite attribute ('Strict', 'Lax', or 'None').

    Returns:
        bool: True if set successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    
    if not url and not domain:
        current_url = tab.url if hasattr(tab, 'url') else None
        if current_url:
            url = current_url
        else:
            raise Exception("At least one of 'url' or 'domain' must be specified")
    
    cookie = {
        "name": name,
        "value": value,
        "path": path,
        "secure": secure,
        "http_only": http_only
    }
    if url:
        cookie["url"] = url
    if domain:
        cookie["domain"] = domain
    if same_site:
        try:
            cookie["same_site"] = uc.cdp.network.CookieSameSite(same_site.capitalize())
        except ValueError:
            raise ValueError(f"Invalid same_site value: {same_site}. Use 'Strict', 'Lax', or 'None'.")
    return await network_interceptor.set_cookie(tab, cookie)


@section_tool("cookies-storage")
async def clear_cookies(
    instance_id: str,
    url: Optional[str] = None
) -> bool:
    """
    Clear cookies.

    Args:
        instance_id (str): Browser instance ID.
        url (Optional[str]): Optional URL to clear cookies for (clears all if not specified).

    Returns:
        bool: True if cleared successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await network_interceptor.clear_cookies(tab, url)


@section_tool("cookies-storage")
async def save_session(
    instance_id: str,
    file_path: Optional[str] = None,
    origins: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Save browser session state (cookies and localStorage per origin) to JSON.

    Saves cookies via CDP Network.getAllCookies and localStorage per origin
    in Playwright-compatible storageState format. The output file is written
    with mode 0600 (POSIX).

    Args:
        instance_id (str): Browser instance ID.
        file_path (Optional[str]): Destination JSON file path.
        origins (Optional[List[str]]): Specific origins to save localStorage for.

    Returns:
        Dict[str, Any]: Result with file_path, cookies_count, origins_count, and session data.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await session_state.save_session(
        instance_id=instance_id,
        file_path=file_path,
        origins=origins,
        browser_manager=browser_manager,
    )


@section_tool("cookies-storage")
async def restore_session(
    instance_id: str,
    file_path: Optional[str] = None,
    session_data: Optional[Dict[str, Any]] = None,
    clear_existing: bool = True,
) -> Dict[str, Any]:
    """
    Restore browser session state (cookies and localStorage per origin) from JSON.

    Restores cookies via CDP Network.setCookies and localStorage per origin
    from Playwright-compatible storageState format.

    Args:
        instance_id (str): Browser instance ID.
        file_path (Optional[str]): Source JSON file path.
        session_data (Optional[Dict[str, Any]]): Session state dictionary.
        clear_existing (bool): Whether to clear existing cookies and storage before restore.

    Returns:
        Dict[str, Any]: Result with success, cookies_restored, and origins_restored.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await session_state.restore_session(
        instance_id=instance_id,
        file_path=file_path,
        session_data=session_data,
        clear_existing=clear_existing,
        browser_manager=browser_manager,
    )


@mcp.resource("browser://{instance_id}/state")
async def get_browser_state_resource(instance_id: str) -> str:
    """
    Get current state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of the browser state or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return to_json(state, indent=2).decode()
    return json.dumps({"error": "Instance not found"})


@mcp.resource("browser://{instance_id}/cookies")
async def get_cookies_resource(instance_id: str) -> str:
    """
    Get cookies for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of cookies or error message.
    """
    tab = await browser_manager.get_tab(instance_id)
    if tab:
        cookies = await network_interceptor.get_cookies(tab)
        return to_json(cookies, indent=2).decode()
    return json.dumps({"error": "Instance not found"})


@mcp.resource("browser://{instance_id}/network")
async def get_network_resource(instance_id: str) -> str:
    """
    Get network requests for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of network requests.
    """
    requests = await network_interceptor.list_requests(instance_id)
    return to_json(requests, indent=2).decode()


@mcp.resource("browser://{instance_id}/console")
async def get_console_resource(instance_id: str) -> str:
    """
    Get console logs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of console logs or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return json.dumps(state.console_logs, indent=2)
    return json.dumps({"error": "Instance not found"})


@section_tool("debugging")
async def get_debug_view(
    max_errors: int = 50,
    max_warnings: int = 50,
    max_info: int = 50,
    include_all: bool = False
) -> Dict[str, Any]:
    """
    Get comprehensive debug view with all logged errors and statistics.

    Args:
        max_errors (int): Maximum number of errors to include (default: 50).
        max_warnings (int): Maximum number of warnings to include (default: 50).
        max_info (int): Maximum number of info logs to include (default: 50).
        include_all (bool): Include all logs regardless of limits (default: False).

    Returns:
        Dict[str, Any]: Debug information including errors, warnings, and statistics.
    """
    debug_data = debug_logger.get_debug_view_paginated(
        max_errors=max_errors if not include_all else None,
        max_warnings=max_warnings if not include_all else None,
        max_info=max_info if not include_all else None
    )
    return debug_data


@section_tool("debugging")
async def clear_debug_view() -> bool:
    """
    Clear all debug logs and statistics with timeout protection.

    Returns:
        bool: True if cleared successfully.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(debug_logger.clear_debug_view_safe),
            timeout=10.0
        )
        return True
    except asyncio.TimeoutError:
        return False


@section_tool("debugging")
async def export_debug_logs(
    filename: str = "debug_log.json",
    max_errors: int = 100,
    max_warnings: int = 100,
    max_info: int = 100,
    include_all: bool = False,
    format: str = "auto"
) -> str:
    """
    Export debug logs to a file using the fastest available method with timeout protection.

    Args:
        filename (str): Name of the file to export to.
        max_errors (int): Maximum number of errors to export (default: 100).
        max_warnings (int): Maximum number of warnings to export (default: 100).
        max_info (int): Maximum number of info logs to export (default: 100).
        include_all (bool): Include all logs regardless of limits (default: False).
        format (str): Export format: 'json', 'pickle', 'gzip-pickle', 'auto' (default: 'auto').
                     'auto' chooses fastest format based on data size:
                     - Small data (<100 items): JSON (human readable)
                     - Medium data (100-1000 items): Pickle (fast binary)
                     - Large data (>1000 items): Gzip-Pickle (fastest, compressed)

    Returns:
        str: Path to the exported file.
    """
    try:
        filepath = await asyncio.wait_for(
            asyncio.to_thread(
                debug_logger.export_to_file_paginated,
                filename,
                max_errors if not include_all else None,
                max_warnings if not include_all else None,
                max_info if not include_all else None,
                format
            ),
            timeout=30.0
        )
        return filepath
    except asyncio.TimeoutError:
        return "Export timeout - file too large. Try with smaller limits or 'gzip-pickle' format."


@section_tool("debugging")
async def get_debug_lock_status() -> Dict[str, Any]:
    """
    Get current debug logger lock status for debugging hanging exports.

    Returns:
        Dict[str, Any]: Lock status information.
    """
    try:
        return debug_logger.get_lock_status()
    except Exception as e:
        return {"error": str(e)}


@section_tool("tabs")
async def list_tabs(instance_id: str) -> List[Dict[str, str]]:
    """
    List all tabs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, str]]: List of tabs with their details.
    """
    return await browser_manager.list_tabs(instance_id)


@section_tool("tabs")
async def switch_tab(
    instance_id: str,
    tab_id: str
) -> bool:
    """
    Switch to a specific tab by bringing it to front.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Target tab ID to switch to.

    Returns:
        bool: True if switched successfully.
    """
    return await browser_manager.switch_to_tab(instance_id, tab_id)


@section_tool("tabs")
async def close_tab(
    instance_id: str,
    tab_id: str
) -> bool:
    """
    Close a specific tab.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Tab ID to close.

    Returns:
        bool: True if closed successfully.
    """
    return await browser_manager.close_tab(instance_id, tab_id)


@section_tool("tabs")
async def get_active_tab(instance_id: str) -> Dict[str, Any]:
    """
    Get information about the currently active tab.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: Active tab information.
    """
    tab = await browser_manager.get_active_tab(instance_id)
    if not tab:
        return {"error": "No active tab found"}
    browser = await browser_manager.get_browser(instance_id, touch_activity=False)
    if browser:
        try:
            await browser.update_targets()
        except Exception:
            pass
    return {
        "tab_id": str(tab.target.target_id),
        "url": getattr(tab, 'url', '') or '',
        "title": getattr(tab.target, 'title', '') or 'Untitled',
        "type": getattr(tab.target, 'type_', 'page')
    }


@section_tool("tabs")
async def new_tab(
    instance_id: str,
    url: str = "about:blank"
) -> Dict[str, Any]:
    """
    Open a new tab in the browser instance.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to open in the new tab.

    Returns:
        Dict[str, Any]: New tab information.
    """
    try:
        new_tab_obj = await browser_manager.open_tab(instance_id, url)
        return {
            "tab_id": str(new_tab_obj.target.target_id),
            "url": getattr(new_tab_obj, 'url', '') or url,
            "title": getattr(new_tab_obj.target, 'title', '') or 'New Tab',
            "type": getattr(new_tab_obj.target, 'type_', 'page')
        }
    except Exception as e:
        raise Exception(f"Failed to create new tab: {str(e)}")


@section_tool("element-extraction")
async def extract_element_styles(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False
) -> Dict[str, Any]:
    """
    Extract complete styling information from an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles (::before, ::after).
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: Complete styling data including computed styles, CSS rules, pseudo-elements.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await element_cloner.extract_element_styles(
        tab,
        selector=selector,
        include_computed=include_computed,
        include_css_rules=include_css_rules,
        include_pseudo=include_pseudo,
        include_inheritance=include_inheritance
    )


@section_tool("element-extraction")
async def extract_element_structure(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3
) -> Dict[str, Any]:
    """
    Extract complete HTML structure and DOM information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes specifically.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: HTML structure, attributes, position, and children data.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await element_cloner.extract_element_structure(
        tab,
        selector=selector,
        include_children=include_children,
        include_attributes=include_attributes,
        include_data_attributes=include_data_attributes,
        max_depth=max_depth
    )


@section_tool("element-extraction")
async def extract_element_events(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = False
) -> Dict[str, Any]:
    """
    Extract complete event listener and JavaScript handler information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers (onclick, etc.).
        include_listeners (bool): Include addEventListener attached handlers.
        include_framework (bool): Include framework-specific handlers (React, Vue, etc.).
        analyze_handlers (bool): Analyze handler functions for full details (can be large).

    Returns:
        Dict[str, Any]: Event listeners, inline handlers, framework handlers, detected frameworks.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await element_cloner.extract_element_events(
        tab,
        selector=selector,
        include_inline=include_inline,
        include_listeners=include_listeners,
        include_framework=include_framework,
        analyze_handlers=analyze_handlers
    )


@section_tool("element-extraction")
async def extract_element_animations(
    instance_id: str,
    selector: str,
    include_css_animations: bool = True,
    include_transitions: bool = True,
    include_transforms: bool = True,
    analyze_keyframes: bool = True
) -> Dict[str, Any]:
    """
    Extract CSS animations, transitions, and transforms.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_css_animations (bool): Include CSS @keyframes animations.
        include_transitions (bool): Include CSS transitions.
        include_transforms (bool): Include CSS transforms.
        analyze_keyframes (bool): Analyze keyframe rules.

    Returns:
        Dict[str, Any]: Animation data, transition data, transform data, keyframe rules.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await element_cloner.extract_element_animations(
        tab,
        selector=selector,
        include_css_animations=include_css_animations,
        include_transitions=include_transitions,
        include_transforms=include_transforms,
        analyze_keyframes=analyze_keyframes
    )


@section_tool("element-extraction")
async def extract_element_assets(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False
) -> Dict[str, Any]:
    """
    Extract all assets related to an element (images, fonts, etc.).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include img src and related images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Whether to fetch external assets for analysis.

    Returns:
        Dict[str, Any]: Images, background images, fonts, icons, videos, audio assets.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await element_cloner.extract_element_assets(
        tab,
        selector=selector,
        include_images=include_images,
        include_backgrounds=include_backgrounds,
        include_fonts=include_fonts,
        fetch_external=fetch_external
    )
    return response_handler.handle_response(result, f"element_assets_{instance_id}_{selector.replace(' ', '_')}")


@section_tool("element-extraction")
async def extract_element_styles_cdp(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> Dict[str, Any]:
    """
    Extract element styles using direct CDP calls (no JavaScript evaluation).
    This prevents hanging issues by using nodriver's native CDP methods.
    
    Args:
        instance_id (str): Browser instance ID
        selector (str): CSS selector for the element
        include_computed (bool): Include computed styles
        include_css_rules (bool): Include matching CSS rules
        include_pseudo (bool): Include pseudo-element styles
        include_inheritance (bool): Include style inheritance chain
    
    Returns:
        Dict[str, Any]: Styling data extracted using CDP
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await element_cloner.extract_element_styles_cdp(
        tab,
        selector=selector,
        include_computed=include_computed,
        include_css_rules=include_css_rules,
        include_pseudo=include_pseudo,
        include_inheritance=include_inheritance
    )


@section_tool("element-extraction")
async def extract_related_files(
    instance_id: str,
    analyze_css: bool = True,
    analyze_js: bool = True,
    follow_imports: bool = False,
    max_depth: int = 2
) -> Dict[str, Any]:
    """
    Discover and analyze related CSS/JS files for context.

    Args:
        instance_id (str): Browser instance ID.
        analyze_css (bool): Analyze linked CSS files.
        analyze_js (bool): Analyze linked JS files.
        follow_imports (bool): Follow @import and module imports (uses network).
        max_depth (int): Maximum depth for following imports.

    Returns:
        Dict[str, Any]: Stylesheets, scripts, imports, modules, framework detection.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await element_cloner.extract_related_files(
        tab,
        analyze_css=analyze_css,
        analyze_js=analyze_js,
        follow_imports=follow_imports,
        max_depth=max_depth
    )
    return response_handler.handle_response(result, f"related_files_{instance_id}")


@section_tool("element-extraction")
async def clone_element_complete(
    instance_id: str,
    selector: str,
    extraction_options: Optional[str] = None
) -> Dict[str, Any]:
    """
    Master function that extracts ALL element data using specialized functions.

    This is the ultimate element cloning tool that combines all extraction methods.
    Use this when you want complete element fidelity for recreation or analysis.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): Dict specifying what to extract and options for each.
            Example: {
                'styles': {'include_computed': True, 'include_pseudo': True},
                'structure': {'include_children': True, 'max_depth': 2},
                'events': {'include_framework': True, 'analyze_handlers': False},
                'animations': {'analyze_keyframes': True},
                'assets': {'fetch_external': False},
                'related_files': {'follow_imports': True, 'max_depth': 1}
            }

    Returns:
        Dict[str, Any]: Complete element clone with styles, structure, events, animations, assets, related files.
    """
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError:
            raise Exception(f"Invalid JSON in extraction_options: {extraction_options}")
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await comprehensive_element_cloner.extract_complete_element(
        tab,
        selector=selector,
        include_children=parsed_options.get('structure', {}).get('include_children', True) if parsed_options else True
    )
    
    return response_handler.handle_response(
        result,
        fallback_filename_prefix="complete_clone",
        metadata={
            "selector": selector,
            "extraction_options": parsed_options,
            "url": getattr(tab, 'url', 'unknown')
        }
    )


@section_tool("debugging")
async def hot_reload() -> str:
    """
    Hot reload all modules without restarting the server.

    Reloading replaces the browser manager and network interceptor, so it is
    refused while browser instances are open to avoid orphaning them.

    Returns:
        str: Status message.
    """
    global browser_manager, network_interceptor, dom_handler, debug_logger
    try:
        active_instances = await browser_manager.list_instances()
        if active_instances:
            return (
                f"Hot reload refused: {len(active_instances)} browser instance(s) are open. "
                "Close them first so they are not orphaned."
            )
        modules_to_reload = [
            'browser_manager',
            'network_interceptor',
            'dom_handler',
            'debug_logger',
            'models'
        ]
        reloaded_modules = []
        for module_name in modules_to_reload:
            qualified_name = f"{__package__}.{module_name}"
            if qualified_name in sys.modules:
                module = importlib.reload(sys.modules[qualified_name])
                reloaded_modules.append(module_name)
                if module_name == 'browser_manager':
                    await browser_manager.stop_idle_reaper()
                    browser_manager = module.BrowserManager()
                    _register_manager_listeners()
                    await browser_manager.start_idle_reaper()
                elif module_name == 'network_interceptor':
                    network_interceptor = module.NetworkInterceptor()
                elif module_name == 'dom_handler':
                    dom_handler = module.DOMHandler()
                elif module_name == 'debug_logger':
                    debug_logger = module.debug_logger
        return f"Hot reload completed. Reloaded modules: {', '.join(reloaded_modules)}"
    except Exception as e:
        return f"Hot reload failed: {str(e)}"


@section_tool("debugging")
async def reload_status() -> str:
    """
    Check the status of loaded modules.

    Returns:
        str: Module status information.
    """
    try:
        modules_info = []
        modules_to_check = [
            'browser_manager',
            'network_interceptor',
            'dom_handler',
            'debug_logger',
            'models',
            'persistent_storage'
        ]
        for module_name in modules_to_check:
            qualified_name = f"{__package__}.{module_name}"
            if qualified_name in sys.modules:
                module = sys.modules[qualified_name]
                modules_info.append(f"[loaded] {module_name}: {getattr(module, '__file__', 'built-in')}")
            else:
                modules_info.append(f"[missing] {module_name}: Not loaded")
        return "\n".join(modules_info)
    except Exception as e:
        return f"Error checking module status: {str(e)}"


@section_tool("debugging")
async def validate_browser_environment_tool() -> Dict[str, Any]:
    """
    Validate browser environment and diagnose potential issues.
    
    Returns:
        Dict[str, Any]: Environment validation results with platform info and recommendations
    """
    try:
        return validate_browser_environment()
    except Exception as e:
        return {
            "error": str(e),
            "platform_info": get_platform_info(),
            "is_ready": False,
            "issues": [f"Validation failed: {str(e)}"],
            "warnings": []
        }


@section_tool("debugging")
async def doctor() -> Dict[str, Any]:
    """
    Run environment health checks: browser binaries per family, Linux keyring
    reachability, seed ages, live sessions vs cap, stale locks, and whether the
    data directory is writable. Secrets are redacted from the output.

    Returns:
        Dict[str, Any]: A summary with per-check status and an overall verdict.
    """
    try:
        from .doctor import run_checks as doctor_run_checks
        return doctor_run_checks()
    except Exception as e:
        return {"error": str(e)}


@section_tool("progressive-cloning")
async def clone_element_progressive(
    instance_id: str,
    selector: str,
    include_children: bool = True
) -> Dict[str, Any]:
    """
    Clone element progressively - returns lightweight base structure with element_id.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to extract child elements.

    Returns:
        Dict[str, Any]: Base structure with element_id for progressive expansion.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await progressive_element_cloner.clone_element_progressive(tab, selector, include_children)


@section_tool("progressive-cloning")
async def expand_styles(
    element_id: str,
    categories: Optional[List[str]] = None,
    properties: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Expand styles data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        categories (Optional[List[str]]): Style categories to include (layout, typography, colors, spacing, borders, backgrounds, effects, animation).
        properties (Optional[List[str]]): Specific CSS property names to include.

    Returns:
        Dict[str, Any]: Filtered styles data.
    """
    return progressive_element_cloner.expand_styles(element_id, categories, properties)


@section_tool("progressive-cloning")
async def expand_events(
    element_id: str,
    event_types: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Expand event listeners data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        event_types (Optional[List[str]]): Event types or sources to include (click, react, inline, addEventListener).

    Returns:
        Dict[str, Any]: Filtered event listeners data.
    """
    return progressive_element_cloner.expand_events(element_id, event_types)


@section_tool("progressive-cloning")
async def expand_children(
    element_id: str,
    depth_range: Optional[List] = None,
    max_count: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Expand children data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        depth_range (Optional[List]): [min_depth, max_depth] range to include.
        max_count (Optional[Any]): Maximum number of children to return.

    Returns:
        Dict[str, Any]: Filtered children data.
    """
    if isinstance(max_count, str):
        try:
            max_count = int(max_count) if max_count else None
        except ValueError:
            return {"error": f"Invalid max_count value: {max_count}"}
    
    if isinstance(depth_range, list):
        try:
            depth_range = [int(x) if isinstance(x, str) else x for x in depth_range]
        except ValueError:
            return {"error": f"Invalid depth_range values: {depth_range}"}
    
    depth_tuple = tuple(depth_range) if depth_range else None

    result = progressive_element_cloner.expand_children(element_id, depth_tuple, max_count)
    return response_handler.handle_response(result, f"expand_children_{element_id}")


@section_tool("progressive-cloning")
async def expand_css_rules(
    element_id: str,
    source_types: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Expand CSS rules data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        source_types (Optional[List[str]]): CSS rule sources to include (inline, external stylesheet URLs).

    Returns:
        Dict[str, Any]: Filtered CSS rules data.
    """
    return progressive_element_cloner.expand_css_rules(element_id, source_types)


@section_tool("progressive-cloning")
async def expand_pseudo_elements(
    element_id: str
) -> Dict[str, Any]:
    """
    Expand pseudo-elements data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: Pseudo-elements data (::before, ::after, etc.).
    """
    return progressive_element_cloner.expand_pseudo_elements(element_id)


@section_tool("progressive-cloning")
async def expand_animations(
    element_id: str
) -> Dict[str, Any]:
    """
    Expand animations and fonts data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: Animations, transitions, and fonts data.
    """
    return progressive_element_cloner.expand_animations(element_id)


@section_tool("progressive-cloning")
async def list_stored_elements() -> Dict[str, Any]:
    """
    List all stored elements with their basic info.

    Returns:
        Dict[str, Any]: List of stored elements with metadata.
    """
    return progressive_element_cloner.list_stored_elements()


@section_tool("progressive-cloning")
async def clear_stored_element(
    element_id: str
) -> Dict[str, Any]:
    """
    Clear a specific stored element.

    Args:
        element_id (str): Element ID to clear.

    Returns:
        Dict[str, Any]: Success/error message.
    """
    return progressive_element_cloner.clear_stored_element(element_id)


@section_tool("progressive-cloning")
async def clear_all_elements() -> Dict[str, Any]:
    """
    Clear all stored elements.

    Returns:
        Dict[str, Any]: Success message.
    """
    return progressive_element_cloner.clear_all_elements()


@section_tool("file-extraction")
async def clone_element_to_file(
    instance_id: str,
    selector: str,
    extraction_options: Optional[str] = None
) -> Dict[str, Any]:
    """
    Clone element completely and save to file, returning file path instead of full data.

    This is ideal when you want complete element data but don't want to overwhelm
    the response with large JSON objects. The data is saved to a JSON file that
    can be read later.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): JSON string with extraction options.

    Returns:
        Dict[str, Any]: File path and summary information about the cloned element.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError:
            return {"error": "Invalid extraction_options JSON"}
    return await file_based_element_cloner.clone_element_complete_to_file(
        tab, selector=selector, extraction_options=parsed_options
    )


@section_tool("file-extraction")
async def extract_complete_element_to_file(
    instance_id: str,
    selector: str,
    include_children: bool = True
) -> Dict[str, Any]:
    """
    Extract complete element using working comprehensive cloner and save to file.

    This uses the proven comprehensive extraction logic that returns large amounts
    of data, but saves it to a file instead of overwhelming the response.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: File path and concise summary instead of massive data dump.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_complete_element_to_file(
        tab, selector, include_children
    )


@section_tool("element-extraction")
async def extract_complete_element_cdp(
    instance_id: str,
    selector: str,
    include_children: bool = True
) -> Dict[str, Any]:
    """
    Extract complete element using native CDP methods for 100% accuracy.

    This uses Chrome DevTools Protocol's native methods to extract:
    - Complete computed styles via CSS.getComputedStyleForNode
    - Matched CSS rules via CSS.getMatchedStylesForNode  
    - Event listeners via DOMDebugger.getEventListeners
    - Complete DOM structure and attributes

    This provides the most accurate element cloning possible by bypassing
    JavaScript limitations and using CDP's direct browser access.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: Complete element data with 100% accuracy.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    cdp_cloner = CDPElementCloner()
    return await cdp_cloner.extract_complete_element_cdp(tab, selector, include_children)


@section_tool("file-extraction")
async def extract_element_styles_to_file(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False
) -> Dict[str, Any]:
    """
    Extract element styles and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles.
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: File path and summary of extracted styles.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_element_styles_to_file(
        tab,
        selector=selector,
        include_computed=include_computed,
        include_css_rules=include_css_rules,
        include_pseudo=include_pseudo,
        include_inheritance=include_inheritance
    )


@section_tool("file-extraction")
async def extract_element_structure_to_file(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3
) -> Dict[str, Any]:
    """
    Extract element structure and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: File path and summary of extracted structure.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_element_structure_to_file(
        tab,
        selector=selector,
        include_children=include_children,
        include_attributes=include_attributes,
        include_data_attributes=include_data_attributes,
        max_depth=max_depth
    )


@section_tool("file-extraction")
async def extract_element_events_to_file(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = True
) -> Dict[str, Any]:
    """
    Extract element events and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers.
        include_listeners (bool): Include addEventListener handlers.
        include_framework (bool): Include framework-specific handlers.
        analyze_handlers (bool): Analyze handler functions.

    Returns:
        Dict[str, Any]: File path and summary of extracted events.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_element_events_to_file(
        tab,
        selector=selector,
        include_inline=include_inline,
        include_listeners=include_listeners,
        include_framework=include_framework,
        analyze_handlers=analyze_handlers
    )


@section_tool("file-extraction")
async def extract_element_animations_to_file(
    instance_id: str,
    selector: str,
    include_css_animations: bool = True,
    include_transitions: bool = True,
    include_transforms: bool = True,
    analyze_keyframes: bool = True
) -> Dict[str, Any]:
    """
    Extract element animations and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_css_animations (bool): Include CSS animations.
        include_transitions (bool): Include CSS transitions.
        include_transforms (bool): Include CSS transforms.
        analyze_keyframes (bool): Analyze keyframe rules.

    Returns:
        Dict[str, Any]: File path and summary of extracted animations.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_element_animations_to_file(
        tab,
        selector=selector,
        include_css_animations=include_css_animations,
        include_transitions=include_transitions,
        include_transforms=include_transforms,
        analyze_keyframes=analyze_keyframes
    )


@section_tool("file-extraction")
async def extract_element_assets_to_file(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False
) -> Dict[str, Any]:
    """
    Extract element assets and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Fetch external assets.

    Returns:
        Dict[str, Any]: File path and summary of extracted assets.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await file_based_element_cloner.extract_element_assets_to_file(
        tab,
        selector=selector,
        include_images=include_images,
        include_backgrounds=include_backgrounds,
        include_fonts=include_fonts,
        fetch_external=fetch_external
    )


@section_tool("file-extraction")
async def list_clone_files() -> List[Dict[str, Any]]:
    """
    List all element clone files saved to disk.

    Returns:
        List[Dict[str, Any]]: List of clone files with metadata and file information.
    """
    return file_based_element_cloner.list_clone_files()


@section_tool("file-extraction")
async def cleanup_clone_files(
    max_age_hours: int = 24
) -> Dict[str, int]:
    """
    Clean up old clone files to save disk space.

    Args:
        max_age_hours (int): Maximum age in hours for files to keep.

    Returns:
        Dict[str, int]: Number of files deleted.
    """
    deleted_count = file_based_element_cloner.cleanup_old_files(max_age_hours)
    return {"deleted_count": deleted_count}


@section_tool("cdp-functions")
async def list_cdp_commands() -> List[str]:
    """
    List all available CDP Runtime commands for function execution.

    Returns:
        List[str]: List of available CDP command names.
    """
    return await cdp_function_executor.list_cdp_commands()


@section_tool("cdp-functions")
async def execute_cdp_command(
    instance_id: str,
    command: str,
    params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Execute any CDP Runtime command with given parameters.

    Args:
        instance_id (str): Browser instance ID.
        command (str): CDP command name (e.g., 'evaluate', 'callFunctionOn').
        params (Dict[str, Any], optional): Command parameters as a dictionary.
                IMPORTANT: Use snake_case parameter names (e.g., 'return_by_value') 
                NOT camelCase ('returnByValue'). The nodriver library expects 
                Python-style parameter names.

    Returns:
        Dict[str, Any]: Command execution result.
        
    Example:
        # Correct - use snake_case
        params = {"expression": "document.title", "return_by_value": True}
        
        params = {"expression": "document.title", "returnByValue": True}
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await cdp_function_executor.execute_cdp_command(tab, command, params or {})


@section_tool("cdp-functions")
async def add_script_to_evaluate_on_new_document(
    instance_id: str,
    source: str,
    world_name: Optional[str] = None,
    include_command_line_api: Optional[bool] = None,
    run_immediately: bool = True,
) -> Dict[str, Any]:
    """
    Install JavaScript that runs before page scripts on each new document.

    Args:
        instance_id (str): Browser instance ID.
        source (str): JavaScript source to evaluate before page scripts.
        world_name (Optional[str]): Isolated world name, or None for the main world.
        include_command_line_api (Optional[bool]): Whether command line API is available.
        run_immediately (bool): Whether to run the script in existing contexts too.

    Returns:
        Dict[str, Any]: Result with success state and script identifier.
    """
    try:
        identifier = await browser_manager.add_init_script(
            instance_id,
            source,
            world_name=world_name,
            include_command_line_api=include_command_line_api,
            run_immediately=run_immediately,
        )
        return {"success": True, "identifier": identifier}
    except Exception as e:
        debug_logger.log_error("server", "add_script_to_evaluate_on_new_document", e)
        return {"success": False, "error": str(e)}


@section_tool("cdp-functions")
async def get_execution_contexts(
    instance_id: str
) -> List[Dict[str, Any]]:
    """
    Get all available JavaScript execution contexts.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, Any]]: List of execution contexts with their details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    contexts = await cdp_function_executor.get_execution_contexts(tab)
    return [
        {
            "id": ctx.id,
            "name": ctx.name,
            "origin": ctx.origin,
            "unique_id": ctx.unique_id,
            "aux_data": ctx.aux_data
        }
        for ctx in contexts
    ]


@section_tool("cdp-functions")
async def discover_global_functions(
    instance_id: str,
    context_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Discover all global JavaScript functions available in the page.

    Args:
        instance_id (str): Browser instance ID.
        context_id (str, optional): Optional execution context ID.

    Returns:
        List[Dict[str, Any]]: List of discovered functions with their details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    functions = await cdp_function_executor.discover_global_functions(tab, context_id)
    result = [
        {
            "name": func.name,
            "path": func.path,
            "signature": func.signature,
            "description": func.description
        }
        for func in functions
    ]
    
    file_response = response_handler.handle_response(
        result,
        fallback_filename_prefix="global_functions",
        metadata={
            "context_id": context_id,
            "function_count": len(result),
            "url": getattr(tab, 'url', 'unknown')
        }
    )
    
    if isinstance(file_response, dict) and "file_path" in file_response:
        return [{
            "name": "LARGE_RESPONSE_SAVED_TO_FILE",
            "path": "file_storage",
            "signature": "automatic_file_fallback",
            "description": f"Response too large ({file_response['estimated_tokens']} tokens), saved to: {file_response['filename']}"
        }]
    
    return file_response


@section_tool("cdp-functions")
async def discover_object_methods(
    instance_id: str,
    object_path: str
) -> List[Dict[str, Any]]:
    """
    Discover methods of a specific JavaScript object.

    Args:
        instance_id (str): Browser instance ID.
        object_path (str): Path to the object (e.g., 'document', 'window.localStorage').

    Returns:
        List[Dict[str, Any]]: List of discovered methods.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    methods = await cdp_function_executor.discover_object_methods(tab, object_path)
    methods_data = [
        {
            "name": method.name,
            "path": method.path,
            "signature": method.signature,
            "description": method.description
        }
        for method in methods
    ]
    
    return response_handler.handle_response(
        methods_data,
        f"object_methods_{object_path.replace('.', '_')}"
    )


@section_tool("cdp-functions")
async def call_javascript_function(
    instance_id: str,
    function_path: str,
    args: Optional[List[Any]] = None
) -> Dict[str, Any]:
    """
    Call a JavaScript function with arguments.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function (e.g., 'document.getElementById').
        args (List[Any], optional): List of arguments to pass to the function.

    Returns:
        Dict[str, Any]: Function call result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await cdp_function_executor.call_discovered_function(tab, function_path, args or [])


@section_tool("cdp-functions")
async def inspect_function_signature(
    instance_id: str,
    function_path: str
) -> Dict[str, Any]:
    """
    Inspect a JavaScript function's signature and details.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function.

    Returns:
        Dict[str, Any]: Function signature and details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await cdp_function_executor.inspect_function_signature(tab, function_path)


@section_tool("cdp-functions")
async def inject_and_execute_script(
    instance_id: str,
    script_code: str,
    context_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Inject and execute custom JavaScript code.

    Args:
        instance_id (str): Browser instance ID.
        script_code (str): JavaScript code to execute.
        context_id (str, optional): Optional execution context ID.

    Returns:
        Dict[str, Any]: Script execution result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await cdp_function_executor.inject_and_execute_script(tab, script_code, context_id)


@section_tool("cdp-functions")
async def create_persistent_function(
    instance_id: str,
    function_name: str,
    function_code: str
) -> Dict[str, Any]:
    """
    Create a persistent JavaScript function that survives page reloads.

    Args:
        instance_id (str): Browser instance ID.
        function_name (str): Name for the function.
        function_code (str): JavaScript function code.

    Returns:
        Dict[str, Any]: Function creation result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    try:
        source = await cdp_function_executor.prepare_persistent_function(tab, function_name, function_code, instance_id)
        identifier = await browser_manager.add_init_script(instance_id, source)
    except Exception as e:
        return {"success": False, "error": str(e), "function_name": function_name}
    return {
        "success": True,
        "function_name": function_name,
        "available_as": f"window.{function_name}",
        "script_identifier": identifier,
    }


@section_tool("cdp-functions")
async def execute_function_sequence(
    instance_id: str,
    function_calls: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Execute a sequence of JavaScript function calls.

    Args:
        instance_id (str): Browser instance ID.
        function_calls (List[Dict[str, Any]]): List of function calls, each with 'function_path', 'args', and optional 'context_id'.

    Returns:
        List[Dict[str, Any]]: List of function call results.
    """
    from .cdp_function_executor import FunctionCall
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return [{"success": False, "error": f"Instance not found: {instance_id}"}]
    calls = []
    for call_data in function_calls:
        calls.append(FunctionCall(
            function_path=call_data['function_path'],
            args=call_data.get('args', []),
            context_id=call_data.get('context_id')
        ))
    return await cdp_function_executor.execute_function_sequence(tab, calls)


@section_tool("cdp-functions")
async def create_page_binding(
    instance_id: str,
    binding_name: str,
    auto_resolve: bool = False,
    auto_response: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Expose window.<binding_name>(...args) so page JavaScript can call back to you.

    Each call returns a Promise in the page and is queued here. Read calls with
    get_page_binding_calls and answer them with resolve_page_binding_call, or set
    auto_resolve to answer every call immediately with auto_response. Nothing
    runs on the server host. Arguments must be JSON, payloads are capped at
    64 KB, and at most 500 calls are queued per instance. The binding is
    installed on every tab of the instance and survives navigation. Calls are
    delivered through Runtime events.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (str): JavaScript identifier for the function.
        auto_resolve (bool): Resolve every call immediately with auto_response.
        auto_response (Optional[Any]): JSON value returned to the page when auto_resolve is set.

    Returns:
        Dict[str, Any]: Creation result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    try:
        return await page_bindings.create(tab, instance_id, binding_name, auto_resolve, auto_response)
    except Exception as e:
        return {"success": False, "error": str(e)}


@section_tool("cdp-functions")
async def get_page_binding_calls(
    instance_id: str,
    binding_name: Optional[str] = None,
    wait_seconds: float = 0,
    limit: int = 50
) -> Dict[str, Any]:
    """
    Read calls made by page JavaScript to page bindings.

    Pending calls stay queued until resolved. Auto-resolved calls are returned
    once and then removed.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (Optional[str]): Only return calls to this binding.
        wait_seconds (float): Wait up to this many seconds (max 60) for a call when none is queued.
        limit (int): Maximum number of calls to return.

    Returns:
        Dict[str, Any]: Calls (call_id, binding, args, received_at, status) and binding names.
    """
    await browser_manager.touch_instance(instance_id)
    calls = await page_bindings.get_calls(instance_id, binding_name, wait_seconds, limit)
    return {"calls": calls, "bindings": page_bindings.list_bindings(instance_id)}


@section_tool("cdp-functions")
async def resolve_page_binding_call(
    instance_id: str,
    call_id: str,
    result: Optional[Any] = None,
    error: Optional[str] = None
) -> Dict[str, Any]:
    """
    Answer a pending page binding call.

    Args:
        instance_id (str): Browser instance ID.
        call_id (str): Call ID from get_page_binding_calls.
        result (Optional[Any]): JSON value the page Promise resolves with.
        error (Optional[str]): Reject the page Promise with this message instead.

    Returns:
        Dict[str, Any]: Whether the answer reached the page.
    """
    await browser_manager.touch_instance(instance_id)
    try:
        return await page_bindings.resolve(instance_id, call_id, result, error)
    except Exception as e:
        return {"success": False, "error": str(e)}


@section_tool("cdp-functions")
async def remove_page_binding(
    instance_id: str,
    binding_name: str
) -> Dict[str, Any]:
    """
    Remove a page binding from every tab and reject its pending calls.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (str): Binding to remove.

    Returns:
        Dict[str, Any]: Removal result.
    """
    await browser_manager.touch_instance(instance_id)
    try:
        return await page_bindings.remove(instance_id, binding_name)
    except Exception as e:
        return {"success": False, "error": str(e)}


@section_tool("cdp-functions")
async def execute_python_in_browser(
    instance_id: str,
    python_code: str
) -> Dict[str, Any]:
    """
    Execute Python code by translating it to JavaScript.

    Args:
        instance_id (str): Browser instance ID.
        python_code (str): Python code to translate and execute.

    Returns:
        Dict[str, Any]: Execution result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await cdp_function_executor.execute_python_in_browser(tab, python_code)


@section_tool("cdp-functions")
async def get_function_executor_info(
    instance_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get information about the CDP function executor state.

    Args:
        instance_id (str, optional): Optional browser instance ID for specific info.

    Returns:
        Dict[str, Any]: Function executor state and capabilities.
    """
    return await cdp_function_executor.get_function_executor_info(instance_id)


@section_tool("dynamic-hooks")
async def create_dynamic_hook(
    name: str,
    requirements: Dict[str, Any],
    function_code: str,
    instance_ids: Optional[List[str]] = None,
    priority: int = 100
) -> Dict[str, Any]:
    """
    Create a new dynamic hook with AI-generated Python function.
    
    This is the new powerful hook system that allows AI to write custom Python functions
    that process network requests in real-time with no pending state.
    
    Args:
        name (str): Human-readable hook name
        requirements (Dict[str, Any]): Matching criteria (url_pattern, method, resource_type, custom_condition)
        function_code (str): Python function code that processes requests (must define process_request(request))
        instance_ids (Optional[List[str]]): Browser instances to apply hook to (all if None)
        priority (int): Hook priority (lower = higher priority)
        
    Returns:
        Dict[str, Any]: Hook creation result with hook_id
        
    Example function_code:
        ```python
        def process_request(request):
            if "example.com" in request["url"]:
                return HookAction(action="redirect", url="https://httpbin.org/get")
            return HookAction(action="continue")
        ```
    """
    return await dynamic_hook_ai.create_dynamic_hook(
        name=name,
        requirements=requirements,
        function_code=function_code,
        instance_ids=instance_ids,
        priority=priority
    )


@section_tool("dynamic-hooks")
async def create_simple_dynamic_hook(
    name: str,
    url_pattern: str,
    action: str,
    target_url: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    instance_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Create a simple dynamic hook using predefined templates (easier for AI).
    
    Args:
        name (str): Hook name
        url_pattern (str): URL pattern to match
        action (str): Action type - 'block', 'redirect', 'add_headers', or 'log'
        target_url (Optional[str]): Target URL for redirect action
        custom_headers (Optional[Dict[str, str]]): Headers to add for add_headers action
        instance_ids (Optional[List[str]]): Browser instances to apply hook to
        
    Returns:
        Dict[str, Any]: Hook creation result
    """
    return await dynamic_hook_ai.create_simple_hook(
        name=name,
        url_pattern=url_pattern,
        action=action,
        target_url=target_url,
        custom_headers=custom_headers,
        instance_ids=instance_ids
    )


@section_tool("dynamic-hooks")
async def list_dynamic_hooks(instance_id: Optional[str] = None) -> Dict[str, Any]:
    """
    List all dynamic hooks.
    
    Args:
        instance_id (Optional[str]): Optional filter by browser instance
        
    Returns:
        Dict[str, Any]: List of hooks with details and statistics
    """
    return await dynamic_hook_ai.list_dynamic_hooks(instance_id=instance_id)


@section_tool("dynamic-hooks")
async def get_dynamic_hook_details(hook_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific dynamic hook.
    
    Args:
        hook_id (str): Hook identifier
        
    Returns:
        Dict[str, Any]: Detailed hook information including function code
    """
    return await dynamic_hook_ai.get_hook_details(hook_id=hook_id)


@section_tool("dynamic-hooks")
async def remove_dynamic_hook(hook_id: str) -> Dict[str, Any]:
    """
    Remove a dynamic hook.
    
    Args:
        hook_id (str): Hook identifier to remove
        
    Returns:
        Dict[str, Any]: Removal status
    """
    return await dynamic_hook_ai.remove_dynamic_hook(hook_id=hook_id)


@section_tool("dynamic-hooks")
def get_hook_documentation() -> Dict[str, Any]:
    """
    Get comprehensive documentation for creating hook functions (AI learning).
    
    Returns:
        Dict[str, Any]: Documentation of request object structure and HookAction types
    """
    return dynamic_hook_ai.get_request_documentation()


@section_tool("dynamic-hooks")
def get_hook_examples() -> Dict[str, Any]:
    """
    Get example hook functions for AI learning.
    
    Returns:
        Dict[str, Any]: Collection of example hook functions with explanations
    """
    return dynamic_hook_ai.get_hook_examples()


@section_tool("dynamic-hooks")
def get_hook_requirements_documentation() -> Dict[str, Any]:
    """
    Get documentation on hook requirements and matching criteria.
    
    Returns:
        Dict[str, Any]: Requirements documentation and best practices
    """
    return dynamic_hook_ai.get_requirements_documentation()


@section_tool("dynamic-hooks")
def get_hook_common_patterns() -> Dict[str, Any]:
    """
    Get common hook patterns and use cases.
    
    Returns:
        Dict[str, Any]: Common patterns like ad blocking, API proxying, etc.
    """
    return dynamic_hook_ai.get_common_patterns()


@section_tool("dynamic-hooks")
def validate_hook_function(function_code: str) -> Dict[str, Any]:
    """
    Validate hook function code for common issues before creating.
    
    Args:
        function_code (str): Python function code to validate
        
    Returns:
        Dict[str, Any]: Validation results with issues and warnings
    """
    return dynamic_hook_ai.validate_hook_function(function_code=function_code)


@section_tool("browser-management")
async def list_sources() -> Dict[str, Any]:
    """
    List installed browser profile sources and existing seeds with their age.

    Discovers installed Chrome, Edge, and Brave browser profiles on Linux,
    and reports existing seeds created with refresh_seed.

    Returns:
        Dict[str, Any]: Discovered browser profile sources and existing seeds.
    """
    return seed_tools.list_sources()


@section_tool("browser-management")
async def refresh_seed(
    name: str,
    source_browser: str,
    profile: Optional[str] = None,
    include_passwords: bool = False,
    include_cards: bool = False,
    indexeddb_cap_bytes: int = 50 * 1024 * 1024,
    source_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Refresh or create a curated browser profile seed from a source browser.

    Copies session state (cookies, local storage, extensions, settings) without
    caches from the source profile into a dedicated seed directory. The source
    directory is opened read-only and never modified.

    Args:
        name (str): Unique name for the seed (1-40 chars, [a-z0-9_-]).
        source_browser (str): Source browser family ("chrome", "edge", "brave")
            or path to user data directory.
        profile (Optional[str]): Profile directory name inside the source (e.g.
            "Default" or "Profile 1"). Defaults to auto-detecting the last-used profile.
        include_passwords (bool): Whether to include saved login credentials.
            Defaults to False.
        include_cards (bool): Whether to include saved payment data.
            Defaults to False.
        indexeddb_cap_bytes (int): Maximum bytes to keep per IndexedDB origin.
            Defaults to 52428800 (50 MB).
        source_dir (Optional[str]): Explicit path to user data directory if not
            using standard system locations.

    Returns:
        Dict[str, Any]: Summary of the refresh operation (status, counts, bytes).
    """
    return seed_tools.refresh_seed(
        name=name,
        source_browser=source_browser,
        profile=profile,
        include_passwords=include_passwords,
        include_cards=include_cards,
        indexeddb_cap_bytes=indexeddb_cap_bytes,
        source_dir=source_dir,
    )


if parse_bool_env("XPOOL_SAFE_MODE", default=False):
    DISABLED_SECTIONS.add("cdp-functions")
    apply_disabled_sections()


def main(argv: Optional[List[str]] = None) -> None:
    """
    Parse command line options and run the MCP server.

    Args:
        argv (Optional[List[str]]): Arguments to parse instead of sys.argv.

    Returns:
        None
    """
    parser = argparse.ArgumentParser(
        description=f"Stealth Browser MCP Server with {sum(len(tools) for tools in SECTION_TOOLS.values())} tools"
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                      help="Transport protocol to use")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8000)),
                      help="Port for HTTP transport")
    parser.add_argument("--host", default="127.0.0.1",
                      help="Host for HTTP transport (use 0.0.0.0 only with an auth token on trusted networks)")
    
    parser.add_argument("--disable-browser-management", action="store_true",
                      help="Disable browser management tools (spawn, navigate, close, etc.)")
    parser.add_argument("--disable-element-interaction", action="store_true",
                      help="Disable element interaction tools (click, type, scroll, etc.)")
    parser.add_argument("--disable-element-extraction", action="store_true",
                      help="Disable element extraction tools (styles, structure, events, etc.)")
    parser.add_argument("--disable-file-extraction", action="store_true",
                      help="Disable file-based extraction tools")
    parser.add_argument("--disable-network-debugging", action="store_true",
                      help="Disable network debugging and interception tools")
    parser.add_argument("--disable-cdp-functions", action="store_true",
                      help="Disable CDP function execution tools")
    parser.add_argument("--disable-progressive-cloning", action="store_true",
                      help="Disable progressive element cloning tools")
    parser.add_argument("--disable-cookies-storage", action="store_true",
                      help="Disable cookie and storage management tools")
    parser.add_argument("--disable-tabs", action="store_true",
                      help="Disable tab management tools")
    parser.add_argument("--disable-debugging", action="store_true",
                      help="Disable debug and system tools")
    parser.add_argument("--disable-dynamic-hooks", action="store_true",
                      help="Disable dynamic network hook system")
    
    parser.add_argument("--minimal", action="store_true",
                      help="Enable only core browser management and element interaction (disable everything else)")
    parser.add_argument("--list-sections", action="store_true",
                      help="List all available tool sections and exit")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG_LOGGING_ENABLED,
        help="Enable debug logging to stderr",
    )
    parser.add_argument(
        "--xpool-safe",
        action="store_true",
        default=parse_bool_env("XPOOL_SAFE_MODE", default=False),
        help="Enable xpool-safe surface (disables cdp-functions tools that trigger Runtime.enable)",
    )
    
    args = parser.parse_args(argv)

    if args.debug and not debug_logger._enabled:
        debug_logger.enable()
    
    if args.list_sections:
        print("Available tool sections:")
        for section, description in SECTION_DESCRIPTIONS.items():
            print(f"  {section}: {description} ({len(SECTION_TOOLS.get(section, []))} tools)")
        print("\nUse --disable-<section-name> to disable specific sections")
        print("Use --minimal to enable only core functionality")
        sys.exit(0)
    
    if args.minimal:
        DISABLED_SECTIONS.update([
            "element-extraction", "file-extraction", "network-debugging",
            "cdp-functions", "progressive-cloning", "cookies-storage",
            "tabs", "debugging", "dynamic-hooks"
        ])
    
    if args.disable_browser_management:
        DISABLED_SECTIONS.add("browser-management")
    if args.disable_element_interaction:
        DISABLED_SECTIONS.add("element-interaction")
    if args.disable_element_extraction:
        DISABLED_SECTIONS.add("element-extraction")
    if args.disable_file_extraction:
        DISABLED_SECTIONS.add("file-extraction")
    if args.disable_network_debugging:
        DISABLED_SECTIONS.add("network-debugging")
    if args.disable_cdp_functions:
        DISABLED_SECTIONS.add("cdp-functions")
    if args.disable_progressive_cloning:
        DISABLED_SECTIONS.add("progressive-cloning")
    if args.disable_cookies_storage:
        DISABLED_SECTIONS.add("cookies-storage")
    if args.disable_tabs:
        DISABLED_SECTIONS.add("tabs")
    if args.disable_debugging:
        DISABLED_SECTIONS.add("debugging")
    if args.disable_dynamic_hooks:
        DISABLED_SECTIONS.add("dynamic-hooks")

    if args.xpool_safe:
        DISABLED_SECTIONS.add("cdp-functions")

    apply_disabled_sections()
    
    if DISABLED_SECTIONS:
        debug_logger.log_info(
            "server",
            "startup",
            f"Disabled tool sections: {', '.join(sorted(DISABLED_SECTIONS))}",
        )
    
    if args.transport == "http":
        if HTTP_AUTH_TOKEN is None and not is_loopback_host(args.host):
            print(
                f"WARNING: HTTP transport is listening on {args.host} without an auth token. "
                "Set STEALTH_BROWSER_MCP_AUTH_TOKEN before exposing the server beyond localhost.",
                file=sys.stderr,
            )
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        install_stdio_guard()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
