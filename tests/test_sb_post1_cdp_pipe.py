"""SB-POST-1: tests for corrected CDP-over-pipe transport (D1/D2 + Phase 2 adapter).

Covers:
  * D2: NUL-delimited JSON framing (split_nul_frames), including partial reads
    and several messages packed into a single read chunk.
  * D1: fd 3 (read commands) / fd 4 (write responses) mapping via
    PipeDescriptors (fd-mapping fakes, no real Chrome).
  * Phase 2: the duck-typed nodriver Connection adapter (PipeConnection over a
    PipeWebSocket) driven by a fake fd-backed PipeCDPClient.

Real-browser coverage lives in tests/test_sb_post1_cdp_pipe_real.py and is
gated behind STEALTH_BROWSER_TESTS.
"""

import asyncio
import functools
import json
import os
import unittest
from unittest import IsolatedAsyncioTestCase

from stealth_browser_mcp import cdp_pipe as cp


# Per-test timeout so a hang fails fast instead of stalling the suite or
# teardown. Guarded methods are wrapped in asyncio.wait_for with this cap.
_TIMEOUT = 10


def guarded(method):
    """Wrap an async test in a timeout so it cannot hang the suite."""

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        return await asyncio.wait_for(method(self, *args, **kwargs), timeout=_TIMEOUT)

    return wrapper


# ---------------------------------------------------------------------------
# D2: NUL-delimited JSON framing
# ---------------------------------------------------------------------------
class SplitNulFramesTests(unittest.TestCase):
    def test_two_messages_one_chunk(self):
        a = {"id": 1, "result": {"ok": True}}
        b = {"id": 2, "result": {"ok": False}}
        chunk = json.dumps(a).encode() + b"\0" + json.dumps(b).encode() + b"\0"
        frames, leftover = cp.split_nul_frames(chunk)
        self.assertEqual(json.loads(frames[0]), a)
        self.assertEqual(json.loads(frames[1]), b)
        self.assertEqual(leftover, b"")

    def test_multiple_reads_accumulate(self):
        leftover = b""
        messages = [
            json.dumps({"id": 1}).encode() + b"\0",
            json.dumps({"id": 2}).encode() + b"\0",
            json.dumps({"id": 3}).encode() + b"\0",
        ]
        collected = []
        for chunk in messages:
            frames, leftover = cp.split_nul_frames(leftover + chunk)
            collected.extend(frames)
        self.assertEqual([json.loads(f)["id"] for f in collected], [1, 2, 3])
        self.assertEqual(leftover, b"")

    def test_message_split_across_reads(self):
        leftover = b""
        full = json.dumps({"id": 1, "result": {"n": 1}}).encode()
        first, second = full[:7], full[7:]
        frames, leftover = cp.split_nul_frames(leftover + first)
        self.assertEqual(frames, [], "no complete frame until NUL arrives")
        frames, leftover = cp.split_nul_frames(leftover + second + b"\0")
        self.assertEqual(json.loads(frames[0]), {"id": 1, "result": {"n": 1}})

    def test_internal_nul_byte_in_json_string(self):
        payload = {"id": 7, "result": {"title": "a\u0000b"}}
        chunk = json.dumps(payload).encode() + b"\0"
        frames, leftover = cp.split_nul_frames(chunk)
        self.assertEqual(json.loads(frames[0]), payload)

    def test_truncated_then_completed(self):
        frames, leftover = cp.split_nul_frames(b'{"id": ')
        self.assertEqual(frames, [])
        frames, leftover = cp.split_nul_frames(leftover + b'9,"result":{}}\0')
        self.assertEqual(json.loads(frames[0]), {"id": 9, "result": {}})


# ---------------------------------------------------------------------------
# D1: fd 3/4 mapping (PipeDescriptors)
# ---------------------------------------------------------------------------
class PipeDescriptorsMappingTests(unittest.TestCase):
    def test_command_fd_is_three_and_response_is_four(self):
        self.assertEqual(cp.COMMAND_FD, 3)
        self.assertEqual(cp.RESPONSE_FD, 4)

    def test_map_child_fds_returns_callable(self):
        descs = cp.open_chrome_pipe()
        try:
            preexec = descs.map_child_fds()
            self.assertTrue(callable(preexec))
        finally:
            descs.close()

    def test_close_parent_unused_leaves_two_ends(self):
        # Parent only uses command_w (write) and response_r (read); the other
        # two ends are closed by close_parent_unused after the child inherits.
        cmd_r, cmd_w = os.pipe()
        resp_r, resp_w = os.pipe()
        descs = cp.PipeDescriptors(cmd_r, cmd_w, resp_r, resp_w)
        descs.close_parent_unused()
        for fd in (cmd_r, resp_w):
            with self.assertRaises(OSError):
                os.fstat(fd)
        os.fstat(cmd_w)
        os.fstat(resp_r)
        descs.close()


# ---------------------------------------------------------------------------
# Phase 2: PipeCDPClient + PipeWebSocket + PipeConnection over fake fds
# ---------------------------------------------------------------------------
class FakePipe:
    """A fake Chrome pipe pair: responses produced via feed(), commands read via
    read_one(). Uses two real os.pipe() pairs so the client's reader loop and
    the test's consumer never contend for the same descriptor."""

    def __init__(self):
        self.resp_r, self.resp_w = os.pipe()  # client reads responses here
        self.cmd_r, self.cmd_w = os.pipe()    # client writes commands here
        self.read_fd = self.resp_r
        self.write_fd = self.cmd_w

    async def feed(self, data: bytes):
        os.write(self.resp_w, data)

    async def read_one(self) -> bytes:
        return os.read(self.cmd_r, 65536)

    async def close(self):
        for fd in (self.resp_r, self.resp_w, self.cmd_r, self.cmd_w):
            try:
                os.close(fd)
            except OSError:
                pass


