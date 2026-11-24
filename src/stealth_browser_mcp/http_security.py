"""HTTP transport security helpers (SB-60 forwards to http_auth).

Maintained for backward compatibility. New code should use http_auth.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from fastmcp.server.auth import TokenVerifier

from .http_auth import (
    HTTP_CONTROL_SCOPE,
    LEGACY_HTTP_AUTH_TOKEN_ENV,
    BearerTokenVerifier,
    create_http_auth_provider,
    get_mcp_token as _get_mcp_token,
    is_loopback_host,
    validate_bind,
    verify_token,
)

HTTP_AUTH_TOKEN_ENV = "STEALTH_BROWSER_MCP_AUTH_TOKEN"


def get_http_auth_token(env_var: Optional[str] = None) -> Optional[str]:
    """Read configured auth token, supporting optional env_var override."""
    if env_var:
        val = os.getenv(env_var, "").strip()
        if val:
            return val
    return _get_mcp_token()


class EnvironmentBearerTokenVerifier(BearerTokenVerifier):
    """Validate HTTP bearer tokens for FastMCP auth using constant-time comparison."""

    def __init__(
        self,
        token: Optional[str] = None,
        required_scopes: Optional[List[str]] = None,
        env_var: Optional[str] = None,
    ):
        super().__init__(token=token, required_scopes=required_scopes, env_var=env_var)


__all__ = [
    "EnvironmentBearerTokenVerifier",
    "HTTP_AUTH_TOKEN_ENV",
    "HTTP_CONTROL_SCOPE",
    "LEGACY_HTTP_AUTH_TOKEN_ENV",
    "create_http_auth_provider",
    "get_http_auth_token",
    "is_loopback_host",
    "validate_bind",
    "verify_token",
]
