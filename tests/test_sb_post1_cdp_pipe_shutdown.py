"""SB-POST-1: unit tests for the CDP pipe shutdown path.

These tests exercise the bugfix from the review blocker: on Chrome EOF or an
OSError the reader must (1) remove the read fd from the event loop so an fd at
EOF does not keep the loop spinning, and (2) fail every in-flight ``call()``
with ``CdpPipeError`` so no caller hangs. ``close`` / ``sync_close`` must be
idempotent, and ``PipeBrowser.stop()`` must tear down the shared client.

The tests use a real ``os.pipe()`` pair so the reader callback actually runs
against a live fd; no browser is launched.
"""

from __future__ import annotations

import asyncio
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Optional
from unittest.mock import patch

import nodriver as uc

from stealth_browser_mcp import browser_launcher
from stealth_browser_mcp.browser_launcher import (
    CHILD_CDP_FDS,
    _fd_is_open,
    PipeBrowser,
    launch_browser,
    prepare_child_pipe_launch,
)
from stealth_browser_mcp.cdp_pipe import (
    CdpPipeError,
    PipeCDPClient,
    PipeDescriptors,
    open_chrome_pipe,
)


@unittest.skipIf(
    sys.platform == "win32",
    "asyncio add_reader is not implemented for pipe fds on the Windows ProactorEventLoop",
)
class PipeShutdownTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the reader shutdown path against a real pipe fd."""

    async def asyncSetUp(self) -> None:
        # response_r / response_w are one pipe: the parent writes Chrome's
        # responses here and Chrome reads from fd 4 (mapped to response_w).
        # command_r / command_w are the other pipe: the parent writes commands
        # here (fd 3) and Chrome reads them. The client reads response_r and
        # writes commands to command_w.
        #
        # Unlike the real child, there is no process reading command_r, so we
        # keep all four fds open in the parent. Writes to command_w still
        # succeed (they land in the pipe buffer); closing command_r would make
        # them fail with BrokenPipeError.
        response_r, response_w = os.pipe()
        command_r, command_w = os.pipe()
        self.descriptors = PipeDescriptors(
            command_r=command_r,
            command_w=command_w,
            response_r=response_r,
            response_w=response_w,
        )

        self.loop = asyncio.get_running_loop()
        self.client = PipeCDPClient(
            self.descriptors.response_r,
            self.descriptors.command_w,
            loop=self.loop,
        )
        await self.client.start()

    async def asyncTearDown(self) -> None:
        # Unregister and close the fds without raising in cleanup.
        try:
            self.loop.remove_reader(self.descriptors.response_r)
        except (RuntimeError, ValueError, FileNotFoundError):
            pass
        for fd in (
            self.descriptors.response_r,
            self.descriptors.response_w,
            self.descriptors.command_r,
            self.descriptors.command_w,
        ):
            try:
                os.close(fd)
            except OSError:
                pass

    async def _register_pending(self) -> asyncio.Future:
        """Start a call() and wait until it is registered in _pending."""
        future = asyncio.ensure_future(self.client.call("Browser.getVersion"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self.client._pending:
                break
        self.assertTrue(
            self.client._pending, "pending call should be registered in _pending"
        )
        return future

    async def test_eof_fails_pending_calls_and_removes_reader(self) -> None:
        future = await self._register_pending()

        # Simulate Chrome EOF: close the write end of the response pipe so the
        # parent reader sees b"".
        os.close(self.descriptors.response_w)

        # Wait for the loop's reader callback to run the shutdown path.
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if self.client._closed and not self.client._pending:
                break
            await asyncio.sleep(0.01)

        self.assertTrue(
            self.client._closed, "client should be marked closed on EOF"
        )
        self.assertFalse(
            self.client._pending, "pending futures should be cleared on EOF"
        )

        # The read fd must no longer be registered (no loop spin on EOF).
        self.assertNotIn(
            self.descriptors.response_r,
            self.loop._selector.get_map(),
            "read fd must be unregistered from the selector on EOF",
        )

        # The pending call must resolve with CdpPipeError, not hang.
        with self.assertRaises(CdpPipeError):
            await asyncio.wait_for(future, 5.0)

    async def test_oserror_fails_pending_calls(self) -> None:
        future = await self._register_pending()

        # Wake the registered reader and inject OSError from os.read so the
        # shutdown path runs without blocking the loop on a still-open pipe.
        orig_read = os.read

        def _read(fd, n, *args, **kwargs):
            if fd == self.client._read_fd:
                raise OSError("injected")
            return orig_read(fd, n, *args, **kwargs)

        os.read = _read  # type: ignore[assignment]
        try:
            os.write(self.descriptors.response_w, b"x")
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if self.client._closed:
                    break
                await asyncio.sleep(0.01)
        finally:
            os.read = orig_read  # type: ignore[assignment]

        self.assertTrue(self.client._closed, "client should be closed on OSError")
        self.assertFalse(self.client._pending, "pending futures should be cleared")

        with self.assertRaises(CdpPipeError):
            await asyncio.wait_for(future, 5.0)

    async def test_close_is_idempotent(self) -> None:
        await self.client.close()
        self.assertTrue(self.client._closed)
        # Second close must not raise (fds already closed -> OSError swallowed).
        await self.client.close()
        self.assertTrue(self.client._closed)

    async def test_sync_close_is_idempotent(self) -> None:
        self.client.sync_close()
        self.assertTrue(self.client._closed)
        self.client.sync_close()
        self.assertTrue(self.client._closed)

    async def test_stop_closes_client(self) -> None:
        descriptors = PipeDescriptors(
            command_r=os.pipe()[0],
            command_w=os.pipe()[1],
            response_r=self.descriptors.response_r,
            response_w=self.descriptors.response_w,
        )
        import nodriver as uc

        config = uc.Config(headless=True, user_data_dir="/tmp")
        browser = PipeBrowser(config, descriptors)
        browser._client = self.client
        await self.client.start()
        self.assertFalse(self.client._closed)
        browser.stop()
        self.assertTrue(self.client._closed)


# A fake Chrome child: it maps the pipe ends onto fds 3 and 4 via preexec_fn
# and proves they survive exec with a round-trip. The child runs in a fresh
# interpreter and only reads fd 3 and writes fd 4 without modifying those fds.
FAKE_CHILD = r'''
import os, sys

sys.stdout.write("CHILD_OCCUPIED\n")
sys.stdout.flush()

try:
    data = os.read(3, 256)
except OSError as e:
    sys.stdout.write("READ3_FAILED:%s\n" % e)
    sys.stdout.flush()
    sys.exit(1)

try:
    os.write(4, b"reply:" + data)
except OSError as e:
    sys.stdout.write("WRITE4_FAILED:%s\n" % e)
    sys.stdout.flush()
    sys.exit(1)

sys.stdout.write("READ3_OK=%r\n" % data)
sys.stdout.flush()
'''


class PipeChildFdSurvivalTests(unittest.TestCase):
    """Fake-child test that fds 3/4 survive exec with close_fds=True.

    Mirrors the real launch: occupy the child CDP fds in the parent (if not
    already open), then spawn with the same pass_fds/dummy/map_child_fds()
    helper the production spawn uses and confirm the child can read fd 3
    and write fd 4. No browser is launched.
    """

    def _spawn_fake_child(self, descriptors: PipeDescriptors) -> "subprocess.Popen":
        pass_fds, preexec_fn = prepare_child_pipe_launch(descriptors)
        return subprocess.Popen(
            [sys.executable, "-c", FAKE_CHILD],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            pass_fds=pass_fds,
            preexec_fn=preexec_fn,
        )

    @unittest.skipIf(
        sys.platform == "win32",
        "preexec_fn and pass_fds are Unix-only",
    )
    def test_child_fds_survive_exec(self) -> None:
        for child_fd in CHILD_CDP_FDS:
            if not _fd_is_open(child_fd):
                dummy = os.open(os.devnull, os.O_RDONLY)
                os.dup2(dummy, child_fd)
                os.close(dummy)
        descriptors = PipeDescriptors(*os.pipe(), *os.pipe())
        proc = self._spawn_fake_child(descriptors)
        descriptors.close_parent_unused()
        try:
            self.assertEqual(proc.poll(), None, "fake child exited early")
            # Header proves the child is up before it touches the mapped fds.
            self.assertTrue(
                proc.stdout.readline().startswith(b"CHILD_OCCUPIED"),
                "fake child did not reach its occupied-fd read",
            )
            # Write a command; the child should have read fd 3 and echoed it.
            os.write(descriptors.command_w, b"PING")
            r, _, _ = select.select([descriptors.response_r], [], [], 3.0)
            self.assertTrue(r, "child did not write fd 4")
            reply = os.read(descriptors.response_r, 256)
            self.assertEqual(reply, b"reply:PING")
            rest = proc.stdout.read().decode()
            self.assertIn("READ3_OK=b'PING'", rest,
                          "child fd 3 did not receive the command: %r" % rest)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            for fd in (
                descriptors.command_r,
                descriptors.command_w,
                descriptors.response_r,
                descriptors.response_w,
            ):
                try:
                    os.close(fd)
                except OSError:
                    pass


class DistinctPipeBrowserError(Exception):
    """Sentinel exception to verify original exception surfaces on PipeBrowser failure."""


class FakeChildProcess:
    """Asyncio subprocess stub to verify process termination and reaping without Chrome."""

    def __init__(self, pid: int = 42424):
        self.pid = pid
        self.returncode: Optional[int] = None
        self.terminated = False
        self.killed = False
        self.reaped = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.reaped = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self) -> Optional[int]:
        return self.returncode


class PipeBrowserEarlyFailureTests(unittest.IsolatedAsyncioTestCase):
    """SB-POST-2: verify no NameError, no fd leak, and child reaped if PipeBrowser fails."""

    async def test_pipe_browser_init_failure_cleans_up_and_reraises(self) -> None:
        orig_transport = os.environ.get("STEALTH_CDP_TRANSPORT")
        os.environ["STEALTH_CDP_TRANSPORT"] = "pipe"
        self.addCleanup(
            lambda: os.environ.pop("STEALTH_CDP_TRANSPORT", None)
            if orig_transport is None
            else os.environ.__setitem__("STEALTH_CDP_TRANSPORT", orig_transport)
        )

        config = uc.Config(
            headless=True,
            browser_executable_path="/bin/true",
        )
        temp_dir = config.user_data_dir
        self.assertFalse(config.uses_custom_data_dir)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))

        fake_proc = FakeChildProcess()
        captured_descriptors: list = []
        real_open_pipe = open_chrome_pipe

        def capturing_open_pipe():
            descs = real_open_pipe()
            captured_descriptors.append(descs)
            return descs

        async def fake_subprocess_exec(*args, **kwargs):
            return fake_proc

        with patch.object(
            browser_launcher, "open_chrome_pipe", side_effect=capturing_open_pipe
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec
        ), patch.object(
            browser_launcher,
            "PipeBrowser",
            side_effect=DistinctPipeBrowserError("synthetic PipeBrowser init failure"),
        ):
            with self.assertRaises(DistinctPipeBrowserError) as cm:
                await launch_browser(config, timeout=5.0)

        # 1. Assert the original exception surfaced, not NameError or UnboundLocalError
        self.assertIsInstance(cm.exception, DistinctPipeBrowserError)
        self.assertFalse(issubclass(type(cm.exception), NameError))

        # 2. Assert no leaked pipe fds (the descriptor fds are closed)
        self.assertEqual(len(captured_descriptors), 1, "Descriptors should be captured")
        descs = captured_descriptors[0]
        for fd in (descs.command_r, descs.command_w, descs.response_r, descs.response_w):
            self.assertFalse(
                _fd_is_open(fd), f"Descriptor fd {fd} must be closed after failure"
            )

        # 3. Assert fake process was stopped (terminate/kill or returncode set) and reaped
        self.assertTrue(
            fake_proc.terminated or fake_proc.killed or fake_proc.returncode is not None,
            "Process must be terminated or killed",
        )
        self.assertTrue(fake_proc.reaped, "Process wait() must be called to reap it")
        self.assertIsNotNone(fake_proc.returncode, "Process returncode must be set")

        # 4. Assert temporary user_data_dir was cleaned up when uses_custom_data_dir is False
        self.assertFalse(
            os.path.exists(temp_dir),
            "Temporary user_data_dir should be removed on failure when uses_custom_data_dir is False",
        )

    @unittest.skipUnless(sys.platform != "win32", "preexec_fn and pass_fds are Unix-only")
    async def test_pipe_browser_init_failure_with_child_process(self) -> None:
        orig_transport = os.environ.get("STEALTH_CDP_TRANSPORT")
        os.environ["STEALTH_CDP_TRANSPORT"] = "pipe"
        self.addCleanup(
            lambda: os.environ.pop("STEALTH_CDP_TRANSPORT", None)
            if orig_transport is None
            else os.environ.__setitem__("STEALTH_CDP_TRANSPORT", orig_transport)
        )

        config = uc.Config(
            headless=True,
            browser_executable_path=sys.executable,
        )
        temp_dir = config.user_data_dir
        self.assertFalse(config.uses_custom_data_dir)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))

        captured_descriptors: list = []
        real_open_pipe = open_chrome_pipe

        def capturing_open_pipe():
            descs = real_open_pipe()
            captured_descriptors.append(descs)
            return descs

        real_create_subprocess = asyncio.create_subprocess_exec
        spawned_procs: list = []

        async def stubbed_subprocess_exec(*args, **kwargs):
            proc = await real_create_subprocess(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                close_fds=kwargs.get("close_fds", True),
                pass_fds=kwargs.get("pass_fds", ()),
                preexec_fn=kwargs.get("preexec_fn", None),
            )
            spawned_procs.append(proc)
            return proc

        with patch.object(
            browser_launcher, "open_chrome_pipe", side_effect=capturing_open_pipe
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=stubbed_subprocess_exec
        ), patch.object(
            browser_launcher,
            "PipeBrowser",
            side_effect=DistinctPipeBrowserError("synthetic PipeBrowser init failure"),
        ):
            with self.assertRaises(DistinctPipeBrowserError) as cm:
                await launch_browser(config, timeout=5.0)

        self.assertIsInstance(cm.exception, DistinctPipeBrowserError)
        self.assertFalse(issubclass(type(cm.exception), NameError))
        self.assertEqual(len(spawned_procs), 1)
        child_proc = spawned_procs[0]
        self.assertIsNotNone(child_proc.returncode, "Child process must have exited and been reaped")

        self.assertEqual(len(captured_descriptors), 1)
        descs = captured_descriptors[0]
        for fd in (descs.command_r, descs.command_w, descs.response_r, descs.response_w):
            self.assertFalse(
                _fd_is_open(fd), f"Descriptor fd {fd} must be closed after failure"
            )

        self.assertFalse(os.path.exists(temp_dir))


if __name__ == "__main__":
    unittest.main()
