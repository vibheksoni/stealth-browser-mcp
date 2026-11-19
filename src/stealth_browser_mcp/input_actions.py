"""Trusted mouse and keyboard input through CDP Input events."""

import asyncio
import json
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from nodriver import Tab, cdp

from .key_definitions import resolve_key_descriptor

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
  let hit = document.elementFromPoint(x, y);
  if (!hit) return false;
  if (hit === this || this.contains(hit)) return true;
  if (hit.contentDocument && hit.contentDocument.contains(this)) return true;
  while (hit && hit.shadowRoot) {
    if (hit.shadowRoot.contains(this)) return true;
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
    if (hit === this || this.contains(hit)) return true;
  }
  return false;
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

RESOLVE_ELEMENT_JS = """function (selector, frameRef, parts, isXPath, xpathExpr) {
  let root = document;
  if (frameRef) {
    let iframe = null;
    if (frameRef.startsWith('//') || frameRef.toLowerCase().startsWith('xpath=')) {
      const fXpath = frameRef.toLowerCase().startsWith('xpath=') ? frameRef.substring(6).trim() : frameRef;
      try {
        const res = document.evaluate(fXpath, document, null, 9, null);
        iframe = res ? res.singleNodeValue : null;
      } catch (e) {}
    }
    if (!iframe) {
      try {
        iframe = document.querySelector(frameRef);
      } catch (e) {}
    }
    if (!iframe) {
      try {
        iframe = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef);
      } catch (e) {}
    }
    if (!iframe) {
      try {
        let frameWin = window.frames[frameRef];
        if (!frameWin && /^\\d+$/.test(frameRef)) {
          frameWin = window.frames[parseInt(frameRef, 10)];
        }
        if (frameWin && frameWin.document) {
          root = frameWin.document;
        }
      } catch (e) {}
      if (root === document) {
        return null;
      }
    } else {
      try {
        root = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
      } catch (e) {
        return null;
      }
      if (!root) return null;
    }
  }

  if (isXPath) {
    const doc = root.ownerDocument || root;
    try {
      const result = doc.evaluate(xpathExpr, root, null, 9 /* FIRST_ORDERED_NODE_TYPE */, null);
      const node = result ? result.singleNodeValue : null;
      if (!node) return null;
      if (node.nodeType === 1) return node;
      if (node.nodeType === 2) return node.ownerElement;
      if (node.nodeType === 3) return node.parentElement;
      return null;
    } catch (e) {
      return null;
    }
  }

  if (parts && parts.length > 1) {
    let curr = root;
    for (let i = 0; i < parts.length; i++) {
      if (!curr) return null;
      let found = null;
      try {
        found = curr.querySelector(parts[i]);
      } catch (e) {
        return null;
      }
      if (!found) return null;
      if (i === parts.length - 1) {
        return found;
      }
      if (!found.shadowRoot) return null;
      curr = found.shadowRoot;
    }
    return null;
  }

  try {
    return root.querySelector(selector);
  } catch (e) {
    return null;
  }
}"""


def is_xpath_selector(selector: str) -> bool:
    """Return True if selector begins with // or xpath=."""
    s = selector.strip()
    return s.startswith("//") or s.lower().startswith("xpath=")


def extract_xpath(selector: str) -> str:
    """Extract XPath expression by stripping optional xpath= prefix."""
    s = selector.strip()
    if s.lower().startswith("xpath="):
        return s[6:].strip()
    return s


def split_deep_selector(selector: str) -> List[str]:
    """
    Split a deep selector on >>> or >> combinators outside quotes.

    Returns a list of selector segments. If the selector contains no deep
    combinator, a single-element list [selector.strip()] is returned.
    """
    s = selector.strip()
    parts = []
    current = []
    in_quote = None
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if in_quote:
            current.append(c)
            if c == in_quote and (i == 0 or s[i - 1] != "\\"):
                in_quote = None
            i += 1
        elif c in ('"', "'"):
            in_quote = c
            current.append(c)
            i += 1
        elif s[i:i + 3] == ">>>":
            parts.append("".join(current).strip())
            current = []
            i += 3
        elif s[i:i + 2] == ">>" and (
            (current and current[-1].isspace()) or (i + 2 < n and s[i + 2].isspace())
        ):
            parts.append("".join(current).strip())
            current = []
            i += 2
        else:
            current.append(c)
            i += 1
    if current:
        parts.append("".join(current).strip())
    cleaned = [p for p in parts if p]
    return cleaned if cleaned else [s]


