"""SB-79 regression tests for HTTP Host/Origin, required token, and token-file policy.

Reproduces the security review attacks from REPORT.md (F1, F6, F11, F13, F15)
without printing token values.
"""
from __future__ import annotations

import asyncio
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from stealth_browser_mcp import http_auth


def _run_asgi(app: Any, headers: List[tuple], path: str = "/mcp/", method: str = "POST") -> Tuple[int, bytes]:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(msg: Dict[str, Any]) -> None:
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = 0
    body = b""
    for msg in sent:
        if msg["type"] == "http.response.start":
            status = int(msg["status"])
        elif msg["type"] == "http.response.body":
            body += msg.get("body") or b""
    return status, body


class _Inner:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


class TokenFilePolicyTests(unittest.TestCase):
    """F6: a configured token file that cannot be used must fail closed. F11: mode/symlink."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in (
            http_auth.STEALTH_MCP_TOKEN_ENV,
            http_auth.STEALTH_MCP_TOKEN_FILE_ENV,
            http_auth.LEGACY_HTTP_AUTH_TOKEN_ENV,
            http_auth.LEGACY_MCP_AUTH_TOKEN_ENV,
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_missing_token_file_raises(self) -> None:
        os.environ[http_auth.STEALTH_MCP_TOKEN_FILE_ENV] = "/nonexistent/stealth-mcp-token"
        with self.assertRaises(http_auth.TokenConfigError):
            http_auth.get_mcp_token()

    def test_empty_token_file_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            path = handle.name
        try:
            os.chmod(path, 0o600)
            os.environ[http_auth.STEALTH_MCP_TOKEN_FILE_ENV] = path
            with self.assertRaises(http_auth.TokenConfigError):
                http_auth.get_mcp_token()
        finally:
            os.unlink(path)

    @unittest.skipIf(os.name != "posix", "POSIX file permission bits only")
    def test_group_readable_token_file_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("secret-file-token\n")
            path = handle.name
        try:
            os.chmod(path, 0o640)
            os.environ[http_auth.STEALTH_MCP_TOKEN_FILE_ENV] = path
            with self.assertRaises(http_auth.TokenConfigError):
                http_auth.get_mcp_token()
        finally:
            os.chmod(path, 0o600)
            os.unlink(path)

    def test_owned_symlink_with_safe_mode_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "token"
            real.write_text("file-token-67890\n", encoding="utf-8")
            os.chmod(real, 0o600)
            link = Path(folder) / "link"
            link.symlink_to(real)
            os.environ[http_auth.STEALTH_MCP_TOKEN_FILE_ENV] = str(link)
            self.assertEqual(http_auth.get_mcp_token(), "file-token-67890")

    def test_ensure_http_token_writes_0600_file_and_announces_only_the_path(self) -> None:
        stderr = io.StringIO()
        with mock.patch("stealth_browser_mcp.http_auth.sys.stderr", stderr):
            token = http_auth.ensure_http_token(announce=True)
        path = Path(os.environ[http_auth.STEALTH_MCP_TOKEN_FILE_ENV])
        try:
            text = stderr.getvalue()
            self.assertIn(str(path), text)
            self.assertNotIn(token, text)
            if os.name == "posix":
                mode = path.stat().st_mode
                self.assertEqual(mode & 0o777, 0o600)
            else:
                self.assertTrue(path.is_file())
                self.assertTrue(os.access(path, os.R_OK))
            self.assertEqual(path.read_text(encoding="utf-8").strip(), token)
        finally:
            path.unlink(missing_ok=True)


class HostOriginMiddlewareTests(unittest.TestCase):
    """F1: DNS-rebinding Host/Origin must be rejected on every route."""

    def setUp(self) -> None:
        self.inner = _Inner()
        self.mw = http_auth.HostOriginMiddleware(self.inner)

    def test_rebinding_host_and_origin_rejected(self) -> None:
        status, body = _run_asgi(
            self.mw,
            [
                (b"host", b"rebind.attacker.example:8765"),
                (b"origin", b"http://rebind.attacker.example:8765"),
            ],
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.inner.called)
        self.assertIn(b"Invalid Host or Origin", body)
        self.assertNotIn(b"rebind.attacker.example", body)

    def test_loopback_host_without_origin_passes(self) -> None:
        status, _body = _run_asgi(self.mw, [(b"host", b"127.0.0.1:8000")])
        self.assertEqual(status, 200)
        self.assertTrue(self.inner.called)

    def test_loopback_host_with_any_origin_rejected_by_default(self) -> None:
        status, _body = _run_asgi(
            self.mw,
            [
                (b"host", b"127.0.0.1:8000"),
                (b"origin", b"http://127.0.0.1:8000"),
            ],
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.inner.called)

    def test_allowed_origin_and_allowed_host_accepted(self) -> None:
        mw = http_auth.HostOriginMiddleware(
            self.inner,
            allowed_hosts=["mcp.example.com"],
            allowed_origins=["https://app.example.com"],
        )
        status, _body = _run_asgi(
            mw,
            [
                (b"host", b"mcp.example.com"),
                (b"origin", b"https://app.example.com"),
            ],
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.inner.called)

    def test_missing_host_rejected(self) -> None:
        status, _body = _run_asgi(self.mw, [])
        self.assertEqual(status, 403)
        self.assertFalse(self.inner.called)

    def test_healthz_loopback_needs_no_token(self) -> None:
        status, body = _run_asgi(self.mw, [(b"host", b"localhost:8000")], path="/healthz", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.assertFalse(self.inner.called)

    def test_healthz_hostile_host_rejected(self) -> None:
        status, _body = _run_asgi(
            self.mw,
            [(b"host", b"rebind.attacker.example")],
            path="/healthz",
            method="GET",
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.inner.called)


class StackedMiddlewareTests(unittest.TestCase):
    """Host/Origin is checked before bearer auth; /healthz stays unauthenticated."""

    def setUp(self) -> None:
        self.inner = _Inner()
        self.token = "stacked-test-token"
        self.stack = http_auth.HostOriginMiddleware(
            http_auth.BearerAuthMiddleware(self.inner, expected_token=self.token)
        )

    def test_valid_loopback_request_without_token_is_401_not_200(self) -> None:
        status, _body = _run_asgi(self.stack, [(b"host", b"127.0.0.1")])
        self.assertEqual(status, 401)
        self.assertFalse(self.inner.called)

    def test_rebind_is_403_even_with_a_valid_token(self) -> None:
        status, _body = _run_asgi(
            self.stack,
            [
                (b"host", b"rebind.attacker.example:9000"),
                (b"origin", b"http://rebind.attacker.example:9000"),
                (b"authorization", b"Bearer " + self.token.encode("ascii")),
            ],
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.inner.called)

    def test_loopback_with_valid_token_passes(self) -> None:
        status, body = _run_asgi(
            self.stack,
            [
                (b"host", b"127.0.0.1:8000"),
                (b"authorization", b"Bearer " + self.token.encode("ascii")),
            ],
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.inner.called)
        self.assertEqual(body, b'{"ok":true}')

    def test_healthz_does_not_require_token(self) -> None:
        status, body = _run_asgi(self.stack, [(b"host", b"127.0.0.1")], path="/healthz", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.assertFalse(self.inner.called)


class BearerScopeTests(unittest.TestCase):
    """F15: non-http, non-lifespan scopes are rejected."""

    def test_websocket_scope_is_forbidden(self) -> None:
        inner = _Inner()
        mw = http_auth.BearerAuthMiddleware(inner, expected_token="t")
        sent: List[Dict[str, Any]] = []

        async def receive() -> Dict[str, Any]:
            return {"type": "websocket.connect"}

        async def send(msg: Dict[str, Any]) -> None:
            sent.append(msg)

        asyncio.run(mw({"type": "websocket", "headers": []}, receive, send))
        self.assertTrue(
            any(m.get("status") == 403 or m.get("type") == "websocket.close" for m in sent)
        )
        self.assertFalse(inner.called)


class HostHeaderHelpersTests(unittest.TestCase):
    def test_split_ipv6_and_ipv4(self) -> None:
        self.assertEqual(http_auth.split_host_header("[::1]:8000"), ("::1", "8000"))
        self.assertEqual(http_auth.split_host_header("127.0.0.1:8000"), ("127.0.0.1", "8000"))
        self.assertEqual(http_auth.split_host_header("LOCALHOST"), ("localhost", None))

    def test_parse_csv_list(self) -> None:
        self.assertEqual(
            http_auth.parse_csv_list(["a", "b, c"], ["d,,e"]),
            ["a", "b", "c", "d", "e"],
        )


if __name__ == "__main__":
    unittest.main()
