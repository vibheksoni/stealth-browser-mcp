"""Synthetic paste input strategy using ClipboardEvent and DataTransfer."""

from typing import Any, Dict, Optional

from nodriver import Tab

from . import input_actions

SYNTHETIC_PASTE_JS = """function (text, html) {
  if (typeof this.focus === 'function') {
    this.focus();
  }
  const dt = new DataTransfer();
  if (text !== null && text !== undefined) {
    dt.setData('text/plain', String(text));
  }
  if (html !== null && html !== undefined && html !== '') {
    dt.setData('text/html', String(html));
  }
  let event;
  try {
    event = new ClipboardEvent('paste', {
      clipboardData: dt,
      bubbles: true,
      cancelable: true,
      composed: true
    });
  } catch (e) {
    event = document.createEvent('Event');
    event.initEvent('paste', true, true);
    event.clipboardData = dt;
  }
  const notPrevented = this.dispatchEvent(event);
  const isDefaultPrevented = Boolean(event.defaultPrevented || !notPrevented);
  return {
    defaultPrevented: isDefaultPrevented,
    default_prevented: isDefaultPrevented
  };
}"""


def _extract_default_prevented(result: Any) -> bool:
    """Extract default_prevented boolean from JavaScript return value."""
    if isinstance(result, dict):
        if "default_prevented" in result:
            return bool(result["default_prevented"])
        if "defaultPrevented" in result:
            return bool(result["defaultPrevented"])
    if isinstance(result, bool):
        return result
    return False


async def _resolve_backend_node_id(
    tab: Any,
    target: Any,
    timeout: float = 0.0,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> int:
    """Resolve target argument to a backend node ID."""
    if isinstance(target, int):
        return int(target)
    if hasattr(target, "backend_node_id") and target.backend_node_id is not None:
        return int(target.backend_node_id)
    if isinstance(target, str):
        node_id = await input_actions.find_node(
            tab,
            target,
            timeout=timeout,
            frame=frame,
            instance_id=instance_id,
        )
        if node_id is None:
            target_desc = f"{target} (in frame {frame})" if frame else target
            raise RuntimeError(f"Element not found: {target_desc}")
        return int(node_id)
    raise TypeError(f"Unsupported target type: {type(target)!r}")


async def fill_synthetic_paste(
    tab: Any,
    target: Any,
    text: str,
    html: Optional[str] = None,
    timeout: float = 0.0,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, bool]:
    """
    Dispatch a synthetic paste ClipboardEvent with a DataTransfer payload.

    Focuses the target element and dispatches a bubbling, cancelable paste
    ClipboardEvent containing text/plain (and text/html if provided) on the
    element. Returns whether default was prevented so callers can determine
    if a rich editor or custom handler processed the paste.

    Args:
        tab: Browser tab object.
        target: Target element, backend node ID, or selector string.
        text: Plain text content to paste.
        html: Optional HTML markup to paste.
        timeout: Seconds to wait when resolving a selector string.
        frame: Optional iframe selector, name, or id.
        instance_id: Optional browser instance ID for ref targeting.
        **kwargs: Extra keyword arguments for forward compatibility.

    Returns:
        Dict[str, bool]: {'default_prevented': bool}
    """
    text_val = str(text) if text is not None else ""
    html_val = str(html) if html is not None else None

    backend_node_id = await _resolve_backend_node_id(
        tab,
        target,
        timeout=timeout,
        frame=frame,
        instance_id=instance_id,
    )

    await input_actions.focus_node(tab, backend_node_id)

    raw_result = await input_actions.call_on_node(
        tab,
        backend_node_id,
        SYNTHETIC_PASTE_JS,
        text_val,
        html_val,
    )

    return {"default_prevented": _extract_default_prevented(raw_result)}
