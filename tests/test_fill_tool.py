"""Unit tests and gated browser integration tests for fill_tool and paste_text modes."""

import asyncio
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nodriver as uc

from stealth_browser_mcp import dom_handler, element_kind, fill_tool, server
from stealth_browser_mcp.element_kind import ElementKind
from stealth_browser_mcp.paths import DATA_DIR_ENV
from stealth_browser_mcp.secrets_env import (
    REDACTED_PLACEHOLDER,
    SECRET_ENV_PREFIX,
    SecretEnvError,
    clear_secrets,
    default_redactor,
    register_secret,
)
from tests.browser_harness.server import FixtureServer

RUN_BROWSER_TESTS = os.getenv("STEALTH_BROWSER_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


class FakeFillTab:
    """Tab recording CDP commands and answering DOM, Runtime, and Input calls."""

    def __init__(self, backend_node_id=42, facts=None, read_value=None):
        self.backend_node_id = backend_node_id
        self.facts = facts or {}
        self.read_value = read_value
        self.calls = []
        self.events = []
        self.typed_chars = []
        self.current_value = None
        self.current_checked = None

    async def send(self, command):
        request = next(command)
        method = request["method"]
        params = request.get("params", {})
        self.calls.append((method, params))

        if method.startswith("Input."):
            t = params.get("type")
            char = params.get("text") or params.get("key")
            self.events.append((method, t, char))
            if (t == "keyDown" or t == "rawKeyDown") and char:
                if "paste" in params.get("commands", []):
                    try:
                        from stealth_browser_mcp import os_clipboard
                        b = os_clipboard._get_backend()
                        if b is not None:
                            self.current_value = b.get_text()
                    except Exception:
                        pass
                if t == "keyDown":
                    self.typed_chars.append(char)
                if char == "Backspace":
                    self.current_value = ""
                elif t == "keyDown" and len(char) == 1:
                    if self.current_value is None:
                        self.current_value = ""
                    self.current_value += char
            elif method == "Input.insertText":
                ins_text = params.get("text", "")
                if self.current_value is None:
                    self.current_value = ""
                self.current_value += ins_text
            return None
        if method == "DOM.focus":
            return None
        if method == "DOM.getContentQuads":
            return [[10, 20, 30, 20, 30, 40, 10, 40]]
        if method == "DOM.resolveNode":
            return SimpleNamespace(object_id=uc.cdp.runtime.RemoteObjectId("obj-42"))
        if method == "DOM.getDocument":
            return SimpleNamespace(node_id=uc.cdp.dom.NodeId(1))
        if method == "DOM.querySelector":
            return uc.cdp.dom.NodeId(10)
        if method == "DOM.describeNode":
            return SimpleNamespace(
                backend_node_id=uc.cdp.dom.BackendNodeId(self.backend_node_id)
            )
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            return_by_val = params.get("returnByValue", False)

            # Check if an option is being located or verified in a combobox
            m = re.search(r'\)\(\s*"([^"]+)"', expr)
            if m:
                self.current_value = m.group(1)
            elif "Option A" in expr:
                self.current_value = "Option A"

            if return_by_val:
                val = {"verified": True, "reason": "verified"}
                if self.current_value is not None:
                    val["value"] = self.current_value
                    val["text"] = self.current_value
                return SimpleNamespace(
                    object_id=uc.cdp.runtime.RemoteObjectId("elem-obj-1"),
                    value=val,
                ), None
            return SimpleNamespace(
                object_id=uc.cdp.runtime.RemoteObjectId("elem-obj-1"),
                value=self.current_value,
            ), None
        if method == "Runtime.releaseObject":
            return None
        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            args = params.get("arguments", [])

            def _get_arg(idx=0, default=None):
                if not args or len(args) <= idx:
                    return default
                a = args[idx]
                if isinstance(a, dict):
                    return a.get("value", default)
                return getattr(a, "value", default)

            # Probe facts query
            if "tag_name" in fn and "is_content_editable" in fn:
                return SimpleNamespace(value=self.facts), None

            # Read value query
            if "selectedIndex" in fn and "SELECT" in fn:
                if self.read_value is not None:
                    return SimpleNamespace(value=self.read_value), None
                tag = str(self.facts.get("tag_name") or "").lower()
                inp_type = str(self.facts.get("input_type") or "").lower()
                if tag == "select":
                    val = self.current_value or "first"
                    return SimpleNamespace(value={"value": val, "text": val}), None
                if inp_type in ("checkbox", "radio"):
                    return SimpleNamespace(
                        value={"checked": bool(self.current_checked), "value": "agree"}
                    ), None
                if self.facts.get("is_content_editable") or self.facts.get("contenteditable"):
                    return SimpleNamespace(value={"text": str(self.current_value or "")}), None
                return SimpleNamespace(
                    value={"value": str(self.current_value or ""), "text": str(self.current_value or "")}
                ), None

            # Native setter
            if "HTMLInputElement" in fn and "value" in fn:
                arg_val = _get_arg(0, "")
                self.current_value = arg_val
                return SimpleNamespace(value=arg_val), None

            # Synthetic paste
            if "ClipboardEvent" in fn:
                arg_val = _get_arg(0, "")
                self.current_value = arg_val
                return SimpleNamespace(
                    value={"default_prevented": True, "defaultPrevented": True}
                ), None

            # Checkbox / Radio setter
            if "checkedVal" in fn:
                arg_val = _get_arg(0, False)
                self.current_checked = arg_val
                return SimpleNamespace(value=arg_val), None

            # Select option on node
            if "criteria" in fn:
                arg_val = _get_arg(0, "")
                self.current_value = arg_val
                return SimpleNamespace(value=True), None

            # Content clearing
            if "selectNodeContents" in fn or "this.select()" in fn:
                self.current_value = ""
                return SimpleNamespace(value=True), None

            return SimpleNamespace(value=True), None

        return None


class ClassifyAndStrategyTableTests(unittest.TestCase):
    """Table-driven unit tests verifying strategy=auto across every element kind."""

    def setUp(self):
        self._orig_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name
        clear_secrets()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()
        clear_secrets()

    def test_every_kind_row_passes_with_strategy_auto(self):
        """Every element kind row passes with strategy=auto and verified=True."""
        kind_cases = [
            # (kind, facts_dict, value, expected_strategy)
            (
                ElementKind.TEXT,
                {"tag_name": "input", "type": "text", "input_type": "text"},
                "hello text",
                "native_setter",
            ),
            (
                ElementKind.PASSWORD,
                {"tag_name": "input", "type": "password", "input_type": "password"},
                "secret_pw_123",
                "native_setter",
            ),
            (
                ElementKind.EMAIL,
                {"tag_name": "input", "type": "email", "input_type": "email"},
                "user@example.com",
                "native_setter",
            ),
            (
                ElementKind.NUMBER,
                {"tag_name": "input", "type": "number", "input_type": "number"},
                "42",
                "native_setter",
            ),
            (
                ElementKind.TEXTAREA,
                {"tag_name": "textarea"},
                "multiline\ntext",
                "native_setter",
            ),
            (
                ElementKind.DATE,
                {"tag_name": "input", "type": "date", "input_type": "date"},
                "2026-11-19",
                "native_setter",
            ),
            (
                ElementKind.TIME,
                {"tag_name": "input", "type": "time", "input_type": "time"},
                "15:30",
                "native_setter",
            ),
            (
                ElementKind.DATETIME,
                {"tag_name": "input", "type": "datetime-local", "input_type": "datetime"},
                "2026-11-19T15:30",
                "native_setter",
            ),
            (
                ElementKind.MONTH,
                {"tag_name": "input", "type": "month", "input_type": "month"},
                "2026-11",
                "native_setter",
            ),
            (
                ElementKind.WEEK,
                {"tag_name": "input", "type": "week", "input_type": "week"},
                "2026-W47",
                "native_setter",
            ),
            (
                ElementKind.COLOR,
                {"tag_name": "input", "type": "color", "input_type": "color"},
                "#ff5500",
                "native_setter",
            ),
            (
                ElementKind.RANGE,
                {"tag_name": "input", "type": "range", "input_type": "range"},
                "50",
                "native_setter",
            ),
            (
                ElementKind.CONTENTEDITABLE,
                {"tag_name": "div", "is_content_editable": True, "contenteditable": "true"},
                "editable content",
                "synthetic_paste",
            ),
            (
                ElementKind.RICH_EDITOR,
                {
                    "tag_name": "div",
                    "is_content_editable": True,
                    "contenteditable": "true",
                    "hints": ["ProseMirror"],
                },
                "editor content",
                "synthetic_paste",
            ),
            (
                ElementKind.SELECT,
                {"tag_name": "select", "options": 3},
                "Second Option",
                "choose_option",
            ),
            (
                ElementKind.COMBOBOX,
                {"tag_name": "input", "aria_role": "combobox"},
                "Option A",
                "choose_option",
            ),
            (
                ElementKind.CHECKBOX,
                {"tag_name": "input", "type": "checkbox", "input_type": "checkbox"},
                True,
                "checkbox",
            ),
            (
                ElementKind.RADIO,
                {"tag_name": "input", "type": "radio", "input_type": "radio"},
                "opt1",
                "radio",
            ),
        ]

        for kind, facts_dict, value, expected_strat in kind_cases:
            with self.subTest(kind=kind, strategy=expected_strat):
                tab = FakeFillTab(backend_node_id=42, facts=facts_dict)
                result = asyncio.run(
                    fill_tool.fill(
                        tab=tab,
                        target="#test-target",
                        value=value,
                        strategy="auto",
                        verify=True,
                    )
                )

                self.assertTrue(result["success"], f"Failed for kind {kind}: {result}")
                self.assertEqual(result["strategy"], expected_strat)
                self.assertEqual(result["kind"], kind)
                self.assertTrue(result["verified"])
                self.assertIn("preview", result)


class VerifyMismatchReportingTests(unittest.TestCase):
    """Tests for mismatch reporting when read-back does not match expected value."""

    def setUp(self):
        self._orig_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_mismatch_reports_verified_false_and_diff(self):
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "text", "input_type": "text"},
            read_value={"value": "different_value"},
        )

        result = asyncio.run(
            fill_tool.fill(
                tab=tab,
                target="#text-input",
                value="expected_value",
                strategy="auto",
                verify=True,
            )
        )

        self.assertFalse(result["verified"])
        self.assertFalse(result["success"])
        self.assertIn("mismatch", result)
        self.assertGreaterEqual(result["mismatch"]["first_diff_index"], 0)
        self.assertIn("actual_preview", result["mismatch"])
        self.assertIn("different_value", result["mismatch"]["actual_preview"]["text"])


