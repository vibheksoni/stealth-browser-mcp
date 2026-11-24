"""Opt-in CDP-over-pipe transport (SB-POST-1, closes residual F9).

The default launch path (and the only one unless STEALTH_CDP_TRANSPORT=pipe)
controls spawned Chrome over a loopback DevTools port on 127.0.0.1. That port
has no DevTools authentication, so another local user or another process in
the same network namespace can attach to the browser. See SECURITY.md section
"Accepted residual: the loopback DevTools port (F9)".

This module adds an opt-in alternative that talks CDP over Chrome's inherited
``--remote-debugging-pipe`` instead of a TCP port, closing that gap for
browsers this server spawns. Attaching to an already-running external browser
still needs the port and is untouched here.

Chrome's plain ``--remote-debugging-pipe`` (no value) speaks JSON CDP messages
each terminated by a single NUL byte (``\\0``) in both directions. The 4-byte
big-endian length prefix is only used for ``--remote-debugging-pipe=cbor`` which
this server never requests, so the framing in this module is NUL-delimited JSON.

No nodriver import lives at module import time so the parsing, argv building,
and framing stay unit-testable without a real browser or the nodriver package.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# Environment flag that selects the opt-in pipe transport. Exact match.
CDP_TRANSPORT_ENV = "STEALTH_CDP_TRANSPORT"

# The two supported transports.
TRANSPORT_PORT = "port"
TRANSPORT_PIPE = "pipe"

# NUL is the delimiter Chrome uses between CDP messages on a plain pipe.
_NUL = b"\0"

# fd numbers Chrome uses for a plain --remote-debugging-pipe: commands from fd 3,
# responses/events on fd 4 (Puppeteer's PipeTransport and Playwright's pipe
# launch map the same two fds).
COMMAND_FD = 3
RESPONSE_FD = 4

# Sentinel placed on the frame queue when the pipe read end is exhausted.
_EOF = object()

# Raised when Chrome replies with a CDP error object.
class CdpPipeError(Exception):
    """Raised when the pipe transport reports a CDP error from the peer."""

class CdpTransport(Enum):
    """Which transport to use to reach the spawned browser's DevTools."""

    PORT = "port"
    PIPE = "pipe"

    @property
    def is_pipe(self) -> bool:
        return self is CdpTransport.PIPE


def parse_cdp_transport(env_value: Optional[str]) -> CdpTransport:
    """Resolve a transport from the raw environment value.

    Unset, empty, and the literal string "port" all select the loopback-port
    path so v1.0.0 behavior is preserved bit for bit. The literal string
    "pipe" selects the pipe transport. Any other value is a hard error rather
    than a silent fall back, because a typo could leave F9 unaddressed.

    Args:
        env_value (Optional[str]): Raw env value (already unstripped or None).

    Returns:
        CdpTransport: PORT for unset/empty/"port", PIPE for "pipe".

    Raises:
        ValueError: When env_value is a non-empty value other than port/pipe.
    """
    if env_value is None:
        return CdpTransport.PORT
    value = env_value.strip().lower()
    if value == "" or value == TRANSPORT_PORT:
        return CdpTransport.PORT
    if value == TRANSPORT_PIPE:
        return CdpTransport.PIPE
    raise ValueError(
        f"unknown {CDP_TRANSPORT_ENV}={env_value!r}: expected "
        f"'{TRANSPORT_PORT}', '{TRANSPORT_PIPE}', or unset"
    )


def resolve_cdp_transport() -> CdpTransport:
    """Resolve the transport from the process environment."""
    return parse_cdp_transport(os.getenv(CDP_TRANSPORT_ENV))


