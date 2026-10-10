import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import browser_manager as browser_manager_module
from browser_manager import BrowserManager

HEADLESS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/154.0.0.0 Safari/537.36"
)
CLEAN_UA = HEADLESS_UA.replace("HeadlessChrome/", "Chrome/")


class FakeTab:
    def __init__(self, user_agent):
        self.user_agent = user_agent

    async def send(self, command):
        return ("1.3", "HeadlessChrome/154.0.8037.98", "rev", self.user_agent, "js")


class FakeBrowser:
    def __init__(self, user_agent):
        self.main_tab = FakeTab(user_agent)
        self.config = mock.Mock(user_data_dir="/tmp/uc_probe")
        self._process = mock.Mock(pid=4242, returncode=0)


class HeadlessUserAgentTests(unittest.TestCase):
    def test_headful_user_agent(self):
        self.assertEqual(BrowserManager._headful_user_agent(HEADLESS_UA), CLEAN_UA)
        self.assertIsNone(BrowserManager._headful_user_agent(CLEAN_UA))

    def test_has_user_agent_arg(self):
        self.assertTrue(BrowserManager._has_user_agent_arg(["--foo", "--user-agent=x"]))
        self.assertFalse(BrowserManager._has_user_agent_arg(["--foo"]))

    def test_probe_result_is_cached_and_cleaned_up(self):
        browsers = []

        async def fake_start(config):
            browser = FakeBrowser(HEADLESS_UA)
            browsers.append((config, browser))
            return browser

        manager = BrowserManager()
        cleanup = mock.Mock()
        with mock.patch.object(browser_manager_module.uc, "start", side_effect=fake_start), mock.patch.object(
            browser_manager_module, "process_cleanup", cleanup
        ):
            first = asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, ["--lang=en-US"]))
            second = asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, ["--lang=en-US"]))

        self.assertEqual(first, CLEAN_UA)
        self.assertEqual(second, CLEAN_UA)
        self.assertEqual(len(browsers), 1)
        config, browser = browsers[0]
        self.assertTrue(config.headless)
        self.assertIsNone(browser._process)
        cleanup.track_browser_process.assert_called_once()
        cleanup.kill_browser_process.assert_called_once()

    def test_probe_failure_is_not_cached(self):
        calls = []

        async def failing_start(config):
            calls.append(config)
            raise RuntimeError("no browser")

        manager = BrowserManager()
        with mock.patch.object(browser_manager_module.uc, "start", side_effect=failing_start):
            self.assertIsNone(asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, [])))
            self.assertIsNone(asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, [])))
        self.assertEqual(len(calls), 2)

    def test_clean_user_agent_is_cached_as_none(self):
        async def fake_start(config):
            return FakeBrowser(CLEAN_UA)

        manager = BrowserManager()
        with mock.patch.object(browser_manager_module.uc, "start", side_effect=fake_start) as start, mock.patch.object(
            browser_manager_module, "process_cleanup", mock.Mock()
        ):
            self.assertIsNone(asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, [])))
            self.assertIsNone(asyncio.run(manager._resolve_headless_user_agent("/missing/chrome", True, [])))
        self.assertEqual(start.call_count, 1)


if __name__ == "__main__":
    unittest.main()
