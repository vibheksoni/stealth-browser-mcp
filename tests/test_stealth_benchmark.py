import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.stealth import __main__ as cli
from benchmarks.stealth.chrome import download_url, extract_zip, resolve_milestone
from benchmarks.stealth.environment import runner_label
from benchmarks.stealth.regression import compare_runs, find_regressions, previous_run, render_issue
from benchmarks.stealth.report import END_MARKER, START_MARKER, render_section, update_report
from benchmarks.stealth.results import (
    Check,
    RunResult,
    Status,
    TargetResult,
    latest_by_runner,
    load_run,
    load_runs,
    save_run,
)
from benchmarks.stealth.targets import select_targets
from benchmarks.stealth.targets.base import status_from_checks
from benchmarks.stealth.targets.cloudflare import CloudflareTarget
from benchmarks.stealth.targets.creepjs import CreepJSTarget, parse_scores
from benchmarks.stealth.targets.local_probe import (
    INIT_MARKER,
    SPOOFED_RENDERER,
    InitScriptTarget,
    LocalProbeTarget,
    platform_matches,
)
from benchmarks.stealth.targets.input_fidelity import TYPED_TEXT, InputFidelityTarget
from benchmarks.stealth.targets.launch_flags import LaunchFlagsTarget
from benchmarks.stealth.targets.result_tables import SannysoftTarget
from benchmarks.stealth.targets.tls_fingerprint import (
    CHROME_AKAMAI_FINGERPRINT,
    TLSFingerprintTarget,
)

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {"1", "true", "yes"}

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
HEADLESS_UA = CHROME_UA.replace("Chrome/", "HeadlessChrome/")
CREEPJS_PANEL = (
    "7.30ms\nHeadless1634593a\nchromium: true\n25% like headless: 8d3a3ce5\n"
    "67% headless: f49f2dad\n0% stealth: 0c019315\nplatform hints:"
)


def probe_payload(**overrides):
    payload = {
        "webdriver": False,
        "userAgent": CHROME_UA,
        "platform": "Win32",
        "brands": ["Chromium", "Google Chrome", "Not A(Brand"],
        "uaPlatform": "Windows",
        "hasChrome": True,
        "plugins": 5,
        "pluginsIsArray": True,
        "languages": ["en-US", "en"],
        "language": "en-US",
        "hardwareConcurrency": 16,
        "outer": [0, 0],
        "outerAfterLoad": [1920, 1080],
        "notificationPermission": "default",
        "permissionQuery": "prompt",
        "webgl": True,
        "webglVendor": "Google Inc. (NVIDIA)",
        "webglRenderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 SUPER Direct3D11)",
        "iframeChrome": True,
        "iframeWebdriver": False,
        "consoleStackRead": False,
    }
    payload.update(overrides)
    return payload


def tls_payload(**overrides):
    payload = {
        "http_version": "h2",
        "user_agent": CHROME_UA,
        "tls": {
            "ja3_hash": "3b9c1f63151fb23feec32294cdd489c7",
            "ja4": "t13d1517h2_8daaf6152771_cb7bf5808d99",
            "peetprint_hash": "fc97c1cdfb1409c9a9326c1b726d1dee",
        },
        "http2": {
            "akamai_fingerprint": CHROME_AKAMAI_FINGERPRINT,
            "akamai_fingerprint_hash": "52d84b11737d980aef856699f885ca86",
        },
    }
    payload.update(overrides)
    return payload


def make_target(name, status, checks=(), details=None, title=None):
    return TargetResult(
        name=name,
        title=title or name.title(),
        category="test",
        url="https://example.com/",
        status=status,
        summary=f"{name} {status.value}",
        checks=list(checks),
        details=details or {},
    )


def make_run(date, runner="linux-x64-headless-stable", targets=None):
    return RunResult(
        runner=runner,
        date=date,
        started_at=f"{date}T00:00:00+00:00",
        finished_at=f"{date}T00:05:00+00:00",
        environment={"chrome_version": "154.0.1.2", "nodriver": "0.47.0", "os_release": "Linux"},
        targets=targets or [],
    )


class StatusTests(unittest.TestCase):
    def test_status_from_checks(self):
        self.assertEqual(status_from_checks([]), Status.UNREACHABLE)
        self.assertEqual(status_from_checks([Check("a", True)]), Status.PASS)
        self.assertEqual(status_from_checks([Check("a", True), Check("b", False, critical=False)]), Status.DEGRADED)
        self.assertEqual(status_from_checks([Check("a", False), Check("b", False, critical=False)]), Status.FAIL)

    def test_severity_order(self):
        self.assertLess(Status.PASS.severity, Status.DEGRADED.severity)
        self.assertLess(Status.DEGRADED.severity, Status.FAIL.severity)
        self.assertIsNone(Status.UNREACHABLE.severity)


