import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import seed_identity


SEED = {
    "user_agent": "seed-ua",
    "lang": "en-US",
    "timezone": "UTC",
    "window_size": "1920,1080",
    "platform": "linux",
    "proxy": "http://seed:3128",
}


def _make_identity_file(directory: Path, body: str):
    (directory / seed_identity.IDENTITY_FILE).write_text(body, encoding="utf-8")


class PrecedenceTests(unittest.TestCase):
    def test_explicit_values_win(self):
        explicit = {
            "user_agent": "my-ua",
            "lang": "fr",
            "timezone": "Europe/Paris",
            "window_size": "1280,720",
            "platform": "win32",
            "proxy": "http://local:8080",
        }
        result = seed_identity.merge_identity_defaults(SEED, explicit)
        self.assertEqual(result, explicit)

    def test_explicit_none_uses_seed_default(self):
        explicit = {"user_agent": None, "lang": None}
        result = seed_identity.merge_identity_defaults(SEED, explicit)
        self.assertEqual(result["user_agent"], "seed-ua")
        self.assertEqual(result["lang"], "en-US")
        self.assertEqual(result["timezone"], "UTC")

    def test_explicit_missing_key_uses_seed_default(self):
        result = seed_identity.merge_identity_defaults(SEED, {"platform": "macos"})
        self.assertEqual(result["platform"], "macos")
        self.assertEqual(result["user_agent"], "seed-ua")
        self.assertEqual(result["proxy"], "http://seed:3128")

    def test_missing_seed_uses_explicit(self):
        explicit = {"user_agent": "my-ua", "proxy": "http://local:8080"}
        result = seed_identity.merge_identity_defaults({}, explicit)
        self.assertEqual(result["user_agent"], "my-ua")
        self.assertEqual(result["proxy"], "http://local:8080")
        self.assertIsNone(result["lang"])

    def test_unknown_keys_in_seed_are_ignored(self):
        seed_with_extra = {**SEED, "extra": "value", "notes": ["a", "b"]}
        result = seed_identity.merge_identity_defaults(seed_with_extra, {})
        for key in seed_identity.KNOWN_KEYS:
            self.assertIn(key, result)
        self.assertNotIn("extra", result)
        self.assertNotIn("notes", result)

    def test_known_keys_only_in_output(self):
        explicit = {"user_agent": "my-ua"}
        result = seed_identity.merge_identity_defaults({}, explicit)
        self.assertEqual(set(result.keys()), set(seed_identity.KNOWN_KEYS))
        self.assertEqual(result["user_agent"], "my-ua")

    def test_empty_inputs(self):
        self.assertEqual(
            set(seed_identity.merge_identity_defaults({}, {}).keys()),
            set(seed_identity.KNOWN_KEYS),
        )


class NoMutationTests(unittest.TestCase):
    def test_inputs_not_mutated(self):
        seed = deepcopy(SEED)
        explicit = {"user_agent": "my-ua", "lang": None}
        seed_identity.merge_identity_defaults(seed, explicit)
        self.assertEqual(seed, deepcopy(SEED))
        self.assertEqual(explicit, {"user_agent": "my-ua", "lang": None})

    def test_none_in_explicit_left_in_place(self):
        explicit = {"user_agent": None}
        seed_identity.merge_identity_defaults(SEED, explicit)
        self.assertEqual(explicit, {"user_agent": None})


class LoadIdentityTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="stealth-seed-identity-test-")
        self.path = Path(self._dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        self.assertEqual(seed_identity.load_identity(self.path), {})

    def test_invalid_json_returns_empty(self):
        _make_identity_file(self.path, "{not valid json")
        self.assertEqual(seed_identity.load_identity(self.path), {})

    def test_empty_body_returns_empty(self):
        _make_identity_file(self.path, "")
        self.assertEqual(seed_identity.load_identity(self.path), {})

    def test_non_object_json_returns_empty(self):
        _make_identity_file(self.path, "[1, 2, 3]")
        self.assertEqual(seed_identity.load_identity(self.path), {})

    def test_valid_identity_parsed(self):
        body = json.dumps({"user_agent": "loaded-ua", "platform": "linux"})
        _make_identity_file(self.path, body)
        self.assertEqual(
            seed_identity.load_identity(self.path),
            {"user_agent": "loaded-ua", "platform": "linux"},
        )

    def test_accepts_string_path(self):
        body = json.dumps({"lang": "de-DE"})
        _make_identity_file(self.path, body)
        self.assertEqual(seed_identity.load_identity(self._dir)["lang"], "de-DE")


if __name__ == "__main__":
    unittest.main()
