"""Trusted mouse and keyboard input through CDP Input events."""

import asyncio
import random
import time
from typing import Any, Dict, Optional, Tuple

from nodriver import Tab, cdp

from key_definitions import resolve_key_descriptor

SHIFTED_SYMBOLS = set('~!@#$%^&*()_+{}|:"<>?')
SHIFT_KEY = {"key": "Shift", "code": "ShiftLeft", "virtual_key_code": 16}
BACKSPACE_KEY = {"key": "Backspace", "code": "Backspace", "virtual_key_code": 8}
ENTER_KEY = {"key": "Enter", "code": "Enter", "virtual_key_code": 13}
MODIFIER_SHIFT = 8
NODE_POLL_INTERVAL = 0.1
SCROLL_SETTLE_INTERVAL = 0.03
SCROLL_SETTLE_ATTEMPTS = 10
MOUSE_MOVE_STEPS = 6
PRESS_DURATION_RANGE = (0.04, 0.09)
MAX_TRACKED_TABS = 64

HIT_TEST_JS = """function (x, y) {
  const hit = document.elementFromPoint(x, y);
  return !!hit && (hit === this || this.contains(hit) || (hit.shadowRoot && hit.shadowRoot.contains(this)));
}"""
JS_CLICK = "function () { this.click(); }"
FOCUS_JS = "function () { if (typeof this.focus === 'function') this.focus(); }"
SELECT_CONTENTS_JS = """function () {
  if (typeof this.select === 'function' && 'value' in this) {
    if (!this.value) return false;
    this.select();
    return true;
  }
  if (!this.isContentEditable || !this.textContent) return false;
  const range = document.createRange();
  range.selectNodeContents(this);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}"""

_last_mouse_positions: Dict[int, Tuple[float, float]] = {}