def build_chrome_launch_args(
    base_args: Optional[List[str]] = None,
    *,
    transport: Optional[CdpTransport] = None,
    port: int = 0,
) -> List[str]:
    """Return the browser launch args with the DevTools flag for a transport.

    Args:
        base_args (Optional[List[str]]): Args already assembled for the spawn.
        transport (Optional[CdpTransport]): Transport to target.
        port (int): Port to emit only in PORT mode. Ignored in PIPE mode.

    Returns:
        List[str]: The base args plus exactly one DevTools flag. PORT mode
        emits ``--remote-debugging-port=<port>`` and no pipe flag. PIPE mode
        emits ``--remote-debugging-pipe`` and never a port flag.

    Raises:
        ValueError: When transport parsing of the base args fails.
    """
    args = list(base_args or [])
    resolved = transport or resolve_cdp_transport()

    if resolved.is_pipe:
        args = [a for a in args if not a.startswith("--remote-debugging-port")]
        if "--remote-debugging-pipe" not in args:
            args.append("--remote-debugging-pipe")
        return args

    args = [a for a in args if not a.startswith("--remote-debugging-pipe")]
    args.append(f"--remote-debugging-port={port}")
    return args


def split_nul_frames(buffer: bytes) -> Tuple[List[bytes], bytes]:
    """Split a byte buffer into complete NUL-delimited messages.

    Args:
        buffer (bytes): Bytes accumulated so far from the pipe read end.

    Returns:
        Tuple[List[bytes], bytes]: The complete frame payloads (without the
        trailing NUL) and the leftover partial bytes that do not yet contain a
        full delimiter.
    """
    parts = buffer.split(_NUL)
    return parts[:-1], parts[-1]


class PipeDescriptors:
    """Two os.pipe() pairs used to drive a pipe-launched Chrome child.

    The parent writes CDP commands on ``command_w`` and the child maps its fd 3
    to the matching read end; Chrome writes responses on ``response_w`` and the
    parent reads them from ``response_r``.
    """

    def __init__(
        self,
        command_r: int,
        command_w: int,
        response_r: int,
        response_w: int,
    ) -> None:
        self.command_r = command_r
        self.command_w = command_w
        self.response_r = response_r
        self.response_w = response_w

    def map_child_fds(self) -> Any:
        """Return a preexec_fn mapping child fd 3/4 onto these pipes."""

        def _map_fds():
            os.dup2(self.command_r, COMMAND_FD)
            os.dup2(self.response_w, RESPONSE_FD)
            for fd in (
                self.command_r,
                self.command_w,
                self.response_r,
                self.response_w,
            ):
                if fd not in (COMMAND_FD, RESPONSE_FD):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        return _map_fds

    def close_parent_unused(self) -> None:
        """Close the ends the parent does not use after the child inherits them."""
        for fd in (self.command_r, self.response_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self) -> None:
        for fd in (
            self.command_r,
            self.command_w,
            self.response_r,
            self.response_w,
        ):
            try:
                os.close(fd)
            except OSError:
                pass


def open_chrome_pipe() -> PipeDescriptors:
    """Create the two pipe pairs used to drive a pipe-launched Chrome child.

    The caller must spawn Chrome with ``--remote-debugging-pipe`` and a preexec_fn
    from :meth:`PipeDescriptors.map_child_fds`, then close the parent's unused
    ends via :meth:`PipeDescriptors.close_parent_unused`.
    """
    cmd_r, cmd_w = os.pipe()
    resp_r, resp_w = os.pipe()
    return PipeDescriptors(cmd_r, cmd_w, resp_r, resp_w)


# A single frame must not grow past this without a NUL delimiter. A longer
# buffer means the peer never terminates a frame, which would otherwise let a
# misbehaving Chrome accumulate unbounded memory in _buffer.
MAX_FRAME_SIZE = 64 * 1024 * 1024


