"""Runs benchmark targets against real browsers spawned by BrowserManager."""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import nodriver as uc

from browser_manager import BrowserManager
from dom_handler import DOMHandler
from js_values import evaluate_to_python
from models import BrowserOptions

from .environment import base_environment, runner_label
from .local_server import LocalPageServer
from .results import RunResult, Status, TargetResult
from .targets.base import Target, Unreachable, status_from_checks


class BrowserSession:
    """
    Session implementation backed by one BrowserManager instance.

    Attributes:
        manager (BrowserManager): Manager that owns the browser
        instance_id (str): Browser instance in use
        local_base_url (str): Root URL of the local page server
    """

    def __init__(self, manager: BrowserManager, instance_id: str, local_base_url: str):
        """
        Wrap a spawned browser instance.

        Args:
            manager (BrowserManager): Manager that owns the browser
            instance_id (str): Browser instance in use
            local_base_url (str): Root URL of the local page server
        """
        self.manager = manager
        self.instance_id = instance_id
        self.local_base_url = local_base_url

    async def navigate(self, url: str, timeout: float = 45.0) -> None:
        """
        Load a URL through BrowserManager.navigate.

        Args:
            url (str): Page to load
            timeout (float): Navigation timeout in seconds

        Raises:
            Unreachable: When navigation fails or times out
        """
        try:
            await self.manager.navigate(self.instance_id, url, wait_until="load", timeout=int(timeout * 1000))
        except Exception as error:
            raise Unreachable(f"Navigation to {url} failed: {error}") from error

    async def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        """
        Evaluate JavaScript in the current tab.

        Args:
            expression (str): JavaScript expression
            await_promise (bool): Await the result when it is a Promise

        Returns:
            Any: Plain Python value

        Raises:
            RuntimeError: When the script throws or the tab is gone
        """
        tab = await self.manager.get_tab(self.instance_id)
        if tab is None:
            raise RuntimeError(f"Instance not found: {self.instance_id}")
        value, error = await evaluate_to_python(tab, expression, await_promise=await_promise)
        if error:
            raise RuntimeError(error)
        return value

    async def add_init_script(self, source: str) -> str:
        """
        Install an init script through BrowserManager.add_init_script.

        Args:
            source (str): JavaScript source

        Returns:
            str: Script identifier
        """
        return await self.manager.add_init_script(self.instance_id, source)

    async def click(self, selector: str) -> None:
        """
        Click through DOMHandler.click_element.

        Args:
            selector (str): CSS selector
        """
        await DOMHandler.click_element(await self.manager.get_tab(self.instance_id), selector)

    async def type_text(self, selector: str, text: str) -> None:
        """
        Type through DOMHandler.type_text with a short keystroke delay.

        Args:
            selector (str): CSS selector
            text (str): Text to type
        """
        await DOMHandler.type_text(await self.manager.get_tab(self.instance_id), selector, text, delay_ms=20)


def utc_now() -> datetime:
    """
    Current UTC time.

    Returns:
        datetime: Timezone-aware UTC timestamp
    """
    return datetime.now(timezone.utc)


def write_raw_payload(raw_dir: Optional[Path], runner: str, target: str, payload: Any) -> None:
    """
    Save a raw payload for later inspection.

    Args:
        raw_dir (Optional[Path]): Root raw directory, or None to skip
        runner (str): Runner label
        target (str): Target name
        payload (Any): Payload returned by collect()
    """
    if raw_dir is None:
        return
    directory = raw_dir / runner
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{target}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


async def browser_version(manager: BrowserManager, options: BrowserOptions) -> Dict[str, Any]:
    """
    Start a browser once to read its product version and default user agent.

    Args:
        manager (BrowserManager): Browser manager
        options (BrowserOptions): Spawn options used for the run

    Returns:
        Dict[str, Any]: chrome_product, chrome_version, user_agent, and protocol_version
    """
    instance = await manager.spawn_browser(options)
    try:
        tab = await manager.get_tab(instance.instance_id)
        protocol_version, product, _revision, user_agent, _js_version = await tab.send(uc.cdp.browser.get_version())
        return {
            "chrome_product": product,
            "chrome_version": product.split("/", 1)[-1],
            "user_agent": user_agent,
            "protocol_version": protocol_version,
        }
    finally:
        await manager.close_instance(instance.instance_id)


async def run_target(
    target: Target,
    manager: BrowserManager,
    options: BrowserOptions,
    local_base_url: str,
    runner: str,
    raw_dir: Optional[Path] = None,
) -> TargetResult:
    """
    Run one target in a fresh browser instance.

    Args:
        target (Target): Target to run
        manager (BrowserManager): Browser manager
        options (BrowserOptions): Spawn options
        local_base_url (str): Root URL of the local page server
        runner (str): Runner label, used for raw payload paths
        raw_dir (Optional[Path]): Directory for raw payloads, or None to skip

    Returns:
        TargetResult: Outcome, UNREACHABLE when the page could not be measured
    """
    result = TargetResult(
        name=target.name,
        title=target.title,
        category=target.category,
        url=target.url.replace("{local}", "local"),
        status=Status.UNREACHABLE,
    )
    started = time.monotonic()
    instance_id: Optional[str] = None
    try:
        instance = await manager.spawn_browser(options)
        instance_id = instance.instance_id
        session = BrowserSession(manager, instance_id, local_base_url)
        payload = await asyncio.wait_for(target.collect(session), timeout=target.timeout)
        write_raw_payload(raw_dir, runner, target.name, payload)
        checks, details, summary = target.evaluate(payload)
        result.checks = checks
        result.details = details
        result.summary = summary
        result.status = status_from_checks(checks)
    except asyncio.TimeoutError:
        result.error = f"Timed out after {target.timeout:.0f}s"
        result.summary = result.error
    except Unreachable as error:
        result.error = str(error)
        result.summary = "Could not be measured"
    except Exception as error:
        result.error = f"{type(error).__name__}: {error}"
        result.summary = "Harness error"
    finally:
        if instance_id is not None:
            try:
                await manager.close_instance(instance_id)
            except Exception:
                pass
        result.duration_seconds = round(time.monotonic() - started, 2)
    return result


async def run_benchmark(
    targets: List[Target],
    mode: str = "headless",
    channel: str = "stable",
    label: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    progress: Optional[Callable[[TargetResult], None]] = None,
) -> RunResult:
    """
    Run every target and collect a RunResult.

    Args:
        targets (List[Target]): Targets to run, in order
        mode (str): headless or headed
        channel (str): Chrome channel label recorded in the runner label
        label (Optional[str]): Explicit runner label
        raw_dir (Optional[Path]): Directory for raw payloads, or None to skip
        progress (Optional[Callable[[TargetResult], None]]): Called after each target

    Returns:
        RunResult: Full run outcome
    """
    started = utc_now()
    runner = runner_label(mode, channel, label)
    options = BrowserOptions(headless=mode == "headless")
    manager = BrowserManager()
    environment = base_environment(mode, channel)
    try:
        environment.update(await browser_version(manager, options))
        results: List[TargetResult] = []
        with LocalPageServer() as server:
            for target in targets:
                result = await run_target(target, manager, options, server.base_url, runner, raw_dir)
                results.append(result)
                if progress is not None:
                    progress(result)
    finally:
        await manager.close_all()
    return RunResult(
        runner=runner,
        date=started.strftime("%Y-%m-%d"),
        started_at=started.isoformat(timespec="seconds"),
        finished_at=utc_now().isoformat(timespec="seconds"),
        environment=environment,
        targets=results,
    )
