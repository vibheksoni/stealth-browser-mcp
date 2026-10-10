"""
Command line entry point.

    python -m benchmarks.stealth run --mode headless
    python -m benchmarks.stealth render
    python -m benchmarks.stealth compare --incoming bench-out
    python -m benchmarks.stealth chrome --milestone previous --dest .chrome
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional

from .report import DEFAULT_HISTORY, update_report
from .regression import ISSUE_TITLE_PREFIX, find_regressions, render_issue
from .results import TargetResult, load_runs, save_run

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_DIR = ROOT / "docs" / "stealth-results"
DEFAULT_REPORT = ROOT / "STEALTH_TESTS.md"
DEFAULT_LOCAL_OUTPUT = ROOT / ".stealth-results"


def print_progress(result: TargetResult) -> None:
    """
    Print one line per finished target.

    Args:
        result (TargetResult): Finished target
    """
    line = f"[{result.status.value}] {result.title} ({result.duration_seconds:.1f}s) {result.summary}"
    if result.error:
        line += f" | {result.error}"
    print(line, flush=True)


def command_run(args: argparse.Namespace) -> int:
    """
    Run the benchmark and save the result file.

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code
    """
    from .runner import run_benchmark
    from .targets import select_targets

    names = [name.strip() for name in args.targets.split(",") if name.strip()] if args.targets else None
    try:
        targets = select_targets(names)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    run = asyncio.run(
        run_benchmark(
            targets,
            mode=args.mode,
            channel=args.channel,
            label=args.label,
            raw_dir=Path(args.raw_dir) if args.raw_dir else None,
            progress=print_progress,
        )
    )
    path = save_run(run, Path(args.output_dir))
    print(f"{run.runner}: {run.counts} -> {path}", flush=True)
    return 0


def command_render(args: argparse.Namespace) -> int:
    """
    Regenerate the automated section of the report.

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code
    """
    runs = load_runs(Path(args.results_dir))
    try:
        changed = update_report(Path(args.report), runs, args.history)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    print(f"{args.report}: {'updated' if changed else 'unchanged'} ({len(runs)} runs)")
    return 0


def write_github_output(values: dict) -> None:
    """
    Append step outputs when running in GitHub Actions.

    Args:
        values (dict): Output names and values
    """
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def command_compare(args: argparse.Namespace) -> int:
    """
    Compare incoming runs with stored history and write an issue body on regressions.

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code, 0 even when regressions are found
    """
    history = load_runs(Path(args.results_dir))
    incoming = load_runs(Path(args.incoming))
    if not incoming:
        print(f"No result files found in {args.incoming}", file=sys.stderr)
        return 2
    regressions = find_regressions(history, incoming)
    date = max(run.date for run in incoming)
    title = f"{ISSUE_TITLE_PREFIX} on {date}"
    for item in regressions:
        print(f"REGRESSION {item.runner} {item.target}: {item.previous_status.value} -> {item.current_status.value}")
    if regressions and args.issue_file:
        Path(args.issue_file).write_text(render_issue(regressions, date, args.run_url), encoding="utf-8")
    if not regressions:
        print(f"No regressions across {len(incoming)} run(s)")
    write_github_output({"regressions": len(regressions), "title": title})
    return 0


def command_chrome(args: argparse.Namespace) -> int:
    """
    Install a Chrome for Testing build and print its executable path.

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code
    """
    from .chrome import install_chrome

    version, executable = install_chrome(args.milestone, Path(args.dest))
    print(f"Installed Chrome for Testing {version}", file=sys.stderr)
    print(executable)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    Returns:
        argparse.ArgumentParser: Parser with run, render, compare, and chrome commands
    """
    parser = argparse.ArgumentParser(prog="python -m benchmarks.stealth", description="Stealth benchmark harness")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run the benchmark and save a result file")
    run.add_argument("--mode", choices=("headless", "headed"), default="headless")
    run.add_argument("--channel", default="stable", help="Chrome channel label used in the runner name")
    run.add_argument("--label", help="Explicit runner label")
    run.add_argument("--targets", help="Comma separated target names, default all")
    run.add_argument("--output-dir", default=str(DEFAULT_LOCAL_OUTPUT), help="Directory for the result file")
    run.add_argument("--raw-dir", help="Directory for raw target payloads")
    run.set_defaults(handler=command_run)

    render = commands.add_parser("render", help="Regenerate the automated section of STEALTH_TESTS.md")
    render.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    render.add_argument("--report", default=str(DEFAULT_REPORT))
    render.add_argument("--history", type=int, default=DEFAULT_HISTORY)
    render.set_defaults(handler=command_render)

    compare = commands.add_parser("compare", help="Compare new runs with stored results")
    compare.add_argument("--incoming", required=True, help="Directory with new result files")
    compare.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    compare.add_argument("--issue-file", help="Write a regression issue body here when regressions are found")
    compare.add_argument("--run-url", help="Workflow run URL included in the issue body")
    compare.set_defaults(handler=command_compare)

    chrome = commands.add_parser("chrome", help="Install a Chrome for Testing build")
    chrome.add_argument("--milestone", default="previous", help="stable, previous, or a milestone number")
    chrome.add_argument("--dest", required=True, help="Install directory")
    chrome.set_defaults(handler=command_chrome)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """
    Parse arguments and dispatch to a command.

    Args:
        argv (Optional[List[str]]): Arguments, defaults to sys.argv

    Returns:
        int: Exit code
    """
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
