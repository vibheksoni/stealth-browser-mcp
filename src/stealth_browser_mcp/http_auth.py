"""HTTP transport security and bearer-token authentication for remote MCP (SB-60).

Handles token retrieval from environment variable STEALTH_MCP_TOKEN or the file
named by STEALTH_MCP_TOKEN_FILE, constant-time token comparison via
hmac.compare_digest, loopback host binding validation, and ASGI/FastMCP auth
middleware.

Security requirements (SB-79 / review F1, F6, F11, F13, F15):
- The token is NEVER logged, echoed, or committed.
- HTTP transport always requires a token. If none is configured, one is
  written to a 0600 file and only the path is printed to stderr.
- If STEALTH_MCP_TOKEN_FILE is set but missing, empty, unreadable, a
  symlink not owned by the current user, or group/other-readable, startup
  exits with an error (fail closed).
- Invalid or missing tokens return HTTP 401 with no tool execution.
- Comparison is constant-time to resist timing attacks.
- Every HTTP request must present a loopback Host (or --allowed-host).
  An Origin header is rejected unless it is in --allowed-origin
  (default allowlist is empty).
- Non-http, non-lifespan ASGI scopes are rejected.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple
from contextvars import ContextVar

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from starlette.types import Receive, Scope, Send

STEALTH_MCP_TOKEN_ENV = "STEALTH_MCP_TOKEN"
STEALTH_MCP_TOKEN_FILE_ENV = "STEALTH_MCP_TOKEN_FILE"
LEGACY_HTTP_AUTH_TOKEN_ENV = "STEALTH_BROWSER_MCP_AUTH_TOKEN"
LEGACY_MCP_AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"
HTTP_CONTROL_SCOPE = "browser:control"


_REQUEST_FORWARDED: ContextVar[bool] = ContextVar("_REQUEST_FORWARDED", default=False)
_GENERATED_TOKEN_FILES: List[Path] = []

class TokenConfigError(ValueError):
    """Raised when a configured token file cannot be used safely."""


def get_mcp_token() -> Optional[str]:
    """Read the configured HTTP bearer token without logging or echoing it.

    Priority:
    1. STEALTH_MCP_TOKEN environment variable.
    2. STEALTH_MCP_TOKEN_FILE file path.
    3. Legacy STEALTH_BROWSER_MCP_AUTH_TOKEN environment variable.
    4. Legacy MCP_AUTH_TOKEN environment variable.

    Returns:
        Optional[str]: Token string, or None if no token is configured.

    Raises:
        TokenConfigError: If STEALTH_MCP_TOKEN_FILE is set but missing, empty,
            unreadable, group/other-accessible, or a symlink not owned by us.
    """
    token = os.getenv(STEALTH_MCP_TOKEN_ENV, "").strip()
    if token:
        return token

    token_file = os.getenv(STEALTH_MCP_TOKEN_FILE_ENV, "").strip()
    if token_file:
        return _read_token_file(Path(token_file))

    for legacy_env in (LEGACY_HTTP_AUTH_TOKEN_ENV, LEGACY_MCP_AUTH_TOKEN_ENV):
        legacy_token = os.getenv(legacy_env, "").strip()
        if legacy_token:
            return legacy_token

    return None


def _read_token_file(path: Path) -> str:
    """Return the token stored in ``path``, or raise TokenConfigError."""
    try:
        lstat = path.lstat()
    except FileNotFoundError:
        raise TokenConfigError(
            "STEALTH_MCP_TOKEN_FILE is set but the file does not exist. "
            "HTTP startup refuses to run without a token."
        ) from None
    except OSError as exc:
        raise TokenConfigError(
            "STEALTH_MCP_TOKEN_FILE is set but the file cannot be read."
        ) from exc

    if stat.S_ISLNK(lstat.st_mode):
        if lstat.st_uid != os.getuid():
            raise TokenConfigError(
                "STEALTH_MCP_TOKEN_FILE is a symlink not owned by the current user."
            )
        try:
            lstat = path.stat()
        except OSError as exc:
            raise TokenConfigError(
                "STEALTH_MCP_TOKEN_FILE symlink could not be followed."
            ) from exc

    if not stat.S_ISREG(lstat.st_mode):
        raise TokenConfigError("STEALTH_MCP_TOKEN_FILE must be a regular file.")
    if lstat.st_mode & 0o077:
        raise TokenConfigError(
            "STEALTH_MCP_TOKEN_FILE must not be group- or world-accessible (use mode 0600)."
        )
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenConfigError(
            "STEALTH_MCP_TOKEN_FILE is set but the file cannot be read."
        ) from exc
    if not content:
        raise TokenConfigError("STEALTH_MCP_TOKEN_FILE is set but the file is empty.")
    return content


def _token_directory() -> Path:
    runtime = os.getenv("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        path = Path(runtime)
        if path.is_dir() and os.access(path, os.W_OK):
            return path
    return Path(tempfile.gettempdir())


def write_generated_token_file(directory: Optional[Path] = None) -> Tuple[str, Path]:
    """Write a new 0600 token file and return (token, path). Never logs the value."""
    directory = Path(directory) if directory is not None else _token_directory()
    directory.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="stealth-mcp-token-", dir=str(directory), text=True)
    path = Path(raw_path)
    token = secrets.token_urlsafe(32)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _GENERATED_TOKEN_FILES.append(path)
    return token, path


def announce_token_path(path: Path) -> None:
    """Print only the token file path to stderr (never the value)."""
    sys.stderr.write(
        "Stealth Browser MCP HTTP token written to {} (mode 0600). "
        "Pass Authorization: Bearer with the file contents. "
        "The token value is not printed.\n".format(path)
    )
    sys.stderr.flush()


def cleanup_generated_token_files() -> None:
    """Unlink token files this process generated (not operator-supplied files)."""
    while _GENERATED_TOKEN_FILES:
        path = _GENERATED_TOKEN_FILES.pop()
        try:
            path.unlink()
        except OSError:
            pass


def request_appears_proxied() -> bool:
    """True when the current HTTP request carries X-Forwarded-For or Forwarded."""
    return bool(_REQUEST_FORWARDED.get())


def ensure_http_token(announce: bool = True) -> str:
    """Return a token for HTTP, generating a 0600 file if none is configured.

    Raises:
        TokenConfigError: If a configured token file cannot be used.
    """
    existing = get_mcp_token()
    if existing:
        return existing
    token, path = write_generated_token_file()
    os.environ[STEALTH_MCP_TOKEN_FILE_ENV] = str(path)
    if announce:
        announce_token_path(path)
    return token


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
    """Validate HTTP bind host and token. HTTP always requires a token.

    Args:
        host (str): Host address to bind to.
        token (Optional[str]): Configured bearer token.

    Raises:
        ValueError: When no token is configured, or when binding to a
            non-loopback host without a token (defensive; HTTP startup
            generates a token before calling this).
    """
    effective_token = token if token is not None else get_mcp_token()
    if not effective_token:
        raise ValueError(
            "HTTP transport requires an authentication token. "
            "Set STEALTH_MCP_TOKEN or STEALTH_MCP_TOKEN_FILE, or let the server "
            "generate a 0600 token file."
        )
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
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _reject_unsupported_protocol(scope, send)
            return

        expected = (
            self._expected_token
            if self._expected_token is not None
            else get_mcp_token()
        )
        if not expected:
            # HTTP always requires a token (SB-79). Fail closed if startup
            # somehow left us without one.
            await self._send_401(send, "Authentication required")
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


class McpPathRewriteMiddleware:
    """ASGI middleware that rewrites scope['path'] from /mcp to /mcp/ (and raw_path).

    Placed inside the auth middleware to ensure unauthenticated requests are
    rejected with 401 first, while allowing authenticated clients to post to
    either /mcp or /mcp/ without triggering a Starlette 307 redirect.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = scope.get("path")
            if path == "/mcp":
                scope["path"] = "/mcp/"
                raw_path = scope.get("raw_path")
                if raw_path == b"/mcp":
                    scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)