class PasswordAndSecretSecurityTests(unittest.TestCase):
    """Acceptance: Password never echoed in output or error messages."""

    def setUp(self):
        self._orig_environ = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name
        clear_secrets()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_environ)
        self._tmp.cleanup()
        clear_secrets()

    def test_password_field_value_never_echoed_in_result(self):
        secret_password = "SuperSecretP@ssw0rd999!"
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "password", "input_type": "password"},
        )

        result = asyncio.run(
            fill_tool.fill(
                tab=tab,
                target="#password-input",
                value=secret_password,
                verify=True,
            )
        )

        result_str = str(result)
        self.assertNotIn(secret_password, result_str)
        self.assertTrue(result["preview"]["redacted"])
        self.assertEqual(result["preview"]["length"], len(secret_password))
        self.assertIn("sha256_prefix", result["preview"])

    def test_password_mismatch_never_echoes_passwords(self):
        secret_expected = "ExpectedSecret123"
        secret_actual = "ActualWrongSecret456"
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "password", "input_type": "password"},
            read_value={"value": secret_actual},
        )

        result = asyncio.run(
            fill_tool.fill(
                tab=tab,
                target="#password-input",
                value=secret_expected,
                verify=True,
            )
        )

        self.assertFalse(result["verified"])
        result_str = str(result)
        self.assertNotIn(secret_expected, result_str)
        self.assertNotIn(secret_actual, result_str)
        self.assertTrue(result["preview"]["redacted"])
        self.assertTrue(result["mismatch"]["actual_preview"]["redacted"])

    def test_value_env_secret_never_echoed_in_result(self):
        secret_token = "tok_live_9876543210abcdef"
        os.environ["STEALTH_SECRET_API_TOKEN"] = secret_token

        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "text", "input_type": "text"},
        )

        result = asyncio.run(
            fill_tool.fill(
                tab=tab,
                target="#api-token",
                value_env="STEALTH_SECRET_API_TOKEN",
                verify=True,
            )
        )

        result_str = str(result)
        self.assertNotIn(secret_token, result_str)
        self.assertTrue(result["preview"]["redacted"])
        self.assertEqual(result["preview"]["length"], len(secret_token))

    def test_value_env_validation(self):
        tab = FakeFillTab(backend_node_id=42)

        # Non-prefixed variable name
        with self.assertRaises(SecretEnvError) as ctx:
            asyncio.run(
                fill_tool.fill(
                    tab=tab,
                    target="#target",
                    value_env="INVALID_PREFIX_SECRET",
                )
            )
        self.assertIn(SECRET_ENV_PREFIX, str(ctx.exception))

        # Missing variable in environment
        with self.assertRaises(SecretEnvError):
            asyncio.run(
                fill_tool.fill(
                    tab=tab,
                    target="#target",
                    value_env="STEALTH_SECRET_NONEXISTENT_XYZ",
                )
            )

        # Both value and value_env provided
        with self.assertRaises(ValueError):
            asyncio.run(
                fill_tool.fill(
                    tab=tab,
                    target="#target",
                    value="plain",
                    value_env="STEALTH_SECRET_FOO",
                )
            )

        # Neither value nor value_env provided
        with self.assertRaises(ValueError):
            asyncio.run(
                fill_tool.fill(
                    tab=tab,
                    target="#target",
                    value=None,
                    value_env=None,
                )
            )


