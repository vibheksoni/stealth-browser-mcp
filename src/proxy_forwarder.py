"""Authenticated proxy forwarding for browser launch proxies."""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
from ssl import SSLContext
from struct import calcsize, error as struct_error, pack, unpack
from typing import Optional
from urllib.parse import urlparse

from debug_logger import debug_logger


def _free_port() -> int:
    """Return a free loopback TCP port for the local proxy listener."""
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(("127.0.0.1", 0))
    free_socket.listen(5)
    port = free_socket.getsockname()[1]
    free_socket.close()
    return port


class AuthenticatedProxyForwarder:
    """Forward local unauthenticated proxy traffic to an authenticated upstream proxy."""

    def __init__(
        self,
        proxy_server: str,
        ssl_context: Optional[SSLContext] = None,
    ) -> None:
        """
        Initialize an authenticated proxy forwarder.

        Args:
            proxy_server (str): Upstream proxy URL with credentials.
            ssl_context (Optional[SSLContext]): SSL context for HTTPS upstream proxies.
        """
        raw_value = proxy_server.strip()
        normalized_value = (
            raw_value if "://" in raw_value else f"http://{raw_value}"
        )
        parsed = urlparse(normalized_value)

        if not parsed.scheme:
            raise ValueError("Proxy URL is missing a scheme")
        if not parsed.hostname:
            raise ValueError("Proxy URL is missing a hostname")
        if parsed.port is None:
            raise ValueError("Proxy URL is missing a port")
        if parsed.username is None or parsed.password is None:
            raise ValueError("Proxy URL must include both username and password")

        self.server: Optional[asyncio.AbstractServer] = None
        self.ssl_context = ssl_context
        self.scheme = parsed.scheme
        self.use_ssl = parsed.scheme == "https"
        self.username = parsed.username
        self.password = parsed.password
        self.fw_host = parsed.hostname
        self.fw_port = parsed.port
        self.host = "127.0.0.1"
        self.port = _free_port()

        if self.scheme.startswith("http"):
            self._proxy_server = f"http://{self.host}:{self.port}"
        else:
            self._proxy_server = f"{self.scheme}://{self.host}:{self.port}"

    @property
    def proxy_server(self) -> str:
        """Return the local proxy address exposed to the browser."""
        return self._proxy_server

    async def start(self) -> None:
        """Start the local authenticated proxy forwarder."""
        if self.server is not None:
            return

        self.server = await asyncio.start_server(
            self.handle_request,
            host=self.host,
            port=self.port,
        )
        await self.server.start_serving()
        debug_logger.log_info(
            "proxy_forwarder",
            "start",
            f"Started {self.scheme} forwarder on {self.proxy_server}",
            {"upstream_host": self.fw_host, "upstream_port": self.fw_port},
        )

    async def close(self) -> None:
        """Stop the local authenticated proxy forwarder."""
        if self.server is None:
            return

        self.server.close()
        await self.server.wait_closed()
        self.server = None
        debug_logger.log_info(
            "proxy_forwarder",
            "close",
            f"Stopped forwarder on {self.proxy_server}",
        )

    async def handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Route an incoming local proxy connection to the scheme-specific handler.

        Args:
            reader (asyncio.StreamReader): Local client reader.
            writer (asyncio.StreamWriter): Local client writer.
        """
        try:
            if self.scheme.startswith("socks"):
                await self._handle_socks_request(reader, writer)
                return

            if self.scheme.startswith("http"):
                await self._handle_http_request(reader, writer)
                return

            raise ValueError(f"Unsupported proxy scheme: {self.scheme}")
        except Exception as error:
            debug_logger.log_error(
                "proxy_forwarder",
                "handle_request",
                error,
                {"scheme": self.scheme},
            )
            await self._close_writer(writer)

    async def _handle_http_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Forward an HTTP CONNECT tunnel through an authenticated upstream HTTP proxy.

        Args:
            reader (asyncio.StreamReader): Local client reader.
            writer (asyncio.StreamWriter): Local client writer.
        """
        max_line_length = 8192
        request_timeout = 5.0
        upstream_connect_timeout = 30.0
        remote_writer: Optional[asyncio.StreamWriter] = None
        pipe_tasks: list[asyncio.Task] = []
        header_lines: list[bytes] = []

        try:
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=request_timeout,
            )
            if not request_line:
                await self._close_writer(writer)
                return
            if len(request_line) > max_line_length:
                await self._write_and_close(
                    writer,
                    b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n",
                )
                return

            request_line_text = request_line.decode("utf-8", errors="ignore")
            parts = request_line_text.split()
            if len(parts) < 3:
                await self._write_and_close(writer, b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            method = parts[0].upper()
            target_host_port = parts[1]
            if method == "CONNECT":
                if ":" not in target_host_port:
                    await self._write_and_close(
                        writer,
                        b"HTTP/1.1 400 Bad Request\r\n\r\n",
                    )
                    return

                host, port_text = target_host_port.rsplit(":", 1)
                try:
                    port = int(port_text)
                except ValueError as error:
                    raise ValueError(f"Invalid target port: {target_host_port}") from error
                if not host or port < 1 or port > 65535:
                    raise ValueError(f"Invalid target endpoint: {target_host_port}")

            while True:
                header = await asyncio.wait_for(
                    reader.readline(),
                    timeout=request_timeout,
                )
                if len(header) > max_line_length:
                    await self._write_and_close(
                        writer,
                        b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n",
                    )
                    return
                if not header or header in {b"\r\n", b"\n"}:
                    break
                header_lines.append(header)

            connection_args = {"host": self.fw_host, "port": self.fw_port}
            if self.use_ssl:
                connection_args["ssl"] = self.ssl_context or ssl.create_default_context()

            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(**connection_args),
                timeout=upstream_connect_timeout,
            )

            credentials = f"{self.username}:{self.password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode("ascii")
            remote_writer.write(request_line)
            for header in header_lines:
                header_text = header.decode("utf-8", errors="ignore").lower()
                if header_text.startswith("proxy-authorization:"):
                    continue
                remote_writer.write(header)
            remote_writer.write(
                f"Proxy-Authorization: Basic {encoded_credentials}\r\n".encode()
            )
            if method == "CONNECT":
                remote_writer.write(b"Proxy-Connection: Keep-Alive\r\n")
            remote_writer.write(b"\r\n")
            await remote_writer.drain()

            if method == "CONNECT":
                response_line = await asyncio.wait_for(
                    remote_reader.readline(),
                    timeout=request_timeout,
                )
                if not response_line:
                    await self._write_and_close(
                        writer,
                        b"HTTP/1.1 502 Bad Gateway\r\n\r\n",
                    )
                    return

                while True:
                    header = await asyncio.wait_for(
                        remote_reader.readline(),
                        timeout=request_timeout,
                    )
                    if not header or header in {b"\r\n", b"\n"}:
                        break

                response_text = response_line.decode("utf-8", errors="ignore")
                if "200" not in response_text:
                    await self._write_and_close(
                        writer,
                        b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n\r\n"
                        b"Upstream proxy rejected the connection\r\n",
                    )
                    return

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

            event = asyncio.Event()
            pipe_tasks = [
                asyncio.create_task(self.pipe(remote_reader, writer, event)),
                asyncio.create_task(self.pipe(reader, remote_writer, event)),
            ]
            await asyncio.gather(*pipe_tasks)
        except asyncio.TimeoutError:
            await self._write_and_close(writer, b"HTTP/1.1 504 Gateway Timeout\r\n\r\n")
        except Exception:
            raise
        finally:
            for task in pipe_tasks:
                if not task.done():
                    task.cancel()
            if pipe_tasks:
                await asyncio.gather(*pipe_tasks, return_exceptions=True)
            if remote_writer is not None:
                await self._close_writer(remote_writer)
            await self._close_writer(writer)

    async def _handle_socks_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Forward a SOCKS5 tunnel through an authenticated upstream SOCKS proxy.

        Args:
            reader (asyncio.StreamReader): Local client reader.
            writer (asyncio.StreamWriter): Local client writer.
        """
        atyp_ipv4 = 0x01
        atyp_dns = 0x03
        atyp_ipv6 = 0x04
        remote_writer: Optional[asyncio.StreamWriter] = None

        async def read_struct(
            stream: asyncio.StreamReader,
            fmt: str,
        ) -> tuple:
            """Read and unpack an exact struct payload from a stream."""
            size = calcsize(fmt)
            data = await stream.readexactly(size)
            try:
                return unpack(fmt, data)
            except struct_error as error:
                raise ValueError(f"Invalid SOCKS packet for format {fmt}") from error

        try:
            version, num_methods = await read_struct(reader, "!BB")
            await reader.readexactly(num_methods)
            writer.write(pack("!BB", version, 0))
            await writer.drain()

            version, cmd, reserved, atyp = await read_struct(reader, "!BBBB")

            if atyp == atyp_ipv4:
                address_payload = await reader.readexactly(4)
            elif atyp == atyp_ipv6:
                address_payload = await reader.readexactly(16)
            elif atyp == atyp_dns:
                hostname_length = (await read_struct(reader, "!B"))[0]
                hostname = await reader.readexactly(hostname_length)
                address_payload = pack("!B", hostname_length) + hostname
            else:
                raise ValueError(f"Unsupported SOCKS address type: {atyp}")

            port_payload = await reader.readexactly(2)

            remote_reader, remote_writer = await asyncio.open_connection(
                host=self.fw_host,
                port=self.fw_port,
            )

            remote_writer.write(pack("!BBB", version, 1, 2))
            await remote_writer.drain()
            _, auth_method = await read_struct(remote_reader, "!BB")

            if auth_method == 2:
                auth_ticket = pack(
                    f"!BB{len(self.username)}sB{len(self.password)}s",
                    1,
                    len(self.username),
                    self.username.encode(),
                    len(self.password),
                    self.password.encode(),
                )
                remote_writer.write(auth_ticket)
                await remote_writer.drain()
                _, auth_result = await read_struct(remote_reader, "!BB")
                if auth_result != 0:
                    raise ValueError(
                        f"SOCKS upstream authentication failed: {auth_result}"
                    )

            remote_writer.write(pack("!BBBB", version, cmd, reserved, atyp))
            remote_writer.write(address_payload)
            remote_writer.write(port_payload)
            await remote_writer.drain()

            event = asyncio.Event()
            await asyncio.gather(
                self.pipe(remote_reader, writer, event),
                self.pipe(reader, remote_writer, event),
            )
        finally:
            if remote_writer is not None:
                await self._close_writer(remote_writer)
            await self._close_writer(writer)

    @staticmethod
    async def pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        event: asyncio.Event,
    ) -> None:
        """
        Relay bytes between two streams until either side closes.

        Args:
            reader (asyncio.StreamReader): Source stream.
            writer (asyncio.StreamWriter): Destination stream.
            event (asyncio.Event): Shared completion signal.
        """
        while not event.is_set():
            try:
                data = await asyncio.wait_for(reader.read(2**16), 1)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        event.set()

    @staticmethod
    async def _write_and_close(
        writer: asyncio.StreamWriter,
        payload: bytes,
    ) -> None:
        """
        Write a terminal response and close a stream.

        Args:
            writer (asyncio.StreamWriter): Stream writer to close.
            payload (bytes): Response payload to write before closing.
        """
        writer.write(payload)
        await writer.drain()
        await AuthenticatedProxyForwarder._close_writer(writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        """
        Close a stream writer if it is still open.

        Args:
            writer (asyncio.StreamWriter): Stream writer to close.
        """
        if writer.is_closing():
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
