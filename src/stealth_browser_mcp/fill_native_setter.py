"""Native value setter fill strategy for React-controlled and framework inputs."""

from typing import Any, Optional, Union

from nodriver import Tab

from .input_actions import call_on_node, find_node

NATIVE_SETTER_JS = """function (text) {
  const win = this.ownerDocument ? (this.ownerDocument.defaultView || window) : window;
  const tag = (this.tagName || '').toUpperCase();
  const isTextArea = tag === 'TEXTAREA' || (win.HTMLTextAreaElement && this instanceof win.HTMLTextAreaElement);
  let proto = isTextArea
    ? (win.HTMLTextAreaElement ? win.HTMLTextAreaElement.prototype : Object.getPrototypeOf(this))
    : (win.HTMLInputElement ? win.HTMLInputElement.prototype : Object.getPrototypeOf(this));
  if (!proto || !Object.getOwnPropertyDescriptor(proto, 'value')) {
    let curr = Object.getPrototypeOf(this);
    while (curr) {
      const desc = Object.getOwnPropertyDescriptor(curr, 'value');
      if (desc && desc.set) {
        proto = curr;
        break;
      }
      curr = Object.getPrototypeOf(curr);
    }
  }
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') || Object.getOwnPropertyDescriptor(proto,'value');
  let setter = descriptor ? descriptor.set : null;
  if (!setter && Object.getOwnPropertyDescriptor(proto, 'value')) {
    setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  }
  if (!setter && Object.getOwnPropertyDescriptor(proto,'value')) {
    setter = Object.getOwnPropertyDescriptor(proto,'value').set;
  }
  if (setter) {
    setter.call(this, text);
  } else {
    this.value = text;
  }
  const EventCtor = win.Event || Event;
  this.dispatchEvent(new EventCtor('input', { bubbles: true }));
  this.dispatchEvent(new EventCtor('change', { bubbles: true }));
  return this.value;
}"""

FILL_NATIVE_SETTER_JS = NATIVE_SETTER_JS


async def fill_native_setter(
    tab: Any,
    backend_node_id_or_selector: Optional[Union[int, str]] = None,
    text: str = "",
    timeout: float = 0.0,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
    selector: Optional[str] = None,
    backend_node_id: Optional[int] = None,
    **kwargs: Any,
) -> Any:
    """
    Fill an input or textarea using the native prototype value setter.

    Bypasses React, Angular, or Vue value tracking overrides by calling
    the underlying HTMLInputElement or HTMLTextAreaElement prototype value
    setter directly, followed by bubbling 'input' and 'change' events.

    Args:
        tab: Browser tab object.
        backend_node_id_or_selector: Backend node ID (int) or element selector (str).
        text: Text to set into the input element.
        timeout: Timeout in seconds for finding the element if selector is given.
        frame: Optional frame selector or name if inside an iframe.
        instance_id: Optional browser instance ID for ref resolution.
        selector: Optional element selector alias.
        backend_node_id: Optional backend node ID alias.
        **kwargs: Additional keyword arguments.

    Returns:
        Any: The resulting value of the element after setting and event dispatch.
    """
    target = backend_node_id_or_selector
    if target is None:
        if backend_node_id is not None:
            target = backend_node_id
        elif selector is not None:
            target = selector
        elif "target" in kwargs:
            target = kwargs["target"]
        else:
            raise ValueError(
                "A backend_node_id or selector must be provided to fill_native_setter"
            )

    if "value" in kwargs and not text:
        text = str(kwargs["value"])

    if isinstance(target, int):
        node_id = target
    elif isinstance(target, str):
        node_id = await find_node(
            tab,
            target,
            timeout=timeout,
            frame=frame,
            instance_id=instance_id,
        )
        if node_id is None:
            if target.isdigit():
                node_id = int(target)
            else:
                raise ValueError(f"Element not found for selector: {target}")
    else:
        raise TypeError(
            f"backend_node_id_or_selector must be int or str, got {type(target).__name__}"
        )

    return await call_on_node(tab, node_id, NATIVE_SETTER_JS, str(text))
