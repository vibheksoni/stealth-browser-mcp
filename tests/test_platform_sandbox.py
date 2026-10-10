import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import platform_utils


def environment(system, root, container):
    return (
        mock.patch.object(platform_utils.platform, "system", return_value=system),
        mock.patch.object(platform_utils, "is_running_as_root", return_value=root),
        mock.patch.object(platform_utils, "is_running_in_container", return_value=container),
    )


class SandboxRuleTests(unittest.TestCase):
    def check(self, system, root, container):
        patches = environment(system, root, container)
        with patches[0], patches[1], patches[2]:
            return platform_utils.sandbox_must_be_disabled(), platform_utils.get_required_sandbox_args()

    def test_windows_admin_keeps_sandbox(self):
        self.assertEqual(self.check("Windows", True, False), (False, []))

    def test_macos_keeps_sandbox(self):
        self.assertEqual(self.check("Darwin", True, False), (False, []))

    def test_linux_root_or_container_disables_sandbox_only(self):
        self.assertEqual(self.check("Linux", True, False), (True, ["--no-sandbox"]))
        self.assertEqual(self.check("Linux", False, True), (True, ["--no-sandbox"]))

    def test_linux_user_keeps_sandbox(self):
        self.assertEqual(self.check("Linux", False, False), (False, []))

    def test_merge_never_adds_warning_flags(self):
        patches = environment("Linux", True, True)
        with patches[0], patches[1], patches[2]:
            merged = platform_utils.merge_browser_args(["--lang=en-US"])
        self.assertEqual(merged, ["--lang=en-US", "--no-sandbox"])
        for flag in ("--disable-setuid-sandbox", "--single-process", "--disable-gpu"):
            self.assertNotIn(flag, merged)


if __name__ == "__main__":
    unittest.main()
