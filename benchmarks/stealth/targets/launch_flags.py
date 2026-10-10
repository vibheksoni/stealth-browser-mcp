"""Target that checks the browser command line for automation and warning flags."""

from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target

AUTOMATION_FLAGS = (
    "--enable-automation",
    "--remote-debugging-pipe",
    "--remote-debugging-port=0",
    "--headless=old",
)
WARNING_FLAGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--single-process",
    "--disable-web-security",
    "--disable-site-isolation-trials",
    "--ignore-certificate-errors",
)
DEGRADING_FLAGS = ("--disable-gpu",)


def matching_flags(arguments: List[str], flags: Tuple[str, ...]) -> List[str]:
    """
    Find launch arguments that start with any of the given flags.

    Args:
        arguments (List[str]): Browser command line arguments.
        flags (Tuple[str, ...]): Flag prefixes to look for.

    Returns:
        List[str]: Matching arguments in command line order.
    """
    return [argument for argument in arguments if argument.startswith(flags)]


class LaunchFlagsTarget(Target):
    """
    Reads the real browser command line.

    Automation flags turn on navigator.webdriver or similar tells. Warning
    flags make headed Chrome show an "unsupported command-line flag" bar and
    weaken the browser. --disable-gpu removes WebGL, which is itself a signal.
    """

    name = "launch_flags"
    title = "Launch flags"
    category = "Browser configuration"
    url = ""
    timeout = 15.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Read the browser process arguments.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: {"arguments": [...]} without the profile path
        """
        arguments = await session.launch_arguments()
        return {"arguments": [argument for argument in arguments if not argument.startswith("--user-data-dir")]}

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn the command line into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        arguments = payload.get("arguments") or []
        automation = matching_flags(arguments, AUTOMATION_FLAGS)
        warnings = matching_flags(arguments, WARNING_FLAGS)
        degrading = matching_flags(arguments, DEGRADING_FLAGS)
        checks = [
            Check("no automation flags", not automation, True, automation),
            Check("no unsupported or sandbox-disabling flags", not warnings, True, warnings),
            Check("GPU not disabled", not degrading, False, degrading),
        ]
        found = automation + warnings + degrading
        summary = f"found {', '.join(found)}" if found else f"{len(arguments)} arguments, none flagged"
        return checks, {"arguments": arguments}, summary