class PipeClientTransportTests(IsolatedAsyncioTestCase):
    async def _client(self):
        fake = FakePipe()
        client = await cp.PipeCDPClient(fake.read_fd, fake.write_fd).start()
        self.addCleanup(client.sync_close)
        return client, fake

    @guarded
    async def test_send_writes_nul_delimited_json(self):
        client, fake = await self._client()
        msg_id = client.send("Browser.getVersion", {"a": 1})
        self.assertEqual(msg_id, 1)
        got = await asyncio.wait_for(fake.read_one(), timeout=1)
        obj = json.loads(got.split(b"\0")[0])
        self.assertEqual(obj["method"], "Browser.getVersion")
        self.assertEqual(obj["id"], 1)
        self.assertEqual(obj["params"], {"a": 1})
        client.send("Page.navigate", {})
        got2 = await asyncio.wait_for(fake.read_one(), timeout=1)
        self.assertEqual(json.loads(got2.split(b"\0")[0])["id"], 2)

    @guarded
    async def test_call_resolves_from_pipe_response(self):
        client, fake = await self._client()
        resp = {"id": 1, "result": {"protocolVersion": "1.3"}}
        await fake.feed(json.dumps(resp).encode() + b"\0")
        out = await client.call("Browser.getVersion")
        self.assertEqual(out["protocolVersion"], "1.3")

    @guarded
    async def test_call_receives_error(self):
        client, fake = await self._client()
        resp = {"id": 1, "error": {"code": -32000, "message": "boom"}}
        await fake.feed(json.dumps(resp).encode() + b"\0")
        with self.assertRaises(cp.CdpPipeError):
            await client.call("X.Y")

    @guarded
    async def test_frame_routed_for_recv(self):
        client, fake = await self._client()
        event = {"method": "Page.frameNavigated", "params": {"frame": {"id": "f1"}}}
        await fake.feed(json.dumps(event).encode() + b"\0")
        frame = await client.get_frame(1.0)
        self.assertEqual(frame["method"], "Page.frameNavigated")


class PipeWebSocketAdapterTests(IsolatedAsyncioTestCase):
    @guarded
    async def test_attributes_and_send_recv(self):
        fake = FakePipe()
        ws = await cp.PipeWebSocket(fake.read_fd, fake.write_fd).start()
        self.addCleanup(ws.sync_close)
        self.assertFalse(ws.closed)
        self.assertEqual(ws.close_code, None)
        self.assertEqual(ws.websocket_url, "ws://pipe")

        await ws.send(json.dumps({"id": 5, "method": "Runtime.enable"}))
        got = await asyncio.wait_for(fake.read_one(), timeout=1)
        self.assertEqual(json.loads(got.split(b"\0")[0])["id"], 5)

        resp = json.dumps({"id": 5, "result": {}}).encode() + b"\0"
        await fake.feed(resp)
        decoded = json.loads(await ws.recv())
        self.assertEqual(decoded, {"id": 5, "result": {}})

    @guarded
    async def test_recv_times_out_when_idle(self):
        fake = FakePipe()
        ws = await cp.PipeWebSocket(fake.read_fd, fake.write_fd).start()
        self.addCleanup(ws.sync_close)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.2)

    @guarded
    async def test_close_is_idempotent(self):
        fake = FakePipe()
        ws = await cp.PipeWebSocket(fake.read_fd, fake.write_fd).start()
        self.addCleanup(ws.sync_close)
        await ws.close()
        await ws.close()  # second close is a no-op
        self.assertTrue(ws.closed)
        self.assertEqual(ws.close_code, 1000)


# ---------------------------------------------------------------------------
# D1/D2 transport resolution
# ---------------------------------------------------------------------------
class TransportResolutionTests(unittest.TestCase):
    def test_parse_none_and_empty_and_port_select_port(self):
        for v in (None, "", "  ", "port", "PORT"):
            self.assertIs(cp.parse_cdp_transport(v), cp.CdpTransport.PORT)

    def test_parse_pipe_selects_pipe(self):
        self.assertIs(cp.parse_cdp_transport("pipe"), cp.CdpTransport.PIPE)
        self.assertTrue(cp.CdpTransport.PIPE.is_pipe)

    def test_parse_unknown_raises(self):
        with self.assertRaises(ValueError):
            cp.parse_cdp_transport("http")

    def test_build_pipe_removes_port_adds_pipe(self):
        args = ["--headless", "--no-sandbox", "--remote-debugging-port=0"]
        out = cp.build_chrome_launch_args(args, transport=cp.CdpTransport.PIPE)
        self.assertIn("--remote-debugging-pipe", out)
        self.assertFalse(
            any(a.startswith("--remote-debugging-port") for a in out)
        )
        self.assertIn("--headless", out)
        self.assertIn("--no-sandbox", out)

    def test_build_port_removes_pipe(self):
        args = ["--headless", "--remote-debugging-pipe"]
        out = cp.build_chrome_launch_args(args, transport=cp.CdpTransport.PORT, port=0)
        self.assertIn("--remote-debugging-port=0", out)
        self.assertFalse(any(a == "--remote-debugging-pipe" for a in out))


if __name__ == "__main__":
    unittest.main()
