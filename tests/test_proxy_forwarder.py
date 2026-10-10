import asyncio
import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proxy_forwarder import AuthenticatedProxyForwarder

RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


class FakeUpstream:
    """Upstream proxy that records request heads and answers each once."""

    def __init__(self, connect_status=b"200 Connection established"):
        self.requests = []
        self.connect_status = connect_status
        self.server = None

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(head.decode())
        if head.startswith(b"CONNECT"):
            writer.write(b"HTTP/1.1 " + self.connect_status + b"\r\n\r\n")
            await writer.drain()
            if self.connect_status.startswith(b"200"):
                data = await reader.read(5)
                writer.write(data.upper())
                await writer.drain()
        else:
            writer.write(RESPONSE)
            await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1]


async def forward(upstream, payload, read_bytes=1024):
    forwarder = AuthenticatedProxyForwarder(f"http://user:p%40ss@127.0.0.1:{upstream.port}")
    await forwarder.start()
    try:
        reader, writer = await asyncio.open_connection(forwarder.host, forwarder.port)
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(read_bytes), 5)
        closed = await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
        return data, closed
    finally:
        await forwarder.close()


class ProxyForwarderTests(unittest.TestCase):
    def test_plain_http_sends_credentials_and_closes(self):
        async def run():
            async with FakeUpstream() as upstream:
                request = (
                    b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
                    b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n"
                    b"Proxy-Authorization: Basic stale\r\n\r\n"
                )
                data, closed = await forward(upstream, request)
                return upstream.requests, data, closed

        requests, data, closed = asyncio.run(run())
        expected = base64.b64encode(b"user:p@ss").decode()
        head = requests[0]
        self.assertIn(f"Proxy-Authorization: Basic {expected}", head)
        self.assertNotIn("stale", head)
        self.assertNotIn("keep-alive", head.lower())
        self.assertIn("Connection: close", head)
        self.assertTrue(data.endswith(b"ok"))
        self.assertTrue(closed)

    def test_connect_tunnel_relays_data(self):
        async def run():
            async with FakeUpstream() as upstream:
                forwarder = AuthenticatedProxyForwarder(f"http://user:pass@127.0.0.1:{upstream.port}")
                await forwarder.start()
                try:
                    reader, writer = await asyncio.open_connection(forwarder.host, forwarder.port)
                    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
                    await writer.drain()
                    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                    writer.write(b"hello")
                    await writer.drain()
                    echoed = await asyncio.wait_for(reader.read(5), 5)
                    writer.close()
                    return upstream.requests, status, echoed
                finally:
                    await forwarder.close()

        requests, status, echoed = asyncio.run(run())
        self.assertIn("Proxy-Authorization: Basic ", requests[0])
        self.assertTrue(status.startswith(b"HTTP/1.1 200"))
        self.assertEqual(echoed, b"HELLO")

    def test_connect_rejection_is_reported(self):
        async def run():
            async with FakeUpstream(connect_status=b"407 Proxy Authentication Required 200") as upstream:
                return await forward(upstream, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")

        data, closed = asyncio.run(run())
        self.assertTrue(data.startswith(b"HTTP/1.1 502"))
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
