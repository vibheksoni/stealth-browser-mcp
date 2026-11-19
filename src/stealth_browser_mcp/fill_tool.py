"""Unified fill tool for stealth-browser-mcp.

Classifies target DOM elements by kind, picks an appropriate input strategy
(native value setter, synthetic paste, custom combobox choose_option, or formatted
inputs), applies the value, and verifies read-back via text normalization.

Sensitive values (passwords, value_env secrets) are never echoed in results or
error messages.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple, Union

from . import element_kind, input_actions
from .dom_handler import DEFAULT_ELEMENT_TIMEOUT, DOMHandler
from .element_kind import ElementKind, classify
from .fill_native_setter import fill_native_setter
from .fill_synthetic_paste import fill_synthetic_paste
from .input_formats import format_for_input_type
from .secrets_env import (
    SecretEnvError,
    default_redactor,
    redact,
    register_secret,
    resolve_value_env,
)
from .text_compare import compare_text, safe_preview

PROBE_ON_NODE_JS = """function () {
  const el = this;
  if (!el) return { error: 'Element not found' };
  const result = {
    tag_name: el.tagName ? el.tagName.toLowerCase() : null,
    type: (el.getAttribute('type') || '') + '',
    input_type: (el.type || '') + '',
    contenteditable: (el.getAttribute('contenteditable') || '') + '',
    is_content_editable: !!el.isContentEditable,
    aria_role: (el.getAttribute('role') || '') + '',
    read_only: !!el.readOnly,
    disabled: !!el.disabled,
    required: !!el.required,
    multiple: !!el.multiple,
    is_contenteditable_element: !!el.isContentEditable
  };
  result.class_list = Array.from(el.classList || []);
  result.class_names = result.class_list.join(' ');
  result.has_shadow_root = !!el.shadowRoot;
  let frame = null;
  let node = el;
  while (node && node.nodeType === 1 && node !== document.documentElement) {
    const name = node.tagName ? node.tagName.toLowerCase() : '';
    if (name === 'iframe' || name === 'frame') {
      frame = node.id || (node.getAttribute('id') || '') + '';
      break;
    }
    node = node.parentNode;
  }
  result.iframe = frame || null;
  const hintNames = ['ProseMirror', 'ql-editor', 'react-select', 'Select2',
    'CodeMirror', 'Monaco', 'trix-editor', 'ck-editor'];
  result.hints = hintNames.filter(function(name) {
    return result.class_names.toLowerCase().indexOf(name.toLowerCase()) !== -1;
  });
  if (result.tag_name === 'select') {
    result.multiple_select = !!el.multiple;
    result.options = el.options ? el.options.length : 0;
    result.selectedIndex = el.selectedIndex;
  }
  return result;
}"""

READ_VALUE_JS = """function () {
  const el = this;
  if (!el) return null;
  if (el.tagName === 'SELECT') {
    let selText = '';
    if (el.selectedIndex >= 0 && el.options && el.options[el.selectedIndex]) {
      const opt = el.options[el.selectedIndex];
      selText = (opt.text || opt.innerText || '').trim();
    }
    return { value: el.value !== undefined ? String(el.value) : '', text: selText };
  }
  if (el.type === 'checkbox' || el.type === 'radio') {
    return { checked: Boolean(el.checked), value: el.value !== undefined ? String(el.value) : '' };
  }
  if (el.isContentEditable) {
    const txt = el.innerText !== undefined ? el.innerText : (el.textContent || '');
    return { text: txt.trim ? txt.trim() : txt };
  }
  if ('value' in el && el.tagName !== 'DIV' && el.tagName !== 'P') {
    return { value: el.value !== undefined ? String(el.value) : '' };
  }
  return { value: el.value !== undefined ? String(el.value) : '', text: (el.textContent || '').trim() };
}"""

SET_CHECKED_JS = """function (checkedVal) {
  const win = this.ownerDocument ? (this.ownerDocument.defaultView || window) : window;
  const proto = win.HTMLInputElement ? win.HTMLInputElement.prototype : Object.getPrototypeOf(this);
  let desc = Object.getOwnPropertyDescriptor(proto, 'checked');
  if (!desc || !desc.set) {
    let curr = Object.getPrototypeOf(this);
    while (curr) {
      desc = Object.getOwnPropertyDescriptor(curr, 'checked');
      if (desc && desc.set) break;
      curr = Object.getPrototypeOf(curr);
    }
  }
  const setter = desc ? desc.set : null;
  if (setter) {
    setter.call(this, checkedVal);
  } else {
    this.checked = checkedVal;
  }
  const EventCtor = win.Event || Event;
  this.dispatchEvent(new EventCtor('input', { bubbles: true }));
  this.dispatchEvent(new EventCtor('change', { bubbles: true }));
  return Boolean(this.checked);
}"""

SELECT_OPTION_ON_NODE_JS = """function (criteria) {
  const select = this;
  if (!select || !select.options) return false;
  const options = Array.from(select.options);
  let target = -1;
  const critVal = String(criteria).trim();
  target = options.findIndex(o => (o.value || '').trim() === critVal || (o.text || o.innerText || '').trim() === critVal);
  if (target < 0) {
    const critLower = critVal.toLowerCase();
    target = options.findIndex(o => (o.value || '').trim().toLowerCase() === critLower || (o.text || o.innerText || '').trim().toLowerCase() === critLower);
  }
  if (target < 0) return false;
  select.selectedIndex = target;
  const win = select.ownerDocument ? (select.ownerDocument.defaultView || window) : window;
  const EventCtor = win.Event || Event;
  select.dispatchEvent(new EventCtor('input', { bubbles: true }));
  select.dispatchEvent(new EventCtor('change', { bubbles: true }));
  return true;
}"""

FORMATTABLE_KINDS = {
    ElementKind.DATE,
    ElementKind.TIME,
    ElementKind.DATETIME,
    ElementKind.MONTH,
    ElementKind.WEEK,
    ElementKind.COLOR,
    ElementKind.RANGE,
    ElementKind.NUMBER,
}


def _extract_prop(data: Any, prop: str, default: Any = "") -> Any:
    """Extract a property from a dict, object, or primitive safely."""
    if isinstance(data, dict):
        if prop in data:
            return data[prop]
        if "value" in data and isinstance(data["value"], dict) and prop in data["value"]:
            return data["value"][prop]
    if hasattr(data, prop):
        val = getattr(data, prop)
        if val is not None:
            return val
    if hasattr(data, "value"):
        val = getattr(data, "value")
        if isinstance(val, dict) and prop in val:
            return val[prop]
        if prop == "value" and val is not None:
            return val
    if prop == "value":
        if isinstance(data, (str, int, float)):
            return str(data)
    elif prop == "checked":
        if isinstance(data, bool):
            return data
    elif prop == "text":
        if isinstance(data, str):
            return data
    return default


async def _resolve_node_and_facts(
    tab: Any,
    target: Any,
    timeout: float = DEFAULT_ELEMENT_TIMEOUT,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
    override_facts: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Dict[str, Any]]:
    """Resolve target to backend node ID, selector string, and element facts."""
    selector = ""
    if isinstance(target, int):
        node_id = target
        selector = str(target)
    elif isinstance(target, str):
        selector = target
        node_id = await input_actions.find_node(
            tab, target, timeout=timeout, frame=frame, instance_id=instance_id
        )
        if node_id is None:
            if target.isdigit():
                node_id = int(target)
            else:
                target_desc = f"{target} (in frame {frame})" if frame else target
                raise ValueError(f"Element not found: {target_desc}")
    elif hasattr(target, "backend_node_id") and target.backend_node_id is not None:
        node_id = int(target.backend_node_id)
        selector = str(node_id)
    else:
        raise TypeError(f"Unsupported target type: {type(target).__name__}")

    if override_facts is not None and isinstance(override_facts, dict):
        facts = override_facts
    else:
        raw = await input_actions.call_on_node(tab, node_id, PROBE_ON_NODE_JS)
        facts = raw if isinstance(raw, dict) else {}

    return node_id, selector, facts


def _pick_strategy(kind: str, facts: Dict[str, Any], requested_strategy: str = "auto") -> str:
    """Select appropriate input strategy based on element kind and requested mode."""
    strat = requested_strategy.lower().strip() if isinstance(requested_strategy, str) else "auto"

    if strat not in ("auto", "default"):
        if strat in ("native", "native_setter"):
            return "native_setter"
        if strat in ("synthetic", "synthetic_paste"):
            return "synthetic_paste"
        if strat in ("type", "typing"):
            return "typing"
        if strat in ("choose_option", "select"):
            return "choose_option"
        if strat == "checkbox":
            return "checkbox"
        if strat == "radio":
            return "radio"
        if strat == "file":
            return "file"
        raise ValueError(f"Unsupported strategy: '{requested_strategy}'")

    # Strategy selection for 'auto'
    if kind in (
        ElementKind.TEXT,
        ElementKind.PASSWORD,
        ElementKind.EMAIL,
        ElementKind.NUMBER,
        ElementKind.TEXTAREA,
    ):
        return "native_setter"

    if kind in (
        ElementKind.DATE,
        ElementKind.TIME,
        ElementKind.DATETIME,
        ElementKind.MONTH,
        ElementKind.WEEK,
        ElementKind.COLOR,
        ElementKind.RANGE,
    ):
        return "native_setter"

    if kind in (ElementKind.CONTENTEDITABLE, ElementKind.RICH_EDITOR):
        return "synthetic_paste"

    if kind in (ElementKind.SELECT, ElementKind.COMBOBOX):
        return "choose_option"

    if kind == ElementKind.CHECKBOX:
        return "checkbox"

    if kind == ElementKind.RADIO:
        return "radio"

    if kind == ElementKind.FILE:
        raise ValueError("Element kind 'file' is not supported by fill; use file_upload instead")

    if kind == ElementKind.BUTTON:
        raise ValueError("Element kind 'button' cannot be filled")

    if kind == ElementKind.UNKNOWN:
        tag = str(facts.get("tag_name") or "").lower()
        if tag in ("input", "textarea"):
            return "native_setter"
        raise ValueError(f"Unsupported element kind: '{kind}'")

    raise ValueError(f"Unsupported element kind: '{kind}'")


async def fill(
    tab: Any,
    target: Any,
    value: Optional[Any] = None,
    value_env: Optional[str] = None,
    strategy: str = "auto",
    verify: bool = True,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
    timeout: float = DEFAULT_ELEMENT_TIMEOUT,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Fill an input, textarea, select, combobox, or contenteditable element.

    Classifies the element kind, chooses an appropriate input strategy, applies
    the value, and verifies read-back via normalized comparison when verify is True.
    Password field values and value_env secrets are never echoed in results or errors.

    Args:
        tab: Browser tab object.
        target: Target selector string, backend node ID, or element handle.
        value: Direct text or value to fill into the element.
        value_env: Environment variable name holding secret (STEALTH_SECRET_ prefix).
        strategy: 'auto', 'native_setter', 'synthetic_paste', 'typing', or 'choose_option'.
        verify: Whether to verify applied value by reading it back.
        frame: Optional iframe selector, name, or id.
        instance_id: Optional browser instance ID for ref targeting.
        timeout: Seconds to wait for element resolution.
        **kwargs: Additional options (e.g. facts override for testing).

    Returns:
        Dict[str, Any]: Result dictionary with strategy, verified, and safe preview.
    """
    if value is not None and value_env is not None:
        raise ValueError("Cannot provide both 'value' and 'value_env'")
    if value is None and value_env is None:
        raise ValueError("Either 'value' or 'value_env' must be provided")

    is_secret = False
    if value_env is not None:
        raw_val = resolve_value_env(value_env)
        is_secret = True
    else:
        raw_val = str(value)

    try:
        node_id, selector, facts = await _resolve_node_and_facts(
            tab=tab,
            target=target,
            timeout=timeout,
            frame=frame,
            instance_id=instance_id,
            override_facts=kwargs.get("facts"),
        )

        kind = classify(facts)
        if kind == ElementKind.PASSWORD or str(facts.get("input_type") or "").lower() == "password":
            is_secret = True
            register_secret(raw_val)

        chosen_strategy = _pick_strategy(kind, facts, strategy)

        # Format input values for date/time/color/range/number
        if kind in FORMATTABLE_KINDS:
            input_type_key = "datetime-local" if kind == ElementKind.DATETIME else kind
            formatted_val = format_for_input_type(input_type_key, raw_val)
        else:
            formatted_val = raw_val

        if is_secret:
            register_secret(formatted_val)

        # Apply chosen strategy
        checked_target: Optional[bool] = None

        if chosen_strategy == "native_setter":
            await fill_native_setter(
                tab,
                node_id,
                text=formatted_val,
                frame=frame,
                instance_id=instance_id,
            )

        elif chosen_strategy == "synthetic_paste":
            paste_res = await fill_synthetic_paste(
                tab,
                node_id,
                text=formatted_val,
                frame=frame,
                instance_id=instance_id,
            )
            # If paste wasn't prevented and verify is on, check if fallback typing is needed
            if verify:
                read_data = await input_actions.call_on_node(tab, node_id, READ_VALUE_JS)
                act_text = str(_extract_prop(read_data, "text", "") or _extract_prop(read_data, "value", "") or "").strip()
                if not compare_text(formatted_val, act_text, mode="auto").equal:
                    await input_actions.focus_node(tab, node_id)
                    await input_actions.clear_field(tab, node_id)
                    for char in formatted_val:
                        await input_actions.type_character(tab, char)

        elif chosen_strategy == "typing":
            await input_actions.focus_node(tab, node_id)
            await input_actions.clear_field(tab, node_id)
            for char in formatted_val:
                await input_actions.type_character(tab, char)

        elif chosen_strategy == "choose_option":
            tag_name = str(facts.get("tag_name") or "").lower()
            if tag_name == "select":
                handled = await input_actions.call_on_node(
                    tab, node_id, SELECT_OPTION_ON_NODE_JS, formatted_val
                )
                if not handled:
                    try:
                        await DOMHandler.select_option(
                            tab, selector, value=formatted_val, text=formatted_val
                        )
                    except Exception:
                        await DOMHandler.choose_option(tab, selector, formatted_val, frame=frame)
            else:
                await DOMHandler.choose_option(tab, selector, formatted_val, frame=frame)

        elif chosen_strategy == "checkbox":
            if isinstance(value, bool):
                checked_target = value
            elif isinstance(formatted_val, str):
                checked_target = formatted_val.strip().lower() in ("true", "1", "checked", "yes", "on")
            else:
                checked_target = bool(formatted_val)
            await input_actions.call_on_node(tab, node_id, SET_CHECKED_JS, checked_target)

        elif chosen_strategy == "radio":
            checked_target = True
            await input_actions.call_on_node(tab, node_id, SET_CHECKED_JS, True)

        elif chosen_strategy == "file":
            if isinstance(value, (list, tuple)):
                paths = [str(p) for p in value]
            else:
                paths = [formatted_val]
            await DOMHandler.file_upload(tab, selector, paths)

        # Verification step
        verified = True
        first_diff = -1
        actual_for_preview: Optional[str] = None

        if verify:
            read_data = await input_actions.call_on_node(tab, node_id, READ_VALUE_JS)

            if chosen_strategy == "checkbox":
                actual_checked = bool(_extract_prop(read_data, "checked", False))
                verified = (actual_checked == checked_target)
                actual_for_preview = str(actual_checked)
                first_diff = -1 if verified else 0

            elif chosen_strategy == "radio":
                actual_checked = bool(_extract_prop(read_data, "checked", False))
                verified = (actual_checked is True)
                actual_for_preview = str(actual_checked)
                first_diff = -1 if verified else 0

            elif chosen_strategy == "choose_option":
                act_val = str(_extract_prop(read_data, "value", "") or "")
                act_txt = str(_extract_prop(read_data, "text", "") or "")
                comp_v = compare_text(formatted_val, act_val, mode="auto")
                comp_t = compare_text(formatted_val, act_txt, mode="auto")
                verified = comp_v.equal or comp_t.equal
                actual_for_preview = act_txt if comp_t.equal else act_val
                first_diff = -1 if verified else (comp_t.first_diff_index if not comp_t.equal else comp_v.first_diff_index)

            elif kind in (ElementKind.CONTENTEDITABLE, ElementKind.RICH_EDITOR):
                act_text = str(_extract_prop(read_data, "text", "") or _extract_prop(read_data, "value", "") or "")
                comp = compare_text(formatted_val, act_text, mode="auto")
                verified = comp.equal
                actual_for_preview = act_text
                first_diff = comp.first_diff_index

            else:
                act_val = str(_extract_prop(read_data, "value", "") or _extract_prop(read_data, "text", "") or "")
                comp = compare_text(formatted_val, act_val, mode="auto")
                verified = comp.equal
                actual_for_preview = act_val
                first_diff = comp.first_diff_index

        preview = safe_preview(formatted_val, is_password=is_secret)
        result: Dict[str, Any] = {
            "success": verified,
            "strategy": chosen_strategy,
            "kind": kind,
            "verified": verified,
            "preview": preview,
        }

        if verify and not verified:
            result["mismatch"] = {
                "first_diff_index": first_diff,
                "actual_preview": safe_preview(actual_for_preview or "", is_password=is_secret),
            }

        return default_redactor.redact(result)

    except Exception as exc:
        err_msg = default_redactor.redact(str(exc))
        raise type(exc)(err_msg) from None


__all__ = ["fill"]
