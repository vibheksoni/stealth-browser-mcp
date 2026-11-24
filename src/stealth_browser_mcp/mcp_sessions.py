"""MCP client session to isolated browser session mapping and lifecycle (SB-60).

Provides per-MCP-client browser session isolation:
- Maps each MCP client session ID to an isolated browser session (SB-47 per-chat session).
- Spawns the browser instance lazily via session_spawn / browser_manager upon first action.
- Automatically closes the browser session when the MCP client disconnects.
- Enforces strict isolation: two client sessions get distinct browser instances and
  cannot access or see each other's instances or tabs.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Generator, List, Optional

from starlette.types import Message, Receive, Scope, Send

from .errors import instance_not_found_error

_current_mcp_session_id: ContextVar[Optional[str]] = ContextVar(
    "current_mcp_session_id",
    default=None,
)


@contextmanager
def set_current_mcp_session_id(session_id: Optional[str]) -> Generator[None, None, None]:
    """Context manager to set the active MCP client session ID for a task."""
    token = _current_mcp_session_id.set(session_id)
    try:
        yield
    finally:
        _current_mcp_session_id.reset(token)


def get_current_mcp_session_id() -> Optional[str]:
    """Retrieve the active MCP client session ID.

    Checks:
    1. Explicit ContextVar set by middleware or test harness.
    2. FastMCP dependency context (ctx.session_id).
    3. Low-level MCP request context headers.

    Returns:
        Optional[str]: Session ID if within an MCP client session, else None.
    """
    sid = _current_mcp_session_id.get()
    if sid:
        return sid

    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        if ctx and hasattr(ctx, "session_id"):
            session_val = ctx.session_id
            if session_val:
                return str(session_val)
    except Exception:
        pass

    try:
        from mcp.server.lowlevel.server import request_ctx
        rc = request_ctx.get()
        if rc and getattr(rc, "request", None):
            req = rc.request
            if hasattr(req, "headers"):
                h_sid = req.headers.get("mcp-session-id")
                if h_sid:
                    return str(h_sid)
        if rc and getattr(rc, "session", None):
            s = rc.session
            fastmcp_id = getattr(s, "_fastmcp_id", None)
            if fastmcp_id:
                return str(fastmcp_id)
    except Exception:
        pass

    return None


class MCPSessionManager:
    """Manages mappings between MCP client sessions and isolated browser instances."""

    def __init__(self, browser_manager: Optional[Any] = None):
        self._browser_manager = browser_manager
        self._session_to_instance: Dict[str, str] = {}
        self._instance_to_session: Dict[str, str] = {}
        self._last_seen: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    def set_browser_manager(self, bm: Any) -> None:
        """Attach or update the underlying BrowserManager."""
        self._browser_manager = bm

    def register_session(self, mcp_session_id: str, instance_id: str) -> None:
        """Explicitly register an instance for an MCP client session."""
        self._session_to_instance[mcp_session_id] = instance_id
        self._instance_to_session[instance_id] = mcp_session_id
        self.touch_session(mcp_session_id)

    def get_instance_for_session(self, mcp_session_id: str) -> Optional[str]:
        """Get the browser instance ID associated with an MCP client session."""
        return self._session_to_instance.get(mcp_session_id)

    def get_session_for_instance(self, instance_id: str) -> Optional[str]:
        """Get the MCP client session ID that owns a browser instance."""
        return self._instance_to_session.get(instance_id)

    def isolation_armed(self) -> bool:
        """True once any instance has an owner session."""
        return bool(self._instance_to_session)

    def session_ttl_seconds(self) -> float:
        raw = os.getenv("STEALTH_MCP_SESSION_TTL", "600").strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 600.0

    def touch_session(self, mcp_session_id: Optional[str]) -> None:
        """Record activity so the session expiry reaper leaves this session alone."""
        if mcp_session_id:
            self._last_seen[mcp_session_id] = time.monotonic()

    async def expire_stale_sessions(self, now: Optional[float] = None) -> List[str]:
        """Close sessions that have been idle longer than STEALTH_MCP_SESSION_TTL.

        TTL 0 disables expiry. Returns the session ids that were closed.
        """
        ttl = self.session_ttl_seconds()
        if ttl <= 0:
            return []
        current = time.monotonic() if now is None else now
        expired = [
            sid for sid, seen in list(self._last_seen.items())
            if current - seen >= ttl
        ]
        closed: List[str] = []
        for sid in expired:
            await self.close_session(sid)
            self._last_seen.pop(sid, None)
            closed.append(sid)
        return closed

    async def start_session_reaper(self, interval: float = 30.0) -> None:
        if self._reaper_task is not None:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.expire_stale_sessions()
                except Exception:
                    pass

        self._reaper_task = asyncio.create_task(_loop())

    async def stop_session_reaper(self) -> None:
        task = self._reaper_task
        self._reaper_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def list_instance_ids_for_session(self, mcp_session_id: str) -> List[str]:
        """Return every instance currently owned by an MCP client session."""
        return [
            instance_id
            for instance_id, owner in self._instance_to_session.items()
            if owner == mcp_session_id
        ]

    def is_instance_accessible(
        self,
        mcp_session_id: Optional[str],
        instance_id: str,
    ) -> bool:
        """Check whether the given MCP session is authorized to access an instance.

        An instance owned by another MCP session cannot be accessed.
        """
        owner = self._instance_to_session.get(instance_id)
        if mcp_session_id is None:
            # Local/direct caller without session context (stdio)
            return True
        if owner is not None:
            return owner == mcp_session_id
        # Unowned instances are not shared with session-bound callers once
        # session isolation has been armed (any instance has an owner).
        if self._instance_to_session:
            return False
        return True

    def check_access(self, instance_id: str, mcp_session_id: Optional[str] = None) -> None:
        """Enforce session isolation.

        Raises:
            Exception: If the instance is owned by a different MCP client session.
        """
        sid = mcp_session_id if mcp_session_id is not None else get_current_mcp_session_id()
        if sid and not self.is_instance_accessible(sid, instance_id):
            raise instance_not_found_error(instance_id)

    def filter_instances(
        self,
        instances: List[Any],
        mcp_session_id: Optional[str] = None,
    ) -> List[Any]:
        """Filter a list of instances or instance dicts to only those accessible."""
        sid = mcp_session_id if mcp_session_id is not None else get_current_mcp_session_id()
        if not sid:
            return instances
        allowed: List[Any] = []
        for inst in instances:
            iid = inst.instance_id if hasattr(inst, "instance_id") else inst.get("instance_id")
            if iid and self.is_instance_accessible(sid, iid):
                allowed.append(inst)
        return allowed

    async def get_or_create_instance(
        self,
        mcp_session_id: str,
        seed: Optional[str] = None,
        **spawn_kwargs: Any,
    ) -> str:
        """Lazily retrieve or spawn an isolated browser instance for an MCP session."""
        async with self._lock:
            existing = self._session_to_instance.get(mcp_session_id)
            if existing and self._browser_manager:
                inst = await self._browser_manager.get_instance(existing)
                if inst:
                    return existing

            instance_id = await self._spawn_for_session(
                mcp_session_id,
                seed=seed,
                **spawn_kwargs,
            )
            self._session_to_instance[mcp_session_id] = instance_id
            self._instance_to_session[instance_id] = mcp_session_id
            return instance_id

    async def _spawn_for_session(
        self,
        mcp_session_id: str,
        seed: Optional[str] = None,
        **spawn_kwargs: Any,
    ) -> str:
        """Spawn a fresh isolated browser instance."""
        if not self._browser_manager:
            raise RuntimeError("BrowserManager not configured on MCPSessionManager")

        try:
            from .models import BrowserOptions
        except Exception:
            from stealth_browser_mcp.models import BrowserOptions
        effective_seed = seed or os.getenv("STEALTH_DEFAULT_SEED")
        options = BrowserOptions(seed=effective_seed, **spawn_kwargs)
        instance = await self._browser_manager.spawn_browser(options)
        return instance.instance_id

    async def close_session(self, mcp_session_id: str) -> bool:
        """Close every browser instance belonging to an MCP client session."""
        try:
            from . import tool_surface
            tool_surface.clean_session(mcp_session_id)
        except Exception:
            pass
        async with self._lock:
            instance_ids = [
                instance_id
                for instance_id, owner in list(self._instance_to_session.items())
                if owner == mcp_session_id
            ]
            self._session_to_instance.pop(mcp_session_id, None)
            if not instance_ids:
                return False
            for instance_id in instance_ids:
                self._instance_to_session.pop(instance_id, None)
                if self._browser_manager:
                    try:
                        await self._browser_manager.close_instance(instance_id)
                    except Exception:
                        pass
            return True

    async def on_instance_closed(self, instance_id: str) -> None:
        """Callback invoked when a browser instance closes."""
        sid = self._instance_to_session.pop(instance_id, None)
        if not sid:
            return
        if self._session_to_instance.get(sid) == instance_id:
            remaining = [
                iid for iid, owner in self._instance_to_session.items() if owner == sid
            ]
            if remaining:
                self._session_to_instance[sid] = remaining[-1]
            else:
                self._session_to_instance.pop(sid, None)


mcp_session_manager = MCPSessionManager()


class SessionLifecycleMiddleware:
    """ASGI middleware to bind MCP session IDs to context and close browsers on disconnect."""

    def __init__(self, app: Any, session_manager: Optional[MCPSessionManager] = None):
        self.app = app
        self.session_manager = session_manager or mcp_session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session_id: Optional[str] = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"mcp-session-id":
                try:
                    session_id = v.decode("ascii")
                except Exception:
                    session_id = None
                break

        is_delete = scope.get("method") == "DELETE"
        disconnected = False

        async def custom_receive() -> Message:
            nonlocal disconnected
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                disconnected = True
            return msg

        with set_current_mcp_session_id(session_id):
            if session_id:
                self.session_manager.touch_session(session_id)
            try:
                await self.app(scope, custom_receive, send)
            finally:
                if session_id and is_delete:
                    # Close browsers only on DELETE (client drop of the MCP
                    # session). A GET SSE stream disconnect must not close
                    # the session's browsers; expiry and DELETE handle that.
                    await self.session_manager.close_session(session_id)
