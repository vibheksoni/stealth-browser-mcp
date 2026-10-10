"""Renders the automated results section of STEALTH_TESTS.md."""

from pathlib import Path
from typing import Dict, List

from .results import RunResult, Status, latest_by_runner

START_MARKER = "<!-- stealth-benchmark:start -->"
END_MARKER = "<!-- stealth-benchmark:end -->"
DEFAULT_HISTORY = 8


def escape_cell(text: str) -> str:
    """
    Make text safe for a markdown table cell.

    Args:
        text (str): Raw text

    Returns:
        str: Text with pipes escaped and newlines flattened
    """
    return str(text).replace("|", "\\|").replace("\n", " ")


def count_text(run: RunResult) -> str:
    """
    Short status count text for a run.

    Args:
        run (RunResult): Run to describe

    Returns:
        str: Text such as "5 pass, 1 degraded, 1 unreachable"
    """
    parts = [f"{count} {status.lower()}" for status, count in run.counts.items() if count]
    return ", ".join(parts) or "no targets"


def target_matrix(latest: Dict[str, RunResult]) -> List[str]:
    """
    Target by runner status table.

    Args:
        latest (Dict[str, RunResult]): Newest run per runner

    Returns:
        List[str]: Markdown lines
    """
    runners = list(latest)
    rows: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    meta: Dict[str, str] = {}
    for runner, run in latest.items():
        for target in run.targets:
            if target.name not in rows:
                rows[target.name] = {}
                order.append(target.name)
                meta[target.name] = f"{escape_cell(target.title)} | {escape_cell(target.category)}"
            rows[target.name][runner] = target.status.value
    lines = [
        "| Target | Category | " + " | ".join(runners) + " |",
        "|--------|----------|" + "|".join("---" for _ in runners) + "|",
    ]
    for name in order:
        cells = [rows[name].get(runner, "not run") for runner in runners]
        lines.append(f"| {meta[name]} | " + " | ".join(cells) + " |")
    return lines


def runner_table(latest: Dict[str, RunResult]) -> List[str]:
    """
    Environment table, one row per runner.

    Args:
        latest (Dict[str, RunResult]): Newest run per runner

    Returns:
        List[str]: Markdown lines
    """
    lines = [
        "| Runner | Date | Chrome | nodriver | OS | Result |",
        "|--------|------|--------|----------|----|--------|",
    ]
    for runner, run in latest.items():
        env = run.environment
        date = run.date
        run_url = (env.get("ci") or {}).get("run_url")
        if run_url:
            date = f"[{date}]({run_url})"
        lines.append(
            "| "
            + " | ".join(
                [
                    runner,
                    date,
                    escape_cell(env.get("chrome_version") or "unknown"),
                    escape_cell(env.get("nodriver") or "unknown"),
                    escape_cell(env.get("os_release") or env.get("os") or "unknown"),
                    count_text(run),
                ]
            )
            + " |"
        )
    return lines


def issue_lines(latest: Dict[str, RunResult]) -> List[str]:
    """
    Failing checks and unreachable targets per runner.

    Args:
        latest (Dict[str, RunResult]): Newest run per runner

    Returns:
        List[str]: Markdown lines, empty when everything passed
    """
    lines: List[str] = []
    for runner, run in latest.items():
        entries = []
        for target in run.targets:
            if target.status == Status.UNREACHABLE:
                reason = (target.error or target.summary or "unknown").splitlines()[0][:160]
                entries.append(f"- {target.title}: not measured, {reason}")
                continue
            failed = target.failed_checks
            if not failed:
                continue
            names = ", ".join(
                f"{check.name}{'' if check.critical else ' (minor)'}" for check in failed
            )
            entries.append(f"- {target.title}: {names}")
        if entries:
            lines += [f"#### {runner}", ""] + entries + [""]
    return lines


def history_table(runs: List[RunResult], limit: int) -> List[str]:
    """
    Passing target counts for the most recent run dates.

    Args:
        runs (List[RunResult]): All runs, oldest first
        limit (int): Number of dates to include

    Returns:
        List[str]: Markdown lines
    """
    runners = sorted({run.runner for run in runs})
    dates = sorted({run.date for run in runs}, reverse=True)[:limit]
    by_key = {(run.date, run.runner): run for run in runs}
    lines = [
        "| Date | " + " | ".join(runners) + " |",
        "|------|" + "|".join("---" for _ in runners) + "|",
    ]
    for date in dates:
        cells = []
        for runner in runners:
            run = by_key.get((date, runner))
            cells.append(f"{run.counts[Status.PASS.value]}/{len(run.targets)}" if run else "")
        lines.append(f"| {date} | " + " | ".join(cells) + " |")
    return lines


def render_section(runs: List[RunResult], history: int = DEFAULT_HISTORY) -> str:
    """
    Render the generated block, markers included.

    Args:
        runs (List[RunResult]): All stored runs, oldest first
        history (int): Number of run dates in the history table

    Returns:
        str: Markdown block
    """
    lines = [START_MARKER, "## Automated Results", ""]
    if not runs:
        lines += ["No automated runs have been recorded yet.", "", END_MARKER]
        return "\n".join(lines)
    latest = latest_by_runner(runs)
    newest = max(run.date for run in latest.values())
    lines += [
        f"Last updated {newest} from {len(latest)} runner(s). This section is generated by "
        "`python -m benchmarks.stealth render` from `docs/stealth-results/`, do not edit it by hand.",
        "",
    ]
    lines += target_matrix(latest) + [""]
    lines += ["### Runners", ""] + runner_table(latest) + [""]
    problems = issue_lines(latest)
    if problems:
        lines += ["### Checks Not Passing", ""] + problems
    lines += ["### History", "", "Passing targets per run.", ""] + history_table(runs, history) + [""]
    lines.append(END_MARKER)
    return "\n".join(lines)


def update_report(path: Path, runs: List[RunResult], history: int = DEFAULT_HISTORY) -> bool:
    """
    Replace the generated block in a markdown file.

    Args:
        path (Path): File holding the start and end markers
        runs (List[RunResult]): All stored runs, oldest first
        history (int): Number of run dates in the history table

    Returns:
        bool: True when the file content changed

    Raises:
        ValueError: When the markers are missing or out of order
    """
    text = path.read_text(encoding="utf-8")
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"{path} must contain {START_MARKER} followed by {END_MARKER}")
    updated = text[:start] + render_section(runs, history) + text[end + len(END_MARKER):]
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True