def split_host_header(host: str) -> Tuple[str, Optional[str]]:
    """Split a Host header into (hostname, port). Hostname is lowercased."""
    text = (host or "").strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return text, None
        name = text[1:end]
        rest = text[end + 1:]
        port = rest[1:] if rest.startswith(":") else None
        return name, port
    if text.count(":") == 1:
        name, port = text.rsplit(":", 1)
        if port.isdigit():
            return name, port
    return text, None


def _is_loopback_name(name: str) -> bool:
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def host_header_allowed(host_header: str, extra_allowed: Sequence[str] = ()) -> bool:
    """True if the Host header is loopback or listed in extra_allowed (no DNS)."""
    name, port = split_host_header(host_header)
    if not name:
        return False
    if _is_loopback_name(name):
        return True
    presented = name if port is None else "{}:{}".format(name, port)
    for raw in extra_allowed:
        allowed = (raw or "").strip().lower()
        if not allowed:
            continue
        allowed_name, allowed_port = split_host_header(allowed)
        if allowed_port:
            if presented == "{}:{}".format(allowed_name, allowed_port):
                return True
        elif name == allowed_name:
            return True
    return False


def origin_allowed(origin: str, allowed_origins: Sequence[str]) -> bool:
    """True if Origin exactly matches an allowlist entry (case-insensitive host)."""
    value = (origin or "").strip()
    if not value or not allowed_origins:
        return False
    lowered = value.lower()
    for raw in allowed_origins:
        if (raw or "").strip().lower() == lowered:
            return True
    return False