class TargetEvaluationTests(unittest.TestCase):
    def test_result_table_verdicts(self):
        rows = [
            {"name": "WebDriver (New)", "verdict": "passed", "value": "missing (passed)"},
            {"name": "CHR_MEMORY", "verdict": "warn", "value": "WARN"},
        ]
        checks, details, summary = SannysoftTarget().evaluate({"rows": rows})
        self.assertEqual(status_from_checks(checks), Status.DEGRADED)
        self.assertEqual(details["WebDriver (New)"], "missing (passed)")
        self.assertIn("1/2", summary)

        rows.append({"name": "HEADCHR_UA", "verdict": "failed", "value": "FAIL"})
        checks, _, summary = SannysoftTarget().evaluate({"rows": rows})
        self.assertEqual(status_from_checks(checks), Status.FAIL)
        self.assertIn("HEADCHR_UA", summary)

    def test_creepjs_scores(self):
        self.assertEqual(parse_scores(CREEPJS_PANEL), {"like_headless": 25, "headless": 67, "stealth": 0})
        checks, details, _ = CreepJSTarget().evaluate({"panel_text": CREEPJS_PANEL})
        self.assertEqual(status_from_checks(checks), Status.FAIL)
        self.assertEqual(details["headless_percent"], 67)

        clean = CREEPJS_PANEL.replace("67% headless", "0% headless")
        self.assertEqual(status_from_checks(CreepJSTarget().evaluate({"panel_text": clean})[0]), Status.PASS)
        partial = CREEPJS_PANEL.replace("67% headless", "33% headless")
        self.assertEqual(status_from_checks(CreepJSTarget().evaluate({"panel_text": partial})[0]), Status.DEGRADED)
        tampered = clean.replace("0% stealth", "20% stealth")
        self.assertEqual(status_from_checks(CreepJSTarget().evaluate({"panel_text": tampered})[0]), Status.FAIL)
        self.assertEqual(CreepJSTarget().evaluate({"panel_text": "nothing"})[0], [])

    def test_cloudflare(self):
        target = CloudflareTarget()
        passed = target.evaluate({"title": "nowsecure.nl", "text": "NOWSECURE BY NODRIVER", "waited_seconds": 1.0})
        self.assertEqual(status_from_checks(passed[0]), Status.PASS)
        stuck = target.evaluate({"title": "Just a moment...", "text": "Verify you are human", "challengeFrame": True})
        self.assertEqual(status_from_checks(stuck[0]), Status.FAIL)
        other = target.evaluate({"title": "502 Bad Gateway", "text": "nginx"})
        self.assertEqual(status_from_checks(other[0]), Status.UNREACHABLE)

    def test_tls(self):
        target = TLSFingerprintTarget()
        checks, details, _ = target.evaluate(tls_payload())
        self.assertEqual(status_from_checks(checks), Status.PASS)
        self.assertEqual(details["ja4"], "t13d1517h2_8daaf6152771_cb7bf5808d99")
        self.assertNotIn("ip", details)

        headless = target.evaluate(tls_payload(user_agent=HEADLESS_UA))[0]
        self.assertEqual(status_from_checks(headless), Status.FAIL)
        drifted = tls_payload(http2={"akamai_fingerprint": "1:65536|0|0|m,p,a,s"})
        self.assertEqual(status_from_checks(target.evaluate(drifted)[0]), Status.DEGRADED)

    def test_local_probe(self):
        target = LocalProbeTarget()
        self.assertEqual(status_from_checks(target.evaluate(probe_payload())[0]), Status.PASS)
        self.assertEqual(status_from_checks(target.evaluate(probe_payload(userAgent=HEADLESS_UA))[0]), Status.FAIL)
        self.assertEqual(status_from_checks(target.evaluate(probe_payload(webdriver=True))[0]), Status.FAIL)
        self.assertEqual(status_from_checks(target.evaluate(probe_payload(outerAfterLoad=[0, 0]))[0]), Status.FAIL)
        software = probe_payload(webglRenderer="ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))")
        self.assertEqual(status_from_checks(target.evaluate(software)[0]), Status.DEGRADED)
        inconsistent = probe_payload(notificationPermission="denied", permissionQuery="prompt")
        self.assertEqual(status_from_checks(target.evaluate(inconsistent)[0]), Status.FAIL)

    def test_launch_flags(self):
        target = LaunchFlagsTarget()
        clean = ["--no-first-run", "--remote-debugging-port=51234", "--headless=new"]
        self.assertEqual(status_from_checks(target.evaluate({"arguments": clean})[0]), Status.PASS)
        for flag in ("--no-sandbox", "--disable-setuid-sandbox", "--remote-debugging-port=0", "--enable-automation"):
            checks = target.evaluate({"arguments": clean + [flag]})[0]
            self.assertEqual(status_from_checks(checks), Status.FAIL, flag)
        self.assertEqual(status_from_checks(target.evaluate({"arguments": clean + ["--disable-gpu"]})[0]), Status.DEGRADED)

    def test_input_fidelity(self):
        def events(trusted=True):
            recorded = [
                {"type": "pointerdown", "trusted": trusted, "key": None, "time": 10.0},
                {"type": "mousedown", "trusted": trusted, "key": None, "time": 10.0},
                {"type": "mouseup", "trusted": trusted, "key": None, "time": 80.0},
                {"type": "click", "trusted": trusted, "key": None, "time": 80.0},
            ]
            for char in TYPED_TEXT:
                recorded.append({"type": "keydown", "trusted": trusted, "key": char, "time": 90.0})
                recorded.append({"type": "input", "trusted": trusted, "key": None, "time": 90.0})
                recorded.append({"type": "keyup", "trusted": trusted, "key": char, "time": 95.0})
            return recorded

        target = InputFidelityTarget()
        good = {"events": events(), "moves": 4, "added": 0, "value": TYPED_TEXT}
        self.assertEqual(status_from_checks(target.evaluate(good)[0]), Status.PASS)
        self.assertEqual(status_from_checks(target.evaluate(dict(good, events=events(False)))[0]), Status.FAIL)
        self.assertEqual(status_from_checks(target.evaluate(dict(good, added=1))[0]), Status.FAIL)
        self.assertEqual(status_from_checks(target.evaluate(dict(good, moves=0))[0]), Status.DEGRADED)
        script_click = [event for event in events() if event["type"] not in ("pointerdown", "mousedown", "mouseup")]
        self.assertEqual(status_from_checks(target.evaluate(dict(good, events=script_click))[0]), Status.FAIL)

    def test_platform_matches(self):
        self.assertTrue(platform_matches("Win32", "Windows"))
        self.assertTrue(platform_matches("MacIntel", "macOS"))
        self.assertTrue(platform_matches("Linux x86_64", "Linux"))
        self.assertFalse(platform_matches("Win32", "macOS"))
        self.assertTrue(platform_matches("", "Windows"))

    def test_init_script(self):
        target = InitScriptTarget()
        good = {"webgl": True, "initMarkerAtParse": INIT_MARKER, "webglRenderer": SPOOFED_RENDERER}
        payload = {"identifier": "1", "loads": [good, good]}
        self.assertEqual(status_from_checks(target.evaluate(payload)[0]), Status.PASS)

        missing = {"webgl": True, "initMarkerAtParse": None, "webglRenderer": "real"}
        payload = {"identifier": "1", "loads": [missing, missing]}
        self.assertEqual(status_from_checks(target.evaluate(payload)[0]), Status.FAIL)

        no_webgl = {"webgl": False, "initMarkerAtParse": INIT_MARKER, "webglRenderer": None}
        checks = target.evaluate({"identifier": "1", "loads": [no_webgl, no_webgl]})[0]
        self.assertEqual(status_from_checks(checks), Status.PASS)
        self.assertEqual(len(checks), 3)

    def test_select_targets(self):
        self.assertEqual([t.name for t in select_targets(["tls", "local_probe"])], ["local_probe", "tls"])
        self.assertEqual(len(select_targets()), 9)
        with self.assertRaises(ValueError):
            select_targets(["missing"])