class UnsupportedKindsAndExplicitStrategyTests(unittest.TestCase):
    """Tests for unsupported element kinds and strategy override options."""

    def setUp(self):
        self._orig_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_button_unsupported(self):
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "button", "input_type": "button"},
        )
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                fill_tool.fill(tab=tab, target="#submit-btn", value="click me")
            )
        self.assertIn("cannot be filled", str(ctx.exception))

    def test_file_input_unsupported(self):
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "file", "input_type": "file"},
        )
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                fill_tool.fill(tab=tab, target="#file-upload", value="/tmp/test.txt")
            )
        self.assertIn("file_upload", str(ctx.exception))

    def test_explicit_strategy_override(self):
        tab = FakeFillTab(
            backend_node_id=42,
            facts={"tag_name": "input", "type": "text", "input_type": "text"},
        )
        result = asyncio.run(
            fill_tool.fill(
                tab=tab,
                target="#text-input",
                value="explicit text",
                strategy="typing",
                verify=True,
            )
        )
        self.assertEqual(result["strategy"], "typing")
        self.assertTrue(result["verified"])


class PasteModePlumbingTests(unittest.TestCase):
    """Tests for paste_text(mode=) plumbing in dom_handler and server."""

    def setUp(self):
        self._env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        from stealth_browser_mcp.paths import DATA_DIR_ENV
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_paste_mode_default_keeps_current_behaviour(self):
        tab = FakeFillTab(backend_node_id=42)
        success = asyncio.run(dom_handler.DOMHandler.paste_text(tab, "#target", "pasted text"))
        self.assertTrue(success)
        # Should have sent CDP input_.insert_text
        insert_calls = [
            params
            for method, params in tab.calls
            if method == "Input.insertText"
        ]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0]["text"], "pasted text")

    def test_paste_mode_native(self):
        tab = FakeFillTab(backend_node_id=42)
        success = asyncio.run(
            dom_handler.DOMHandler.paste_text(tab, "#target", "native text", mode="native")
        )
        self.assertTrue(success)
        self.assertEqual(tab.current_value, "native text")

    def test_paste_mode_synthetic(self):
        tab = FakeFillTab(backend_node_id=42)
        success = asyncio.run(
            dom_handler.DOMHandler.paste_text(tab, "#target", "synth text", mode="synthetic")
        )
        self.assertTrue(success)
        self.assertEqual(tab.current_value, "synth text")

    def test_paste_mode_type(self):
        tab = FakeFillTab(backend_node_id=42)
        success = asyncio.run(
            dom_handler.DOMHandler.paste_text(tab, "#target", "typed", mode="type")
        )
        self.assertTrue(success)
        self.assertGreater(len(tab.typed_chars), 0)

    def test_paste_mode_clipboard_plumbing(self):
        from stealth_browser_mcp import os_clipboard
        from tests.test_os_clipboard import RecordingBackend, _fake_backend_factory

        orig_flag = os.environ.get(os_clipboard.ENV_FLAG)
        os.environ[os_clipboard.ENV_FLAG] = "1"
        try:
            backend = RecordingBackend(current="initial")
            with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
                tab = FakeFillTab(backend_node_id=42)
                success = asyncio.run(
                    dom_handler.DOMHandler.paste_text(tab, "#target", "clipboard text", mode="clipboard")
                )
                self.assertTrue(success)
                self.assertEqual(tab.current_value, "clipboard text")
        finally:
            if orig_flag is None:
                os.environ.pop(os_clipboard.ENV_FLAG, None)
            else:
                os.environ[os_clipboard.ENV_FLAG] = orig_flag

    def test_fill_strategy_os_paste(self):
        from stealth_browser_mcp import os_clipboard
        from tests.test_os_clipboard import RecordingBackend, _fake_backend_factory

        orig_flag = os.environ.get(os_clipboard.ENV_FLAG)
        os.environ[os_clipboard.ENV_FLAG] = "1"
        try:
            backend = RecordingBackend(current="initial")
            with unittest.mock.patch.object(os_clipboard, "_get_backend", _fake_backend_factory(backend)):
                tab = FakeFillTab(
                    backend_node_id=42,
                    facts={"tag_name": "input", "type": "text", "input_type": "text"},
                )
                res = asyncio.run(
                    fill_tool.fill(tab, "#target", value="os paste val", strategy="os_paste", verify=True)
                )
                self.assertTrue(res["verified"])
                self.assertEqual(res["strategy"], "os_paste")
                self.assertEqual(tab.current_value, "os paste val")
        finally:
            if orig_flag is None:
                os.environ.pop(os_clipboard.ENV_FLAG, None)
            else:
                os.environ[os_clipboard.ENV_FLAG] = orig_flag

    def test_paste_mode_invalid_raises_value_error(self):
        tab = FakeFillTab(backend_node_id=42)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                dom_handler.DOMHandler.paste_text(tab, "#target", "fail", mode="invalid_mode")
            )
        self.assertIn("Invalid paste mode", str(ctx.exception))


