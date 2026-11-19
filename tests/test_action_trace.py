"""Unit tests for ActionTrace, secret redaction, and bundle exports (SB-35)."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.action_trace import (
    DEFAULT_MAX_BYTES,
    SENSITIVE_KEY_MARKERS,
    ActionEntry,
    ActionTrace,
    is_sensitive_key,
    mask_sensitive_keys,
    redact_sensitive_data,
    redact_url,
)
from stealth_browser_mcp.secrets_env import (
    REDACTED_PLACEHOLDER,
    Redactor,
    clear_secrets,
    default_redactor,
    register_secret,
    resolve_value_env,
)


class TestSensitiveKeyDetection(unittest.TestCase):
    """Test is_sensitive_key matching rules."""

    def test_sensitive_key_matches(self):
        cases = [
            ("password", True),
            ("Password", True),
            ("PASSWORD", True),
            ("user_password", True),
            ("newPassword", True),
            ("password_confirm", True),
            ("token", True),
            ("Token", True),
            ("auth_token", True),
            ("csrf_token", True),
            ("accessToken", True),
            ("secret", True),
            ("Secret", True),
            ("client_secret", True),
            ("shared_secret_key", True),
            ("authorization", True),
            ("Authorization", True),
            ("AUTHORIZATION", True),
            ("cookie", True),
            ("Cookie", True),
            ("cookies", True),
            ("Set-Cookie", True),
            ("session_cookie", True),
            # Non-sensitive keys
            ("username", False),
            ("email", False),
            ("url", False),
            ("selector", False),
            ("text", False),
            ("timeout", False),
            ("viewport", False),
            ("status", False),
            ("id", False),
        ]
        for key, expected in cases:
            with self.subTest(key=key):
                self.assertEqual(is_sensitive_key(key), expected)


class TestActionTraceRedaction(unittest.TestCase):
    """Test redaction of secrets and sensitive keys in action trace."""

    def setUp(self):
        self._orig_environ = dict(os.environ)
        clear_secrets()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_environ)
        clear_secrets()

    def test_sensitive_dict_keys_redacted(self):
        """Values of keys named like password/token/secret/authorization/cookie must be redacted."""
        trace = ActionTrace()
        trace.record_action(
            tool="type_text",
            args={
                "selector": "#pwd",
                "password": "plain_password_123",
                "auth_token": "token_abc_456",
                "client_secret": "secret_xyz_789",
                "Authorization": "Bearer token_hdr_111",
                "cookie": "session=abcde",
                "cookies": [{"name": "sid", "value": "xyz"}],
                "username": "safe_user",
            },
            result={"status": "ok", "user_token": "token_result_999"},
        )
        bundle = trace.export_bundle()
        self.assertEqual(len(bundle["actions"]), 1)
        action = bundle["actions"][0]
        args = action["args"]

        self.assertEqual(args["password"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["auth_token"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["client_secret"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["Authorization"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["cookie"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["cookies"], REDACTED_PLACEHOLDER)
        self.assertEqual(args["username"], "safe_user")
        self.assertEqual(action["result"]["user_token"], REDACTED_PLACEHOLDER)

    def test_registered_secrets_redacted_everywhere(self):
        """Values registered with Redactor must be redacted anywhere in args, results, or urls."""
        secret_pass = "topsecret_pass_12345"
        secret_api = "api_key_secret_67890"
        register_secret([secret_pass, secret_api])

        trace = ActionTrace()
        trace.record_action(
            tool="fill",
            args={
                "selector": "#input",
                "text": f"Logging in with {secret_pass} today",
            },
            result=f"API returned key: {secret_api}",
            url=f"https://example.com/login?redirect={secret_api}",
        )
        bundle = trace.export_bundle()
        action = bundle["actions"][0]

        self.assertNotIn(secret_pass, action["args"]["text"])
        self.assertIn(REDACTED_PLACEHOLDER, action["args"]["text"])
        self.assertNotIn(secret_api, str(action["result"]))
        self.assertIn(REDACTED_PLACEHOLDER, str(action["result"]))
        self.assertNotIn(secret_api, action["url"])
        self.assertIn(REDACTED_PLACEHOLDER, action["url"])

    def test_url_sensitive_query_parameters_redacted(self):
        """URL query parameters named like token/password/secret must be redacted."""
        trace = ActionTrace()
        trace.record_action(
            tool="navigate",
            url="https://example.com/oauth/callback?token=my_oauth_token&password=my_pwd&user=bob",
        )
        bundle = trace.export_bundle()
        action = bundle["actions"][0]

        self.assertNotIn("my_oauth_token", action["url"])
        self.assertNotIn("my_pwd", action["url"])
        self.assertIn(f"token={REDACTED_PLACEHOLDER}", action["url"])
        self.assertIn(f"password={REDACTED_PLACEHOLDER}", action["url"])
        self.assertIn("user=bob", action["url"])

    def test_nested_structures_redacted(self):
        """Nested dicts, lists, and tuples are recursively redacted."""
        trace = ActionTrace()
        trace.record_action(
            tool="batch_exec",
            args={
                "steps": [
                    {"step": 1, "token": "tok_nested_1"},
                    {"step": 2, "details": {"password": "pwd_nested_2"}},
                    {"step": 3, "tags": ("normal", "secret_tag")},
                ],
            },
            result={"auth": {"sub": {"session_cookie": "sid_nested_3"}}},
        )
        bundle = trace.export_bundle()
        steps = bundle["actions"][0]["args"]["steps"]
        self.assertEqual(steps[0]["token"], REDACTED_PLACEHOLDER)
        self.assertEqual(steps[1]["details"]["password"], REDACTED_PLACEHOLDER)
        self.assertEqual(
            bundle["actions"][0]["result"]["auth"]["sub"]["session_cookie"],
            REDACTED_PLACEHOLDER,
        )

    def test_grep_style_assertion_all_files_clean(self):
        """Acceptance test: Registered secrets and sensitive values are absent from all written files."""
        os.environ["STEALTH_SECRET_APP_TOKEN"] = "token_xyz_super_private_9999"
        app_token = resolve_value_env("STEALTH_SECRET_APP_TOKEN")
        raw_password = "raw_plaintext_password_dont_leak"
        raw_secret = "raw_plaintext_secret_dont_leak"

        trace = ActionTrace()
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a dummy screenshot file
            screen_file = Path(tmp_dir) / "shot.png"
            screen_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")

            trace.record_action(
                tool="login_user",
                args={
                    "selector": "#user-form",
                    "password": raw_password,
                    "client_secret": raw_secret,
                    "note": f"Auth with {app_token}",
                },
                result={"status": "ok", "jwt": app_token},
                url=f"https://auth.example.com/v1/auth?token={app_token}",
                screenshot_path=screen_file,
            )

            bundle_dir = Path(tmp_dir) / "bundle_export"
            trace.write_bundle(bundle_dir)

            forbidden_strings = [
                app_token,
                raw_password,
                raw_secret,
            ]

            # Grep every file written
            found_files = list(bundle_dir.rglob("*"))
            self.assertTrue(len(found_files) > 0)
            for fpath in found_files:
                if fpath.is_file():
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    for secret in forbidden_strings:
                        self.assertNotIn(
                            secret,
                            content,
                            f"Secret leaked in file {fpath.name}",
                        )

            # trace.json must contain REDACTED_PLACEHOLDER
            trace_json = (bundle_dir / "trace.json").read_text(encoding="utf-8")
            self.assertIn(REDACTED_PLACEHOLDER, trace_json)


class TestActionTraceSizeCapAndOrdering(unittest.TestCase):
    """Test size cap enforcement, truncation markers, and ordering preservation."""

    def test_default_max_bytes(self):
        """Default max_bytes is 5 MB."""
        trace = ActionTrace()
        self.assertEqual(trace.max_bytes, DEFAULT_MAX_BYTES)
        self.assertEqual(trace.max_bytes, 5 * 1024 * 1024)

    def test_size_cap_drops_oldest_first_and_sets_truncated(self):
        """When max_bytes is exceeded, oldest entries are dropped and truncated is set."""
        # Cap set small enough that only ~2-3 entries fit
        trace = ActionTrace(max_bytes=1200)

        for i in range(15):
            trace.record_action(
                tool="click",
                args={"item_id": i, "padding": "x" * 150},
                result={"status": "ok", "index": i},
                url=f"https://example.com/item/{i}",
            )

        self.assertTrue(trace.truncated)
        self.assertGreater(trace.dropped_count, 0)
        self.assertLess(len(trace.actions), 15)

        bundle = trace.export_bundle()
        self.assertTrue(bundle["truncated"])
        self.assertEqual(bundle["dropped_count"], trace.dropped_count)
        self.assertEqual(bundle["dropped"], trace.dropped_count)
        self.assertEqual(len(bundle["actions"]), len(trace.actions))

    def test_ordering_preserved_after_truncation(self):
        """Remaining entries must retain their relative chronological order."""
        trace = ActionTrace(max_bytes=1500)

        for i in range(20):
            trace.record_action(
                tool="step",
                args={"seq": i, "payload": "A" * 120},
                result={"seq": i},
            )

        self.assertTrue(trace.truncated)
        remaining_seqs = [a.args["seq"] for a in trace.actions]

        # Verify ordering is strictly monotonically increasing
        self.assertEqual(remaining_seqs, sorted(remaining_seqs))
        self.assertGreater(len(remaining_seqs), 1)
        # Verify the newest entries are kept
        self.assertEqual(remaining_seqs[-1], 19)

    def test_screenshot_files_beyond_cap_are_omitted(self):
        """Screenshot files that exceed max_bytes must be omitted from bundle."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create two screenshot files of 3 KB each
            shot1 = tmp_path / "shot1.png"
            shot1.write_bytes(b"A" * 3000)

            shot2 = tmp_path / "shot2.png"
            shot2.write_bytes(b"B" * 3000)

            # max_bytes = 5000: enough for trace.json + shot1, but not shot2
            trace = ActionTrace(max_bytes=5000)
            trace.record_action(tool="shot_one", screenshot_path=shot1)
            trace.record_action(tool="shot_two", screenshot_path=shot2)

            bundle_dir = tmp_path / "out_bundle"
            bundle = trace.write_bundle(bundle_dir)

            # Size cap strictly enforced on disk
            total_size = sum(f.stat().st_size for f in bundle_dir.glob("*") if f.is_file())
            self.assertLessEqual(total_size, 5000)

            # Truncated marker is recorded
            self.assertTrue(bundle["truncated"])

            # shot1 was included, shot2 was omitted
            self.assertTrue((bundle_dir / "shot1.png").is_file())
            self.assertFalse((bundle_dir / "shot2.png").is_file())

            # In trace.json, action 2 has screenshot_path None and screenshot_omitted True
            trace_json = json.loads((bundle_dir / "trace.json").read_text(encoding="utf-8"))
            self.assertTrue(trace_json["truncated"])
            self.assertEqual(trace_json["actions"][0]["screenshot_path"], "shot1.png")
            self.assertIsNone(trace_json["actions"][1]["screenshot_path"])
            self.assertTrue(trace_json["actions"][1].get("screenshot_omitted"))

    def test_bounded_by_max_entries(self):
        """When max_entries is configured, list is bounded to that count."""
        trace = ActionTrace(max_entries=5)
        for i in range(12):
            trace.record_action(tool="nav", args={"i": i})

        self.assertEqual(len(trace.actions), 5)
        self.assertEqual(trace.dropped_count, 7)
        self.assertTrue(trace.truncated)
        self.assertEqual([a.args["i"] for a in trace.actions], [7, 8, 9, 10, 11])


