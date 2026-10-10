"""
Page bindings: page JavaScript calls that are answered by the MCP client.

A binding exposes `window.<name>(...args)` to the page. Each call returns a
Promise and is queued for the client, which reads it with
get_page_binding_calls and answers with resolve_page_binding_call. No code
from the client or the page runs on the server host, arguments must be JSON,
and payload size and queue length are capped so a page cannot exhaust memory.
"""

import asyncio
import itertools
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import nodriver as uc
from nodriver import Tab

from debug_logger import debug_logger

BINDING_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,63}$")
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_QUEUED_CALLS = 500
MAX_WAIT_SECONDS = 60.0
SETTLE_SYMBOL = "stealth-browser-binding"

WRAPPER_TEMPLATE = """(() => {
  const name = NAME;
  const key = Symbol.for(SYMBOL);
  const raw = globalThis[name];
  if (typeof raw !== 'function' || raw[key]) return;
  const pending = new Map();
  let sequence = 0;
  const binding = function (...args) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      let payload;
      try {
        payload = JSON.stringify({ id, args });
      } catch (error) {
        reject(new TypeError('Binding arguments must be JSON serializable'));
        return;
      }
      pending.set(id, { resolve, reject });
      raw(payload);
    });
  };
  Object.defineProperty(binding, key, {
    value: (id, ok, value) => {
      const entry = pending.get(id);
      if (!entry) return false;
      pending.delete(id);
      if (ok) entry.resolve(value); else entry.reject(new Error(String(value)));
      return true;
    }
  });
  Object.defineProperty(globalThis, name, { value: binding, writable: true, configurable: true, enumerable: false });
})();"""


@dataclass
class BindingCall:
    """
    One call made by page JavaScript.

    Attributes:
        call_id (str): Server-side call identifier
        binding (str): Binding name
        args (List[Any]): Arguments passed by the page
        received_at (float): Unix timestamp
        status (str): pending, auto_resolved, or rejected
        tab (Any): Tab that made the call
        context_id (int): Execution context of the calling frame
        page_call_id (int): Call id inside the page wrapper
    """

    call_id: str
    binding: str
    args: List[Any]
    received_at: float
    status: str
    tab: Any = field(repr=False)
    context_id: int = 0
    page_call_id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """
        Public view of the call.

        Returns:
            Dict[str, Any]: call_id, binding, args, received_at, and status
        """
        return {
            "call_id": self.call_id,
            "binding": self.binding,
            "args": self.args,
            "received_at": self.received_at,
            "status": self.status,
        }


@dataclass
class BindingConfig:
    """
    Settings for one binding.

    Attributes:
        auto_resolve (bool): Answer every call immediately with auto_response
        auto_response (Any): JSON value used when auto_resolve is set
        installs (List[Tuple[Any, str]]): (tab, init script identifier) per tab the binding is installed on
    """

    auto_resolve: bool = False
    auto_response: Any = None
    installs: List[Tuple[Any, str]] = field(default_factory=list)