async def find_node(tab: Tab, selector: str, timeout: float = 0.0) -> Optional[int]:
    """
    Resolve a CSS selector to a backend node id.

    Only the document root is fetched, so lookups stay fast on large pages.

    Args:
        tab (Tab): Browser tab.
        selector (str): CSS selector.
        timeout (float): Seconds to keep polling while the element is missing.

    Returns:
        Optional[int]: Backend node id, or None when nothing matched in time.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        document = await tab.send(cdp.dom.get_document(depth=0))
        node_id = await tab.send(cdp.dom.query_selector(document.node_id, selector))
        if node_id:
            node = await tab.send(cdp.dom.describe_node(node_id=node_id))
            return int(node.backend_node_id)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(NODE_POLL_INTERVAL)


async def call_on_node(tab: Tab, backend_node_id: int, function: str, *args: Any) -> Any:
    """
    Call a JavaScript function with the node as `this`.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.
        function (str): Function declaration using `this` for the node.
        *args (Any): JSON-serializable arguments.

    Returns:
        Any: Returned value, by value.
    """
    remote = await tab.send(cdp.dom.resolve_node(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    result, exception = await tab.send(cdp.runtime.call_function_on(
        function,
        object_id=remote.object_id,
        arguments=[cdp.runtime.CallArgument(value=arg) for arg in args],
        return_by_value=True,
        await_promise=True,
    ))
    if exception:
        raise RuntimeError(exception.text)
    return result.value


async def node_center(tab: Tab, backend_node_id: int) -> Optional[Tuple[float, float]]:
    """
    Center of the first content quad of a node, in viewport CSS pixels.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.

    Returns:
        Optional[Tuple[float, float]]: (x, y), or None when the node has no layout box.
    """
    try:
        quads = await tab.send(cdp.dom.get_content_quads(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
        return None
    if not quads:
        return None
    quad = quads[0]
    return sum(quad[0::2]) / 4, sum(quad[1::2]) / 4


async def scroll_into_view(tab: Tab, backend_node_id: int) -> Optional[Tuple[float, float]]:
    """
    Scroll a node into view and wait until its position stops moving.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.

    Returns:
        Optional[Tuple[float, float]]: Settled center point, or None when the node has no layout box.
    """
    try:
        await tab.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
        pass
    previous = await node_center(tab, backend_node_id)
    for _ in range(SCROLL_SETTLE_ATTEMPTS):
        if previous is None:
            return None
        await asyncio.sleep(SCROLL_SETTLE_INTERVAL)
        current = await node_center(tab, backend_node_id)
        if current == previous:
            return current
        previous = current
    return previous


async def move_mouse(tab: Tab, x: float, y: float) -> None:
    """
    Move the mouse to a point along a short path from its last position.

    Args:
        tab (Tab): Browser tab.
        x (float): Target x in viewport CSS pixels.
        y (float): Target y in viewport CSS pixels.
    """
    start_x, start_y = _last_mouse_positions.get(id(tab), (x + random.uniform(-120, 120), y + random.uniform(-80, 80)))
    for step in range(1, MOUSE_MOVE_STEPS + 1):
        progress = step / MOUSE_MOVE_STEPS
        eased = progress * progress * (3 - 2 * progress)
        jitter = 0 if step == MOUSE_MOVE_STEPS else random.uniform(-1.5, 1.5)
        await tab.send(cdp.input_.dispatch_mouse_event(
            "mouseMoved",
            x=start_x + (x - start_x) * eased + jitter,
            y=start_y + (y - start_y) * eased + jitter,
        ))
    _last_mouse_positions.pop(id(tab), None)
    _last_mouse_positions[id(tab)] = (x, y)
    while len(_last_mouse_positions) > MAX_TRACKED_TABS:
        _last_mouse_positions.pop(next(iter(_last_mouse_positions)))


async def click_node(tab: Tab, backend_node_id: int) -> bool:
    """
    Click a node with trusted mouse events.

    Scrolls the node into view, moves the mouse to its center, and presses
    and releases the left button. Falls back to a JavaScript click when the
    node has no layout box or is covered by another element.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.

    Returns:
        bool: True when trusted mouse events were used, False for the JavaScript fallback.
    """
    point = await scroll_into_view(tab, backend_node_id)
    if point is not None and await call_on_node(tab, backend_node_id, HIT_TEST_JS, point[0], point[1]):
        x, y = point
        await move_mouse(tab, x, y)
        await tab.send(cdp.input_.dispatch_mouse_event(
            "mousePressed", x=x, y=y, button=cdp.input_.MouseButton.LEFT, buttons=1, click_count=1,
        ))
        await asyncio.sleep(random.uniform(*PRESS_DURATION_RANGE))
        await tab.send(cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, button=cdp.input_.MouseButton.LEFT, buttons=0, click_count=1,
        ))
        return True
    await call_on_node(tab, backend_node_id, JS_CLICK)
    return False


async def focus_node(tab: Tab, backend_node_id: int) -> None:
    """
    Focus a node.

    Nodes Chrome considers unfocusable get a JavaScript focus() call
    instead, which leaves focus unchanged rather than failing.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.
    """
    try:
        await tab.send(cdp.dom.focus(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
        await call_on_node(tab, backend_node_id, FOCUS_JS)


async def press(tab: Tab, key: Dict[str, Any], text: Optional[str] = None, modifiers: int = 0) -> None:
    """
    Press and release one key with trusted key events.

    Args:
        tab (Tab): Browser tab.
        key (Dict[str, Any]): key, code, and virtual_key_code fields.
        text (Optional[str]): Text the key produces, or None for non-printing keys.
        modifiers (int): CDP modifier bitmask held during the press.
    """
    await tab.send(cdp.input_.dispatch_key_event(
        "keyDown" if text else "rawKeyDown",
        modifiers=modifiers,
        key=key["key"],
        code=key["code"],
        windows_virtual_key_code=key["virtual_key_code"],
        text=text,
        unmodified_text=text,
    ))
    await tab.send(cdp.input_.dispatch_key_event(
        "keyUp",
        modifiers=modifiers,
        key=key["key"],
        code=key["code"],
        windows_virtual_key_code=key["virtual_key_code"],
    ))


def character_key(char: str) -> Optional[Dict[str, Any]]:
    """
    Keyboard fields for an ASCII printable character on a US layout.

    Args:
        char (str): Single character.

    Returns:
        Optional[Dict[str, Any]]: key, code, and virtual_key_code, or None when the character has no key.
    """
    if len(char) != 1 or not char.isascii() or not char.isprintable():
        return None
    try:
        descriptor = resolve_key_descriptor(char)
    except Exception:
        return None
    return {key: descriptor[key] for key in ("key", "code", "virtual_key_code")}


async def type_character(tab: Tab, char: str, newline_as_enter: bool = False, shift_enter: bool = False) -> None:
    """
    Type one character the way a keyboard would.

    ASCII characters produce keydown, keypress, input, and keyup events, with
    Shift held for capitals and shifted symbols. Other characters are
    inserted as text, like an input method would.

    Args:
        tab (Tab): Browser tab.
        char (str): Character to type.
        newline_as_enter (bool): Press Enter for newline characters instead of inserting a line break.
        shift_enter (bool): Hold Shift when pressing Enter for newlines.
    """
    if char == "\n":
        if newline_as_enter:
            await press(tab, ENTER_KEY, "\r", MODIFIER_SHIFT if shift_enter else 0)
        else:
            await tab.send(cdp.input_.insert_text("\n"))
        return
    key = character_key(char)
    if key is None:
        await tab.send(cdp.input_.insert_text(char))
        return
    shifted = char in SHIFTED_SYMBOLS or char.isupper()
    if shifted:
        await tab.send(cdp.input_.dispatch_key_event(
            "rawKeyDown", modifiers=MODIFIER_SHIFT, key=SHIFT_KEY["key"], code=SHIFT_KEY["code"],
            windows_virtual_key_code=SHIFT_KEY["virtual_key_code"],
        ))
    await press(tab, key, char, MODIFIER_SHIFT if shifted else 0)
    if shifted:
        await tab.send(cdp.input_.dispatch_key_event(
            "keyUp", key=SHIFT_KEY["key"], code=SHIFT_KEY["code"],
            windows_virtual_key_code=SHIFT_KEY["virtual_key_code"],
        ))


async def clear_field(tab: Tab, backend_node_id: int) -> bool:
    """
    Clear an input, textarea, or contenteditable element like a user would.

    Selects the current contents and presses Backspace, so frameworks that
    listen for input events see the change.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Focused target node.

    Returns:
        bool: True when there was content to clear.
    """
    if not await call_on_node(tab, backend_node_id, SELECT_CONTENTS_JS):
        return False
    await press(tab, BACKSPACE_KEY)
    return True