class PipeCDPClient:
    """Minimal async CDP client over an inherited Chrome pipe.

    The pipe is exposed to the server as a pair of file descriptors that already
    belong to this process, so this client does not open a socket: it wraps the
    read end to collect frames and the write end to send them. Chrome frames a
    plain ``--remote-debugging-pipe`` as JSON CDP messages each terminated by a
    single NUL byte in both directions.

    The reader never blocks the event loop: readiness on the read fd is handled
    by the loop's selector via ``loop.add_reader``. There is no executor worker
    thread and no reader task, so teardown cannot hang joining a worker blocked
    inside ``os.read`` (which cancelling an asyncio task or closing the fds
    would not interrupt on Linux). The client always binds the loop that is
    already running via ``get_running_loop``. Callers are responsible for
    spawning Chrome with ``--remote-debugging-pipe`` and passing it the matching
    descriptors via a preexec_fn.

    A typical construction is ``await PipeCDPClient(read_fd, write_fd).start()``.

    Args:
        read_fd: File descriptor Chrome writes responses/events to.
        write_fd: File descriptor the server writes commands to.
        loop: Optional running event loop; ignored (``get_running_loop`` wins).

    Attributes:
        next_id (int): Monotonic CDP message id counter, starting at 1.
    """

    def __init__(
        self,
        read_fd: int,
        write_fd: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._loop = loop
        self.next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._frames: "asyncio.Queue" = asyncio.Queue()
        self._session_queues: Dict[str, List[asyncio.Queue]] = collections.defaultdict(list)
        self._buffer = b""
        self._closed = False
        self._write_lock: Optional[asyncio.Lock] = None

    def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def write_frame(self, payload: bytes) -> None:
        lock = self._get_write_lock()
        async with lock:
            total = 0
            while total < len(payload):
                n = os.write(self._write_fd, payload[total:])
                total += n

    def register_session(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._session_queues[session_id].append(q)
        return q

    def unregister_session(self, session_id: str, queue: asyncio.Queue) -> None:
        if session_id in self._session_queues:
            try:
                self._session_queues[session_id].remove(queue)
            except ValueError:
                pass
            if not self._session_queues[session_id]:
                del self._session_queues[session_id]

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Return the running loop, binding it lazily so no loop is required.

        Always uses ``asyncio.get_running_loop`` (never ``get_event_loop``),
        which is the documented way to reach the loop an async call is running
        in.
        """
        loop = self._loop
        if loop is None:
            loop = asyncio.get_running_loop()
            self._loop = loop
        return loop

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        message_id: Optional[int] = None,
    ) -> int:
        """Serialize and write a CDP request as one NUL-delimited JSON frame.

        Args:
            method (str): CDP method name, e.g. "Browser.getVersion".
            params (Optional[Dict[str, Any]]): Optional method parameters.
            message_id (Optional[int]): Override the auto-assigned id.

        Returns:
            int: The message id that pairs with the response.
        """
        if message_id is not None:
            msg_id = message_id
        else:
            msg_id = self.next_id
            self.next_id = msg_id + 1
        request = {"id": msg_id, "method": method}
        if params is not None:
            request["params"] = params
        body = json.dumps(request).encode("utf-8")
        os.write(self._write_fd, body + _NUL)
        return msg_id

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Send a request and wait for its response.

        Args:
            method (str): CDP method name.
            params (Optional[Dict[str, Any]]): Optional method parameters.
            timeout (float): Seconds to wait for the response.

        Returns:
            Dict[str, Any]: The decoded "result" object.

        Raises:
            CdpPipeError: When the peer returns a CDP error object.
            asyncio.TimeoutError: When the response does not arrive in time.
        """
        if self._closed:
            raise CdpPipeError("client is closed")
        future = self._get_loop().create_future()
        msg_id = self.send(method, params)
        self._pending[msg_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    def _on_readable(self) -> None:
        """Selector callback for the read fd; runs on the event loop.

        The selector wakes us only when data is readable, so the loop is never
        blocked waiting on a read. This pulls a chunk, feeds the NUL-framed
        parser, and dispatches completed frames to awaiting callers.
        """
        if self._closed:
            return
        try:
            data = os.read(self._read_fd, 65536)
        except OSError:
            # Pipe closed or the fd was removed during teardown. Stop reading
            # and fail every in-flight call so no caller hangs.
            self._shutdown()
            return
        if not data:
            # EOF: peer closed its write end; stop reading and fail pending.
            self._shutdown()
            return
        if len(self._buffer) + len(data) > MAX_FRAME_SIZE:
            # A frame longer than MAX_FRAME_SIZE never gets a NUL delimiter;
            # fail via the shutdown path so the reader is unregistered and
            # in-flight calls see CdpPipeError instead of hanging.
            self._shutdown()
            return
        self._buffer += data
        frames, leftover = split_nul_frames(self._buffer)
        self._buffer = leftover
        for frame in frames:
            try:
                message = json.loads(frame.decode("utf-8"))
            except Exception:
                continue
            msg_id = message.get("id")
            future = self._pending.pop(msg_id, None)
            if future is not None:
                if "error" in message:
                    if not future.done():
                        future.set_exception(
                            CdpPipeError(json.dumps(message["error"]))
                        )
                else:
                    if not future.done():
                        future.set_result(message.get("result", {}))
            session_id = message.get("sessionId")
            if session_id is not None and session_id in self._session_queues:
                for q in self._session_queues[session_id]:
                    q.put_nowait(message)
            else:
                self._frames.put_nowait(message)

    async def get_frame(self, timeout: float) -> Any:
        """Pop the next parsed frame or raise on timeout/EOF.

        Used by PipeWebSocket.recv(): returns a dict message or, on EOF, raises
        an error mirroring a closed websocket. On timeout it re-raises
        ``asyncio.TimeoutError`` so callers can retry.
        """
        try:
            frame = await asyncio.wait_for(self._frames.get(), timeout)
        except asyncio.TimeoutError:
            raise
        if frame is _EOF:
            raise CdpPipeError("pipe read end closed")
        return frame

    async def start(self) -> "PipeCDPClient":
        """Register the read fd for readiness and return self.

        The loop's selector drives ``_on_readable``; there is no reader task to
        join on shutdown.
        """
        loop = self._get_loop()
        loop.add_reader(self._read_fd, self._on_readable)
        return self

    def _shutdown(self) -> None:
        """Idempotent teardown shared by every shutdown path.

        Removes the read fd from the selector first so the loop stops waking on
        an fd at EOF (which otherwise stays readable and spins). Then fails
        every in-flight call with :class:`CdpPipeError` so no caller hangs on a
        pipe that is already closed. Finally closes the fds. Safe to call more
        than once.
        """
        self._closed = True
        loop = None
        try:
            loop = self._get_loop()
        except (RuntimeError, ValueError):
            loop = None
        if loop is not None:
            try:
                loop.remove_reader(self._read_fd)
            except (RuntimeError, ValueError, FileNotFoundError):
                pass
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CdpPipeError("pipe closed"))
        self._pending.clear()
        self._frames.put_nowait(_EOF)
        for q_list in list(self._session_queues.values()):
            for q in q_list:
                q.put_nowait(_EOF)
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    async def close(self) -> None:
        """Unregister the read fd, fail pending calls, and close the fds.

        Removing the fd from the selector is the only cleanup the loop needs;
        there is no executor thread or reader task to cancel or await.
        """
        self._shutdown()

    def sync_close(self) -> None:
        """Idempotent, await-free shutdown for cleanup-safe teardown.

        Uses :meth:`_shutdown` so the fd is removed before the pending futures
        are failed, then closes the fds directly so no thread has to be
        joined. ``remove_reader`` is best-effort here because the loop may
        already be closing when a test cleanup runs.
        """
        self._shutdown()


class PipeWebSocket:
    """Duck-typed websocket adapter backed by the Chrome pipe fd pair.

    nodriver's Connection calls ``send(str)``, ``recv() -> str``, ``close()``,
    and inspects ``close_code`` on its websocket. This adapter exposes the same
    surface, backed by the NUL-delimited JSON frames on the pipe. It lets a
    pipe-launched Chrome satisfy nodriver without opening any TCP port or real
    websocket.
    """

    def __init__(
        self,
        read_fd: int,
        write_fd: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        session_id: Optional[str] = None,
        client: Optional[PipeCDPClient] = None,
    ) -> None:
        import websockets.exceptions

        self._read_fd = read_fd
        self._write_fd = write_fd
        # Bind the loop that is already running; never get_event_loop(). The
        # loop is passed to the wrapped client so both share one loop.
        self._loop = loop or asyncio.get_running_loop()
        self._owns_client = client is None
        self._client = client or PipeCDPClient(read_fd, write_fd, self._loop)
        self._session_id = session_id
        self._queue: Optional[asyncio.Queue] = None
        if session_id is not None:
            self._queue = self._client.register_session(session_id)
        self.close_code = None
        self.closed = True
        self.websocket_url = f"ws://pipe/{session_id}" if session_id else "ws://pipe"
        self._exceptions = websockets.exceptions

    async def start(self) -> "PipeWebSocket":
        if self._owns_client:
            await self._client.start()
        self.closed = False
        return self

    async def recv(self) -> str:
        """Return the next NUL-delimited message or raise on timeout/EOF.

        nodriver's listener polls ``recv()`` with a 0.05s timeout, so a missing
        frame must raise ``asyncio.TimeoutError`` to let the loop retry, and an
        EOF must raise ``websockets.exceptions.ConnectionClosed`` to let the
        listener tear down.
        """
        if self._session_id is not None and self._queue is not None:
            try:
                frame = await asyncio.wait_for(self._queue.get(), 0.05)
            except asyncio.TimeoutError:
                raise
            if frame is _EOF:
                raise self._exceptions.ConnectionClosed("pipe closed")
            return json.dumps(frame)
        else:
            try:
                frame = await self._client.get_frame(0.05)
            except CdpPipeError:
                raise self._exceptions.ConnectionClosed("pipe closed")
            return json.dumps(frame)

    async def send(self, message: str) -> None:
        if self._session_id is not None:
            obj = json.loads(message)
            obj["sessionId"] = self._session_id
            payload = json.dumps(obj).encode("utf-8") + _NUL
        else:
            payload = message.encode("utf-8") + _NUL
        await self._client.write_frame(payload)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = 1000
        if self._session_id is not None and self._queue is not None:
            self._client.unregister_session(self._session_id, self._queue)
        if self._owns_client:
            await self._client.close()

    def sync_close(self) -> None:
        """Stop and close the adapter without awaiting (cleanup-safe).

        Delegates to the wrapped client's :meth:`PipeCDPClient.sync_close` so a
        SYNC addCleanup can tear the reader down from the adapter's loop.
        """
        self.closed = True
        self.close_code = 1000
        if self._session_id is not None and self._queue is not None:
            self._client.unregister_session(self._session_id, self._queue)
        if self._owns_client:
            self._client.sync_close()


class PipeConnection:
    """Reuses nodriver.Connection over a PipeWebSocket with no TCP port.

    nodriver's Connection lazily opens a websocket via ``websockets.connect``.
    Instead, a nodriver Connection instance is created with its ``_websocket``
    pre-set to a :class:`PipeWebSocket`, so every ``send``/``recv``/``close`` is
    served by the pipe.
    """

    def __init__(self, browser, websocket: PipeWebSocket) -> None:
        import nodriver.core.connection as _conn

        self._browser = browser
        self._ws = websocket
        self._conn_cls = _conn.Connection
        # Reuse nodriver's Connection instance but inject our websocket.
        self._real = self._conn_cls("ws://pipe", browser=browser)
        self._real._websocket = websocket

    @property
    def websocket(self):
        return self._ws

    @property
    def closed(self):
        return self._ws.closed

    async def connect(self, **kwargs):
        if self._ws.closed:
            await self._ws.start()
        if self._real._listener_task is None or self._real._listener_task.done():
            self._real._listener_task = asyncio.ensure_future(self._real._listener())
        await self._real._register_handlers()

    async def disconnect(self):
        if self._real._listener_task:
            self._real._listener_task.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def send(self, *args, **kwargs):
        if self._real._listener_task is None or self._real._listener_task.done():
            if self._ws.closed:
                await self._ws.start()
            self._real._listener_task = asyncio.ensure_future(self._real._listener())
        return await self._real.send(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._real, item)
