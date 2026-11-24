"""Tests for secret environment variable resolution and redaction."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.debug_logger import debug_logger
from stealth_browser_mcp.secrets_env import (
    REDACTED_PLACEHOLDER,
    SECRET_ENV_PREFIX,
    Redactor,
    SecretEnvError,
    clear_secrets,
    default_redactor,
    redact,
    register_secret,
    resolve_value_env,
)


class ResolveValueEnvTests(unittest.TestCase):
    """Tests for resolve_value_env validation and error handling."""

    def setUp(self):
        self._orig_environ = dict(os.environ)
        clear_secrets()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_environ)
        clear_secrets()

    def test_non_prefixed_names_rejected(self):
        """Variables without the STEALTH_SECRET_ prefix must be rejected."""
        invalid_names = [
            "SECRET_PASSWORD",
            "API_KEY",
            "stealth_secret_lower",
            "MY_STEALTH_SECRET_KEY",
            "MY_STEALTH_SECRET_X",
            "",
            "INVALID=KEY",
            "PASSWORD",
        ]
        for name in invalid_names:
            is_legal = bool(isinstance(name, str) and name and "=" not in name and "\0" not in name)
            if is_legal:
                os.environ[name] = "super_secret_value_123"
            with self.subTest(name=name):
                with self.assertRaises(SecretEnvError) as ctx:
                    resolve_value_env(name)
                # Verify secret value is never echoed in error message
                self.assertNotIn("super_secret_value_123", str(ctx.exception))
                self.assertIn(SECRET_ENV_PREFIX, str(ctx.exception))

    def test_non_string_names_rejected(self):
        """Non-string names must be rejected with SecretEnvError."""
        for invalid in [None, 123, ["STEALTH_SECRET_KEY"]]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(SecretEnvError):
                    resolve_value_env(invalid)

    def test_missing_env_var_rejected(self):
        """Prefixed variables not present in os.environ must raise SecretEnvError."""
        missing_var = "STEALTH_SECRET_NON_EXISTENT_VAR_XYZ"
        if missing_var in os.environ:
            del os.environ[missing_var]

        with self.assertRaises(SecretEnvError) as ctx:
            resolve_value_env(missing_var)

        self.assertIn(missing_var, str(ctx.exception))
        # Ensure error clearly indicates missing without leaking values
        self.assertIn("not set", str(ctx.exception).lower())

    def test_valid_prefixed_var_resolves_and_registers(self):
        """Valid prefixed variables return value and auto-register for redaction."""
        var_name = "STEALTH_SECRET_AUTH_TOKEN"
        secret_val = "bearer-secret-token-value-987"
        os.environ[var_name] = secret_val

        resolved = resolve_value_env(var_name)
        self.assertEqual(resolved, secret_val)

        # Value must be in redactor registry
        self.assertIn(secret_val, default_redactor)

        # Redaction should now mask it
        text = f"Using credentials: {secret_val} for authorization"
        redacted = redact(text)
        self.assertNotIn(secret_val, redacted)
        self.assertIn(REDACTED_PLACEHOLDER, redacted)
        self.assertEqual(redacted, f"Using credentials: {REDACTED_PLACEHOLDER} for authorization")


class RedactorTests(unittest.TestCase):
    """Tests for Redactor class and payload redaction."""

    def setUp(self):
        self.redactor = Redactor()

    def test_empty_registry_returns_obj(self):
        """When no secrets are registered, payloads are returned as-is."""
        payload = {"token": "secret_abc", "items": [1, 2, "secret_abc"]}
        self.assertEqual(self.redactor.redact(payload), payload)
        self.assertEqual(self.redactor.redact("secret_abc"), "secret_abc")

    def test_register_ignores_empty_or_invalid(self):
        """Empty string or non-string inputs to register are ignored."""
        self.redactor.register("")
        self.redactor.register(None)
        self.assertEqual(len(self.redactor), 0)
        self.assertEqual(self.redactor.redact("hello world"), "hello world")

    def test_redaction_single_string(self):
        """Secret substrings in strings are replaced with placeholder."""
        self.redactor.register("p@ssw0rd")
        text = "Login with password p@ssw0rd now"
        expected = f"Login with password {REDACTED_PLACEHOLDER} now"
        self.assertEqual(self.redactor.redact(text), expected)

    def test_redaction_multiple_occurrences(self):
        """Multiple occurrences of the same secret in a string are replaced."""
        self.redactor.register("token123")
        text = "token123 and token123 again"
        expected = f"{REDACTED_PLACEHOLDER} and {REDACTED_PLACEHOLDER} again"
        self.assertEqual(self.redactor.redact(text), expected)

    def test_redaction_longer_secrets_matched_first(self):
        """Longer secrets are replaced first to prevent partial redaction bugs."""
        self.redactor.register(["secret", "super_secret"])
        text = "Value is super_secret and secret"
        expected = f"Value is {REDACTED_PLACEHOLDER} and {REDACTED_PLACEHOLDER}"
        self.assertEqual(self.redactor.redact(text), expected)

    def test_redaction_nested_payload(self):
        """Nested dicts, lists, tuples, and sets are recursively redacted."""
        secret_a = "secret_alpha_99"
        secret_b = "secret_beta_88"
        self.redactor.register([secret_a, secret_b])

        payload = {
            "endpoint": "https://api.example.com",
            "auth": {
                "token": secret_a,
                "header": f"Bearer {secret_a}",
                "nested_dict": {
                    "sub_token": secret_b,
                    "mixed": [secret_a, 42, False, None, {"deep": f"prefix-{secret_b}-suffix"}],
                },
            },
            "tuple_data": (secret_a, 10),
            "set_data": {secret_b},
            "count": 5,
        }

        redacted = self.redactor.redact(payload)

        # Verify all occurrences replaced
        self.assertEqual(redacted["auth"]["token"], REDACTED_PLACEHOLDER)
        self.assertEqual(redacted["auth"]["header"], f"Bearer {REDACTED_PLACEHOLDER}")
        self.assertEqual(redacted["auth"]["nested_dict"]["sub_token"], REDACTED_PLACEHOLDER)
        self.assertEqual(
            redacted["auth"]["nested_dict"]["mixed"],
            [REDACTED_PLACEHOLDER, 42, False, None, {"deep": f"prefix-{REDACTED_PLACEHOLDER}-suffix"}],
        )
        self.assertEqual(redacted["tuple_data"], (REDACTED_PLACEHOLDER, 10))
        self.assertEqual(redacted["set_data"], {REDACTED_PLACEHOLDER})
        self.assertEqual(redacted["endpoint"], "https://api.example.com")
        self.assertEqual(redacted["count"], 5)

        # Original payload must not be mutated
        self.assertEqual(payload["auth"]["token"], secret_a)

    def test_circular_reference_handled_gracefully(self):
        """Circular references do not cause recursion errors."""
        self.redactor.register("secret_token")
        circular_list = ["secret_token"]
        circular_list.append(circular_list)

        result = self.redactor.redact(circular_list)
        self.assertEqual(result[0], REDACTED_PLACEHOLDER)
        self.assertEqual(result[1], REDACTED_PLACEHOLDER)


class DebugLoggerRedactionTests(unittest.TestCase):
    """Tests verifying debug_logger payloads and exports are redacted."""

    def setUp(self):
        self._orig_environ = dict(os.environ)
        clear_secrets()
        debug_logger.enable()
        debug_logger.clear_debug_view()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_environ)
        debug_logger.clear_debug_view()
        debug_logger.disable()
        clear_secrets()

    def test_debug_logger_payload_does_not_contain_secret_after_logging(self):
        """Secret resolved via resolve_value_env must never appear in debug_logger."""
        secret_var = "STEALTH_SECRET_DB_PASSWORD"
        secret_val = "pAssW0rd_sUpEr_sEcReT_456"
        os.environ[secret_var] = secret_val

        # Resolve secret
        resolved = resolve_value_env(secret_var)
        self.assertEqual(resolved, secret_val)

        # 1. Log Info with secret in message and data payload
        debug_logger.log_info(
            component="database",
            method="connect",
            message=f"Connecting with password {secret_val}",
            data={
                "connection_string": f"postgres://user:{secret_val}@localhost:5432/db",
                "nested": [{"token": secret_val}],
            },
        )

        # 2. Log Warning with secret in context
        debug_logger.log_warning(
            component="database",
            method="retry",
            message="Retry attempted",
            context={"last_secret_attempted": secret_val},
        )

        # 3. Log Error with secret in exception message and context
        debug_logger.log_error(
            component="database",
            method="auth",
            error=ValueError(f"Authentication failed for secret {secret_val}"),
            context={"credentials": {"pass": secret_val}},
        )

        # Retrieve debug view
        view = debug_logger.get_debug_view()

        # String representation of the entire debug view must NOT contain the raw secret
        view_str = json.dumps(view, default=str)
        self.assertNotIn(secret_val, view_str)
        self.assertIn(REDACTED_PLACEHOLDER, view_str)

        # Verify specific fields were properly redacted
        recent_info = view["recent_info"][-1]
        self.assertIn(REDACTED_PLACEHOLDER, recent_info["message"])
        self.assertNotIn(secret_val, recent_info["message"])
        self.assertIn(REDACTED_PLACEHOLDER, recent_info["data"]["connection_string"])
        self.assertEqual(recent_info["data"]["nested"][0]["token"], REDACTED_PLACEHOLDER)

        recent_warning = view["recent_warnings"][-1]
        self.assertEqual(recent_warning["context"]["last_secret_attempted"], REDACTED_PLACEHOLDER)

        recent_error = view["recent_errors"][-1]
        self.assertIn(REDACTED_PLACEHOLDER, recent_error["error_message"])
        self.assertEqual(recent_error["context"]["credentials"]["pass"], REDACTED_PLACEHOLDER)

    def test_debug_logger_export_does_not_contain_secret(self):
        """Exported debug log file must not contain secret values."""
        secret_var = "STEALTH_SECRET_API_TOKEN"
        secret_val = "tok_live_1234567890abcdef"
        os.environ[secret_var] = secret_val

        resolve_value_env(secret_var)

        debug_logger.log_info("api", "request", f"Token {secret_val} used", data={"auth": secret_val})

        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = os.path.join(tmpdir, "debug_export.json")
            debug_logger.export_to_file_paginated(export_path, format="json")

            with open(export_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertNotIn(secret_val, content)
            self.assertIn(REDACTED_PLACEHOLDER, content)


if __name__ == "__main__":
    unittest.main()
