"""Offline targets that load the bundled probe page from the local server."""

import re
from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target, wait_for_value

READ_PROBE_JS = "window.__stealthProbe || null"
READ_OUTER_JS = "[window.outerWidth, window.outerHeight]"
SOFTWARE_RENDERER = re.compile(r"swiftshader|llvmpipe|software", re.IGNORECASE)
PLATFORM_FAMILIES = {
    "Windows": ("Win",),
    "macOS": ("Mac",),
    "Linux": ("Linux",),
    "Chrome OS": ("Linux", "CrOS"),
    "Android": ("Linux",),
}
SPOOFED_RENDERER = "Stealth Benchmark Renderer"
INIT_MARKER = "installed"
INIT_SCRIPT = f"""
window.__sbmInitMarker = {INIT_MARKER!r};
(() => {{
  const original = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {{
    if (parameter === 37446) return {SPOOFED_RENDERER!r};
    return original.call(this, parameter);
  }};
}})();
"""


def platform_matches(navigator_platform: str, ua_platform: str) -> bool:
    """
    Check that navigator.platform agrees with the client hints platform.

    Args:
        navigator_platform (str): navigator.platform value
        ua_platform (str): navigator.userAgentData.platform value

    Returns:
        bool: True when consistent or when either value is unavailable
    """
    if not navigator_platform or not ua_platform:
        return True
    prefixes = PLATFORM_FAMILIES.get(ua_platform)
    if prefixes is None:
        return True
    return navigator_platform.startswith(prefixes)


class LocalProbeTarget(Target):
    """
    Bundled fingerprint probe served from 127.0.0.1.

    It needs no network access, so it is the most stable regression signal in
    the suite. It covers the classic headless and automation tells.
    """

    name = "local_probe"
    title = "Local fingerprint probe"
    category = "Automation tells"
    url = "{local}/probe.html"
    timeout = 45.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load the probe page and read its findings.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: Values recorded by the probe page, plus the outer
            window size read after load
        """
        await session.navigate(self.resolve_url(session), timeout=20.0)
        payload = await wait_for_value(session, READ_PROBE_JS, timeout=15.0)
        payload["outerAfterLoad"] = await session.evaluate(READ_OUTER_JS)
        return payload

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn probe findings into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        user_agent = payload.get("userAgent") or ""
        brands = payload.get("brands") or []
        languages = payload.get("languages") or []
        outer = payload.get("outerAfterLoad") or payload.get("outer") or [0, 0]
        renderer = payload.get("webglRenderer") or ""
        notification = payload.get("notificationPermission")
        query = payload.get("permissionQuery")
        checks = [
            Check("navigator.webdriver is not true", payload.get("webdriver") is not True, True, payload.get("webdriver")),
            Check("user agent has no HeadlessChrome", "HeadlessChrome" not in user_agent, True, user_agent),
            Check(
                "client hints have no HeadlessChrome",
                not any("Headless" in brand for brand in brands),
                True,
                brands,
            ),
            Check("window.chrome present", bool(payload.get("hasChrome")), True, payload.get("hasChrome")),
            Check(
                "plugins present",
                bool(payload.get("pluginsIsArray")) and (payload.get("plugins") or 0) > 0,
                True,
                payload.get("plugins"),
            ),
            Check(
                "languages consistent",
                bool(languages) and payload.get("language") == languages[0],
                True,
                languages,
            ),
            Check(
                "notification permission consistent",
                not (notification == "denied" and query == "prompt"),
                True,
                f"{notification}/{query}",
            ),
            Check("window has outer size", all(size > 0 for size in outer), True, outer),
            Check(
                "iframe navigator.webdriver is not true",
                payload.get("iframeWebdriver") is not True,
                True,
                payload.get("iframeWebdriver"),
            ),
            Check(
                "platform matches client hints",
                platform_matches(payload.get("platform") or "", payload.get("uaPlatform") or ""),
                False,
                f"{payload.get('platform')}/{payload.get('uaPlatform')}",
            ),
            Check("iframe window.chrome present", bool(payload.get("iframeChrome")), False, payload.get("iframeChrome")),
            Check("WebGL renderer is hardware", bool(renderer) and not SOFTWARE_RENDERER.search(renderer), False, renderer),
            Check(
                "console does not read Error.stack",
                not payload.get("consoleStackRead"),
                False,
                payload.get("consoleStackRead"),
            ),
        ]
        details = {
            "user_agent": user_agent,
            "platform": payload.get("platform"),
            "brands": brands,
            "languages": languages,
            "hardware_concurrency": payload.get("hardwareConcurrency"),
            "webgl_vendor": payload.get("webglVendor"),
            "webgl_renderer": renderer or None,
            "outer_size": outer,
            "outer_size_at_load": payload.get("outer"),
            "screen": payload.get("screen"),
        }
        passed = sum(1 for check in checks if check.passed)
        return checks, details, f"{passed}/{len(checks)} checks passed"


class InitScriptTarget(Target):
    """
    Capability check for add_script_to_evaluate_on_new_document (issue #22).

    Installs a script through BrowserManager.add_init_script that sets a marker
    and spoofs the WebGL renderer, then verifies it ran before page scripts and
    survives a second navigation.
    """

    name = "init_script"
    title = "Init script injection"
    category = "Capability"
    url = "{local}/probe.html"
    timeout = 45.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Install the init script and load the probe page twice.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: Probe findings for the first and second load
        """
        identifier = await session.add_init_script(INIT_SCRIPT)
        loads = []
        for suffix in ("?load=1", "?load=2"):
            await session.navigate(self.resolve_url(session) + suffix, timeout=20.0)
            probe = await wait_for_value(session, READ_PROBE_JS, timeout=15.0)
            loads.append(
                {
                    "initMarkerAtParse": probe.get("initMarkerAtParse"),
                    "webgl": probe.get("webgl"),
                    "webglRenderer": probe.get("webglRenderer"),
                }
            )
        return {"identifier": identifier, "loads": loads}

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Check that the init script ran on both loads.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        loads = payload.get("loads") or [{}, {}]
        first, second = (loads + [{}, {}])[:2]
        checks = [
            Check("script installed", bool(payload.get("identifier")), True, payload.get("identifier")),
            Check(
                "ran before page scripts",
                first.get("initMarkerAtParse") == INIT_MARKER,
                True,
                first.get("initMarkerAtParse"),
            ),
            Check(
                "persists across navigation",
                second.get("initMarkerAtParse") == INIT_MARKER,
                True,
                second.get("initMarkerAtParse"),
            ),
        ]
        if first.get("webgl"):
            checks.append(
                Check(
                    "WebGL renderer override applied",
                    first.get("webglRenderer") == SPOOFED_RENDERER,
                    False,
                    first.get("webglRenderer"),
                )
            )
        passed = sum(1 for check in checks if check.passed)
        return checks, {"webgl_renderer": first.get("webglRenderer")}, f"{passed}/{len(checks)} checks passed"
