"""Main MCP server for browser automation."""

import asyncio
import base64
import importlib
import json
import os
import signal
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import nodriver as uc
from fastmcp import FastMCP

from browser_manager import BrowserManager
from cdp_element_cloner import CDPElementCloner
from cdp_function_executor import CDPFunctionExecutor
from comprehensive_element_cloner import comprehensive_element_cloner
from debug_logger import debug_logger
from dom_handler import DOMHandler
from element_cloner import element_cloner
from file_based_element_cloner import file_based_element_cloner
from models import (
    BrowserOptions,
    NavigationOptions,
    ScriptResult,
    BrowserState,
    PageState,
)
from network_interceptor import NetworkInterceptor
from dynamic_hook_system import dynamic_hook_system
from dynamic_hook_ai_interface import dynamic_hook_ai
from persistent_storage import persistent_storage
from progressive_element_cloner import progressive_element_cloner
from response_handler import response_handler
from platform_utils import validate_browser_environment, get_platform_info
from process_cleanup import process_cleanup

DISABLED_SECTIONS = set()

def is_section_enabled(section: str) -> bool:
    """Check if a tool section is enabled."""
    return section not in DISABLED_SECTIONS

def section_tool(section: str):
    """Decorator to conditionally register tools based on section status."""
    def decorator(func):
        if is_section_enabled(section):
            return mcp.tool(func)
        else:
            return func
    return decorator

