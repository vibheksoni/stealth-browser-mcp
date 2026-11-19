"""Real OS clipboard paste input strategy using machine-wide lock and CDP key events."""

from typing import Any, Dict, Optional

from nodriver import cdp

from . import input_actions
from .key_definitions import MODIFIER_CONTROL, resolve_key_descriptor
from .os_clipboard import clipboard_paste_session


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
            if target.isdigit():
                return int(target)
            target_desc = f"{target} (in frame {frame})" if frame else target
            raise RuntimeError(f"Element not found: {target_desc}")
        return int(node_id)
    raise TypeError(f"Unsupported target type: {type(target)!r}")


async def fill_os_paste(
    tab: Any,
    target: Any,
    text: str,
    timeout: float = 0.0,
    frame: Optional[str] = None,
    instance_id: Optional[str] = None,
    backend: Optional[Any] = None,
    **kwargs: Any,
) -> bool:
    """Fill an element using real OS clipboard paste under machine-wide lock.

    Focuses the target element, enters a clipboard_paste_session to place text
    on the system clipboard under lock, sends the trusted paste keystroke
    (Ctrl+V) via CDP Input.dispatchKeyEvent, and restores the original clipboard
    upon exit.

    Args:
        tab: Browser tab object.
        target: Backend node ID (int), selector (str), or element handle.
        text: Text to paste.
        timeout: Timeout in seconds for finding the element if selector is given.
        frame: Optional iframe selector, name, or id.
        instance_id: Optional browser instance ID for ref resolution.
        backend: Optional clipboard backend to use instead of the platform default.
        **kwargs: Additional keyword arguments.

    Returns:
        bool: True if paste key events were dispatched successfully.
    """
    node_id = await _resolve_backend_node_id(
        tab, target, timeout=timeout, frame=frame, instance_id=instance_id
    )
    await input_actions.focus_node(tab, node_id)

    key_desc = resolve_key_descriptor("v")
    with clipboard_paste_session(text, backend=backend):
        await tab.send(
            cdp.input_.dispatch_key_event(
                "rawKeyDown",
                modifiers=MODIFIER_CONTROL,
                key=key_desc["key"],
                code=key_desc["code"],
                windows_virtual_key_code=key_desc["virtual_key_code"],
                commands=["paste"],
            )
        )
        await tab.send(
            cdp.input_.dispatch_key_event(
                "keyUp",
                modifiers=MODIFIER_CONTROL,
                key=key_desc["key"],
                code=key_desc["code"],
                windows_virtual_key_code=key_desc["virtual_key_code"],
            )
        )
    return True


__all__ = ["fill_os_paste"]
