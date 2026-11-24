"""SB-81 / review F3: allowlist spawn_browser flags and confine user_data_dir."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.symlink_helper import create_symlink

from stealth_browser_mcp.browser_args_policy import (
    BrowserArgsPolicyError,
    managed_profiles_root,
    validate_browser_args,
    validate_sandbox,
    validate_user_data_dir,
)
from stealth_browser_mcp.browser_manager import BrowserManager
from stealth_browser_mcp.models import BrowserOptions
from stealth_browser_mcp.server import spawn_browser


ATTACK_ARGS = ["--no-zygote", "--renderer-cmd-prefix=/tmp/x"]


class AllowlistTests(unittest.TestCase):
    def test_window_size_allowed(self) -> None:
        self.assertEqual(
            validate_browser_args(["--window-size=800,600"]),
            ["--window-size=800,600"],
        )

    def test_attack_args_rejected(self) -> None:
        with self.assertRaisesRegex(BrowserArgsPolicyError, "no-zygote"):
            validate_browser_args(ATTACK_ARGS)

    def test_renderer_cmd_prefix_named_in_error(self) -> None:
        with self.assertRaisesRegex(BrowserArgsPolicyError, "renderer-cmd-prefix"):
            validate_browser_args(["--renderer-cmd-prefix=/tmp/x"])

    def test_unknown_flag_rejected(self) -> None:
        with self.assertRaisesRegex(BrowserArgsPolicyError, "not in the spawn_browser allowlist"):
            validate_browser_args(["--kiosk"])

    def test_user_data_dir_flag_must_use_argument(self) -> None:
        with self.assertRaisesRegex(BrowserArgsPolicyError, "user-data-dir"):
            validate_browser_args(["--user-data-dir=/tmp/pwn"])


class UserDataDirTests(unittest.TestCase):
    def test_etc_rejected(self) -> None:
        with self.assertRaisesRegex(BrowserArgsPolicyError, "managed profiles root"):
            validate_user_data_dir("/etc")

    def test_symlink_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            link = root / "escape"
            create_symlink(link, "/etc", target_is_directory=True)
            with mock.patch.dict(os.environ, {"STEALTH_MANAGED_PROFILES_ROOT": str(root)}):
                with self.assertRaisesRegex(BrowserArgsPolicyError, "symlink|managed profiles root"):
                    validate_user_data_dir(str(link))

    def test_path_under_root_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            dest = root / "session-a"
            dest.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"STEALTH_MANAGED_PROFILES_ROOT": str(root)}):
                self.assertEqual(Path(validate_user_data_dir(str(dest))), dest.resolve())
                self.assertEqual(managed_profiles_root(), root.resolve())

    def test_none_allowed(self) -> None:
        self.assertIsNone(validate_user_data_dir(None))
        self.assertIsNone(validate_user_data_dir("  "))


class SandboxPolicyTests(unittest.TestCase):
    def test_sandbox_false_rejected_on_http_when_not_required(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.browser_args_policy.sandbox_must_be_disabled",
            return_value=False,
        ):
            with self.assertRaisesRegex(BrowserArgsPolicyError, "sandbox=false"):
                validate_sandbox(False, http_mode=True)

    def test_sandbox_false_allowed_when_platform_requires_it(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.browser_args_policy.sandbox_must_be_disabled",
            return_value=True,
        ):
            self.assertFalse(validate_sandbox(False, http_mode=True))

    def test_sandbox_true_allowed(self) -> None:
        with mock.patch(
            "stealth_browser_mcp.browser_args_policy.sandbox_must_be_disabled",
            return_value=False,
        ):
            self.assertTrue(validate_sandbox(True, http_mode=True))


class SpawnBoundaryTests(unittest.TestCase):
    def test_spawn_browser_tool_rejects_attack_args(self) -> None:
        with self.assertRaisesRegex(Exception, "no-zygote|renderer-cmd-prefix"):
            asyncio.run(
                spawn_browser.fn(
                    browser_args=ATTACK_ARGS,
                    sandbox=False,
                )
            )

    def test_attack_args_never_reach_merge_browser_args(self) -> None:
        manager = BrowserManager()
        with mock.patch(
            "stealth_browser_mcp.browser_manager.merge_browser_args"
        ) as merged:
            with mock.patch(
                "stealth_browser_mcp.browser_args_policy.sandbox_must_be_disabled",
                return_value=False,
            ):
                with self.assertRaises(BrowserArgsPolicyError):
                    asyncio.run(
                        manager.spawn_browser(
                            BrowserOptions(browser_args=list(ATTACK_ARGS))
                        )
                    )
            merged.assert_not_called()


if __name__ == "__main__":
    unittest.main()
