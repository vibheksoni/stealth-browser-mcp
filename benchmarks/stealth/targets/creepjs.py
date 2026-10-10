"""CreepJS headless and stealth scoring target."""

import re
from typing import Any, Dict, List, Optional, Tuple

from ..results import Check
from .base import Session, Target, wait_for_value

READ_SCORES_JS = """(() => {
  const node = document.querySelector('#headless-resistance-detection-results');
  if (!node) return null;
  const text = node.innerText;
  return /\\d+% stealth/.test(text) ? text : null;
})()"""

SCORE_PATTERNS = {
    "like_headless": re.compile(r"^\s*(\d+)% like headless", re.IGNORECASE | re.MULTILINE),
    "headless": re.compile(r"^\s*(\d+)% headless", re.IGNORECASE | re.MULTILINE),
    "stealth": re.compile(r"^\s*(\d+)% stealth", re.IGNORECASE | re.MULTILINE),
}
HEADLESS_FAIL_PERCENT = 50


def parse_scores(text: str) -> Dict[str, Optional[int]]:
    """
    Extract CreepJS percentages from the headless panel text.

    Args:
        text (str): innerText of #headless-resistance-detection-results

    Returns:
        Dict[str, Optional[int]]: like_headless, headless, and stealth percentages, None when missing
    """
    scores: Dict[str, Optional[int]] = {}
    for key, pattern in SCORE_PATTERNS.items():
        match = pattern.search(text or "")
        scores[key] = int(match.group(1)) if match else None
    return scores


class CreepJSTarget(Target):
    """
    CreepJS fingerprint analysis.

    Any stealth score means CreepJS saw tampered APIs and fails the target. A
    headless score of 50% or more fails it, a lower nonzero score degrades it.
    The like headless score is recorded only, real Chrome reports some.
    """

    name = "creepjs"
    title = "CreepJS"
    category = "Deep fingerprinting"
    url = "https://abrahamjuliot.github.io/creepjs/"
    timeout = 120.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load CreepJS and wait for the headless panel to finish scoring.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: {"panel_text": str}
        """
        await session.navigate(self.resolve_url(session), timeout=60.0)
        text = await wait_for_value(session, READ_SCORES_JS, timeout=75.0)
        return {"panel_text": text}

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn CreepJS scores into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        scores = parse_scores(payload.get("panel_text", ""))
        headless = scores["headless"]
        stealth = scores["stealth"]
        checks: List[Check] = []
        if headless is not None:
            checks.append(Check("headless score below 50%", headless < HEADLESS_FAIL_PERCENT, True, headless))
            checks.append(Check("headless score is 0%", headless == 0, False, headless))
        if stealth is not None:
            checks.append(Check("stealth score is 0%", stealth == 0, True, stealth))
        details = {f"{key}_percent": value for key, value in scores.items()}
        summary = ", ".join(
            f"{value}% {label}"
            for label, value in (("headless", headless), ("stealth", stealth), ("like headless", scores["like_headless"]))
            if value is not None
        )
        return checks, details, summary or "Scores not found"
