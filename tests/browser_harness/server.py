"""Loopback HTTP server on 127.0.0.1 for browser fixture testing."""

import http.server
import threading
import urllib.parse
from functools import partial
from pathlib import Path
from typing import Any, Optional, Union

PAGES_DIR = Path(__file__).resolve().parent / "pages"


class QuietFixtureHandler(http.server.SimpleHTTPRequestHandler):
    """Static file and fixture request handler that keeps stdio completely quiet."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress standard logging to protect stdio MCP transport and test output."""

    def do_POST(self) -> None:
        """Handle POST requests, such as mock login submissions setting cookies."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in ("/login", "/login.html", "/api/login"):
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                self.rfile.read(content_length)

            body = (
                b"<!DOCTYPE html>\n"
                b"<html lang=\"en\">\n"
                b"<head>\n"
                b"  <meta charset=\"utf-8\">\n"
                b"  <title>Login Successful</title>\n"
                b"</head>\n"
                b"<body>\n"
                b"  <h1>Login Successful</h1>\n"
                b"  <script>\n"
                b"    localStorage.setItem('session_token', 'secret_fixture_cookie_12345');\n"
                b"    localStorage.setItem('auth_user', 'testadmin');\n"
                b"    localStorage.setItem('is_authenticated', 'true');\n"
                b"  </script>\n"
                b"  <p id=\"login-message\">You are now logged in.</p>\n"
                b"  <a href=\"/login.html\" id=\"back-link\">Back to login page</a>\n"
                b"</body>\n"
                b"</html>\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Set-Cookie",
                "session_token=secret_fixture_cookie_12345; Path=/; Max-Age=86400; SameSite=Lax",
            )
            self.send_header(
                "Set-Cookie",
                "auth_user=testadmin; Path=/; Max-Age=86400; SameSite=Lax",
            )
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


class FixtureServer:
    """
    Loopback HTTP fixture server running on 127.0.0.1 with an ephemeral port.

    Serves fixture pages for browser tests and can be used as a context manager.
    """

    def __init__(
        self,
        directory: Optional[Union[str, Path]] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        """
        Initialize the fixture server.

        Args:
            directory: Directory containing HTML fixture pages. Defaults to PAGES_DIR.
            host: Interface to bind. Defaults to "127.0.0.1".
            port: Port to bind. Defaults to 0 (ephemeral port assigned by OS).
        """
        self.directory = Path(directory) if directory else PAGES_DIR
        self.host = host
        self.port = port
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        """
        Root URL of the running server.

        Returns:
            str: URL such as http://127.0.0.1:54321
        """
        if self._server is None:
            raise RuntimeError("FixtureServer is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def port_number(self) -> int:
        """Return the bound port number, or 0 if not running."""
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    def url_for(self, page_name: str) -> str:
        """
        Generate full URL for a page name.

        Args:
            page_name: Name of page (e.g. "inputs.html" or "/login.html")

        Returns:
            str: Full URL to the page
        """
        cleaned = page_name.lstrip("/")
        return f"{self.base_url}/{cleaned}"

    def start(self) -> "FixtureServer":
        """
        Start serving fixture pages in a background daemon thread.

        Returns:
            FixtureServer: The started server instance
        """
        if self._server is not None:
            return self

        handler = partial(QuietFixtureHandler, directory=str(self.directory))
        self._server = http.server.ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving and clean up server resources."""
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            finally:
                self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FixtureServer":
        """Start the server on entering context."""
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Stop the server on exiting context."""
        self.stop()
