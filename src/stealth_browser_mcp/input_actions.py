"""Trusted mouse and keyboard input through CDP Input events."""

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from nodriver import Tab, cdp

from .a11y_snapshot import a11y_manager, extract_ref_id, is_ref_selector
from .cdp_raw import raw_command
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
  if (window.self !== window.top) {
    const win = this.ownerDocument ? (this.ownerDocument.defaultView || window) : window;
    const style = win.getComputedStyle(this);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = this.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0 && !this.getClientRects().length) return false;
    return true;
  }
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

FIND_OPTION_JS = """function (optionText, triggerSelector, frameRef, parts, isXPath, xpathExpr) {
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
      try { iframe = document.querySelector(frameRef); } catch (e) {}
    }
    if (!iframe) {
      try { iframe = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef); } catch (e) {}
    }
    if (iframe) {
      try {
        root = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document) || document;
      } catch (e) {
        return null;
      }
    }
  }

  function resolveEl(sel) {
    if (!sel) return null;
    if (isXPath) {
      try {
        const res = root.evaluate(xpathExpr, root, null, 9, null);
        return res ? res.singleNodeValue : null;
      } catch (e) {
        return null;
      }
    }
    if (parts && parts.length > 1) {
      let curr = root;
      for (let i = 0; i < parts.length; i++) {
        if (!curr) return null;
        let found = null;
        try { found = curr.querySelector(parts[i]); } catch (e) { return null; }
        if (!found) return null;
        if (i === parts.length - 1) return found;
        if (!found.shadowRoot) return null;
        curr = found.shadowRoot;
      }
      return null;
    }
    try {
      return root.querySelector(sel);
    } catch (e) {
      return null;
    }
  }

  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const win = el.ownerDocument ? (el.ownerDocument.defaultView || window) : window;
    const style = win.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0 && !el.getClientRects().length) return false;
    return true;
  }

  const trigger = resolveEl(triggerSelector);

  const scopes = [root];
  if (trigger && typeof trigger.getRootNode === 'function') {
    const triggerScope = trigger.getRootNode();
    if (triggerScope && triggerScope !== root && !scopes.includes(triggerScope)) {
      scopes.unshift(triggerScope);
    }
  }

  let candidates = [];
  const seen = new Set();

  function addCandidate(el) {
    if (el && !seen.has(el) && isVisible(el)) {
      seen.add(el);
      candidates.push(el);
    }
  }

  // 1. If trigger has aria-controls or aria-owns, check that listbox first
  if (trigger) {
    const controlsId = trigger.getAttribute('aria-controls') || trigger.getAttribute('aria-owns');
    if (controlsId) {
      for (let s = 0; s < scopes.length; s++) {
        const controlled = scopes[s].getElementById ? scopes[s].getElementById(controlsId) : scopes[s].querySelector('#' + controlsId);
        if (controlled && isVisible(controlled)) {
          const opts = controlled.querySelectorAll('[role="option"], [role="menuitem"], [class*="option"], li');
          for (let i = 0; i < opts.length; i++) addCandidate(opts[i]);
        }
      }
    }
  }

  // 2. Options by role
  for (let s = 0; s < scopes.length; s++) {
    try {
      const roleOpts = scopes[s].querySelectorAll('[role="option"], [role="menuitem"]');
      for (let i = 0; i < roleOpts.length; i++) addCandidate(roleOpts[i]);
    } catch (e) {}
  }

  // 3. Options by react-select and combobox class patterns
  for (let s = 0; s < scopes.length; s++) {
    try {
      const classOpts = scopes[s].querySelectorAll(
        '[class*="select__option"], [class*="select-option"], [class*="-option"], [class*="Option"], [class*="menu-item"], [class*="menuItem"]'
      );
      for (let i = 0; i < classOpts.length; i++) addCandidate(classOpts[i]);
    } catch (e) {}
  }

  // 4. Options in visible listboxes / menus
  for (let s = 0; s < scopes.length; s++) {
    try {
      const listboxes = scopes[s].querySelectorAll(
        '[role="listbox"], [role="menu"], [class*="select__menu"], [class*="select-menu"], [class*="menu-list"], [class*="MenuList"], [class*="-menu"]'
      );
      for (let i = 0; i < listboxes.length; i++) {
        const lb = listboxes[i];
        if (isVisible(lb)) {
          const items = lb.querySelectorAll('li, div, a, span');
          for (let j = 0; j < items.length; j++) {
            const item = items[j];
            if (isVisible(item)) {
              const hasSub = item.querySelector('[role="option"], [class*="option"], li');
              if (!hasSub) addCandidate(item);
            }
          }
        }
      }
    } catch (e) {}
  }

  // 5. Match by visible text
  const target = (optionText || '').trim();
  const targetLower = target.toLowerCase();

  let exactMatch = null;
  let exactCaseInsensitive = null;
  let startsWithMatch = null;
  let containsMatch = null;

  for (let i = 0; i < candidates.length; i++) {
    const el = candidates[i];
    const text = (el.innerText || el.textContent || '').trim();
    if (!text) continue;
    const textLower = text.toLowerCase();

    if (text === target) {
      exactMatch = el;
      break;
    }
    if (!exactCaseInsensitive && textLower === targetLower) {
      exactCaseInsensitive = el;
    } else if (!startsWithMatch && textLower.startsWith(targetLower)) {
      startsWithMatch = el;
    } else if (!containsMatch && textLower.includes(targetLower)) {
      containsMatch = el;
    }
  }

  return exactMatch || exactCaseInsensitive || startsWithMatch || containsMatch || null;
}"""

