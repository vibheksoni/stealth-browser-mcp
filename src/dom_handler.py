"""DOM manipulation and element interaction utilities."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from nodriver import Tab

import input_actions
from debug_logger import debug_logger
from file_upload_security import validate_upload_paths
from js_values import evaluate_to_python
from key_definitions import MODIFIER_SHIFT, resolve_key_descriptor, resolve_modifiers
from models import ElementInfo

DEFAULT_ELEMENT_TIMEOUT = 10.0
WAIT_POLL_INTERVAL = 0.1
ELEMENT_READY_JS = """function (visible, text) {
  if (visible) {
    const style = window.getComputedStyle(this);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  }
  return !text || (this.innerText || this.textContent || '').includes(text);
}"""


async def evaluate_or_raise(tab: Tab, expression: str, await_promise: bool = False) -> Any:
    """
    Evaluate JavaScript and raise when the script throws.

    Args:
        tab (Tab): The browser tab object.
        expression (str): JavaScript expression to evaluate.
        await_promise (bool): Await the result when it is a Promise.

    Returns:
        Any: Plain Python value produced by the script.
    """
    value, error = await evaluate_to_python(tab, expression, await_promise=await_promise)
    if error:
        raise Exception(f"JavaScript error: {error}")
    return value


class DOMHandler:
    """Handles DOM queries and element interactions."""

    @staticmethod
    async def query_elements(
        tab: Tab,
        selector: str,
        text_filter: Optional[str] = None,
        visible_only: bool = True,
        limit: Optional[Any] = None
    ) -> List[ElementInfo]:
        """
        Query elements with advanced filtering.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS or XPath selector for elements.
            text_filter (Optional[str]): Filter elements by text content.
            visible_only (bool): Only include visible elements.
            limit (Optional[Any]): Limit the number of results.

        Returns:
            List[ElementInfo]: List of element information objects.
        """
        processed_limit = None
        if limit is not None:
            try:
                if isinstance(limit, int):
                    processed_limit = limit
                elif isinstance(limit, str) and limit.isdigit():
                    processed_limit = int(limit)
                elif isinstance(limit, str) and limit.strip() == '':
                    processed_limit = None
                else:
                    debug_logger.log_warning('DOMHandler', 'query_elements',
                                            f'Invalid limit parameter: {limit} (type: {type(limit)})')
                    processed_limit = None
            except (ValueError, TypeError) as e:
                debug_logger.log_error('DOMHandler', 'query_elements', e,
                                      {'limit_value': limit, 'limit_type': type(limit)})
                processed_limit = None

        debug_logger.log_info('DOMHandler', 'query_elements',
                             f'Starting query with selector: {selector}',
                             {'text_filter': text_filter, 'visible_only': visible_only,
                              'limit': limit, 'processed_limit': processed_limit})
        try:
            if selector.startswith('//'):
                elements = await tab.xpath(selector)
                debug_logger.log_info('DOMHandler', 'query_elements',
                                     f'XPath query returned {len(elements)} elements')
            else:
                elements = await tab.select_all(selector)
                debug_logger.log_info('DOMHandler', 'query_elements',
                                     f'CSS query returned {len(elements)} elements')

            results = []
            for idx, elem in enumerate(elements):
                try:
                    if hasattr(elem, 'update'):
                        await elem.update()

                    tag_name = elem.tag_name if hasattr(elem, 'tag_name') else 'unknown'
                    text_content = elem.text_all if hasattr(elem, 'text_all') else ''
                    attrs = elem.attrs if hasattr(elem, 'attrs') else {}

                    if text_filter and text_filter.lower() not in text_content.lower():
                        continue

                    is_visible = True
                    if visible_only:
                        try:
                            is_visible = await elem.apply(
                                """(elem) => {
                                    var style = window.getComputedStyle(elem);
                                    return style.display !== 'none' && 
                                           style.visibility !== 'hidden' && 
                                           style.opacity !== '0';
                                }"""
                            )
                            if not is_visible:
                                continue
                        except:
                            pass

                    bbox = None
                    try:
                        position = await elem.get_position()
                        if position:
                            bbox = {
                                'x': position.x,
                                'y': position.y,
                                'width': position.width,
                                'height': position.height
                            }
                    except Exception:
                        pass

                    is_clickable = False

                    children_count = 0
                    try:
                        if hasattr(elem, 'children'):
                            children = elem.children
                            children_count = len(children) if children else 0
                    except Exception:
                        pass

                    element_info = ElementInfo(
                        selector=selector,
                        tag_name=tag_name,
                        text=text_content[:500] if text_content else None,
                        attributes=attrs or {},
                        is_visible=is_visible,
                        is_clickable=is_clickable,
                        bounding_box=bbox,
                        children_count=children_count
                    )

                    results.append(element_info)

                    if processed_limit and len(results) >= processed_limit:
                        debug_logger.log_info('DOMHandler', 'query_elements',
                                             f'Reached limit of {processed_limit} results')
                        break

                except Exception as elem_error:
                    debug_logger.log_error('DOMHandler', 'query_elements',
                                          elem_error,
                                          {'element_index': idx, 'selector': selector})
                    continue

            debug_logger.log_info('DOMHandler', 'query_elements',
                                 f'Returning {len(results)} results')
            return results

        except Exception as e:
            debug_logger.log_error('DOMHandler', 'query_elements', e,
                                  {'selector': selector, 'tab': str(tab)})
            return []

    @staticmethod
    async def _resolve_target(tab: Tab, selector: str, timeout: float) -> int:
        """
        Resolve a selector to a backend node id or raise.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector.
            timeout (float): Seconds to wait for the element.

        Returns:
            int: Backend node id.
        """
        backend_node_id = await input_actions.find_node(tab, selector, timeout)
        if backend_node_id is None:
            raise Exception(f"Element not found: {selector}")
        return backend_node_id

    @staticmethod
    async def click_element(
        tab: Tab,
        selector: str,
        text_match: Optional[str] = None,
        timeout: int = 10000
    ) -> bool:
        """
        Click an element with trusted mouse events.

        The element is scrolled into view, the mouse moves to its center, and
        the left button is pressed and released, producing the same pointer,
        mouse, and click events a user would. Elements without a layout box or
        covered by another element are clicked through JavaScript instead.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            text_match (Optional[str]): Match element by text content.
            timeout (int): Timeout in milliseconds.

        Returns:
            bool: True if click succeeded.
        """
        try:
            if text_match:
                element = await tab.find(text_match, best_match=True, timeout=timeout / 1000)
                if not element:
                    raise Exception(f"Element not found with text: {text_match}")
                backend_node_id = int(element.backend_node_id)
            else:
                backend_node_id = await DOMHandler._resolve_target(tab, selector, timeout / 1000)
            await input_actions.click_node(tab, backend_node_id)
            return True
        except Exception as e:
            raise Exception(f"Failed to click element: {str(e)}")

    @staticmethod
    async def type_text(
        tab: Tab,
        selector: str,
        text: str,
        clear_first: bool = True,
        delay_ms: int = 50,
        parse_newlines: bool = False,
        shift_enter: bool = False
    ) -> bool:
        """
        Type text with trusted keyboard events and human-like delays.

        Each ASCII character produces keydown, keypress, input, and keyup
        events, with Shift held for capitals and shifted symbols. Other
        characters are inserted like an input method would.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to type.
            clear_first (bool): Clear input before typing.
            delay_ms (int): Delay between keystrokes in milliseconds.
            parse_newlines (bool): If True, press Enter for each \n instead of inserting a line break.
            shift_enter (bool): If True, press Shift+Enter for each \n (for chat apps).

        Returns:
            bool: True if typing succeeded.
        """
        try:
            backend_node_id = await DOMHandler._resolve_target(tab, selector, DEFAULT_ELEMENT_TIMEOUT)
            await input_actions.focus_node(tab, backend_node_id)
            if clear_first:
                await input_actions.clear_field(tab, backend_node_id)
            delay = max(delay_ms, 0) / 1000
            for index, char in enumerate(text):
                await input_actions.type_character(tab, char, parse_newlines or shift_enter, shift_enter)
                if delay and index < len(text) - 1:
                    await asyncio.sleep(delay)
            return True
        except Exception as e:
            raise Exception(f"Failed to type text: {str(e)}")

    @staticmethod
    async def paste_text(
        tab: Tab,
        selector: str,
        text: str,
        clear_first: bool = True
    ) -> bool:
        """
        Insert text in one step, like a paste.

        Much faster than typing character by character. The page receives
        beforeinput and input events but no key events.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to paste.
            clear_first (bool): Clear input before pasting.

        Returns:
            bool: True if pasting succeeded.
        """
        from nodriver import cdp

        try:
            backend_node_id = await DOMHandler._resolve_target(tab, selector, DEFAULT_ELEMENT_TIMEOUT)
            await input_actions.focus_node(tab, backend_node_id)
            if clear_first:
                await input_actions.clear_field(tab, backend_node_id)
            await tab.send(cdp.input_.insert_text(text))
            return True
        except Exception as e:
            raise Exception(f"Failed to paste text: {str(e)}")

    @staticmethod
    async def press_key(
        tab: Tab,
        key: str,
        modifiers: Optional[List[str]] = None,
        selector: Optional[str] = None,
        count: int = 1,
        delay_ms: int = 50
    ) -> Dict[str, Any]:
        """
        Press a real key using trusted CDP Input.dispatchKeyEvent events.

        Unlike synthetic JavaScript KeyboardEvents, these events are trusted by the
        page, so React-select, typeahead and other widgets that gate on
        `event.isTrusted` react to them.

        Args:
            tab (Tab): The browser tab object.
            key (str): DOM KeyboardEvent.key name (Enter, Tab, ArrowDown, F5, ...) or a single printable character.
            modifiers (Optional[List[str]]): Modifier keys to hold: Shift, Control/Ctrl, Alt, Meta/Cmd.
            selector (Optional[str]): CSS selector to focus before pressing. Presses on the active element when omitted.
            count (int): Number of times to press the key.
            delay_ms (int): Delay between presses in milliseconds.

        Returns:
            Dict[str, Any]: { success: True, key: str, count: int, modifiers: [str, ...] }
        """
        from nodriver import cdp

        try:
            if count < 1:
                raise Exception(f"Invalid count: {count}. Must be 1 or greater.")

            descriptor = resolve_key_descriptor(key)
            modifier_mask, canonical_modifiers = resolve_modifiers(modifiers)

            if selector:
                backend_node_id = await DOMHandler._resolve_target(tab, selector, DEFAULT_ELEMENT_TIMEOUT)
                await input_actions.focus_node(tab, backend_node_id)

            text = descriptor["text"]
            unmodified_text = text
            if modifier_mask & MODIFIER_SHIFT and text is not None:
                text = text.upper() if text.isascii() else text
                key = descriptor["key"].upper() if len(descriptor["key"]) == 1 else descriptor["key"]
            else:
                key = descriptor["key"]
            if modifier_mask & ~MODIFIER_SHIFT:
                text = None
                unmodified_text = None

            for index in range(count):
                await tab.send(cdp.input_.dispatch_key_event(
                    "keyDown",
                    modifiers=modifier_mask,
                    key=key,
                    code=descriptor["code"],
                    windows_virtual_key_code=descriptor["virtual_key_code"],
                    text=text,
                    unmodified_text=unmodified_text
                ))
                await tab.send(cdp.input_.dispatch_key_event(
                    "keyUp",
                    modifiers=modifier_mask,
                    key=key,
                    code=descriptor["code"],
                    windows_virtual_key_code=descriptor["virtual_key_code"]
                ))
                if index < count - 1:
                    await asyncio.sleep(delay_ms / 1000)

            return {
                "success": True,
                "key": descriptor["key"],
                "count": count,
                "modifiers": canonical_modifiers
            }

        except Exception as e:
            raise Exception(f"Failed to press key: {str(e)}")

    @staticmethod
    async def file_upload(
        tab: Tab,
        selector: str,
        paths: List[str],
    ) -> Dict[str, Any]:
        """
        Upload allowed local files to a <input type="file"> element via CDP DOM.setFileInputFiles.

        Resolves the actual file input even when the selector matches a wrapper
        (Workday, Ashby, LinkedIn often hide the real input behind a styled button).
        Refuses multi-file upload when the input lacks the `multiple` attribute
        (avoids browser crash documented in nodriver send_file()).

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the file input or any ancestor that contains it.
            paths (List[str]): Absolute allowlisted local file paths to upload.

        Returns:
            Dict[str, Any]: { success: True, count: N, files: [basename, ...] }
        """
        try:
            resolved_paths = validate_upload_paths(paths)

            element = await tab.select(selector)
            if not element:
                raise Exception(f"Element not found: {selector}")

            tag_name = element.tag_name
            input_type = (element.attrs.get('type') or '').lower()
            if tag_name != 'input' or input_type != 'file':
                inner_input = await element.query_selector('input[type="file"]')
                if inner_input:
                    element = inner_input
                else:
                    raise Exception(
                        f"Selector did not resolve to a file input "
                        f"(got <{tag_name} type=\"{input_type}\">). "
                        f"Pass a selector that matches the <input type=\"file\"> "
                        f"or one of its ancestors."
                    )

            if len(resolved_paths) > 1 and 'multiple' not in element.attrs:
                raise Exception(
                    f"File input does not accept multiple files "
                    f"(no 'multiple' attribute), but {len(resolved_paths)} paths were "
                    f"provided. Pass one path or upload sequentially."
                )

            await element.send_file(*resolved_paths)
            return {
                "success": True,
                "count": len(resolved_paths),
                "files": [Path(p).name for p in resolved_paths],
            }

        except Exception as e:
            raise Exception(f"Failed to upload file(s): {str(e)}") from e

    @staticmethod
    async def select_option(
        tab: Tab,
        selector: str,
        value: Optional[str] = None,
        text: Optional[str] = None,
        index: Optional[int] = None
    ) -> bool:
        """
        Select option from dropdown using nodriver's native methods.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the select element.
            value (Optional[str]): Option value to select.
            text (Optional[str]): Option text to select.
            index (Optional[int]): Option index to select.

        Returns:
            bool: True if option selected, False otherwise.
        """
        try:
            select_element = await tab.select(selector)
            if not select_element:
                raise Exception(f"Select element not found: {selector}")

            if value is not None:
                criteria = {"value": str(value)}
            elif text is not None:
                criteria = {"text": str(text)}
            elif index is not None:
                criteria = {"index": int(index)}
            else:
                raise Exception("No selection criteria provided (value, text, or index)")

            selected = await evaluate_or_raise(tab, f"""
                (() => {{
                    const select = document.querySelector({json.dumps(selector)});
                    const criteria = {json.dumps(criteria)};
                    if (!select || !select.options) return false;
                    const options = Array.from(select.options);
                    let target = -1;
                    if ('value' in criteria) target = options.findIndex(o => o.value === criteria.value);
                    else if ('text' in criteria) target = options.findIndex(o => o.text.trim() === criteria.text.trim());
                    else if (criteria.index >= 0 && criteria.index < options.length) target = criteria.index;
                    if (target < 0) return false;
                    select.selectedIndex = target;
                    select.dispatchEvent(new Event('input', {{bubbles: true}}));
                    select.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }})()
            """)
            if not selected:
                raise Exception(f"No matching option for {criteria} in {selector}")
            return True

        except Exception as e:
            raise Exception(f"Failed to select option: {str(e)}")

    @staticmethod
    async def get_element_state(
        tab: Tab,
        selector: str
    ) -> Dict[str, Any]:
        """
        Get complete state of an element.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.

        Returns:
            Dict[str, Any]: Dictionary of element state properties.
        """
        try:
            element = await tab.select(selector)
            if not element:
                raise Exception(f"Element not found: {selector}")

            if hasattr(element, 'update'):
                await element.update()

            state = {
                'tag_name': element.tag_name if hasattr(element, 'tag_name') else 'unknown',
                'text': element.text if hasattr(element, 'text') else '',
                'text_all': element.text_all if hasattr(element, 'text_all') else '',
                'attributes': element.attrs if hasattr(element, 'attrs') else {},
                'is_visible': True,
                'is_clickable': False,
                'is_enabled': True,
                'value': element.attrs.get('value') if hasattr(element, 'attrs') else None,
                'href': element.attrs.get('href') if hasattr(element, 'attrs') else None,
                'src': element.attrs.get('src') if hasattr(element, 'attrs') else None,
                'class': element.attrs.get('class') if hasattr(element, 'attrs') else None,
                'id': element.attrs.get('id') if hasattr(element, 'attrs') else None,
                'position': await element.get_position() if hasattr(element, 'get_position') else None,
                'computed_style': {},
                'children_count': len(element.children) if hasattr(element, 'children') and element.children else 0,
                'parent_tag': None
            }

            return state

        except Exception as e:
            raise Exception(f"Failed to get element state: {str(e)}")

    @staticmethod
    async def wait_for_element(
        tab: Tab,
        selector: str,
        timeout: int = 30000,
        visible: bool = True,
        text_content: Optional[str] = None
    ) -> bool:
        """
        Wait for element to appear and match conditions.

        Polls every 100 ms with a root-only DOM lookup, so it reacts quickly
        without fetching the whole DOM tree on each poll.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            timeout (int): Timeout in milliseconds.
            visible (bool): Wait for element to be visible.
            text_content (Optional[str]): Wait for element to contain text.

        Returns:
            bool: True if element matches conditions, False otherwise.
        """
        deadline = time.monotonic() + max(timeout, 0) / 1000
        while True:
            try:
                backend_node_id = await input_actions.find_node(tab, selector)
                if backend_node_id is not None and await input_actions.call_on_node(
                    tab,
                    backend_node_id,
                    ELEMENT_READY_JS,
                    visible,
                    text_content,
                ):
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(WAIT_POLL_INTERVAL)

    @staticmethod
    async def execute_script(
        tab: Tab,
        script: str,
        args: Optional[List[Any]] = None
    ) -> Any:
        """
        Execute JavaScript in page context.

        Without args, the script is evaluated as an expression and its value
        is returned. With args, the script is used as a function body, so it
        must use return and reads its arguments through the arguments object.

        Args:
            tab (Tab): The browser tab object.
            script (str): JavaScript code to execute.
            args (Optional[List[Any]]): Arguments for the script.

        Returns:
            Any: Result of script execution as plain JSON-like data.
        """
        try:
            if args:
                serialized_args = ",".join(json.dumps(a) for a in args)
                return await evaluate_or_raise(tab, f'(function() {{ {script} }})({serialized_args})')
            return await evaluate_or_raise(tab, script)

        except Exception as e:
            raise Exception(f"Failed to execute script: {str(e)}")

    @staticmethod
    async def get_page_content(
        tab: Tab,
        include_frames: bool = False
    ) -> Dict[str, str]:
        """
        Get page HTML and text content.

        Args:
            tab (Tab): The browser tab object.
            include_frames (bool): Include iframe contents.

        Returns:
            Dict[str, str]: Dictionary with page content.
        """
        try:
            html = await tab.get_content()
            text = await tab.evaluate("document.body.innerText")

            content = {
                'html': html,
                'text': text,
                'url': await tab.evaluate("window.location.href"),
                'title': await tab.evaluate("document.title")
            }

            if include_frames:
                frames = []
                iframe_elements = await tab.select_all('iframe')

                for i, iframe in enumerate(iframe_elements):
                    try:
                        src = iframe.attrs.get('src') if hasattr(iframe, 'attrs') else None
                        if src:
                            frames.append({
                                'index': i,
                                'src': src,
                                'id': iframe.attrs.get('id') if hasattr(iframe, 'attrs') else None,
                                'name': iframe.attrs.get('name') if hasattr(iframe, 'attrs') else None
                            })
                    except Exception:
                        continue

                content['frames'] = frames

            return content

        except Exception as e:
            raise Exception(f"Failed to get page content: {str(e)}")

    @staticmethod
    async def scroll_page(
        tab: Tab,
        direction: str = "down",
        amount: int = 500,
        smooth: bool = True
    ) -> bool:
        """
        Scroll the page in specified direction.

        Args:
            tab (Tab): The browser tab object.
            direction (str): Direction to scroll ('down', 'up', 'right', 'left', 'top', 'bottom').
            amount (int): Amount to scroll in pixels.
            smooth (bool): Use smooth scrolling.

        Returns:
            bool: True if scroll succeeded, False otherwise.
        """
        try:
            behavior = "'smooth'" if smooth else "'instant'"
            distance = abs(int(amount))

            if direction == "down":
                script = f"window.scrollBy({{top: {distance}, left: 0, behavior: {behavior}}})"
            elif direction == "up":
                script = f"window.scrollBy({{top: {-distance}, left: 0, behavior: {behavior}}})"
            elif direction == "right":
                script = f"window.scrollBy({{top: 0, left: {distance}, behavior: {behavior}}})"
            elif direction == "left":
                script = f"window.scrollBy({{top: 0, left: {-distance}, behavior: {behavior}}})"
            elif direction == "top":
                script = f"window.scrollTo({{top: 0, left: 0, behavior: {behavior}}})"
            elif direction == "bottom":
                script = f"window.scrollTo({{top: document.body.scrollHeight, left: 0, behavior: {behavior}}})"
            else:
                raise ValueError(f"Invalid scroll direction: {direction}")

            await evaluate_or_raise(tab, script)
            await asyncio.sleep(0.5 if smooth else 0.1)

            return True

        except Exception as e:
            raise Exception(f"Failed to scroll page: {str(e)}")
