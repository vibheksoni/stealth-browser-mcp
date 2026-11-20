"""SB-80 / review F2: caller Python hooks stay off unless explicitly allowed."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stealth_browser_mcp.dynamic_hook_ai_interface import DynamicHookAIInterface
from stealth_browser_mcp.hook_policy import (
    code_hooks_disabled_result,
    code_hooks_enabled,
    refuse_code_hooks_off_loopback,
)
from stealth_browser_mcp.server import create_dynamic_hook, validate_hook_function


# Review-style payload: would create a marker if compile/exec ran at create time.
REVIEW_PAYLOAD = """
x=[c for c in ().__class__.__base__.__subclasses__() if c.__name__=='_wrap_close'][0].__init__.__globals__['system']({marker!r})
def process_request(request):
    return HookAction(action="continue")
"""


class CodeHookPolicyTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_CODE_HOOKS", None)
            self.assertFalse(code_hooks_enabled())

    def test_truthy_and_falsy(self) -> None:
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_CODE_HOOKS": "1"}):
            self.assertTrue(code_hooks_enabled())
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_CODE_HOOKS": "0"}):
            self.assertFalse(code_hooks_enabled())

    def test_refuse_off_loopback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_CODE_HOOKS", None)
            refuse_code_hooks_off_loopback("0.0.0.0")
        with mock.patch.dict(os.environ, {"STEALTH_ALLOW_CODE_HOOKS": "1"}):
            refuse_code_hooks_off_loopback("127.0.0.1")
            refuse_code_hooks_off_loopback("localhost")
            with self.assertRaisesRegex(ValueError, "STEALTH_ALLOW_CODE_HOOKS"):
                refuse_code_hooks_off_loopback("0.0.0.0")
            with self.assertRaisesRegex(ValueError, "STEALTH_ALLOW_CODE_HOOKS"):
                refuse_code_hooks_off_loopback("192.168.1.10")


class ReviewPayloadDisabledTests(unittest.TestCase):
    """The F2 review attack must error and must not create a marker file."""

    def test_review_payload_does_not_run_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "stealth-hook-pwn-test"
            payload = REVIEW_PAYLOAD.format(marker="touch {}".format(marker))
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STEALTH_ALLOW_CODE_HOOKS", None)
                result = asyncio.run(
                    create_dynamic_hook.fn(
                        name="pwn",
                        requirements={"url_pattern": "*"},
                        function_code=payload,
                    )
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "code_hooks_disabled")
                self.assertFalse(marker.exists())

                validated = validate_hook_function.fn(function_code=payload)
                self.assertFalse(validated["success"])
                self.assertEqual(validated["error"], "code_hooks_disabled")
                self.assertFalse(marker.exists())

    def test_ai_interface_create_dynamic_hook_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "stealth-hook-pwn-test"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STEALTH_ALLOW_CODE_HOOKS", None)
                ai = DynamicHookAIInterface()
                result = asyncio.run(
                    ai.create_dynamic_hook(
                        name="pwn",
                        requirements={"url_pattern": "*"},
                        function_code=REVIEW_PAYLOAD.format(marker="touch {}".format(marker)),
                    )
                )
                self.assertEqual(result, code_hooks_disabled_result())
                self.assertFalse(marker.exists())

    def test_simple_hook_still_works_when_code_hooks_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STEALTH_ALLOW_CODE_HOOKS", None)
            ai = DynamicHookAIInterface()
            result = asyncio.run(
                ai.create_simple_hook(
                    name="block-ads",
                    url_pattern="*ads*",
                    action="block",
                )
            )
            self.assertTrue(result["success"])
            hook_id = result["hook_id"]
            removed = asyncio.run(ai.remove_dynamic_hook(hook_id))
            self.assertTrue(removed.get("success"))


if __name__ == "__main__":
    unittest.main()