class PageBindingManager:
    """Tracks page bindings and queued calls per browser instance."""

    def __init__(self):
        """Create empty binding and call registries."""
        self._bindings: Dict[str, Dict[str, BindingConfig]] = {}
        self._calls: Dict[str, "OrderedDict[str, BindingCall]"] = {}
        self._handlers: Dict[str, List[Tuple[Any, Any]]] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._tasks: set = set()
        self._ids = itertools.count(1)

    async def create(
        self,
        tab: Tab,
        instance_id: str,
        name: str,
        auto_resolve: bool = False,
        auto_response: Any = None,
    ) -> Dict[str, Any]:
        """
        Create a binding and install it on the instance's current tab.

        Args:
            tab (Tab): Current tab of the instance.
            instance_id (str): Browser instance ID.
            name (str): JavaScript identifier exposed as window.<name>.
            auto_resolve (bool): Answer every call immediately with auto_response.
            auto_response (Any): JSON value returned to the page when auto_resolve is set.

        Returns:
            Dict[str, Any]: Creation result.
        """
        if not BINDING_NAME.match(name or ""):
            raise ValueError(f"Invalid binding name: {name!r}. Use a JavaScript identifier up to 64 characters.")
        if name in self._bindings.get(instance_id, {}):
            raise ValueError(f"Binding already exists: {name}")
        exists, _ = await tab.send(uc.cdp.runtime.evaluate(
            expression=f"{json.dumps(name)} in globalThis",
            return_by_value=True,
        ))
        if exists.value:
            raise ValueError(f"window.{name} already exists on the page. Choose another name.")
        json.dumps(auto_response)
        config = BindingConfig(auto_resolve=auto_resolve, auto_response=auto_response)
        self._bindings.setdefault(instance_id, {})[name] = config
        try:
            await self._install(tab, instance_id, name, config)
        except Exception:
            self._bindings[instance_id].pop(name, None)
            raise
        return {
            "success": True,
            "binding_name": name,
            "available_as": f"window.{name}(...args)",
            "auto_resolve": auto_resolve,
        }

    async def prepare_tab(self, tab: Tab, instance_id: str, options: Any = None) -> None:
        """
        Install every binding of an instance on a tab. Used as a tab listener.

        Args:
            tab (Tab): Tab being prepared.
            instance_id (str): Browser instance ID.
            options (Any): Spawn options, unused.
        """
        for name, config in list(self._bindings.get(instance_id, {}).items()):
            if any(installed_tab is tab for installed_tab, _ in config.installs):
                continue
            await self._install(tab, instance_id, name, config)

    async def _install(self, tab: Tab, instance_id: str, name: str, config: BindingConfig) -> None:
        """
        Install one binding on one tab.

        Args:
            tab (Tab): Target tab.
            instance_id (str): Browser instance ID.
            name (str): Binding name.
            config (BindingConfig): Binding settings.
        """
        self._attach_handler(tab, instance_id)
        await tab.send(uc.cdp.runtime.add_binding(name=name))
        await tab.send(uc.cdp.page.enable())
        identifier = await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(
            source=WRAPPER_TEMPLATE.replace("NAME", json.dumps(name)).replace("SYMBOL", json.dumps(SETTLE_SYMBOL)),
            run_immediately=True,
        ))
        config.installs.append((tab, str(identifier)))

    def _attach_handler(self, tab: Tab, instance_id: str) -> None:
        """
        Listen for binding calls on a tab once.

        Args:
            tab (Tab): Target tab.
            instance_id (str): Browser instance ID.
        """
        handlers = self._handlers.setdefault(instance_id, [])
        if any(handled_tab is tab for handled_tab, _ in handlers):
            return

        def on_binding_called(event: uc.cdp.runtime.BindingCalled) -> None:
            self._on_binding_called(tab, instance_id, event)

        tab.add_handler(uc.cdp.runtime.BindingCalled, on_binding_called)
        handlers.append((tab, on_binding_called))

    def _on_binding_called(self, tab: Tab, instance_id: str, event: Any) -> None:
        """
        Queue a call from the page, or answer it right away for auto bindings.

        Args:
            tab (Tab): Tab that made the call.
            instance_id (str): Browser instance ID.
            event (Any): Runtime.bindingCalled event.
        """
        config = self._bindings.get(instance_id, {}).get(event.name)
        if config is None:
            return
        try:
            if len(event.payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise ValueError("payload too large")
            payload = json.loads(event.payload)
            page_call_id = int(payload["id"])
            args = payload.get("args") or []
        except Exception as error:
            debug_logger.log_warning("page_bindings", "binding_called", f"Rejected call to {event.name}: {error}")
            return

        call = BindingCall(
            call_id=f"call_{next(self._ids)}",
            binding=event.name,
            args=args,
            received_at=time.time(),
            status="pending",
            tab=tab,
            context_id=int(event.execution_context_id),
            page_call_id=page_call_id,
        )
        if config.auto_resolve:
            call.status = "auto_resolved"
            self._spawn(self._settle(call, True, config.auto_response))

        calls = self._calls.setdefault(instance_id, OrderedDict())
        calls[call.call_id] = call
        while len(calls) > MAX_QUEUED_CALLS:
            _, dropped = calls.popitem(last=False)
            if dropped.status == "pending":
                self._spawn(self._settle(dropped, False, "Binding call dropped, too many pending calls"))
        self._events.setdefault(instance_id, asyncio.Event()).set()

    def _spawn(self, coroutine: Any) -> None:
        """
        Run a coroutine in the background and keep a reference to it.

        Args:
            coroutine (Any): Coroutine to run.
        """
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _settle(self, call: BindingCall, ok: bool, value: Any) -> bool:
        """
        Resolve or reject the page Promise for a call.

        Args:
            call (BindingCall): Call to settle.
            ok (bool): Resolve when True, reject when False.
            value (Any): Result value, or error message when rejecting.

        Returns:
            bool: True when the page still had the call pending.
        """
        expression = (
            "(() => {"
            f" const binding = globalThis[{json.dumps(call.binding)}];"
            f" const settle = binding && binding[Symbol.for({json.dumps(SETTLE_SYMBOL)})];"
            f" return settle ? settle({call.page_call_id}, {json.dumps(ok)}, {json.dumps(value)}) : false;"
            " })()"
        )
        try:
            result, exception = await call.tab.send(uc.cdp.runtime.evaluate(
                expression=expression,
                context_id=uc.cdp.runtime.ExecutionContextId(call.context_id),
                return_by_value=True,
            ))
        except Exception:
            return False
        return not exception and bool(result.value)

    async def get_calls(
        self,
        instance_id: str,
        name: Optional[str] = None,
        wait_seconds: float = 0.0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Read queued calls, optionally waiting for the first one.

        Pending calls stay queued until resolved. Auto-resolved calls are
        removed once they have been read.

        Args:
            instance_id (str): Browser instance ID.
            name (Optional[str]): Only return calls to this binding.
            wait_seconds (float): Wait up to this long when no call is queued, capped at 60.
            limit (int): Maximum number of calls returned.

        Returns:
            List[Dict[str, Any]]: Calls, oldest first.
        """
        def matching() -> List[BindingCall]:
            return [
                call for call in self._calls.get(instance_id, {}).values()
                if name is None or call.binding == name
            ]

        if not matching() and wait_seconds > 0:
            event = self._events.setdefault(instance_id, asyncio.Event())
            deadline = time.monotonic() + min(wait_seconds, MAX_WAIT_SECONDS)
            while not matching():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError:
                    break

        selected = matching()[:max(limit, 1)]
        calls = self._calls.get(instance_id, {})
        for call in selected:
            if call.status != "pending":
                calls.pop(call.call_id, None)
        return [call.to_dict() for call in selected]

    async def resolve(
        self,
        instance_id: str,
        call_id: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Answer a pending call.

        Args:
            instance_id (str): Browser instance ID.
            call_id (str): Call identifier from get_calls.
            result (Any): JSON value the page Promise resolves with.
            error (Optional[str]): Rejects the page Promise with this message instead.

        Returns:
            Dict[str, Any]: Whether the page received the answer.
        """
        call = self._calls.get(instance_id, {}).get(call_id)
        if call is None or call.status != "pending":
            raise ValueError(f"No pending call with id {call_id}")
        json.dumps(result)
        ok = error is None
        delivered = await self._settle(call, ok, result if ok else str(error))
        self._calls[instance_id].pop(call_id, None)
        response = {"success": True, "call_id": call_id, "delivered": delivered}
        if not delivered:
            response["note"] = "The page navigated or closed before the answer arrived."
        return response

    async def remove(self, instance_id: str, name: str) -> Dict[str, Any]:
        """
        Remove a binding from every tab and reject its pending calls.

        Args:
            instance_id (str): Browser instance ID.
            name (str): Binding name.

        Returns:
            Dict[str, Any]: Removal result.
        """
        config = self._bindings.get(instance_id, {}).pop(name, None)
        if config is None:
            raise ValueError(f"No binding named {name}")
        calls = self._calls.get(instance_id, {})
        for call_id, call in list(calls.items()):
            if call.binding != name:
                continue
            calls.pop(call_id, None)
            if call.status == "pending":
                await self._settle(call, False, "Binding removed")
        for tab, identifier in config.installs:
            for command in (
                uc.cdp.page.remove_script_to_evaluate_on_new_document(
                    identifier=uc.cdp.page.ScriptIdentifier(identifier)
                ),
                uc.cdp.runtime.remove_binding(name=name),
                uc.cdp.runtime.evaluate(expression=f"delete globalThis[{json.dumps(name)}]"),
            ):
                try:
                    await tab.send(command)
                except Exception:
                    pass
        return {"success": True, "binding_name": name}

    def list_bindings(self, instance_id: str) -> List[str]:
        """
        Names of the bindings defined for an instance.

        Args:
            instance_id (str): Browser instance ID.

        Returns:
            List[str]: Binding names.
        """
        return list(self._bindings.get(instance_id, {}))

    async def forget_instance(self, instance_id: str) -> None:
        """
        Drop all binding state for a closed instance. Used as a close listener.

        Args:
            instance_id (str): Closed instance ID.
        """
        self._bindings.pop(instance_id, None)
        self._calls.pop(instance_id, None)
        self._events.pop(instance_id, None)
        for tab, handler in self._handlers.pop(instance_id, []):
            callbacks = getattr(tab, "handlers", {}).get(uc.cdp.runtime.BindingCalled)
            if callbacks and handler in callbacks:
                callbacks.remove(handler)


page_bindings = PageBindingManager()