@asynccontextmanager
async def app_lifespan(server):
    """
    Manage application lifecycle with proper cleanup.

    Args:
        server (Any): The server instance for which the lifespan is being managed.
    """
    debug_logger.log_info("server", "startup", "Starting Browser Automation MCP Server...")
    try:
        yield
    finally:
        debug_logger.log_info("server", "shutdown", "Shutting down Browser Automation MCP Server...")
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
    
    All browser instances are undetectable by anti-bot systems.
    """,
    lifespan=app_lifespan,
)

browser_manager = BrowserManager()
network_interceptor = NetworkInterceptor()
dom_handler = DOMHandler()
cdp_function_executor = CDPFunctionExecutor()

@section_tool("browser-management")
async def spawn_browser(
    headless: bool = False,
    user_agent: Optional[str] = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    proxy: Optional[str] = None,
    block_resources: List[str] = None,
    extra_headers: Dict[str, str] = None,
    user_data_dir: Optional[str] = None,
    sandbox: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Spawn a new browser instance.

    Args:
        headless (bool): Run in headless mode.
        user_agent (Optional[str]): Custom user agent string.
        viewport_width (int): Viewport width in pixels.
        viewport_height (int): Viewport height in pixels.
        proxy (Optional[str]): Proxy server URL.
        block_resources (List[str]): List of resource types to block (e.g., ['image', 'font', 'stylesheet']).
        extra_headers (Dict[str, str]): Additional HTTP headers.
        user_data_dir (Optional[str]): Path to user data directory for persistent sessions.
        sandbox (Optional[Any]): Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect.

    Returns:
        Dict[str, Any]: Instance information including instance_id.
    """
    try:
        from platform_utils import is_running_as_root, is_running_in_container
        
        if sandbox is None:
            sandbox = not (is_running_as_root() or is_running_in_container())
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
            block_resources=block_resources or [],
            extra_headers=extra_headers or {},
            user_data_dir=user_data_dir,
            sandbox=sandbox
        )
        instance = await browser_manager.spawn_browser(options)
        tab = await browser_manager.get_tab(instance.instance_id)
        if tab:
            await network_interceptor.setup_interception(
                tab, instance.instance_id, block_resources
            )
        return {
            "instance_id": instance.instance_id,
            "state": instance.state,
            "headless": instance.headless,
            "viewport": instance.viewport
        }
    except Exception as e:
        raise Exception(f"Failed to spawn browser: {str(e)}")

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
    success = await browser_manager.close_instance(instance_id)
    if success:
        await network_interceptor.clear_instance_data(instance_id)
    return success

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
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    try:
        if referrer:
            await tab.send(uc.cdp.network.set_extra_http_headers(
                headers={"Referer": referrer}
            ))
        await tab.get(url)
        if wait_until == "domcontentloaded":
            await tab.wait(uc.cdp.page.DomContentEventFired)
        elif wait_until == "networkidle":
            await asyncio.sleep(2)
        else:
            await tab.wait(uc.cdp.page.LoadEventFired)
        final_url = await tab.evaluate("window.location.href")
        title = await tab.evaluate("document.title")
        await browser_manager.update_instance_state(instance_id, final_url, title)
        return {
            "url": final_url,
            "title": title,
            "success": True
        }
    except Exception as e:
        raise

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
    await tab.reload()
    return True

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
    timeout: int = 10000
) -> bool:
    """
    Click an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text_match (Optional[str]): Click element with matching text.
        timeout (int): Timeout in milliseconds.

    Returns:
        bool: True if clicked successfully.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.click_element(tab, selector, text_match, timeout)

@section_tool("element-interaction")
async def type_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 50,
    parse_newlines: bool = False,
    shift_enter: bool = False
) -> bool:
    """
    Type text into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to type.
        clear_first (bool): Clear field before typing.
        delay_ms (int): Delay between keystrokes in milliseconds.
        parse_newlines (bool): If True, parse \n as Enter key presses.
        shift_enter (bool): If True, use Shift+Enter instead of Enter (for chat apps).

    Returns:
        bool: True if typed successfully.
    """
    if isinstance(delay_ms, str):
        delay_ms = int(delay_ms)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.type_text(tab, selector, text, clear_first, delay_ms, parse_newlines, shift_enter)

@section_tool("element-interaction")
async def paste_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True
) -> bool:
    """
    Paste text instantly into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to paste.
        clear_first (bool): Clear field before pasting.

    Returns:
        bool: True if pasted successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.paste_text(tab, selector, text, clear_first)

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
    text_content: Optional[str] = None
) -> bool:
    """
    Wait for an element to appear.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        timeout (int): Timeout in milliseconds.
        visible (bool): Wait for element to be visible.
        text_content (Optional[str]): Wait for specific text content.

    Returns:
        bool: True if element found.
    """
    if isinstance(timeout, str):
        timeout = int(timeout)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await dom_handler.wait_for_element(tab, selector, timeout, visible, text_content)

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
        return {
            "success": True,
            "result": result,
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
    file_path: Optional[str] = None
) -> Union[str, Dict[str, Any]]:
    """
    Take a screenshot of the page.

    Args:
        instance_id (str): Browser instance ID.
        full_page (bool): Capture full page (not just viewport).
        format (str): Image format ('png' or 'jpeg').
        file_path (Optional[str]): Optional file path to save screenshot to.

    Returns:
        Union[str, Dict]: File path if file_path provided, otherwise optimized base64 data or file info dict.
    """
    from PIL import Image
    import io
    
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    
    if file_path:
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await tab.save_screenshot(save_path)
        return f"Screenshot saved. AI agents should use the Read tool to view this image: {str(save_path.absolute())}"
    
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)
    
    try:
        await tab.save_screenshot(tmp_path)
        
        with Image.open(tmp_path) as img:
            if img.mode in ('RGBA', 'LA', 'P') and format.lower() == 'jpeg':
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            
            output_buffer = io.BytesIO()
            
            if format.lower() == 'jpeg':
                img.save(output_buffer, format='JPEG', quality=85, optimize=True)
            else:
                img.save(output_buffer, format='PNG', optimize=True)
            
            compressed_bytes = output_buffer.getvalue()
            
            base64_size = len(compressed_bytes) * 1.33
            estimated_tokens = int(base64_size / 4)
            
            if estimated_tokens > 20000:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_filename = f"screenshot_{timestamp}_{instance_id[:8]}.{format.lower()}"
                screenshot_path = response_handler.clone_dir / screenshot_filename
                
                with open(screenshot_path, 'wb') as f:
                    f.write(compressed_bytes)
                
                file_size_kb = len(compressed_bytes) / 1024
                return {
                    "file_path": str(screenshot_path),
                    "filename": screenshot_filename,
                    "file_size_kb": round(file_size_kb, 2),
                    "estimated_tokens": estimated_tokens,
                    "reason": "Screenshot too large, automatically saved to file",
                    "message": f"Screenshot saved. AI agents should use the Read tool to view this image: {str(screenshot_path)}"
                }
            
            return base64.b64encode(compressed_bytes).decode('utf-8')
            
    finally:
        if tmp_path.exists():
            os.unlink(tmp_path)


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
            import base64
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
        cookie["same_site"] = same_site
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
        return json.dumps(state.dict(), indent=2)
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
        return json.dumps(cookies, indent=2)
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
    return json.dumps([req.dict() for req in requests], indent=2)


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
        return f"Export timeout - file too large. Try with smaller limits or 'gzip-pickle' format."


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
    await tab
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
    browser = await browser_manager.get_browser(instance_id)
    if not browser:
        raise Exception(f"Instance not found: {instance_id}")
    try:
        new_tab_obj = await browser.get(url, new_tab=True)
        await new_tab_obj
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
    return await response_handler.handle_response(result, f"element_assets_{instance_id}_{selector.replace(' ', '_')}")


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
    return await response_handler.handle_response(result, f"related_files_{instance_id}")


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

    Returns:
        str: Status message.
    """
    try:
        modules_to_reload = [
            'browser_manager',
            'network_interceptor',
            'dom_handler',
            'debug_logger',
            'models'
        ]
        reloaded_modules = []
        for module_name in modules_to_reload:
            if module_name in sys.modules:
                importlib.reload(sys.modules[module_name])
                reloaded_modules.append(module_name)
                if module_name == 'browser_manager':
                    global browser_manager, BrowserManager
                    browser_manager = BrowserManager()
                elif module_name == 'network_interceptor':
                    global network_interceptor, NetworkInterceptor
                    network_interceptor = NetworkInterceptor()
                elif module_name == 'dom_handler':
                    global dom_handler, DOMHandler
                    dom_handler = DOMHandler()
                elif module_name == 'debug_logger':
                    global debug_logger
                    from debug_logger import debug_logger
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
            if module_name in sys.modules:
                module = sys.modules[module_name]
                modules_info.append(f"✅ {module_name}: {getattr(module, '__file__', 'built-in')}")
            else:
                modules_info.append(f"❌ {module_name}: Not loaded")
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
    params: Dict[str, Any] = None
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
    context_id: str = None
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
    
    return await response_handler.handle_response(
        methods_data,
        f"object_methods_{object_path.replace('.', '_')}"
    )


@section_tool("cdp-functions")
async def call_javascript_function(
    instance_id: str,
    function_path: str,
    args: List[Any] = None
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
    context_id: str = None
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
    return await cdp_function_executor.create_persistent_function(tab, function_name, function_code, instance_id)


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
    from cdp_function_executor import FunctionCall
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
async def create_python_binding(
    instance_id: str,
    binding_name: str,
    python_code: str
) -> Dict[str, Any]:
    """
    Create a binding that allows JavaScript to call Python functions.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (str): Name for the binding.
        python_code (str): Python function code (as string).

    Returns:
        Dict[str, Any]: Binding creation result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    try:
        exec_globals = {}
        exec(python_code, exec_globals)
        python_function = None
        for name, obj in exec_globals.items():
            if callable(obj) and not name.startswith('_'):
                python_function = obj
                break
        if not python_function:
            return {"success": False, "error": "No function found in Python code"}
        return await cdp_function_executor.create_python_binding(tab, binding_name, python_function)
    except Exception as e:
        return {"success": False, "error": f"Failed to create Python function: {str(e)}"}


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
    instance_id: str = None
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



if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Stealth Browser MCP Server with 90 tools")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                      help="Transport protocol to use")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 8000)),
                      help="Port for HTTP transport")
    parser.add_argument("--host", default="0.0.0.0",
                      help="Host for HTTP transport")
    
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
    
    args = parser.parse_args()
    
    if args.list_sections:
        print("Available tool sections:")
        print("  browser-management: Core browser operations (11 tools)")
        print("  element-interaction: Page interaction and element manipulation (8 tools)")
        print("  element-extraction: Element cloning and extraction (10 tools)")
        print("  file-extraction: File-based extraction tools (9 tools)")
        print("  network-debugging: Network monitoring and interception (10 tools)")
        print("  cdp-functions: Chrome DevTools Protocol function execution (15 tools)")
        print("  progressive-cloning: Advanced element cloning system (10 tools)")
        print("  cookies-storage: Cookie and storage management (3 tools)")
        print("  tabs: Tab management (5 tools)")
        print("  debugging: Debug and system tools (6 tools)")
        print("  dynamic-hooks: AI-powered network hook system (12 tools)")
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
    
    if DISABLED_SECTIONS:
        print(f"Disabled tool sections: {', '.join(sorted(DISABLED_SECTIONS))}", file=sys.stderr)
    
    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")