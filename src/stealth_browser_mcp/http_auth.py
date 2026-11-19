"""HTTP transport security and bearer-token authentication for remote MCP (SB-60).

Handles token retrieval from environment variable STEALTH_MCP_TOKEN or the file
named by STEALTH_MCP_TOKEN_FILE, constant-time token comparison via
hmac.compare_digest, loopback host binding validation, and ASGI/FastMCP auth
middleware.

Security requirements:
- The token is NEVER logged, echoed, or committed.
- Non-loopback host binds are refused without a token.
- Invalid or missing tokens return HTTP 401 with no tool execution.
- Comparison is constant-time to resist timing attacks.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from pathlib import Path
from typing import Any, List, Optional

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from starlette.types import Receive, Scope, Send

STEALTH_MCP_TOKEN_ENV = "STEALTH_MCP_TOKEN"
STEALTH_MCP_TOKEN_FILE_ENV = "STEALTH_MCP_TOKEN_FILE"
LEGACY_HTTP_AUTH_TOKEN_ENV = "STEALTH_BROWSER_MCP_AUTH_TOKEN"
LEGACY_MCP_AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"
HTTP_CONTROL_SCOPE = "browser:control"


def get_mcp_token() -> Optional[str]:
    """Read the configured HTTP bearer token without logging or echoing it.

    Priority:
    1. STEALTH_MCP_TOKEN environment variable.
    2. STEALTH_MCP_TOKEN_FILE file path.
    3. Legacy STEALTH_BROWSER_MCP_AUTH_TOKEN environment variable.
    4. Legacy MCP_AUTH_TOKEN environment variable.

    Returns:
        Optional[str]: Token string, or None if no token is configured.
    """
    token = os.getenv(STEALTH_MCP_TOKEN_ENV, "").strip()
    if token:
        return token

    token_file = os.getenv(STEALTH_MCP_TOKEN_FILE_ENV, "").strip()
    if token_file:
        try:
            path = Path(token_file)
            if path.is_file():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception:
            pass

    for legacy_env in (LEGACY_HTTP_AUTH_TOKEN_ENV, LEGACY_MCP_AUTH_TOKEN_ENV):
        legacy_token = os.getenv(legacy_env, "").strip()
        if legacy_token:
            return legacy_token

    return None


def verify_token(provided_token: Optional[str], expected_token: Optional[str]) -> bool:
    """Compare two tokens in constant time using hmac.compare_digest.

    Args:
        provided_token (Optional[str]): Token received from the client.
        expected_token (Optional[str]): Server-side configured token.

    Returns:
        bool: True if tokens match, False otherwise.
    """
    if not provided_token or not expected_token:
        return False
    return hmac.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


def is_loopback_host(host: str) -> bool:
    """Check whether an HTTP bind host is a local loopback address.

    Args:
        host (str): Host name or IP address.

    Returns:
        bool: True for localhost and loopback IP addresses.
    """
    if not host:
        return False
    clean_host = host.strip().lower()
    if clean_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean_host).is_loopback
    except ValueError:
        return False


def validate_bind(host: str, token: Optional[str] = None) -> None:
    """Validate HTTP bind host and token. Refuse non-loopback host without token.

    Args:
        host (str): Host address to bind to.
        token (Optional[str]): Configured bearer token.

    Raises:
        ValueError: When attempting to bind to a non-loopback host without a token.
    """
    effective_token = token if token is not None else get_mcp_token()
    if not is_loopback_host(host) and not effective_token:
        raise ValueError(
            f"Refusing to bind HTTP transport on non-loopback host '{host}' without an authentication token. "
            "Set STEALTH_MCP_TOKEN or STEALTH_MCP_TOKEN_FILE."
        )


class BearerTokenVerifier(TokenVerifier):
    """Validate HTTP bearer tokens for FastMCP auth using constant-time comparison."""

    def __init__(
        self,
        token: Optional[str] = None,
        required_scopes: Optional[List[str]] = None,
        env_var: Optional[str] = None,
    ):
        """Initialize the verifier.

        Args:
            token (Optional[str]): Expected token, or None to read from config dynamically.
            required_scopes (Optional[List[str]]): Required OAuth scopes.
            env_var (Optional[str]): Environment variable name to read token from if token is None.
        """
        scopes = required_scopes or [HTTP_CONTROL_SCOPE]
        super().__init__(required_scopes=scopes)
        self._token = token
        self._env_var = env_var

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Verify an incoming bearer token.

        Args:
            token (str): Bearer token extracted from the Authorization header.

        Returns:
            Optional[AccessToken]: Token metadata when valid, otherwise None.
        """
        expected = self._token
        if expected is None and self._env_var:
            expected = os.getenv(self._env_var, "").strip() or None
        if expected is None:
            expected = get_mcp_token()

        if not expected or not verify_token(token, expected):
            return None
        return AccessToken(
            token=token,
            client_id="mcp-client",
            scopes=self.required_scopes,
        )


class BearerAuthMiddleware:
    """ASGI middleware that enforces bearer token authentication.

    Rejects missing, wrong, or malformed tokens with HTTP 401 before any tool runs.
    Never logs or echoes the token.
    """

    def __init__(self, app: Any, expected_token: Optional[str] = None):
        self.app = app
        self._expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = (
            self._expected_token
            if self._expected_token is not None
            else get_mcp_token()
        )
        if not expected:
            # No token configured; allow request (e.g. localhost default)
            await self.app(scope, receive, send)
            return

        # Extract Authorization header
        auth_header: Optional[str] = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                try:
                    auth_header = value.decode("utf-8")
                except UnicodeDecodeError:
                    auth_header = None
                break

        if not auth_header or not auth_header.lower().startswith("bearer "):
            await self._send_401(send, "Missing or malformed Authorization header")
            return

        token_part = auth_header[7:].strip()
        if not verify_token(token_part, expected):
            await self._send_401(send, "Invalid authentication token")
            return

        await self.app(scope, receive, send)

    async def _send_401(self, send: Send, description: str) -> None:
        body = b'{"error":"Unauthorized","message":"Authentication required"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer error="invalid_token"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


def create_http_auth_provider(token: Optional[str] = None) -> Optional[TokenVerifier]:
    """Create FastMCP token verifier for HTTP transport.

    Args:
        token (Optional[str]): Explicit token override.

    Returns:
        Optional[TokenVerifier]: Token verifier, or None when no token is configured.
    """
    effective = token if token is not None else get_mcp_token()
    if effective is None:
        return None
    return BearerTokenVerifier(effective)