class TestBundleExportAndWriting(unittest.TestCase):
    """Test bundle export dictionary and file writing."""

    def test_write_bundle_creates_trace_json_and_references(self):
        """write_bundle writes trace.json and copies referenced files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            shot = tmp_path / "capture.png"
            shot.write_bytes(b"\x89PNGfakeimage")

            trace = ActionTrace()
            trace.record_action(
                tool="screenshot",
                args={"full_page": True},
                result={"width": 1920, "height": 1080},
                url="https://example.com/page",
                screenshot_path=shot,
            )

            out_dir = tmp_path / "bundle_result"
            bundle = trace.write_bundle(out_dir)

            trace_file = out_dir / "trace.json"
            self.assertTrue(trace_file.is_file())
            self.assertTrue((out_dir / "capture.png").is_file())

            with open(trace_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(data["version"], "1.0")
            self.assertFalse(data["truncated"])
            self.assertEqual(data["dropped_count"], 0)
            self.assertEqual(len(data["actions"]), 1)
            self.assertEqual(data["actions"][0]["tool"], "screenshot")
            self.assertEqual(data["actions"][0]["tool_name"], "screenshot")
            self.assertEqual(data["actions"][0]["screenshot_path"], "capture.png")

    def test_empty_trace_bundle(self):
        """An empty trace exports a valid bundle without errors."""
        trace = ActionTrace()
        bundle = trace.export_bundle()
        self.assertEqual(bundle["actions"], [])
        self.assertFalse(bundle["truncated"])
        self.assertEqual(bundle["dropped_count"], 0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir) / "empty"
            trace.write_bundle(out_dir)
            self.assertTrue((out_dir / "trace.json").is_file())

    def test_clear_resets_trace(self):
        """clear() resets actions and counters."""
        trace = ActionTrace(max_bytes=500)
        for i in range(10):
            trace.record_action(tool="click", args={"data": "x" * 100})
        self.assertTrue(trace.truncated)
        self.assertGreater(trace.dropped_count, 0)

        trace.clear()
        self.assertEqual(len(trace.actions), 0)
        self.assertEqual(trace.dropped_count, 0)
        self.assertFalse(trace.truncated)

    def test_action_entry_dict_and_attr_access(self):
        """ActionEntry supports both attribute and item access."""
        entry = ActionEntry(
            timestamp="2026-11-19T12:00:00Z",
            tool="hover",
            args={"selector": ".btn"},
            result_summary={"hovered": True},
            url="https://example.com",
            screenshot_path="/tmp/shot.png",
        )
        self.assertEqual(entry.tool, "hover")
        self.assertEqual(entry.tool_name, "hover")
        self.assertEqual(entry["tool"], "hover")
        self.assertEqual(entry["tool_name"], "hover")
        self.assertEqual(entry.args["selector"], ".btn")
        self.assertEqual(entry["args"]["selector"], ".btn")
        self.assertEqual(entry.result["hovered"], True)
        self.assertEqual(entry.result_summary["hovered"], True)
        self.assertEqual(entry["result"]["hovered"], True)
        self.assertEqual(entry["result_summary"]["hovered"], True)
        self.assertEqual(entry.url, "https://example.com")
        self.assertEqual(entry["url"], "https://example.com")

    def test_thread_safe_recording(self):
        """Multiple threads recording concurrently remain safe."""
        trace = ActionTrace(max_bytes=100_000)
        errors = []

        def worker(worker_id: int):
            try:
                for j in range(20):
                    trace.record_action(
                        tool="concurrent_click",
                        args={"worker": worker_id, "step": j},
                        result={"status": "done"},
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(trace.actions), 100)


if __name__ == "__main__":
    unittest.main()
