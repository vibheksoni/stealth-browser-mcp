"""Offline target that checks clicks and typing look like real user input."""

from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target, wait_for_value

TYPED_TEXT = "Probe Text 42!"
READ_PROBE_JS = "window.__inputProbe ? JSON.parse(JSON.stringify(window.__inputProbe)) : null"
READ_VALUE_JS = "document.getElementById('field').value"
RESET_PROBE_JS = "Object.assign(window.__inputProbe, { events: [], added: 0, moves: 0 }) && true"
CLICK_SEQUENCE = ["pointerdown", "mousedown", "mouseup", "click"]


class InputFidelityTarget(Target):
    """
    Clicks a button below the fold and types into a field through the same
    code paths as the click_element and type_text tools, then checks the
    events the page saw.
    """

    name = "input_fidelity"
    title = "Input fidelity"
    category = "Interaction"
    url = "{local}/input.html"
    timeout = 45.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Click and type on the input probe page.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: Recorded events, mouse move count, injected node count, and the typed value
        """
        await session.navigate(self.resolve_url(session), timeout=20.0)
        await wait_for_value(session, READ_PROBE_JS, timeout=10.0)
        await session.evaluate(RESET_PROBE_JS)
        await session.click("#target")
        await session.type_text("#field", TYPED_TEXT)
        probe = await session.evaluate(READ_PROBE_JS)
        probe["value"] = await session.evaluate(READ_VALUE_JS)
        return probe

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn recorded input events into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        events = payload.get("events") or []
        mouse = [event for event in events if event["type"] in CLICK_SEQUENCE]
        keys = [event for event in events if event["type"] in ("keydown", "keyup", "input")]
        clicks = [event for event in mouse if event["type"] == "click"]
        downs = [event for event in mouse if event["type"] == "mousedown"]
        ups = [event for event in mouse if event["type"] == "mouseup"]
        press_ms = round(ups[0]["time"] - downs[0]["time"], 1) if downs and ups else None
        printable = list(TYPED_TEXT)
        keydown_keys = [event["key"] for event in keys if event["type"] == "keydown" and event["key"] != "Shift"]
        keyup_count = sum(1 for event in keys if event["type"] == "keyup" and event["key"] != "Shift")
        checks = [
            Check("click is trusted", bool(clicks) and all(event["trusted"] for event in clicks), True, len(clicks)),
            Check(
                "click has pointer and mouse events",
                [event["type"] for event in mouse] == CLICK_SEQUENCE,
                True,
                [event["type"] for event in mouse],
            ),
            Check("mouse moved before click", (payload.get("moves") or 0) > 0, False, payload.get("moves")),
            Check("button held for a moment", press_ms is not None and press_ms > 5, False, press_ms),
            Check("key events are trusted", bool(keys) and all(event["trusted"] for event in keys), True, len(keys)),
            Check("every character has keydown", keydown_keys == printable, True, "".join(key or "" for key in keydown_keys)),
            Check("every keydown has keyup", keyup_count == len(printable), True, keyup_count),
            Check("typed value is exact", payload.get("value") == TYPED_TEXT, True, payload.get("value")),
            Check("no nodes injected into the page", (payload.get("added") or 0) == 0, True, payload.get("added")),
        ]
        passed = sum(1 for check in checks if check.passed)
        details = {"press_ms": press_ms, "mouse_moves": payload.get("moves"), "injected_nodes": payload.get("added")}
        return checks, details, f"{passed}/{len(checks)} checks passed"