@unittest.skipUnless(
    RUN_BROWSER_TESTS,
    "Set STEALTH_BROWSER_TESTS=1 to run tests that launch a real browser.",
)
class TestFillToolGatedBrowser(unittest.TestCase):
    """Gated real-browser integration tests with local fixture harness."""

    def setUp(self):
        self._orig_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        os.environ[DATA_DIR_ENV] = self._tmp.name

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_fill_real_browser_inputs(self):
        """Test filling real inputs in Chrome headless on inputs.html."""
        from stealth_browser_mcp.browser_launcher import _stop_process, launch_browser
        from stealth_browser_mcp.platform_utils import (
            check_browser_executable,
            merge_browser_args,
            sandbox_must_be_disabled,
        )

        executable = os.environ.get("STEALTH_BROWSER_EXECUTABLE", "").strip()
        if not executable or not os.path.exists(executable):
            if os.path.exists("/usr/bin/google-chrome"):
                executable = "/usr/bin/google-chrome"
            else:
                executable = check_browser_executable() or "/usr/bin/google-chrome"

        async def run_test():
            temp_dir = tempfile.mkdtemp(prefix="stealth_fill_test_")
            browser = None
            try:
                with FixtureServer() as fixture_srv:
                    config = uc.Config(
                        headless=True,
                        user_data_dir=temp_dir,
                        browser_executable_path=executable,
                        browser_args=merge_browser_args(["--disable-gpu"]),
                        sandbox=False if sandbox_must_be_disabled() else True,
                    )
                    browser = await launch_browser(config, timeout=30.0)
                    tab = await browser.get(fixture_srv.url_for("inputs.html"))

                    # 1. Fill plain text input
                    res_text = await fill_tool.fill(
                        tab=tab,
                        target="#text-input",
                        value="Automated Fill Content",
                        verify=True,
                    )
                    self.assertTrue(res_text["verified"])
                    self.assertEqual(res_text["strategy"], "native_setter")
                    val = await tab.evaluate("document.getElementById('text-input').value")
                    self.assertEqual(val, "Automated Fill Content")

                    # 2. Fill password input - verify password never in preview
                    secret_pass = "P@ssw0rdFixture2026!"
                    res_pass = await fill_tool.fill(
                        tab=tab,
                        target="#password-input",
                        value=secret_pass,
                        verify=True,
                    )
                    self.assertTrue(res_pass["verified"])
                    self.assertNotIn(secret_pass, str(res_pass))
                    self.assertTrue(res_pass["preview"]["redacted"])
                    pw_val = await tab.evaluate("document.getElementById('password-input').value")
                    self.assertEqual(pw_val, secret_pass)

                    # 3. Fill textarea
                    res_ta = await fill_tool.fill(
                        tab=tab,
                        target="#textarea-input",
                        value="Textarea content line 1\nLine 2",
                        verify=True,
                    )
                    self.assertTrue(res_ta["verified"])
                    ta_val = await tab.evaluate("document.getElementById('textarea-input').value")
                    self.assertIn("Textarea content line 1", ta_val)

                    # 4. Fill checkbox
                    res_cb = await fill_tool.fill(
                        tab=tab,
                        target="#checkbox-input",
                        value=True,
                        verify=True,
                    )
                    self.assertTrue(res_cb["verified"])
                    cb_val = await tab.evaluate("document.getElementById('checkbox-input').checked")
                    self.assertTrue(cb_val)

                    # 5. Fill select
                    res_sel = await fill_tool.fill(
                        tab=tab,
                        target="#select-input",
                        value="Second Option",
                        verify=True,
                    )
                    self.assertTrue(res_sel["verified"])
                    sel_val = await tab.evaluate("document.getElementById('select-input').value")
                    self.assertEqual(sel_val, "second")

            finally:
                if browser is not None:
                    try:
                        if getattr(browser, "connection", None) is not None:
                            await asyncio.wait_for(
                                browser.connection.send(uc.cdp.browser.close()),
                                timeout=3.0,
                            )
                    except Exception:
                        pass
                    if getattr(browser, "_process", None) is not None:
                        try:
                            await _stop_process(browser._process)
                        except Exception:
                            pass
                    browser._process = None

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