VERIFY_SELECTION_JS = """function (optionText, triggerSelector, frameRef, clickedOptionId, parts, isXPath, xpathExpr) {
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
      try { iframe = document.querySelector(frameRef); } catch (e) {}
    }
    if (!iframe) {
      try { iframe = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef); } catch (e) {}
    }
    if (iframe) {
      try {
        root = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document) || document;
      } catch (e) {
        return { verified: false, error: 'frame_not_found' };
      }
    }
  }

  function resolveEl(sel) {
    if (!sel) return null;
    if (isXPath) {
      try {
        const res = root.evaluate(xpathExpr, root, null, 9, null);
        return res ? res.singleNodeValue : null;
      } catch (e) {
        return null;
      }
    }
    if (parts && parts.length > 1) {
      let curr = root;
      for (let i = 0; i < parts.length; i++) {
        if (!curr) return null;
        let found = null;
        try { found = curr.querySelector(parts[i]); } catch (e) { return null; }
        if (!found) return null;
        if (i === parts.length - 1) return found;
        if (!found.shadowRoot) return null;
        curr = found.shadowRoot;
      }
      return null;
    }
    try {
      return root.querySelector(sel);
    } catch (e) {
      return null;
    }
  }

  const target = (optionText || '').trim();
  const targetLower = target.toLowerCase();

  const trigger = resolveEl(triggerSelector);

  // 1. Check trigger itself for text or value
  if (trigger) {
    const triggerText = (trigger.innerText || trigger.textContent || '').trim().toLowerCase();
    if (triggerText.includes(targetLower)) {
      return { verified: true, reason: 'trigger_text_matches', text: triggerText };
    }
    if (trigger.value && String(trigger.value).toLowerCase().includes(targetLower)) {
      return { verified: true, reason: 'trigger_value_matches', value: trigger.value };
    }
    // Check inner input / select elements
    const inputs = trigger.querySelectorAll('input, select, textarea');
    for (let i = 0; i < inputs.length; i++) {
      if (inputs[i].value && String(inputs[i].value).toLowerCase().includes(targetLower)) {
        return { verified: true, reason: 'inner_input_matches', value: inputs[i].value };
      }
    }
    // Check React-Select single-value containers
    const singleVals = trigger.querySelectorAll(
      '[class*="single-value"], [class*="singleValue"], [class*="SingleValue"], [class*="value-container"]'
    );
    for (let i = 0; i < singleVals.length; i++) {
      const svText = (singleVals[i].innerText || singleVals[i].textContent || '').trim().toLowerCase();
      if (svText.includes(targetLower)) {
        return { verified: true, reason: 'single_value_matches', text: svText };
      }
    }
    // Check parent element (in case trigger was an inner element like an input)
    if (trigger.parentElement) {
      const parentText = (trigger.parentElement.innerText || trigger.parentElement.textContent || '').trim().toLowerCase();
      if (parentText.includes(targetLower)) {
        return { verified: true, reason: 'parent_text_matches', text: parentText };
      }
      const pSingleVals = trigger.parentElement.querySelectorAll(
        '[class*="single-value"], [class*="singleValue"], [class*="SingleValue"], [class*="value-container"]'
      );
      for (let i = 0; i < pSingleVals.length; i++) {
        const psvText = (pSingleVals[i].innerText || pSingleVals[i].textContent || '').trim().toLowerCase();
        if (psvText.includes(targetLower)) {
          return { verified: true, reason: 'parent_single_value_matches', text: psvText };
        }
      }
    }
  }

  // 2. Check aria-selected on clicked option if ID was known
  if (clickedOptionId) {
    const clicked = root.getElementById ? root.getElementById(clickedOptionId) : root.querySelector('#' + clickedOptionId);
    if (clicked && clicked.getAttribute('aria-selected') === 'true') {
      return { verified: true, reason: 'aria_selected' };
    }
  }

  // 3. Check any option element that matches text and is marked selected
  const allOpts = root.querySelectorAll('[role="option"], [class*="option"]');
  for (let i = 0; i < allOpts.length; i++) {
    const opt = allOpts[i];
    const optText = (opt.innerText || opt.textContent || '').trim().toLowerCase();
    if (optText === targetLower || optText.includes(targetLower)) {
      if (opt.getAttribute('aria-selected') === 'true' ||
          opt.classList.contains('selected') ||
          opt.className.includes('is-selected') ||
          opt.className.includes('--is-selected')) {
        return { verified: true, reason: 'option_marked_selected' };
      }
    }
  }

  // 4. Check if combobox closed (aria-expanded="false" or menu hidden)
  if (trigger) {
    const expanded = trigger.getAttribute('aria-expanded');
    if (expanded === 'false') {
      return { verified: true, reason: 'combobox_closed' };
    }
    const menu = root.querySelector('[role="listbox"], [class*="select__menu"], [class*="-menu"]');
    if (!menu) {
      return { verified: true, reason: 'menu_closed' };
    } else {
      const win = menu.ownerDocument ? (menu.ownerDocument.defaultView || window) : window;
      const mStyle = win.getComputedStyle(menu);
      if (mStyle.display === 'none' || mStyle.visibility === 'hidden') {
        return { verified: true, reason: 'menu_hidden' };
      }
    }
  }

  return { verified: false };
}"""

