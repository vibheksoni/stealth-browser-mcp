"""Real network idle detection tracking in-flight CDP Network events."""

import asyncio
import time
from typing import Any, List, Optional, Set, Tuple

import nodriver as uc

from .debug_logger import debug_logger

DEFAULT_IDLE_TIME = 0.5
INTERNAL_URL_PREFIXES = (
    "chrome://",
    "chrome-untrusted://",
    "chrome-extension://",
    "devtools://",
)


class NetworkIdleTracker:
    """
    Track in-flight network requests using CDP Network events.

    Network is considered idle when there are zero in-flight requests for a
    continuous window of idle_time seconds (default: 0.5s).
    """

    def __init__(
        self,
        tab: Any,
        idle_time: float = DEFAULT_IDLE_TIME,
        timeout: Optional[float] = None,
    ):
        """
        Initialize the tracker for a tab.

        Args:
            tab (Any): Browser tab instance.
            idle_time (float): Continuous idle duration required in seconds (default: 0.5).
            timeout (Optional[float]): Optional overall timeout in seconds.
        """
        self.tab = tab
        self.idle_time = max(idle_time, 0.0)
        self.timeout = timeout
        self.in_flight: Set[str] = set()
        self._idle_timer: Optional[asyncio.TimerHandle] = None
        self._idle_event = asyncio.Event()
        self._started = False
        self._stopped = False
        self._handlers: List[Tuple[type, Any]] = []

    async def start(self) -> "NetworkIdleTracker":
        """
        Enable network tracking and register CDP event listeners.

        Returns:
            NetworkIdleTracker: self.
        """
        if self._started:
            return self
        self._started = True

        try:
            if hasattr(self.tab, "send"):
                await self.tab.send(uc.cdp.network.enable())
        except Exception:
            pass

        self._handlers = [
            (uc.cdp.network.RequestWillBeSent, self._on_request_will_be_sent),
            (uc.cdp.network.LoadingFinished, self._on_loading_finished),
            (uc.cdp.network.LoadingFailed, self._on_loading_failed),
        ]

        for event_type, handler in self._handlers:
            if hasattr(self.tab, "add_handler"):
                self.tab.add_handler(event_type, handler)

        self._check_idle()
        return self

    async def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait until network is idle within the timeout budget.

        Args:
            timeout (Optional[float]): Maximum seconds to wait. Uses initial timeout if None.

        Returns:
            bool: True if network became idle, False if timed out.
        """
        if not self._started:
            await self.start()

        effective_timeout = timeout if timeout is not None else self.timeout
        if effective_timeout is not None and effective_timeout <= 0:
            return self._idle_event.is_set()

        try:
            if effective_timeout is not None:
                await asyncio.wait_for(self._idle_event.wait(), timeout=effective_timeout)
            else:
                await self._idle_event.wait()
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        """Unregister event handlers and cancel active timers."""
        self._stopped = True
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

        handlers = getattr(self.tab, "handlers", None)
        if isinstance(handlers, dict):
            for event_type, handler in self._handlers:
                callbacks = handlers.get(event_type)
                if callbacks and handler in callbacks:
                    callbacks.remove(handler)
        self._handlers.clear()

    async def __aenter__(self) -> "NetworkIdleTracker":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    def _check_idle(self) -> None:
        if self._stopped or self._idle_event.is_set():
            return
        if len(self.in_flight) == 0:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            try:
                loop = asyncio.get_running_loop()
                self._idle_timer = loop.call_later(self.idle_time, self._on_idle_timeout)
            except RuntimeError:
                pass

    def _on_idle_timeout(self) -> None:
        if not self._stopped and len(self.in_flight) == 0:
            self._idle_event.set()

    def _on_request_will_be_sent(self, event: Any, *args: Any) -> None:
        if self._stopped:
            return
        req = getattr(event, "request", None)
        url = getattr(req, "url", "") if req else ""
        if url and url.startswith(INTERNAL_URL_PREFIXES):
            return

        req_id = str(getattr(event, "request_id", id(event)))
        self.in_flight.add(req_id)
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        self._idle_event.clear()

    def _on_loading_finished(self, event: Any, *args: Any) -> None:
        if self._stopped:
            return
        req_id = str(getattr(event, "request_id", ""))
        self.in_flight.discard(req_id)
        if len(self.in_flight) == 0:
            self._check_idle()

    def _on_loading_failed(self, event: Any, *args: Any) -> None:
        if self._stopped:
            return
        req_id = str(getattr(event, "request_id", ""))
        self.in_flight.discard(req_id)
        if len(self.in_flight) == 0:
            self._check_idle()


async def wait_for_network_idle(
    tab: Any,
    idle_time: float = DEFAULT_IDLE_TIME,
    timeout: Optional[float] = None,
) -> bool:
    """
    Convenience function to wait until network requests are idle.

    Args:
        tab (Any): Browser tab.
        idle_time (float): Idle window duration in seconds (default: 0.5).
        timeout (Optional[float]): Timeout in seconds.

    Returns:
        bool: True if idle was reached, False on timeout.
    """
    async with NetworkIdleTracker(tab, idle_time=idle_time, timeout=timeout) as tracker:
        return await tracker.wait(timeout=timeout)