def parse_csv_list(*groups: Iterable[str]) -> List[str]:
    """Flatten CLI repeats and comma-separated env values, dropping empties."""
    items: List[str] = []
    for group in groups:
        for raw in group:
            for part in (raw or "").split(","):
                item = part.strip()
                if item:
                    items.append(item)
    return items


async def _reject_unsupported_protocol(scope: Scope, send: Send) -> None:
    """Reject a non-HTTP scope without using http.response (avoids a 500 on websocket)."""
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1008, "reason": "Unsupported protocol"})
        return
    await _reject_unsupported_protocol(scope, send)


async def _send_status(send: Send, status: int, body: bytes) -> None:
    if status == 401:
        www = b'Bearer error="invalid_token"'
    else:
        www = b""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if www:
        headers.append((b"www-authenticate", www))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


HEALTHZ_PATHS = ("/healthz", "/healthz/")


class HostOriginMiddleware:
    """Reject requests whose Host is not loopback/--allowed-host, or whose Origin is not allowlisted.

    Default Origin allowlist is empty: a request that carries any Origin is
    rejected unless --allowed-origin / STEALTH_MCP_ALLOWED_ORIGINS lists it.
    Requests with no Origin (typical agent HTTP clients) pass the Origin check.
    GET /healthz is served here after the Host check and does not require a token.
    """

    def __init__(
        self,
        app: Any,
        allowed_hosts: Optional[Sequence[str]] = None,
        allowed_origins: Optional[Sequence[str]] = None,
    ):
        self.app = app
        self.allowed_hosts = list(allowed_hosts or ())
        self.allowed_origins = list(allowed_origins or ())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await _reject_unsupported_protocol(scope, send)
            return

        header_map = {}
        for key, value in scope.get("headers", []):
            if key.lower() not in header_map:
                header_map[key.lower()] = value
        forwarded = b"x-forwarded-for" in header_map or b"forwarded" in header_map
        token = _REQUEST_FORWARDED.set(bool(forwarded))
        try:
            await self._enforce_http(scope, receive, send, header_map)
        finally:
            _REQUEST_FORWARDED.reset(token)

    async def _enforce_http(self, scope: Scope, receive: Receive, send: Send, header_map) -> None:
        host_raw = header_map.get(b"host", b"")
        try:
            host = host_raw.decode("ascii", "replace") if host_raw else ""
        except Exception:
            host = ""
        if not host_header_allowed(host, self.allowed_hosts):
            await _send_status(send, 403, b'{"error":"Forbidden","message":"Invalid Host or Origin"}')
            return

        origin_raw = header_map.get(b"origin")
        if origin_raw is not None:
            try:
                origin = origin_raw.decode("utf-8")
            except UnicodeDecodeError:
                origin = ""
            if not origin_allowed(origin, self.allowed_origins):
                await _send_status(send, 403, b'{"error":"Forbidden","message":"Invalid Host or Origin"}')
                return

        path = scope.get("path") or ""
        if path in HEALTHZ_PATHS and (scope.get("method") or "GET").upper() == "GET":
            await _send_status(send, 200, b'{"ok":true}')
            return

        await self.app(scope, receive, send)


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
