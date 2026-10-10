"""Base class and shared helpers for benchmark targets."""

import asyncio
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..results import Check, Status

DEFAULT_POLL_INTERVAL = 0.5


class Session(Protocol):
    """Browser access handed to targets by the runner."""

    local_base_url: str

    async def navigate(self, url: str, timeout: float = 45.0) -> None:
        """
        Load a URL in the benchmark tab.

        Args:
            url (str): Page to load
            timeout (float): Navigation timeout in seconds
        """

    async def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        """
        Evaluate JavaScript in the benchmark tab.

        Args:
            expression (str): JavaScript expression
            await_promise (bool): Await the result when it is a Promise

        Returns:
            Any: Plain Python value
        """

    async def add_init_script(self, source: str) -> str:
        """
        Install a script that runs before page scripts on new documents.

        Args:
            source (str): JavaScript source

        Returns:
            str: Script identifier
        """

    async def click(self, selector: str) -> None:
        """
        Click an element the way the click_element tool does.

        Args:
            selector (str): CSS selector
        """

    async def type_text(self, selector: str, text: str) -> None:
        """
        Type into an element the way the type_text tool does.

        Args:
            selector (str): CSS selector
            text (str): Text to type
        """


class Target:
    """
    One detection surface measured by the benchmark.

    Subclasses set the class attributes, gather a raw payload in collect(), and
    turn that payload into checks in evaluate(). evaluate() must not touch the
    browser so it can be unit tested with recorded payloads.

    Attributes:
        name (str): Stable identifier used in result files
        title (str): Human readable name
        category (str): Detection surface covered
        url (str): Page loaded by the target, may use {local} for the local server
        timeout (float): Overall time budget in seconds
    """

    name = ""
    title = ""
    category = ""
    url = ""
    timeout = 90.0

    def resolve_url(self, session: Session) -> str:
        """
        Expand the {local} placeholder in the target URL.

        Args:
            session (Session): Active browser session

        Returns:
            str: Absolute URL to load
        """
        return self.url.format(local=session.local_base_url)

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load the target and gather its raw payload.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: Raw payload passed to evaluate()
        """
        raise NotImplementedError

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn a raw payload into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details to keep, and a one line summary
        """
        raise NotImplementedError


class Unreachable(Exception):
    """Raised when a target page could not be loaded or never produced results."""


def status_from_checks(checks: List[Check]) -> Status:
    """
    Derive a target status from its checks.

    Args:
        checks (List[Check]): Checks produced by evaluate()

    Returns:
        Status: FAIL when a critical check failed, DEGRADED when only
        non-critical checks failed, PASS otherwise, UNREACHABLE when there are no checks
    """
    if not checks:
        return Status.UNREACHABLE
    if any(not check.passed and check.critical for check in checks):
        return Status.FAIL
    if any(not check.passed for check in checks):
        return Status.DEGRADED
    return Status.PASS


async def wait_for_value(
    session: Session,
    expression: str,
    timeout: float,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> Any:
    """
    Poll a JavaScript expression until it returns a truthy value.

    Args:
        session (Session): Active browser session
        expression (str): Expression to poll
        timeout (float): Maximum wait in seconds
        interval (float): Delay between polls in seconds

    Returns:
        Any: First truthy value

    Raises:
        Unreachable: When the value stays falsy for the whole timeout
    """
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while True:
        try:
            value = await session.evaluate(expression)
            if value:
                return value
        except Exception as error:
            last_error = error
        if time.monotonic() >= deadline:
            detail = f": {last_error}" if last_error else ""
            raise Unreachable(f"Timed out after {timeout:.0f}s waiting for results{detail}")
        await asyncio.sleep(interval)
