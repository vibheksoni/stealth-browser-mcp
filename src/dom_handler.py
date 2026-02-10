"""DOM manipulation and element interaction utilities."""

import asyncio
import json
import time
from typing import List, Optional, Dict, Any

from nodriver import Tab, Element
from models import ElementInfo, ElementAction
from debug_logger import debug_logger



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
    async def click_element(
        tab: Tab,
        selector: str,
        text_match: Optional[str] = None,
        timeout: int = 10000
    ) -> bool:
        """
        Click an element with smart retry logic.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            text_match (Optional[str]): Match element by text content.
            timeout (int): Timeout in milliseconds.

        Returns:
            bool: True if click succeeded, False otherwise.
        """
        try:
            element = None

            if text_match:
                element = await tab.find(text_match, best_match=True)
            else:
                element = await tab.select(selector, timeout=timeout/1000)

            if not element:
                raise Exception(f"Element not found: {selector}")

            await element.scroll_into_view()
            await asyncio.sleep(0.5)

            try:
                await element.click()
            except Exception:
                await element.mouse_click()

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
        Type text with human-like delays and optional newline parsing.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to type.
            clear_first (bool): Clear input before typing.
            delay_ms (int): Delay between keystrokes in milliseconds.
            parse_newlines (bool): If True, parse \n as Enter key presses.
            shift_enter (bool): If True, use Shift+Enter instead of Enter (for chat apps).

        Returns:
            bool: True if typing succeeded, False otherwise.
        """
        try:
            element = await tab.select(selector)
            if not element:
                raise Exception(f"Element not found: {selector}")

            await element.focus()
            await asyncio.sleep(0.1)

            if clear_first:
                try:
                    await element.apply("(elem) => { elem.value = ''; }")
                except:
                    await element.send_keys('\ue009' + 'a')
                    await element.send_keys('\ue017')
                await asyncio.sleep(0.1)

            if parse_newlines:
                from nodriver import cdp
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    for char in line:
                        await element.send_keys(char)
                        await asyncio.sleep(delay_ms / 1000)
                    
                    if i < len(lines) - 1:
                        if shift_enter:
                            await element.apply('''(elem) => {
                                const start = elem.selectionStart;
                                const end = elem.selectionEnd;
                                const value = elem.value;
                                elem.value = value.substring(0, start) + '\\n' + value.substring(end);
                                elem.selectionStart = elem.selectionEnd = start + 1;
                                
                                elem.dispatchEvent(new KeyboardEvent('keydown', {
                                    key: 'Enter',
                                    code: 'Enter',
                                    shiftKey: true,
                                    bubbles: true
                                }));
                                elem.dispatchEvent(new Event('input', { bubbles: true }));
                            }''')
                        else:
                            await element.apply('''(elem) => {
                                const start = elem.selectionStart;
                                const end = elem.selectionEnd;
                                const value = elem.value;
                                elem.value = value.substring(0, start) + '\\n' + value.substring(end);
                                elem.selectionStart = elem.selectionEnd = start + 1;
                                
                                elem.dispatchEvent(new KeyboardEvent('keydown', {
                                    key: 'Enter',
                                    code: 'Enter',
                                    bubbles: true
                                }));
                                elem.dispatchEvent(new Event('input', { bubbles: true }));
                            }''')
                        await asyncio.sleep(delay_ms / 1000)
            else:
                for char in text:
                    await element.send_keys(char)
                    await asyncio.sleep(delay_ms / 1000)

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
        Paste text instantly using nodriver's insert_text method.
        This is much faster than typing character by character.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to paste.
            clear_first (bool): Clear input before pasting.

        Returns:
            bool: True if pasting succeeded, False otherwise.
        """
        from nodriver import cdp
        
        try:
            element = await tab.select(selector)
            if not element:
                raise Exception(f"Element not found: {selector}")

            await element.focus()
            await asyncio.sleep(0.1)

            if clear_first:
                try:
                    await element.apply("(elem) => { elem.value = ''; }")
                except:
                    await tab.send(cdp.input_.dispatch_key_event(
                        "rawKeyDown", 
                        modifiers=2,  # Ctrl
                        key="a",
                        code="KeyA",
                        windows_virtual_key_code=65
                    ))
                    await tab.send(cdp.input_.dispatch_key_event(
                        "keyUp", 
                        modifiers=2,  # Ctrl
                        key="a",
                        code="KeyA",
                        windows_virtual_key_code=65
                    ))
                    await tab.send(cdp.input_.dispatch_key_event(
                        "rawKeyDown",
                        key="Delete",
                        code="Delete",
                        windows_virtual_key_code=46
                    ))
                    await tab.send(cdp.input_.dispatch_key_event(
                        "keyUp",
                        key="Delete", 
                        code="Delete",
                        windows_virtual_key_code=46
                    ))
                await asyncio.sleep(0.1)

            await tab.send(cdp.input_.insert_text(text))

            return True

        except Exception as e:
            raise Exception(f"Failed to paste text: {str(e)}")

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

            if text is not None:
                await select_element.send_keys(text)
                return True

            if value is not None:
                safe_selector = json.dumps(selector)
                safe_value = json.dumps(value)
                await tab.evaluate(f"""
                    const select = document.querySelector({safe_selector});
                    if (select) {{
                        select.value = {safe_value};
                        select.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                """)
                return True

            elif index is not None:
                safe_selector = json.dumps(selector)
                safe_index = int(index)
                await tab.evaluate(f"""
                    const select = document.querySelector({safe_selector});
                    if (select && {safe_index} >= 0 && {safe_index} < select.options.length) {{
                        select.selectedIndex = {safe_index};
                        select.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                """)
                return True

            raise Exception("No selection criteria provided (value, text, or index)")

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

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            timeout (int): Timeout in milliseconds.
            visible (bool): Wait for element to be visible.
            text_content (Optional[str]): Wait for element to contain text.

        Returns:
            bool: True if element matches conditions, False otherwise.
        """
        start_time = time.time()
        timeout_seconds = timeout / 1000

        while time.time() - start_time < timeout_seconds:
            try:
                element = await tab.select(selector)

                if element:
                    if visible:
                        try:
                            is_visible = await element.apply(
                                """(elem) => {
                                    var style = window.getComputedStyle(elem);
                                    return style.display !== 'none' && 
                                           style.visibility !== 'hidden' && 
                                           style.opacity !== '0';
                                }"""
                            )
                            if not is_visible:
                                await asyncio.sleep(0.5)
                                continue
                        except:
                            pass

                    if text_content:
                        text = element.text_all
                        if text_content not in text:
                            await asyncio.sleep(0.5)
                            continue

                    return True

            except Exception:
                pass

            await asyncio.sleep(0.5)

        return False

    @staticmethod
    async def execute_script(
        tab: Tab,
        script: str,
        args: Optional[List[Any]] = None
    ) -> Any:
        """
        Execute JavaScript in page context.

        Args:
            tab (Tab): The browser tab object.
            script (str): JavaScript code to execute.
            args (Optional[List[Any]]): Arguments for the script.

        Returns:
            Any: Result of script execution.
        """
        try:
            if args:
                serialized_args = ",".join(json.dumps(a) for a in args)
                result = await tab.evaluate(f'(function() {{ {script} }})({serialized_args})')
            else:
                result = await tab.evaluate(script)

            return result

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

            if direction == "down":
                script = f"window.scrollBy({{top: {amount}, left: 0, behavior: {behavior}}})"
            elif direction == "up":
                script = f"window.scrollBy({{top: -{amount}, left: 0, behavior: {behavior}}})"
            elif direction == "right":
                script = f"window.scrollBy({{top: 0, left: {amount}, behavior: {behavior}}})"
            elif direction == "left":
                script = f"window.scrollBy({{top: 0, left: -{amount}, behavior: {behavior}}})"
            elif direction == "top":
                script = f"window.scrollTo({{top: 0, left: 0, behavior: {behavior}}})"
            elif direction == "bottom":
                script = f"window.scrollTo({{top: document.body.scrollHeight, left: 0, behavior: {behavior}}})"
            else:
                raise ValueError(f"Invalid scroll direction: {direction}")

            await tab.evaluate(script)
            await asyncio.sleep(0.5 if smooth else 0.1)

            return True

        except Exception as e:
            raise Exception(f"Failed to scroll page: {str(e)}")