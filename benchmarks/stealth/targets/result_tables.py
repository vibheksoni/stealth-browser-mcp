"""Targets for detection pages that report results as passed or failed table cells."""

import asyncio
from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target, Unreachable, wait_for_value

COLLECT_ROWS_JS = """(() => {
  const rows = [];
  for (const row of document.querySelectorAll('table tr')) {
    const cells = Array.from(row.cells);
    if (cells.length < 2) continue;
    const cell = cells.slice(1).find(c => /\\b(passed|failed|warn)\\b/.test(c.className));
    if (!cell) continue;
    const verdict = cell.className.match(/\\b(passed|failed|warn)\\b/)[1];
    rows.push({
      name: cells[0].innerText.replace(/\\s+/g, ' ').trim(),
      verdict: verdict,
      value: cell.innerText.replace(/\\s+/g, ' ').trim().slice(0, 200)
    });
  }
  return rows.length >= MIN_ROWS ? rows : null;
})()"""


class ResultTableTarget(Target):
    """
    Detection page where each result cell carries a passed, failed, or warn class.

    A failed cell fails the target and a warn cell degrades it.

    Attributes:
        min_rows (int): Rows that must be present before results are read
        settle_seconds (float): Extra wait after the rows appear, for late async tests
    """

    min_rows = 1
    settle_seconds = 3.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load the page and read every verdict cell.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: {"rows": [{"name", "verdict", "value"}, ...]}
        """
        await session.navigate(self.resolve_url(session))
        expression = COLLECT_ROWS_JS.replace("MIN_ROWS", str(self.min_rows))
        await wait_for_value(session, expression, timeout=self.timeout / 2)
        if self.settle_seconds:
            await asyncio.sleep(self.settle_seconds)
        rows = await session.evaluate(expression)
        if not rows:
            raise Unreachable("Result table disappeared after loading")
        return {"rows": rows}

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn verdict cells into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        rows = payload.get("rows") or []
        checks = [
            Check(
                name=row["name"],
                passed=row["verdict"] == "passed",
                critical=row["verdict"] != "warn",
                value=row.get("value"),
            )
            for row in rows
        ]
        passed = sum(1 for check in checks if check.passed)
        summary = f"{passed}/{len(checks)} checks passed"
        failed = [check.name for check in checks if not check.passed]
        if failed:
            summary += f", failed: {', '.join(failed)}"
        details = {row["name"]: row.get("value") for row in rows}
        return checks, details, summary


class SannysoftTarget(ResultTableTarget):
    """bot.sannysoft.com headless and fingerprint table."""

    name = "sannysoft"
    title = "Sannysoft"
    category = "Bot detection suite"
    url = "https://bot.sannysoft.com/"
    min_rows = 25


class IntoliTarget(ResultTableTarget):
    """Intoli headless Chrome detection page."""

    name = "intoli"
    title = "Intoli"
    category = "Headless detection"
    url = "https://intoli.com/blog/not-possible-to-block-chrome-headless/chrome-headless-test.html"
    min_rows = 6
