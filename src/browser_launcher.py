"""Chrome process launch with a patient DevTools handshake and no leaked processes."""

import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import nodriver as uc
import psutil
from nodriver import Browser
from nodriver.core import util as nodriver_util

DEVTOOLS_READY_TIMEOUT = 30.0
DEVTOOLS_POLL_INTERVAL = 0.05
PROCESS_EXIT_TIMEOUT = 3.0
ACTIVE_PORT_FILE = "DevToolsActivePort"


def _is_listening(pid: int, port: int) -> bool:
    """
    Check whether a process or its children listen on a TCP port.

    Inspecting the process avoids probing a closed port, which takes about a
    second to be refused on Windows.

    Args:
        pid (int): Browser process id.
        port (int): DevTools port.

    Returns:
        bool: True once the port is in LISTEN state.
    """
    try:
        root = psutil.Process(pid)
        for process in [root, *root.children(recursive=True)]:
            for connection in process.net_connections(kind="tcp"):
                if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port:
                    return True
    except (psutil.Error, OSError):
        return False
    return False


def _read_active_port(profile_dir: Path) -> Optional[int]:
    """
    Read the DevTools port Chrome writes once its debugging server listens.

    Args:
        profile_dir (Path): Browser user data directory.

    Returns:
        Optional[int]: Port number, or None when the file is not written yet.
    """
    try:
        first_line = (profile_dir / ACTIVE_PORT_FILE).read_text(encoding="utf-8").splitlines()[0]
        return int(first_line.strip())
    except (OSError, ValueError, IndexError):
        return None


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """
    Terminate a process, killing it if it does not exit in time.

    Args:
        process (asyncio.subprocess.Process): Process to stop.
    """
    if process.returncode is not None:
        return
    for signal_process in (process.terminate, process.kill):
        try:
            signal_process()
            await asyncio.wait_for(process.wait(), PROCESS_EXIT_TIMEOUT)
            return
        except ProcessLookupError:
            return
        except Exception:
            continue


async def launch_browser(config: uc.Config, timeout: float = DEVTOOLS_READY_TIMEOUT) -> Browser:
    """
    Start Chrome for a nodriver config and connect to it.

    nodriver launches Chrome itself but gives the DevTools endpoint under
    three seconds to come up, which cold starts on CI machines and slow
    disks miss, and it leaves the process running when that happens. It
    also pipes Chrome's output without reading it, so a chatty browser can
    block on a full pipe. Here readiness is detected by watching the
    process listen on its debugging port, its output goes to the null device, startup gets up to `timeout`
    seconds, and a failed start kills the process and removes its temporary
    profile. Port 0 is avoided on purpose, because Chrome treats
    --remote-debugging-port=0 as automation and sets navigator.webdriver.

    Args:
        config (uc.Config): nodriver config describing the launch.
        timeout (float): Seconds to wait for the DevTools endpoint.

    Returns:
        Browser: Connected nodriver browser whose `_process` is the launched Chrome.

    Raises:
        Exception: When Chrome exits early or the endpoint does not come up in time.
    """
    profile_dir = Path(config.user_data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / ACTIVE_PORT_FILE).unlink(missing_ok=True)
    config.host = "127.0.0.1"
    config.port = nodriver_util.free_port()
    process = await asyncio.create_subprocess_exec(
        config.browser_executable_path,
        *config(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        close_fds=sys.platform != "win32",
    )
    try:
        deadline = time.monotonic() + timeout
        while not (
            _read_active_port(profile_dir) == config.port
            or await asyncio.to_thread(_is_listening, process.pid, config.port)
        ):
            if process.returncode is not None:
                raise Exception(f"Browser exited during startup with code {process.returncode}")
            if time.monotonic() >= deadline:
                raise Exception(f"Browser DevTools endpoint did not start within {timeout:.0f}s")
            await asyncio.sleep(DEVTOOLS_POLL_INTERVAL)

        browser = Browser(config)
        await browser.start()
        browser._process = process
        browser._process_pid = process.pid
        return browser
    except BaseException:
        await _stop_process(process)
        if not config.uses_custom_data_dir:
            shutil.rmtree(config.user_data_dir, ignore_errors=True)
        raise
