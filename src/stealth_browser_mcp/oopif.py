"""Command routing for out-of-process (cross-origin) iframes.

Chrome does not expose a standalone websocket for out-of-process iframe
targets. Commands reach them through the parent page connection using a
flattened session id. SessionTab wraps that session so callers can use the
same ``send`` interface as a regular nodriver tab.
"""

from typing import Any, Dict, Generator, Optional

import nodriver as uc

from . import nodriver_compat

nodriver_compat.apply()


class SessionCommand:
    """Generator-like wrapper that tags a CDP command with a session id."""

    def __init__(self, command: Generator, session_id: str) -> None:
        self._command = command
        self.session_id = session_id

    def __iter__(self) -> "SessionCommand":
        return self

    def __next__(self) -> Dict[str, Any]:
        return next(self._command)

    def send(self, value: Any) -> Any:
        return self._command.send(value)


class SessionTab:
    """Tab-like object that sends commands to a target through a session."""

    is_session_tab = True

    def __init__(self, parent: Any, session_id: str, target_id: str, url: str = "") -> None:
        self.parent = parent
        self.session_id = session_id
        self.target_id = target_id
        self.url = url
        self.closed = False

    async def send(self, command: Generator) -> Any:
        return await self.parent.send(SessionCommand(command, self.session_id))

    async def evaluate(self, expression: str, await_promise: bool = False, return_by_value: bool = True) -> Any:
        result = await self.send(uc.cdp.runtime.evaluate(
            expression=expression,
            await_promise=await_promise,
            return_by_value=return_by_value,
        ))
        remote = result[0] if isinstance(result, tuple) else result
        return getattr(remote, "value", remote)


async def attach_iframe_target(parent: Any, frame_id: str) -> Optional[SessionTab]:
    """
    Attach to the out-of-process iframe target whose id equals ``frame_id``.

    Args:
        parent (Any): nodriver tab that owns the iframe.
        frame_id (str): Frame id of the iframe element (equals the target id for OOPIFs).

    Returns:
        Optional[SessionTab]: Session-backed tab, or None when no such target exists.
    """
    try:
        targets = await parent.send(uc.cdp.target.get_targets())
    except Exception:
        return None
    match = None
    for info in targets or []:
        if str(getattr(info, "target_id", "")) == str(frame_id) and str(getattr(info, "type_", "")) == "iframe":
            match = info
            break
    if match is None:
        return None
    try:
        session_id = await parent.send(uc.cdp.target.attach_to_target(target_id=match.target_id, flatten=True))
    except Exception:
        return None
    if not session_id:
        return None
    return SessionTab(parent, str(session_id), str(match.target_id), str(getattr(match, "url", "") or ""))