def is_deep_selector(selector: str) -> bool:
    """Return True if the selector uses deep combinators (>>> or >>) to pierce shadow roots."""
    return len(split_deep_selector(selector)) > 1


def build_resolve_expression(
    selector: str,
    frame: Optional[str] = None,
    parts: Optional[List[str]] = None,
    is_xpath: bool = False,
    xpath_expr: str = "",
) -> str:
    """Build a JavaScript expression that resolves an element across shadow roots, frames, or XPath."""
    args_json = (
        f"{json.dumps(selector)}, "
        f"{json.dumps(frame)}, "
        f"{json.dumps(parts)}, "
        f"{'true' if is_xpath else 'false'}, "
        f"{json.dumps(xpath_expr)}"
    )
    return f"({RESOLVE_ELEMENT_JS})({args_json})"


_last_mouse_positions: Dict[int, Tuple[float, float]] = {}


async def find_node(
    tab: Tab,
    selector: str,
    timeout: float = 0.0,
    frame: Optional[str] = None,
) -> Optional[int]:
    """
    Resolve a selector to a backend node id.

    Supports:
      - Standard CSS selectors (e.g. #id, .class)
      - Deep selectors that pierce open shadow roots using >>> or >>
        combinators (e.g. host-tag >>> .inner-button or host >> child)
      - XPath expressions using xpath= prefix or starting with //
        (e.g. xpath=//input[@name='q'] or //button[text()='Submit'])
      - Same-origin iframe targeting via the frame argument (accepts iframe
        CSS selector, name, or id attribute)

    Only the document root is fetched for standard selectors with no frame,
    so lookups stay fast on large pages.

    Args:
        tab (Tab): Browser tab.
        selector (str): CSS selector, deep selector, or XPath.
        timeout (float): Seconds to keep polling while the element is missing.
        frame (Optional[str]): Same-origin iframe selector, name, or id.

    Returns:
        Optional[int]: Backend node id, or None when nothing matched in time.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    parts = split_deep_selector(selector)
    is_deep = len(parts) > 1
    is_xpath = is_xpath_selector(selector)
    use_cdp_query = (frame is None and not is_deep and not is_xpath)

    while True:
        if use_cdp_query:
            try:
                document = await tab.send(cdp.dom.get_document(depth=0))
                if document and hasattr(document, "node_id"):
                    node_id = await tab.send(cdp.dom.query_selector(document.node_id, selector))
                    if node_id:
                        node = await tab.send(cdp.dom.describe_node(node_id=node_id))
                        if node and hasattr(node, "backend_node_id"):
                            return int(node.backend_node_id)
            except Exception:
                pass
        else:
            try:
                xpath_expr = extract_xpath(selector) if is_xpath else ""
                expression = build_resolve_expression(
                    selector=selector,
                    frame=frame,
                    parts=parts if is_deep else None,
                    is_xpath=is_xpath,
                    xpath_expr=xpath_expr,
                )
                eval_result = await tab.send(cdp.runtime.evaluate(
                    expression=expression,
                    return_by_value=False,
                    await_promise=True,
                ))
                remote_obj = eval_result[0] if isinstance(eval_result, tuple) else eval_result
                if remote_obj and getattr(remote_obj, "object_id", None):
                    obj_id = cdp.runtime.RemoteObjectId(remote_obj.object_id)
                    node = await tab.send(cdp.dom.describe_node(object_id=obj_id))
                    try:
                        await tab.send(cdp.runtime.release_object(object_id=obj_id))
                    except Exception:
                        pass
                    if node and hasattr(node, "backend_node_id"):
                        return int(node.backend_node_id)
            except Exception:
                pass

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

