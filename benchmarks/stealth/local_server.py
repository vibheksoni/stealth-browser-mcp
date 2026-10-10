"""Loopback HTTP server that serves the local probe pages."""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

PAGES_DIR = Path(__file__).resolve().parent / "pages"


class QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler that keeps request logs off stderr."""

    def log_message(self, format: str, *args) -> None:
        """
        Drop access log lines.

        Args:
            format (str): Log format string
            *args: Format arguments
        """


class LocalPageServer:
    """
    Serves PAGES_DIR on 127.0.0.1 with a random free port.

    Use as a context manager. base_url is valid while the server is running.
    """

    def __init__(self, directory: Path = PAGES_DIR):
        """
        Prepare the server.

        Args:
            directory (Path): Directory to serve
        """
        self.directory = directory
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        """
        Root URL of the running server.

        Returns:
            str: URL such as http://127.0.0.1:54321
        """
        if self._server is None:
            raise RuntimeError("Local page server is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "LocalPageServer":
        """
        Start serving in a background thread.

        Returns:
            LocalPageServer: This server
        """
        handler = partial(QuietHandler, directory=str(self.directory))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        """
        Stop the server.

        Args:
            *exc_info: Exception information from the with block
        """
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
