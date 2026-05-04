"""HTTP transport security helpers."""

from __future__ import annotations

import hmac
import os
from typing import Optional

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

HTTP_AUTH_TOKEN_ENV = "STEALTH_BROWSER_MCP_AUTH_TOKEN"
LEGACY_HTTP_AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"
HTTP_CONTROL_SCOPE = "browser:control"


class EnvironmentBearerTokenVerifier(TokenVerifier):
    """Validate HTTP bearer tokens against a server-side environment token."""

    def __init__(self, token: str):
        """
        Initialize the verifier.

        Args:
            token (str): Bearer token expected in incoming HTTP requests.
        """
        super().__init__(required_scopes=[HTTP_CONTROL_SCOPE])
        self._token = token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify an incoming bearer token.

        Args:
            token (str): Bearer token extracted from the Authorization header.

        Returns:
            Optional[AccessToken]: Access token metadata when valid, otherwise None.
        """
        if not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="environment-token",
            scopes=[HTTP_CONTROL_SCOPE],
        )


def get_http_auth_token() -> Optional[str]:
    """
    Read the configured HTTP bearer token.

    Returns:
        Optional[str]: Configured token, or None when no token is set.
    """
    for env_name in (HTTP_AUTH_TOKEN_ENV, LEGACY_HTTP_AUTH_TOKEN_ENV):
        token = os.getenv(env_name, "").strip()
        if token:
            return token
    return None


def create_http_auth_provider(token: Optional[str]) -> Optional[TokenVerifier]:
    """
    Create the FastMCP auth provider for HTTP transports.

    Args:
        token (Optional[str]): Configured bearer token.

    Returns:
        Optional[TokenVerifier]: FastMCP token verifier, or None when auth is disabled.
    """
    if token is None:
        return None
    return EnvironmentBearerTokenVerifier(token)