class ResultStorageTests(unittest.TestCase):
    def test_round_trip(self):
        run = make_run("2026-10-10", targets=[make_target("tls", Status.PASS, [Check("a", True, value=[1, 2])])])
        with tempfile.TemporaryDirectory() as tmp:
            path = save_run(run, Path(tmp))
            self.assertEqual(path.name, "2026-10-10-linux-x64-headless-stable.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["PASS"], 1)
            self.assertEqual(data["targets"][0]["status"], "PASS")
            loaded = load_run(path)
        self.assertEqual(loaded.target("tls").status, Status.PASS)
        self.assertEqual(loaded.target("tls").checks[0].value, [1, 2])

    def test_load_runs_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            save_run(make_run("2026-10-03"), directory)
            save_run(make_run("2026-10-10"), directory)
            save_run(make_run("2026-10-01", runner="macos-arm64-headed-stable"), directory)
            (directory / "notes.json").write_text("{}", encoding="utf-8")
            runs = load_runs(directory)
        self.assertEqual([run.date for run in runs], ["2026-10-01", "2026-10-03", "2026-10-10"])
        latest = latest_by_runner(runs)
        self.assertEqual(list(latest), ["linux-x64-headless-stable", "macos-arm64-headed-stable"])
        self.assertEqual(latest["linux-x64-headless-stable"].date, "2026-10-10")

    def test_runner_label(self):
        self.assertEqual(runner_label("headless", "stable", "Linux X64 / Custom"), "linux-x64-custom")
        self.assertRegex(runner_label("headed", "previous"), r"^[a-z0-9.-]+-headed-previous$")


class RegressionTests(unittest.TestCase):
    def test_pass_to_fail_is_reported(self):
        before = make_run(
            "2026-10-03",
            targets=[
                make_target(
                    "tls",
                    Status.PASS,
                    [Check("ua", True), Check("h2", True)],
                    {"akamai_fingerprint": "1:65536|15663105|0|m,a,s,p"},
                ),
                make_target("cloudflare", Status.PASS),
            ],
        )
        after = make_run(
            "2026-10-10",
            targets=[
                make_target(
                    "tls",
                    Status.FAIL,
                    [Check("ua", False), Check("h2", True)],
                    {"akamai_fingerprint": "changed|value"},
                ),
                make_target("cloudflare", Status.UNREACHABLE),
            ],
        )
        regressions = compare_runs(before, after)
        self.assertEqual(len(regressions), 1)
        self.assertEqual(regressions[0].new_failed_checks, ["ua"])
        self.assertIn("akamai_fingerprint", regressions[0].changed_details)

        body = render_issue(regressions, "2026-10-10", "https://example.com/run/1")
        self.assertIn("PASS (2026-10-03) to FAIL", body)
        self.assertIn("changed\\|value", body)
        self.assertNotIn("—", body)

    def test_improvement_and_unreachable_are_ignored(self):
        before = make_run("2026-10-03", targets=[make_target("a", Status.FAIL), make_target("b", Status.UNREACHABLE)])
        after = make_run("2026-10-10", targets=[make_target("a", Status.PASS), make_target("b", Status.FAIL)])
        self.assertEqual(compare_runs(before, after), [])

    def test_previous_run_matches_runner_and_date(self):
        history = [
            make_run("2026-09-26"),
            make_run("2026-10-03"),
            make_run("2026-10-05", runner="windows-x64-headed-stable"),
            make_run("2026-10-10"),
        ]
        current = make_run("2026-10-10")
        self.assertEqual(previous_run(history, current).date, "2026-10-03")
        self.assertIsNone(previous_run(history, make_run("2026-10-10", runner="new-runner")))

    def test_find_regressions_across_runs(self):
        history = [make_run("2026-10-03", targets=[make_target("a", Status.PASS)])]
        incoming = [make_run("2026-10-10", targets=[make_target("a", Status.DEGRADED)])]
        self.assertEqual(len(find_regressions(history, incoming)), 1)


class ReportTests(unittest.TestCase):
    def test_render_section(self):
        runs = [
            make_run("2026-10-03", targets=[make_target("tls", Status.PASS, title="TLS")]),
            make_run(
                "2026-10-10",
                targets=[
                    make_target("tls", Status.FAIL, [Check("ua has no | pipe", False)], title="TLS"),
                    make_target("cloudflare", Status.UNREACHABLE, title="Cloudflare"),
                ],
            ),
        ]
        runs[1].targets[1].error = "Navigation failed\nsecond line"
        section = render_section(runs)
        self.assertTrue(section.startswith(START_MARKER))
        self.assertTrue(section.endswith(END_MARKER))
        self.assertIn("| TLS | test | FAIL |", section)
        self.assertIn("Cloudflare: not measured, Navigation failed", section)
        self.assertNotIn("second line", section)
        self.assertIn("| 2026-10-10 | 0/2 |", section)
        self.assertIn("| 2026-10-03 | 1/1 |", section)
        self.assertNotIn("—", section)

    def test_render_without_runs(self):
        self.assertIn("No automated runs", render_section([]))

    def test_update_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "REPORT.md"
            path.write_text(f"# Title\n\n{START_MARKER}\nold\n{END_MARKER}\n\n## Manual\n", encoding="utf-8")
            runs = [make_run("2026-10-10", targets=[make_target("tls", Status.PASS)])]
            self.assertTrue(update_report(path, runs))
            self.assertFalse(update_report(path, runs))
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# Title\n\n"))
            self.assertTrue(text.endswith(f"{END_MARKER}\n\n## Manual\n"))
            self.assertNotIn("old", text)

            path.write_text("# No markers\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                update_report(path, runs)

    def test_repository_report_has_markers(self):
        text = (ROOT / "STEALTH_TESTS.md").read_text(encoding="utf-8")
        self.assertLess(text.index(START_MARKER), text.index(END_MARKER))


class ChromeInstallTests(unittest.TestCase):
    def test_resolve_milestone(self):
        self.assertEqual(resolve_milestone("stable", "154.0.8037.98"), 154)
        self.assertEqual(resolve_milestone("previous", "154.0.8037.98"), 153)
        self.assertEqual(resolve_milestone("150", "154.0.8037.98"), 150)
        with self.assertRaises(ValueError):
            resolve_milestone("beta", "154.0.8037.98")

    def test_download_url(self):
        milestones = {
            "milestones": {
                "153": {
                    "version": "153.0.1.0",
                    "downloads": {"chrome": [{"platform": "linux64", "url": "https://example.com/linux.zip"}]},
                }
            }
        }
        self.assertEqual(download_url(milestones, 153, "linux64"), ("153.0.1.0", "https://example.com/linux.zip"))
        with self.assertRaises(LookupError):
            download_url(milestones, 153, "win64")
        with self.assertRaises(LookupError):
            download_url(milestones, 152, "linux64")

    def test_extract_zip_rejects_traversal(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../escape.txt", "x")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                extract_zip(buffer.getvalue(), Path(tmp) / "out")

    def test_extract_zip_keeps_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("chrome-linux64/chrome")
            info.external_attr = 0o755 << 16
            archive.writestr(info, "binary")
        with tempfile.TemporaryDirectory() as tmp:
            extract_zip(buffer.getvalue(), Path(tmp))
            self.assertEqual((Path(tmp) / "chrome-linux64" / "chrome").read_text(), "binary")


class CommandLineTests(unittest.TestCase):
    def test_compare_writes_issue_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_run(make_run("2026-10-03", targets=[make_target("a", Status.PASS)]), root / "history")
            save_run(make_run("2026-10-10", targets=[make_target("a", Status.FAIL)]), root / "incoming")
            output = root / "github_output"
            issue = root / "issue.md"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), mock.patch("sys.stdout", io.StringIO()):
                code = cli.main(
                    [
                        "compare",
                        "--incoming",
                        str(root / "incoming"),
                        "--results-dir",
                        str(root / "history"),
                        "--issue-file",
                        str(issue),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("regressions=1", output.read_text(encoding="utf-8"))
            self.assertIn("title=Stealth benchmark regression on 2026-10-10", output.read_text(encoding="utf-8"))
            self.assertIn("PASS (2026-10-03) to FAIL", issue.read_text(encoding="utf-8"))

    def test_compare_without_regressions_skips_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_run(make_run("2026-10-10", targets=[make_target("a", Status.PASS)]), root / "incoming")
            issue = root / "issue.md"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), mock.patch("sys.stdout", io.StringIO()):
                code = cli.main(
                    ["compare", "--incoming", str(root / "incoming"), "--results-dir", str(root / "none"), "--issue-file", str(issue)]
                )
            self.assertEqual(code, 0)
            self.assertFalse(issue.exists())

    def test_render_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "REPORT.md"
            report.write_text(f"{START_MARKER}\n{END_MARKER}\n", encoding="utf-8")
            save_run(make_run("2026-10-10", targets=[make_target("a", Status.PASS)]), root / "results")
            with mock.patch("sys.stdout", io.StringIO()):
                code = cli.main(["render", "--results-dir", str(root / "results"), "--report", str(report)])
            self.assertEqual(code, 0)
            self.assertIn("| A | test | PASS |", report.read_text(encoding="utf-8"))


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class LocalTargetsBrowserTests(unittest.TestCase):
    def test_local_targets_run(self):
        from benchmarks.stealth.runner import run_benchmark

        run = asyncio.run(run_benchmark(select_targets(["local_probe", "init_script"]), mode="headless"))
        self.assertEqual(run.target("init_script").status, Status.PASS)
        self.assertNotEqual(run.target("local_probe").status, Status.UNREACHABLE)
        self.assertTrue(run.environment.get("chrome_version"))


if __name__ == "__main__":
    unittest.main()