FOCUS_INPUT_JS = """function (triggerSelector, frameRef, parts, isXPath, xpathExpr) {
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
      try { iframe = document.querySelector(frameRef); } catch (e) {}
    }
    if (!iframe) {
      try { iframe = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef); } catch (e) {}
    }
    if (iframe) {
      try {
        root = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document) || document;
      } catch (e) {
        return false;
      }
    }
  }

  function resolveEl(sel) {
    if (!sel) return null;
    if (isXPath) {
      try {
        const res = root.evaluate(xpathExpr, root, null, 9, null);
        return res ? res.singleNodeValue : null;
      } catch (e) {
        return null;
      }
    }
    if (parts && parts.length > 1) {
      let curr = root;
      for (let i = 0; i < parts.length; i++) {
        if (!curr) return null;
        let found = null;
        try { found = curr.querySelector(parts[i]); } catch (e) { return null; }
        if (!found) return null;
        if (i === parts.length - 1) return found;
        if (!found.shadowRoot) return null;
        curr = found.shadowRoot;
      }
      return null;
    }
    try {
      return root.querySelector(sel);
    } catch (e) {
      return null;
    }
  }

  const trigger = resolveEl(triggerSelector);
  if (trigger) {
    if (trigger.tagName === 'INPUT' || trigger.tagName === 'TEXTAREA') {
      trigger.focus();
      return true;
    }
    const inner = trigger.querySelector('input:not([type="hidden"]), textarea');
    if (inner) {
      inner.focus();
      return true;
    }
    if (typeof trigger.focus === 'function') {
      trigger.focus();
      return true;
    }
  }
  if (root.activeElement && (root.activeElement.tagName === 'INPUT' || root.activeElement.tagName === 'TEXTAREA')) {
    return true;
  }
  return false;
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


def build_find_option_expression(
    option_text: str,
    trigger_selector: Optional[str] = None,
    frame: Optional[str] = None,
    parts: Optional[List[str]] = None,
    is_xpath: Optional[bool] = None,
    xpath_expr: Optional[str] = None,
) -> str:
    """Build a JavaScript expression that finds a combobox option by visible text."""
    if trigger_selector:
        if is_xpath is None:
            is_xpath = is_xpath_selector(trigger_selector)
        if xpath_expr is None:
            xpath_expr = extract_xpath(trigger_selector) if is_xpath else ""
        if parts is None:
            raw_parts = split_deep_selector(trigger_selector)
            parts = raw_parts if len(raw_parts) > 1 else None
    else:
        is_xpath = False if is_xpath is None else is_xpath
        xpath_expr = "" if xpath_expr is None else xpath_expr

    args_json = (
        f"{json.dumps(option_text)}, "
        f"{json.dumps(trigger_selector)}, "
        f"{json.dumps(frame)}, "
        f"{json.dumps(parts)}, "
        f"{'true' if is_xpath else 'false'}, "
        f"{json.dumps(xpath_expr)}"
    )
    return f"({FIND_OPTION_JS})({args_json})"


def build_verify_selection_expression(
    option_text: str,
    trigger_selector: Optional[str] = None,
    frame: Optional[str] = None,
    clicked_option_id: Optional[str] = None,
    parts: Optional[List[str]] = None,
    is_xpath: Optional[bool] = None,
    xpath_expr: Optional[str] = None,
) -> str:
    """Build a JavaScript expression that verifies combobox selection."""
    if trigger_selector:
        if is_xpath is None:
            is_xpath = is_xpath_selector(trigger_selector)
        if xpath_expr is None:
            xpath_expr = extract_xpath(trigger_selector) if is_xpath else ""
        if parts is None:
            raw_parts = split_deep_selector(trigger_selector)
            parts = raw_parts if len(raw_parts) > 1 else None
    else:
        is_xpath = False if is_xpath is None else is_xpath
        xpath_expr = "" if xpath_expr is None else xpath_expr

    args_json = (
        f"{json.dumps(option_text)}, "
        f"{json.dumps(trigger_selector)}, "
        f"{json.dumps(frame)}, "
        f"{json.dumps(clicked_option_id)}, "
        f"{json.dumps(parts)}, "
        f"{'true' if is_xpath else 'false'}, "
        f"{json.dumps(xpath_expr)}"
    )
    return f"({VERIFY_SELECTION_JS})({args_json})"


def build_focus_input_expression(
    trigger_selector: Optional[str] = None,
    frame: Optional[str] = None,
    parts: Optional[List[str]] = None,
    is_xpath: Optional[bool] = None,
    xpath_expr: Optional[str] = None,
) -> str:
    """Build a JavaScript expression that focuses the combobox input element."""
    if trigger_selector:
        if is_xpath is None:
            is_xpath = is_xpath_selector(trigger_selector)
        if xpath_expr is None:
            xpath_expr = extract_xpath(trigger_selector) if is_xpath else ""
        if parts is None:
            raw_parts = split_deep_selector(trigger_selector)
            parts = raw_parts if len(raw_parts) > 1 else None
    else:
        is_xpath = False if is_xpath is None else is_xpath
        xpath_expr = "" if xpath_expr is None else xpath_expr

    args_json = (
        f"{json.dumps(trigger_selector)}, "
        f"{json.dumps(frame)}, "
        f"{json.dumps(parts)}, "
        f"{'true' if is_xpath else 'false'}, "
        f"{json.dumps(xpath_expr)}"
    )
    return f"({FOCUS_INPUT_JS})({args_json})"


@dataclass
class FrameContext:
    """Targeting context for an iframe (same-origin or cross-origin)."""

    frame_id: Optional[str]
    context_id: Optional[int]
    target_tab: Tab
    is_cross_origin: bool


def _tab_cache_id(tab: Any) -> Any:
    """Get a stable cache key for a tab, using CDP target_id when available."""
    target = getattr(tab, "target", None)
    target_id = getattr(target, "target_id", None) if target else None
    if target_id is not None:
        return str(target_id)
    return id(tab)


_node_contexts: Dict[int, Optional[int]] = {}
_node_tabs: Dict[int, Tab] = {}
_frame_contexts: Dict[Tuple[Any, str], FrameContext] = {}
MAX_TRACKED_NODES = 128


def _track_node(backend_node_id: int, context_id: Optional[int], target_tab: Optional[Tab] = None) -> None:
    """Record execution context and target tab for a backend node id."""
    if context_id is not None:
        _node_contexts[backend_node_id] = context_id
    if target_tab is not None and not getattr(target_tab, "closed", False):
        _node_tabs[backend_node_id] = target_tab
    while len(_node_contexts) > MAX_TRACKED_NODES:
        _node_contexts.pop(next(iter(_node_contexts)), None)
    while len(_node_tabs) > MAX_TRACKED_NODES:
        _node_tabs.pop(next(iter(_node_tabs)), None)


def _find_frame_id_in_tree(tree: Any, frame_ref: str) -> Optional[str]:
    """Recursively search a CDP FrameTree for a frame matching the frame reference."""
    if not tree:
        return None
    child_frames = getattr(tree, "child_frames", None) or []
    for child in child_frames:
        f = getattr(child, "frame", None)
        if f:
            fid = getattr(f, "id_", None) or getattr(f, "id", None)
            fname = getattr(f, "name", None) or ""
            furl = getattr(f, "url", None) or ""
            if fname == frame_ref or (fid and str(fid) == frame_ref) or (frame_ref in furl):
                return str(fid)
        sub = _find_frame_id_in_tree(child, frame_ref)
        if sub:
            return sub
    return None


RESOLVE_IFRAME_NODE_JS = """function (frameRef) {
  if (!frameRef) return null;
  if (frameRef.startsWith('//') || frameRef.toLowerCase().startsWith('xpath=')) {
    const fXpath = frameRef.toLowerCase().startsWith('xpath=') ? frameRef.substring(6).trim() : frameRef;
    try {
      const res = document.evaluate(fXpath, document, null, 9, null);
      if (res && res.singleNodeValue) return res.singleNodeValue;
    } catch (e) {}
  }
  try {
    const el = document.querySelector(frameRef);
    if (el) return el;
  } catch (e) {}
  try {
    const el = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef + ', #' + frameRef);
    if (el) return el;
  } catch (e) {}
  try {
    const frames = document.querySelectorAll('iframe, frame');
    for (let i = 0; i < frames.length; i++) {
      const f = frames[i];
      if (f.name === frameRef || f.id === frameRef || (f.src && f.src.includes(frameRef))) {
        return f;
      }
    }
  } catch (e) {}
  return null;
}"""


async def resolve_frame_context(tab: Tab, frame: str) -> Optional[FrameContext]:
    """
    Resolve frame ID and execution context for an iframe.

    Supports both same-origin and cross-origin iframes. For cross-origin
    iframes, obtains the frame ID from DOM.describeNode or Page.getFrameTree,
    creates an isolated world via Page.createIsolatedWorld, or connects to the
    OOPIF target session.

    Args:
        tab (Tab): Browser tab.
        frame (str): Iframe CSS selector, name, id, or XPath.

    Returns:
        Optional[FrameContext]: Resolved frame context, or None when not found.
    """
    cache_key = (_tab_cache_id(tab), frame)
    if cache_key in _frame_contexts:
        cached = _frame_contexts[cache_key]
        if cached.target_tab and not getattr(cached.target_tab, "closed", False) and not getattr(tab, "closed", False):
            return cached

    frame_id: Optional[str] = None
    try:
        iframe_eval = await tab.send(cdp.runtime.evaluate(
            expression=f"({RESOLVE_IFRAME_NODE_JS})({json.dumps(frame)})",
            return_by_value=False,
            await_promise=True,
        ))
        remote_obj = iframe_eval[0] if isinstance(iframe_eval, tuple) else iframe_eval
        if remote_obj and getattr(remote_obj, "object_id", None):
            obj_id = cdp.runtime.RemoteObjectId(remote_obj.object_id)
            iframe_node = await tab.send(cdp.dom.describe_node(object_id=obj_id))
            try:
                await tab.send(cdp.runtime.release_object(object_id=obj_id))
            except Exception:
                pass
            if iframe_node and getattr(iframe_node, "frame_id", None):
                frame_id = str(iframe_node.frame_id)
    except Exception:
        pass

    if not frame_id:
        try:
            tree = await tab.send(cdp.page.get_frame_tree())
            frame_id = _find_frame_id_in_tree(tree, frame)
        except Exception:
            pass

    target_tab = tab
    if hasattr(tab, "browser") and tab.browser and hasattr(tab.browser, "targets"):
        for t in tab.browser.targets:
            if getattr(t, "type_", None) == "iframe":
                target_info = getattr(t, "target", None)
                opener = getattr(target_info, "opener_frame_id", None) if target_info else None
                t_url = getattr(t, "url", None) or (target_info.url if target_info else "")
                t_id = getattr(t, "target_id", None) or (target_info.target_id if target_info else None)
                if (frame_id and opener and str(opener) == frame_id) or \
                   (frame_id and t_id and str(t_id) == frame_id) or \
                   (frame and t_url and frame in t_url):
                    target_tab = t
                    if not frame_id:
                        frame_id = str(opener or t_id or "")
                    break

    context_id: Optional[int] = None
    if frame_id:
        try:
            try:
                await tab.send(cdp.page.enable())
            except Exception:
                pass
            raw_ctx = await tab.send(cdp.page.create_isolated_world(
                frame_id=cdp.page.FrameId(frame_id),
                world_name="stealth_cross_origin",
                grant_univeral_access=True,
            ))
            if raw_ctx is not None:
                context_id = int(raw_ctx)
        except Exception:
            context_id = None

    if context_id is None and target_tab is tab:
        try:
            targets = await tab.send(cdp.target.get_targets())
            for ti in targets:
                if getattr(ti, "type_", None) == "iframe":
                    opener = getattr(ti, "opener_frame_id", None)
                    ti_url = getattr(ti, "url", "")
                    if (frame_id and opener and str(opener) == frame_id) or (frame and ti_url and frame in ti_url):
                        if not frame_id and opener:
                            frame_id = str(opener)
                        try:
                            await tab.send(cdp.target.attach_to_target(target_id=ti.target_id, flatten=True))
                        except Exception:
                            pass
                        if hasattr(tab, "browser") and tab.browser and hasattr(tab.browser, "targets"):
                            for bt in tab.browser.targets:
                                if getattr(bt, "target_id", None) == ti.target_id:
                                    target_tab = bt
                                    break
                        break
        except Exception:
            pass

    if frame_id or context_id is not None or target_tab is not tab:
        ctx = FrameContext(
            frame_id=frame_id,
            context_id=context_id,
            target_tab=target_tab,
            is_cross_origin=True,
        )
        _frame_contexts[cache_key] = ctx
        while len(_frame_contexts) > MAX_TRACKED_NODES:
            _frame_contexts.pop(next(iter(_frame_contexts)), None)
        return ctx

    return None


async def resolve_cross_origin_node(
    tab: Tab,
    selector: str,
    frame: str,
    parts: Optional[List[str]] = None,
    is_deep: bool = False,
    is_xpath: bool = False,
    xpath_expr: str = "",
) -> Optional[int]:
    """
    Resolve a selector inside a cross-origin iframe using its frame context.

    Evaluates the selector within the iframe's isolated world or separate CDP
    target session and returns the resolved backend node ID.

    Args:
        tab (Tab): Browser tab.
        selector (str): CSS selector, deep selector, or XPath.
        frame (str): Iframe reference.
        parts (Optional[List[str]]): Deep selector segments if applicable.
        is_deep (bool): Whether selector uses deep combinators.
        is_xpath (bool): Whether selector is XPath.
        xpath_expr (str): XPath expression string.

    Returns:
        Optional[int]: Backend node id, or None when element was not found.
    """
    frame_ctx = await resolve_frame_context(tab, frame)
    if not frame_ctx:
        return None

    expression = build_resolve_expression(
        selector=selector,
        frame=None,
        parts=parts if is_deep else None,
        is_xpath=is_xpath,
        xpath_expr=xpath_expr,
    )

    eval_kwargs: Dict[str, Any] = {
        "expression": expression,
        "return_by_value": False,
        "await_promise": True,
    }
    if frame_ctx.context_id is not None and frame_ctx.target_tab is tab:
        eval_kwargs["context_id"] = cdp.runtime.ExecutionContextId(int(frame_ctx.context_id))

    try:
        eval_result = await frame_ctx.target_tab.send(cdp.runtime.evaluate(**eval_kwargs))
        remote_obj = eval_result[0] if isinstance(eval_result, tuple) else eval_result
        if remote_obj and getattr(remote_obj, "object_id", None):
            obj_id = cdp.runtime.RemoteObjectId(remote_obj.object_id)
            node = await frame_ctx.target_tab.send(cdp.dom.describe_node(object_id=obj_id))
            try:
                await frame_ctx.target_tab.send(cdp.runtime.release_object(object_id=obj_id))
            except Exception:
                pass
            if node and hasattr(node, "backend_node_id"):
                bid = int(node.backend_node_id)
                _track_node(bid, frame_ctx.context_id, frame_ctx.target_tab if frame_ctx.target_tab is not tab else None)
                return bid
    except Exception:
        pass

    return None


_last_mouse_positions: Dict[int, Tuple[float, float]] = {}


async def find_node(
    tab: Tab,
    selector: str,
    timeout: float = 0.0,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> Optional[int]:
    """
    Resolve a selector to a backend node id.

    Supports:
      - Standard CSS selectors (e.g. #id, .class)
      - Deep selectors that pierce open shadow roots using >>> or >>
        combinators (e.g. host-tag >>> .inner-button or host >> child)
      - XPath expressions using xpath= prefix or starting with //
        (e.g. xpath=//input[@name='q'] or //button[text()='Submit'])
      - Iframe targeting via the frame argument (accepts iframe CSS
        selector, name, id, or XPath; supports both same-origin and cross-origin)
      - Ref targeting using ref=<id> (e.g. ref=e12 or ref="e1") resolved
        from the latest accessibility snapshot

    Only the document root is fetched for standard selectors with no frame,
    so lookups stay fast on large pages.

    Args:
        tab (Tab): Browser tab.
        selector (str): CSS selector, deep selector, XPath, or ref=<id>.
        timeout (float): Seconds to keep polling while the element is missing.
        frame (Optional[str]): Iframe selector, name, id, or XPath.
        instance_id (Optional[str]): Browser instance ID for ref resolution.

    Returns:
        Optional[int]: Backend node id, or None when nothing matched in time.
    """
    known_refs = a11y_manager.get_known_refs(instance_id=instance_id, tab=tab)
    if is_ref_selector(selector, known_refs=known_refs):
        ref_id = extract_ref_id(selector)
        backend_node_id = a11y_manager.resolve_ref(
            ref_id=ref_id,
            instance_id=instance_id,
            tab=tab,
        )
        try:
            node = await asyncio.wait_for(
                tab.send(raw_command("DOM.describeNode", {"backendNodeId": int(backend_node_id)})),
                timeout=3.0,
            )
            b_id = getattr(node, "backend_node_id", None)
            if b_id is not None:
                return int(b_id)
            if isinstance(node, dict) and "node" in node and "backendNodeId" in node["node"]:
                return int(node["node"]["backendNodeId"])
            if isinstance(node, dict) and "backendNodeId" in node:
                return int(node["backendNodeId"])
            return int(backend_node_id)
        except asyncio.TimeoutError:
            return int(backend_node_id)
        except Exception as e:
            raise Exception(f"Stale ref '{selector}': element no longer exists in DOM") from e

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
            xpath_expr = extract_xpath(selector) if is_xpath else ""
            found_bid = None
            try:
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
                        found_bid = int(node.backend_node_id)
            except Exception:
                pass

            if found_bid is not None:
                return found_bid

            if frame is not None:
                try:
                    co_bid = await resolve_cross_origin_node(
                        tab=tab,
                        selector=selector,
                        frame=frame,
                        parts=parts if is_deep else None,
                        is_deep=is_deep,
                        is_xpath=is_xpath,
                        xpath_expr=xpath_expr,
                    )
                    if co_bid is not None:
                        return co_bid
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
    target_tab = _node_tabs.get(backend_node_id, tab)
    if getattr(target_tab, "closed", False):
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and hasattr(target_tab, "browser") and target_tab.browser and target_tab.browser is not tab.browser:
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and not getattr(target_tab, "browser", None):
        target_tab = tab

    resolve_kwargs: Dict[str, Any] = {
        "backend_node_id": cdp.dom.BackendNodeId(backend_node_id)
    }
    ctx_id = _node_contexts.get(backend_node_id)
    if getattr(target_tab, "closed", False) or (target_tab is not tab and target_tab is not _node_tabs.get(backend_node_id)):
        ctx_id = None
    if ctx_id is not None and target_tab is tab:
        resolve_kwargs["execution_context_id"] = cdp.runtime.ExecutionContextId(int(ctx_id))

    try:
        remote = await target_tab.send(cdp.dom.resolve_node(**resolve_kwargs))
    except Exception:
        if "execution_context_id" in resolve_kwargs:
            resolve_kwargs.pop("execution_context_id")
            remote = await target_tab.send(cdp.dom.resolve_node(**resolve_kwargs))
        else:
            raise

    res = await target_tab.send(cdp.runtime.call_function_on(
        function,
        object_id=remote.object_id,
        arguments=[cdp.runtime.CallArgument(value=arg) for arg in args],
        return_by_value=True,
        await_promise=True,
    ))
    try:
        await target_tab.send(cdp.runtime.release_object(object_id=remote.object_id))
    except Exception:
        pass
    if isinstance(res, tuple):
        result, exception = res
    else:
        result, exception = res, None
    if exception:
        raise RuntimeError(exception.text)
    if isinstance(result, dict):
        return result.get("value")
    return getattr(result, "value", result)


async def node_center(tab: Tab, backend_node_id: int) -> Optional[Tuple[float, float]]:
    """
    Center of the first content quad of a node, in viewport CSS pixels.

    Args:
        tab (Tab): Browser tab.
        backend_node_id (int): Target node.

    Returns:
        Optional[Tuple[float, float]]: (x, y), or None when the node has no layout box.
    """
    target_tab = _node_tabs.get(backend_node_id, tab)
    if getattr(target_tab, "closed", False):
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and hasattr(target_tab, "browser") and target_tab.browser and target_tab.browser is not tab.browser:
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and not getattr(target_tab, "browser", None):
        target_tab = tab

    quads = None
    try:
        quads = await target_tab.send(cdp.dom.get_content_quads(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
        pass
    if not quads and target_tab is not tab:
        try:
            quads = await tab.send(cdp.dom.get_content_quads(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
        except Exception:
            pass
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
    target_tab = _node_tabs.get(backend_node_id, tab)
    if getattr(target_tab, "closed", False):
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and hasattr(target_tab, "browser") and target_tab.browser and target_tab.browser is not tab.browser:
        target_tab = tab
    if hasattr(tab, "browser") and tab.browser and not getattr(target_tab, "browser", None):
        target_tab = tab

    try:
        await target_tab.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
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
    tab_key = _tab_cache_id(tab)
    start_x, start_y = _last_mouse_positions.get(tab_key, (x + random.uniform(-120, 120), y + random.uniform(-80, 80)))
    for step in range(1, MOUSE_MOVE_STEPS + 1):
        progress = step / MOUSE_MOVE_STEPS
        eased = progress * progress * (3 - 2 * progress)
        jitter = 0 if step == MOUSE_MOVE_STEPS else random.uniform(-1.5, 1.5)
        await tab.send(cdp.input_.dispatch_mouse_event(
            "mouseMoved",
            x=start_x + (x - start_x) * eased + jitter,
            y=start_y + (y - start_y) * eased + jitter,
        ))
    _last_mouse_positions.pop(tab_key, None)
    _last_mouse_positions[tab_key] = (x, y)
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
    target_tab = _node_tabs.get(backend_node_id, tab)
    try:
        await target_tab.send(cdp.dom.focus(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
    except Exception:
        if target_tab is not tab:
            try:
                await tab.send(cdp.dom.focus(backend_node_id=cdp.dom.BackendNodeId(backend_node_id)))
                return
            except Exception:
                pass
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

