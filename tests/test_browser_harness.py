"""Tests for the browser fixture harness (ungated unit tests and gated browser suite)."""

import asyncio
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from typing import Any, Optional

from tests.browser_harness.server import PAGES_DIR, FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _safe_cleanup_dir(dir_path: Optional[str]) -> None:
    """Safely remove a temporary directory with retry and ignore_errors fallback."""
    if not dir_path or not os.path.exists(dir_path):
        return
    for attempt in range(10):
        try:
            shutil.rmtree(dir_path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass


class TestFixtureServerUngated(unittest.TestCase):
    """Ungated unit tests verifying the local fixture server serves pages on 127.0.0.1 without network."""

    def test_server_starts_and_stops_on_loopback(self) -> None:
        """Verify the server binds to 127.0.0.1 on an ephemeral port and stops cleanly."""
        server = FixtureServer()
        self.assertEqual(server.host, "127.0.0.1")
        self.assertEqual(server.port_number, 0)
        server.start()
        try:
            self.assertTrue(server.base_url.startswith("http://127.0.0.1:"))
            self.assertGreater(server.port_number, 0)
            url = server.url_for("index.html")
            with urllib.request.urlopen(url, timeout=5.0) as response:
                self.assertEqual(response.status, 200)
                content = response.read().decode("utf-8")
                self.assertIn("Browser Fixture Harness Pages", content)
        finally:
            server.stop()
            self.assertEqual(server.port_number, 0)

    def test_context_manager(self) -> None:
        """Verify the server works as a context manager."""
        with FixtureServer() as server:
            self.assertTrue(server.base_url.startswith("http://127.0.0.1:"))
            url = server.url_for("inputs.html")
            with urllib.request.urlopen(url, timeout=5.0) as response:
                self.assertEqual(response.status, 200)

    def test_serves_all_fixture_pages(self) -> None:
        """Verify all required fixture pages are served with HTTP 200 and expected markup."""
        expected_pages = {
            "index.html": "Browser Fixture Harness",
            "inputs.html": "Plain Inputs Fixture",
            "react_controlled.html": "React-Controlled Input Fixture",
            "contenteditable.html": "Contenteditable Fixture",
            "shadow_dom.html": "Shadow DOM Fixture",
            "iframe.html": "Same-Origin Iframe Fixture",
            "iframe_content.html": "Inside Same-Origin Frame",
            "dates.html": "Date and Time Inputs Fixture",
            "prosemirror.html": "ProseMirror-like Editor Fixture",
            "login.html": "Login Fixture Page",
            "react_select.html": "React-Select-like Combobox Fixture",
            "aria_combobox.html": "ARIA Combobox and Listbox Fixture",
            "download.html": "Download and Drop Fixture",
        }

        with FixtureServer() as server:
            for page_name, expected_text in expected_pages.items():
                url = server.url_for(page_name)
                with urllib.request.urlopen(url, timeout=5.0) as response:
                    self.assertEqual(
                        response.status,
                        200,
                        f"Page {page_name} failed to return HTTP 200",
                    )
                    content_type = response.headers.get("Content-Type", "")
                    self.assertIn("text/html", content_type)
                    body = response.read().decode("utf-8")
                    self.assertIn(
                        expected_text,
                        body,
                        f"Page {page_name} missing expected content marker: {expected_text}",
                    )

    def test_url_for_strips_leading_slashes(self) -> None:
        """Verify url_for handles relative names with and without leading slash."""
        with FixtureServer() as server:
            self.assertEqual(server.url_for("/inputs.html"), server.url_for("inputs.html"))

    def test_login_post_endpoint_sets_cookie_and_storage(self) -> None:
        """Verify POST /login returns Set-Cookie header and sets storage script."""
        with FixtureServer() as server:
            url = server.url_for("login")
            post_data = urllib.parse.urlencode({"username": "testadmin", "password": "secretpass"}).encode("utf-8")
            req = urllib.request.Request(url, data=post_data, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as response:
                self.assertEqual(response.status, 200)
                set_cookie = response.headers.get("Set-Cookie", "")
                self.assertIn("session_token=secret_fixture_cookie_12345", set_cookie)
                body = response.read().decode("utf-8")
                self.assertIn("Login Successful", body)
                self.assertIn("localStorage.setItem('session_token'", body)

    def test_download_endpoint_returns_attachment(self) -> None:
        """Verify GET /download returns expected attachment headers and body."""
        with FixtureServer() as server:
            url = server.url_for("download?filename=custom_sample.csv")
            with urllib.request.urlopen(url, timeout=5.0) as response:
                self.assertEqual(response.status, 200)
                content_type = response.headers.get("Content-Type", "")
                self.assertIn("application/octet-stream", content_type)
                disp = response.headers.get("Content-Disposition", "")
                self.assertIn('attachment; filename="custom_sample.csv"', disp)
                body = response.read()
                self.assertIn(b"Stealth browser fixture download content", body)

    def test_pages_files_are_ascii_only(self) -> None:
        """Verify that all fixture files in PAGES_DIR contain valid ASCII characters only."""
        self.assertTrue(PAGES_DIR.is_dir(), f"PAGES_DIR does not exist: {PAGES_DIR}")
        html_files = list(PAGES_DIR.glob("*.html"))
        self.assertGreaterEqual(len(html_files), 10)
        for html_file in html_files:
            content_bytes = html_file.read_bytes()
            try:
                content_bytes.decode("ascii")
            except UnicodeDecodeError as exc:
                self.fail(f"Non-ASCII character in {html_file.name}: {exc}")


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestBrowserHarnessGated(unittest.TestCase):
    """
    Gated browser integration suite.

    Launches Chrome headless via the project's browser launcher on a temp
    user-data-dir, loads fixture pages, verifies DOM interaction, and tears
    down in under 60 seconds without external network access.
    """

    def test_launch_and_load_fixture_page(self) -> None:
        """Launch Chrome on a temp dir via launch_browser, load a page, and tear down."""
        import nodriver as uc
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )
        from stealth_browser_mcp.process_cleanup import process_cleanup

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        async def run_test() -> None:
            start_time = time.monotonic()
            temp_dir = tempfile.mkdtemp(prefix="stealth_harness_launch_")
            browser: Optional[Any] = None
            try:
                with FixtureServer() as server:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)
                    tab = await browser.get(server.url_for("inputs.html"))
                    title = await tab.evaluate("document.title")
                    self.assertEqual(title, "Inputs Fixture")

                    placeholder = await tab.evaluate(
                        "document.getElementById('text-input').getAttribute('placeholder')"
                    )
                    self.assertEqual(placeholder, "Type text here")
            finally:
                if browser is not None:
                    # 1. Close browser via CDP
                    try:
                        if getattr(browser, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    # 2. Terminate main process
                    if getattr(browser, "_process", None) is not None:
                        try:
                            await _stop_process(browser._process)
                        except Exception:
                            pass
                    # 3. Disconnect open target handlers
                    connections = [
                        getattr(tab, "disconnect", None)
                        for tab in list(getattr(browser, "targets", []) or [])
                    ]
                    if getattr(browser, "connection", None) is not None:
                        connections.append(getattr(browser.connection, "disconnect", None))
                    disconnects = [d() for d in connections if d is not None]
                    if disconnects:
                        try:
                            await asyncio.wait(
                                [asyncio.ensure_future(item) for item in disconnects],
                                timeout=2.0,
                            )
                        except Exception:
                            pass
                    browser._process = None
                    browser._process_pid = None

                # 4. Clean up any lingering processes referencing temp profile
                try:
                    for pid in process_cleanup._get_browser_pids_for_profile(temp_dir):
                        process_cleanup._kill_process_by_pid(pid, "test_harness")
                except Exception:
                    pass

                # 5. Remove temp profile directory with retrying cleanup
                _safe_cleanup_dir(temp_dir)

            elapsed = time.monotonic() - start_time
            self.assertLess(elapsed, 60.0, f"Gated launch exceeded 60 seconds ({elapsed:.2f}s)")

        asyncio.run(run_test())

    def test_login_page_cookies_and_local_storage(self) -> None:
        """Verify login page sets cookies and localStorage in a real browser session."""
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions
        from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
        from stealth_browser_mcp.process_cleanup import process_cleanup

        async def run_test() -> None:
            start_time = time.monotonic()
            temp_dir = tempfile.mkdtemp(prefix="stealth_harness_login_")
            manager = BrowserManager()
            instance = None
            try:
                with FixtureServer() as server:
                    options = BrowserOptions(
                        headless=True,
                        user_data_dir=temp_dir,
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    instance = await manager.spawn_browser(options)
                    nav_result = await manager.navigate(
                        instance.instance_id,
                        server.url_for("login.html"),
                        wait_until="load",
                        timeout=15000,
                    )
                    self.assertTrue(nav_result.get("success"))

                    tab = await manager.get_navigation_tab(instance.instance_id)

                    status_before = await tab.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(status_before, "logged_out")

                    await tab.evaluate("document.getElementById('login-button').click()")
                    await asyncio.sleep(0.1)

                    status_after = await tab.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(status_after, "logged_in")

                    cookie_text = await tab.evaluate("document.cookie")
                    self.assertIn("session_token=secret_fixture_cookie_12345", cookie_text)

                    storage_token = await tab.evaluate(
                        "localStorage.getItem('session_token')"
                    )
                    self.assertEqual(storage_token, "secret_fixture_cookie_12345")

                    storage_user = await tab.evaluate(
                        "localStorage.getItem('auth_user')"
                    )
                    self.assertEqual(storage_user, "testadmin")

                    await tab.evaluate("document.getElementById('logout-button').click()")
                    await asyncio.sleep(0.1)
                    status_logged_out = await tab.evaluate(
                        "document.getElementById('auth-state').textContent"
                    )
                    self.assertEqual(status_logged_out, "logged_out")
            finally:
                if instance is not None:
                    try:
                        await manager.close_instance(instance.instance_id)
                    except Exception:
                        pass
                try:
                    await manager.close_all()
                except Exception:
                    pass
                try:
                    for pid in process_cleanup._get_browser_pids_for_profile(temp_dir):
                        process_cleanup._kill_process_by_pid(pid, "test_harness")
                except Exception:
                    pass
                _safe_cleanup_dir(temp_dir)

            elapsed = time.monotonic() - start_time
            self.assertLess(elapsed, 60.0, f"Login test exceeded 60 seconds ({elapsed:.2f}s)")

        asyncio.run(run_test())

    def test_react_controlled_input_fixture(self) -> None:
        """Verify the React-controlled input fixture resets direct .value assignment in a real browser."""
        from stealth_browser_mcp.browser_manager import BrowserManager
        from stealth_browser_mcp.models import BrowserOptions
        from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled
        from stealth_browser_mcp.process_cleanup import process_cleanup

        async def run_test() -> None:
            start_time = time.monotonic()
            temp_dir = tempfile.mkdtemp(prefix="stealth_harness_react_")
            manager = BrowserManager()
            instance = None
            try:
                with FixtureServer() as server:
                    options = BrowserOptions(
                        headless=True,
                        user_data_dir=temp_dir,
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    instance = await manager.spawn_browser(options)
                    await manager.navigate(
                        instance.instance_id,
                        server.url_for("react_controlled.html"),
                        wait_until="load",
                        timeout=15000,
                    )
                    tab = await manager.get_navigation_tab(instance.instance_id)

                    initial_state = await tab.evaluate(
                        "document.getElementById('tracked-state').textContent"
                    )
                    self.assertEqual(initial_state, "initial")

                    # Attempt direct programmatic assignment: should be rejected/reverted
                    await tab.evaluate(
                        "document.getElementById('controlled-input').value = 'injected'"
                    )
                    reverted_val = await tab.evaluate(
                        "document.getElementById('controlled-input').value"
                    )
                    self.assertEqual(reverted_val, "initial")

                    # Now use native descriptor setter + dispatch input event (React setter strategy)
                    await tab.evaluate(
                        "(function() {"
                        "  const input = document.getElementById('controlled-input');"
                        "  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'updated_value');"
                        "  input.dispatchEvent(new Event('input', { bubbles: true }));"
                        "})()"
                    )
                    await asyncio.sleep(0.1)
                    updated_state = await tab.evaluate(
                        "document.getElementById('tracked-state').textContent"
                    )
                    self.assertEqual(updated_state, "updated_value")
            finally:
                if instance is not None:
                    try:
                        await manager.close_instance(instance.instance_id)
                    except Exception:
                        pass
                try:
                    await manager.close_all()
                except Exception:
                    pass
                try:
                    for pid in process_cleanup._get_browser_pids_for_profile(temp_dir):
                        process_cleanup._kill_process_by_pid(pid, "test_harness")
                except Exception:
                    pass
                _safe_cleanup_dir(temp_dir)

            elapsed = time.monotonic() - start_time
            self.assertLess(elapsed, 60.0, f"React controlled test exceeded 60 seconds ({elapsed:.2f}s)")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
