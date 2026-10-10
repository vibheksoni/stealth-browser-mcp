"""Cloudflare challenge target backed by nowsecure.nl."""

import asyncio
import time
from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target

READ_PAGE_JS = """(() => ({
  title: document.title,
  text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 500),
  challengeFrame: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]')
}))()"""

CHALLENGE_MARKERS = ("just a moment", "verify you are human", "checking your browser", "attention required")
SUCCESS_MARKER = "nowsecure"
CHALLENGE_WAIT_SECONDS = 45.0


def is_challenge(page: Dict[str, Any]) -> bool:
    """
    Tell whether a page snapshot is still a Cloudflare challenge.

    Args:
        page (Dict[str, Any]): Snapshot with title, text, and challengeFrame

    Returns:
        bool: True when challenge markers are present
    """
    haystack = f"{page.get('title', '')} {page.get('text', '')}".lower()
    return bool(page.get("challengeFrame")) or any(marker in haystack for marker in CHALLENGE_MARKERS)


class CloudflareTarget(Target):
    """
    nowsecure.nl sits behind a Cloudflare challenge and shows a NOWSECURE
    banner once the browser gets through.
    """

    name = "cloudflare"
    title = "Cloudflare (nowsecure.nl)"
    category = "JS challenge"
    url = "https://nowsecure.nl/"
    timeout = 90.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load the page and wait for the challenge to clear.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: Final page snapshot plus seconds spent waiting
        """
        await session.navigate(self.resolve_url(session), timeout=40.0)
        started = time.monotonic()
        page: Dict[str, Any] = {}
        while time.monotonic() - started < CHALLENGE_WAIT_SECONDS:
            page = await session.evaluate(READ_PAGE_JS) or {}
            if SUCCESS_MARKER in page.get("text", "").lower() and not is_challenge(page):
                break
            await asyncio.sleep(1.0)
        page["waited_seconds"] = round(time.monotonic() - started, 1)
        return page

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Decide whether the challenge was passed.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        challenged = is_challenge(payload)
        reached = SUCCESS_MARKER in payload.get("text", "").lower() and not challenged
        details = {"title": payload.get("title"), "waited_seconds": payload.get("waited_seconds")}
        if reached:
            check = Check("challenge cleared", True, True, payload.get("title"))
            return [check], details, f"Reached protected page after {payload.get('waited_seconds')}s"
        if challenged:
            check = Check("challenge cleared", False, True, payload.get("title"))
            return [check], details, "Stuck on Cloudflare challenge"
        return [], details, f"Unexpected page: {payload.get('title') or 'no title'}"
