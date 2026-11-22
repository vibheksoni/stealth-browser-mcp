"""SB-POST-1: gated real-browser test for CDP over pipe transport.

Gated behind STEALTH_BROWSER_TESTS=1.
Verifies that when STEALTH_CDP_TRANSPORT=pipe:
1. Chrome starts with --remote-debugging-pipe.
2. Chrome does not start with --remote-debugging-port.
3. No LISTEN socket is owned by any process in the Chrome pid tree.
4. Main Chrome process is headless (--headless or --headless=new); aborts immediately if not.
5. Navigation to a data: URL succeeds and document.title is readable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional

import nodriver as uc
import psutil
from nodriver import Browser

from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
from stealth_browser_mcp.browser_manager import check_browser_executable
from stealth_browser_mcp.platform_utils import sandbox_must_be_disabled


RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}


class CdpPipeRealBrowserTests(unittest.IsolatedAsyncioTestCase):
    """Real Chrome tests over inherited CDP pipe."""

    async def asyncSetUp(self) -> None:
        if not RUN_BROWSER_TESTS:
            self.skipTest("STEALTH_BROWSER_TESTS is not set; skipping real browser test")

        # Select and verify the pipe transport and headless mode from inside the
        # test so it is self-contained (still gated on STEALTH_BROWSER_TESTS).
        os.environ["STEALTH_CDP_TRANSPORT"] = "pipe"
        os.environ["STEALTH_WINDOW_MODE"] = "headless"

    async def test_pipe_transport_headless(self) -> None:
        executable = check_browser_executable() or "/usr/bin/google-chrome"
        if not os.path.exists(executable):
            self.skipTest(f"Browser executable not found: {executable}")

        temp_dir = tempfile.mkdtemp(prefix="stealth_pipe_test_")
        browser: Optional[Browser] = None
        process = None

        try:
            config = uc.Config(
                headless=True,
                user_data_dir=temp_dir,
                browser_executable_path=executable,
                sandbox=not sandbox_must_be_disabled(),
            )

            browser = await launch_browser(config, timeout=30.0)
            self.assertIsNotNone(browser)
            process = getattr(browser, "_process", None)
            pid = getattr(browser, "_process_pid", None) or (process.pid if process else None)
            self.assertIsNotNone(pid, "Browser must have an active process PID")

            root_proc = psutil.Process(pid)
            all_procs = [root_proc]
            try:
                all_procs.extend(root_proc.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Identify main process (no --type=)
            main_cmdline = None
            for p in all_procs:
                try:
                    cmd = p.cmdline()
                    if not any(arg.startswith("--type=") for arg in cmd):
                        main_cmdline = cmd
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if main_cmdline is None:
                try:
                    main_cmdline = root_proc.cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    main_cmdline = []

            # Abort immediately if the main Chrome process lacks --headless or --headless=new
            is_headless = any(
                arg in ("--headless", "--headless=new") or arg.startswith("--headless=")
                for arg in main_cmdline
            )
            if not is_headless:
                await _stop_process(process, browser)
                self.fail(f"ABORT: Chrome main process was not headless! cmdline: {main_cmdline}")

            # Assert --remote-debugging-pipe is present
            self.assertIn(
                "--remote-debugging-pipe",
                main_cmdline,
                f"Expected --remote-debugging-pipe in main Chrome cmdline: {main_cmdline}",
            )

            # Assert --remote-debugging-port is absent
            self.assertFalse(
                any(arg.startswith("--remote-debugging-port") for arg in main_cmdline),
                f"Found unexpected --remote-debugging-port in cmdline: {main_cmdline}",
            )

            # Assert there is no LISTEN socket owned by that Chrome pid tree
            for p in all_procs:
                try:
                    for conn in p.net_connections(kind="all"):
                        if conn.status == psutil.CONN_LISTEN:
                            self.fail(
                                f"Found unexpected LISTEN socket on pid {p.pid} ({p.name()}): {conn}"
                            )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Navigate to a data: URL and read document.title
            test_url = "data:text/html,<title>PipeTitle12345</title><h1>Testing Pipe</h1>"
            tab = await browser.get(test_url)
            self.assertIsNotNone(tab)

            title = None
            for _ in range(50):
                title = await tab.evaluate("document.title")
                if title == "PipeTitle12345":
                    break
                await asyncio.sleep(0.1)

            self.assertEqual(title, "PipeTitle12345")

        finally:
            if browser is not None and process is not None:
                await _stop_process(process, browser)
            elif process is not None:
                await _stop_process(process)
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
