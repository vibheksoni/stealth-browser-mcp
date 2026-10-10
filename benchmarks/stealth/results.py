"""Result models and JSON storage for stealth benchmark runs."""

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
RESULT_FILE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<runner>[a-z0-9][a-z0-9.-]*)\.json$")


class Status(str, Enum):
    """Outcome of one benchmark target."""

    PASS = "PASS"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"
    UNREACHABLE = "UNREACHABLE"

    @property
    def severity(self) -> Optional[int]:
        """
        Rank used to detect regressions.

        Returns:
            Optional[int]: 0 for PASS, 1 for DEGRADED, 2 for FAIL, None when unreachable
        """
        return {Status.PASS: 0, Status.DEGRADED: 1, Status.FAIL: 2}.get(self)


@dataclass
class Check:
    """
    Single assertion made against a target.

    Attributes:
        name (str): Check identifier shown in reports
        passed (bool): Whether the check passed
        critical (bool): A failed critical check fails the target, others degrade it
        value (Any): Observed value that the check was based on
    """

    name: str
    passed: bool
    critical: bool = True
    value: Any = None


@dataclass
class TargetResult:
    """
    Outcome of one target in one run.

    Attributes:
        name (str): Target identifier
        title (str): Human readable target name
        category (str): Detection surface the target covers
        url (str): Page the target loads
        status (Status): Overall outcome
        summary (str): One line description of the outcome
        checks (List[Check]): Individual assertions
        details (Dict[str, Any]): Key fingerprint values kept in the committed results
        duration_seconds (float): Wall time spent on the target
        error (Optional[str]): Harness or navigation error, when any
    """

    name: str
    title: str
    category: str
    url: str
    status: Status
    summary: str = ""
    checks: List[Check] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    error: Optional[str] = None

    @property
    def failed_checks(self) -> List[Check]:
        """
        Checks that did not pass.

        Returns:
            List[Check]: Failed checks in their original order
        """
        return [check for check in self.checks if not check.passed]


@dataclass
class RunResult:
    """
    Outcome of one benchmark run on one runner.

    Attributes:
        runner (str): Runner label, for example linux-x64-headless-stable
        date (str): Run date in YYYY-MM-DD form (UTC)
        started_at (str): ISO 8601 start time (UTC)
        finished_at (str): ISO 8601 finish time (UTC)
        environment (Dict[str, Any]): Platform, browser, and CI metadata
        targets (List[TargetResult]): Per-target outcomes
        schema_version (int): Result file format version
    """

    runner: str
    date: str
    started_at: str
    finished_at: str
    environment: Dict[str, Any]
    targets: List[TargetResult]
    schema_version: int = SCHEMA_VERSION

    @property
    def counts(self) -> Dict[str, int]:
        """
        Number of targets per status.

        Returns:
            Dict[str, int]: Count keyed by status value, including zero counts
        """
        counts = {status.value: 0 for status in Status}
        for target in self.targets:
            counts[target.status.value] += 1
        return counts

    @property
    def filename(self) -> str:
        """
        File name used when saving this run.

        Returns:
            str: Name in YYYY-MM-DD-<runner>.json form
        """
        return f"{self.date}-{self.runner}.json"

    def target(self, name: str) -> Optional[TargetResult]:
        """
        Find a target result by name.

        Args:
            name (str): Target identifier

        Returns:
            Optional[TargetResult]: Matching result, or None when the run did not include it
        """
        return next((target for target in self.targets if target.name == name), None)

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert the run to a JSON-ready dictionary.

        Returns:
            Dict[str, Any]: Serializable run data
        """
        data = asdict(self)
        data["summary"] = self.counts
        for target in data["targets"]:
            target["status"] = Status(target["status"]).value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunResult":
        """
        Build a run from its JSON dictionary.

        Args:
            data (Dict[str, Any]): Data produced by to_dict

        Returns:
            RunResult: Parsed run
        """
        targets = []
        for raw in data.get("targets", []):
            checks = [Check(**check) for check in raw.get("checks", [])]
            targets.append(
                TargetResult(
                    name=raw["name"],
                    title=raw.get("title", raw["name"]),
                    category=raw.get("category", ""),
                    url=raw.get("url", ""),
                    status=Status(raw["status"]),
                    summary=raw.get("summary", ""),
                    checks=checks,
                    details=raw.get("details", {}),
                    duration_seconds=raw.get("duration_seconds", 0.0),
                    error=raw.get("error"),
                )
            )
        return cls(
            runner=data["runner"],
            date=data["date"],
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            environment=data.get("environment", {}),
            targets=targets,
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


def save_run(run: RunResult, directory: Path) -> Path:
    """
    Write a run to its result file.

    Args:
        run (RunResult): Run to save
        directory (Path): Destination directory, created when missing

    Returns:
        Path: Written file path
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / run.filename
    path.write_text(json.dumps(run.to_dict(), indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    return path


def load_run(path: Path) -> RunResult:
    """
    Read a run from a result file.

    Args:
        path (Path): Result file path

    Returns:
        RunResult: Parsed run
    """
    return RunResult.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_runs(directory: Path) -> List[RunResult]:
    """
    Read every result file in a directory, oldest first.

    Args:
        directory (Path): Directory holding YYYY-MM-DD-<runner>.json files

    Returns:
        List[RunResult]: Runs sorted by date, then runner
    """
    if not directory.is_dir():
        return []
    runs = [load_run(path) for path in directory.iterdir() if RESULT_FILE_PATTERN.match(path.name)]
    return sorted(runs, key=lambda run: (run.date, run.runner))


def latest_by_runner(runs: List[RunResult]) -> Dict[str, RunResult]:
    """
    Pick the newest run for each runner label.

    Args:
        runs (List[RunResult]): Runs sorted oldest first

    Returns:
        Dict[str, RunResult]: Newest run keyed by runner label, in label order
    """
    latest: Dict[str, RunResult] = {}
    for run in runs:
        latest[run.runner] = run
    return dict(sorted(latest.items()))
