"""Detects regressions between benchmark runs and renders the issue body."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .results import RunResult, Status, TargetResult

ISSUE_TITLE_PREFIX = "Stealth benchmark regression"


@dataclass
class Regression:
    """
    A target that got worse on one runner.

    Attributes:
        runner (str): Runner label
        target (str): Target title
        previous_status (Status): Status in the previous run
        current_status (Status): Status in the current run
        previous_date (str): Date of the previous run
        new_failed_checks (List[str]): Checks that passed before and fail now
        changed_details (Dict[str, Dict[str, Any]]): Detail values that changed, keyed by detail name
        summary (str): Current target summary
    """

    runner: str
    target: str
    previous_status: Status
    current_status: Status
    previous_date: str
    new_failed_checks: List[str] = field(default_factory=list)
    changed_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    summary: str = ""


def is_regression(previous: Status, current: Status) -> bool:
    """
    Tell whether a status change is a regression.

    UNREACHABLE on either side is never a regression, network failures are
    reported in the results but do not open issues.

    Args:
        previous (Status): Earlier status
        current (Status): Later status

    Returns:
        bool: True when the later status is strictly worse
    """
    if previous.severity is None or current.severity is None:
        return False
    return current.severity > previous.severity


def changed_details(previous: TargetResult, current: TargetResult) -> Dict[str, Dict[str, Any]]:
    """
    Detail values that differ between two results of the same target.

    Args:
        previous (TargetResult): Earlier result
        current (TargetResult): Later result

    Returns:
        Dict[str, Dict[str, Any]]: {"name": {"before": ..., "after": ...}} for each change
    """
    keys = sorted(set(previous.details) | set(current.details))
    return {
        key: {"before": previous.details.get(key), "after": current.details.get(key)}
        for key in keys
        if previous.details.get(key) != current.details.get(key)
    }


def compare_runs(previous: RunResult, current: RunResult) -> List[Regression]:
    """
    Compare two runs from the same runner.

    Args:
        previous (RunResult): Earlier run
        current (RunResult): Later run

    Returns:
        List[Regression]: Targets that got worse, in current run order
    """
    regressions = []
    for target in current.targets:
        before = previous.target(target.name)
        if before is None or not is_regression(before.status, target.status):
            continue
        previously_passing = {check.name for check in before.checks if check.passed}
        regressions.append(
            Regression(
                runner=current.runner,
                target=target.title,
                previous_status=before.status,
                current_status=target.status,
                previous_date=previous.date,
                new_failed_checks=[check.name for check in target.failed_checks if check.name in previously_passing],
                changed_details=changed_details(before, target),
                summary=target.summary,
            )
        )
    return regressions


def previous_run(history: List[RunResult], current: RunResult) -> Optional[RunResult]:
    """
    Find the newest earlier run from the same runner.

    Args:
        history (List[RunResult]): Stored runs, oldest first
        current (RunResult): Run being compared

    Returns:
        Optional[RunResult]: Newest run with the same runner and an earlier date, or None
    """
    candidates = [run for run in history if run.runner == current.runner and run.date < current.date]
    return candidates[-1] if candidates else None


def find_regressions(history: List[RunResult], incoming: List[RunResult]) -> List[Regression]:
    """
    Compare each incoming run with the previous stored run of its runner.

    Args:
        history (List[RunResult]): Stored runs, oldest first
        incoming (List[RunResult]): New runs

    Returns:
        List[Regression]: Every regression across the incoming runs
    """
    regressions: List[Regression] = []
    for run in incoming:
        previous = previous_run(history, run)
        if previous is not None:
            regressions.extend(compare_runs(previous, run))
    return regressions


def format_value(value: Any, limit: int = 120) -> str:
    """
    Render a detail value for a markdown table cell.

    Args:
        value (Any): Value to render
        limit (int): Maximum length

    Returns:
        str: Inline code text with pipes escaped, truncated when long
    """
    text = "none" if value is None else str(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return "`" + text.replace("`", "'").replace("|", "\\|") + "`"


def render_issue(regressions: List[Regression], date: str, run_url: Optional[str] = None) -> str:
    """
    Render the body of a regression issue.

    Args:
        regressions (List[Regression]): Regressions to report
        date (str): Date of the run that regressed
        run_url (Optional[str]): Link to the workflow run

    Returns:
        str: Markdown issue body
    """
    lines = [f"The stealth benchmark on {date} found {len(regressions)} regression(s).", ""]
    if run_url:
        lines += [f"Workflow run and raw payloads: {run_url}", ""]
    for item in regressions:
        lines.append(f"### {item.target} on {item.runner}")
        lines.append("")
        lines.append(
            f"Status went from {item.previous_status.value} ({item.previous_date}) to {item.current_status.value}."
        )
        if item.summary:
            lines.append(f"Summary: {item.summary}")
        if item.new_failed_checks:
            lines.append("")
            lines.append("Checks that passed before and fail now:")
            lines.extend(f"- {name}" for name in item.new_failed_checks)
        if item.changed_details:
            lines.append("")
            lines.append("| Detail | Before | After |")
            lines.append("|--------|--------|-------|")
            for key, change in item.changed_details.items():
                lines.append(f"| {key} | {format_value(change['before'])} | {format_value(change['after'])} |")
        lines.append("")
    lines.append("Results are in `docs/stealth-results/` and the summary is in `STEALTH_TESTS.md`.")
    return "\n".join(lines) + "\n"
