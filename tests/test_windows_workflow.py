"""Static assertions for the Windows gated CI workflow (SB-48).

The workflow file is validated here so the slice is testable on any OS. The
workflow itself is only *executed* on the GitHub `windows-latest` runner; this
module does not run the workflow, only asserts its contents are correct:
it targets windows-latest, includes both chrome and edge in its matrix, sets
STEALTH_BROWSER_TESTS, and never references a secret.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "windows-gated.yml"

# Keys whose values look like real credentials. A correct workflow file should
# never reference these.
_FORBIDDEN_SECRETS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "access_key",
    "private_key",
    "aws_secret",
)


def _read_workflow():
    """Return the workflow file text, or None if it does not exist."""
    if not WORKFLOW_PATH.is_file():
        return None
    with WORKFLOW_PATH.open("r", encoding="ascii") as handle:
        return handle.read()


class TestWindowsWorkflowFile(unittest.TestCase):
    """The workflow file exists and has the required shape."""

    def test_workflow_file_exists(self):
        self.assertTrue(
            WORKFLOW_PATH.is_file(),
            f"Missing workflow file: {WORKFLOW_PATH}",
        )

    def test_is_ascii(self):
        """The workflow must be pure ASCII."""
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        text.encode("ascii")  # raises UnicodeEncodeError if non-ASCII

    def test_targets_windows_latest(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("runs-on: windows-latest", text)

    def test_matrix_includes_chrome_and_edge(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        # The browser matrix line must name both chrome and edge.
        matrix_lines = [
            line.strip().lstrip("- ").strip()
            for line in text.splitlines()
        ]
        joined = " ".join(matrix_lines)
        self.assertIn("chrome", joined, "matrix must include chrome")
        self.assertIn("edge", joined, "matrix must include edge")
        # A matrix key named browser is present so chrome/edge are the matrix.
        self.assertIn("browser:", joined, "matrix must declare a browser key")

    def test_sets_stealth_browser_tests(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("STEALTH_BROWSER_TESTS", text)

    def test_uses_python_3_12(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        # setup-python declares python-version: '3.12'.
        self.assertIn("3.12", text)

    def test_installs_package_and_runs_tests(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("pip install -e .", text)
        self.assertIn("python -m unittest tests.test_seed_tools tests.test_session_spawn", text)

    def test_sets_timeout_minutes(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("timeout-minutes: 15", text)

    def test_no_secret_references(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        lower = text.lower()
        for needle in _FORBIDDEN_SECRETS:
            with self.subTest(needle=needle):
                self.assertNotIn(
                    needle,
                    lower,
                    f"workflow must not reference secret token {needle!r}",
                )

    def test_has_continue_on_error_cookie_step(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        # The cookie-carryover test step must be allowed to fail.
        self.assertIn("continue-on-error: true", text)

    def test_has_github_step_summary_artifact(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("GITHUB_STEP_SUMMARY", text)
        self.assertIn("GITHUB_ARTIFACT_PATH", text)

    def test_triggers_are_dispatched(self):
        text = _read_workflow()
        self.assertIsNotNone(text, "workflow file must exist")
        self.assertIn("workflow_dispatch", text)


if __name__ == "__main__":
    unittest.main()
