"""SB-82 / review F4 and F5: session isolation on instance_id and hook ownership."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastmcp.exceptions import ToolError

from stealth_browser_mcp import mcp_sessions, server
from stealth_browser_mcp.dynamic_hook_ai_interface import DynamicHookAIInterface
from stealth_browser_mcp.dynamic_hook_system import dynamic_hook_system
from stealth_browser_mcp.mcp_sessions import MCPSessionManager


class UnownedAccessTests(unittest.TestCase):
    def test_session_bound_caller_denied_unowned_instance(self) -> None:
        mgr = MCPSessionManager()
        self.assertTrue(mgr.is_instance_accessible(None, "unowned"))
        # Isolation is not armed until some instance is owned.
        self.assertTrue(mgr.is_instance_accessible("session-B", "unowned"))
        mgr.register_session("session-A", "inst-A")
        self.assertTrue(mgr.is_instance_accessible("session-A", "inst-A"))
        self.assertFalse(mgr.is_instance_accessible("session-B", "inst-A"))
        self.assertFalse(mgr.is_instance_accessible("session-B", "unowned"))
        with self.assertRaises(ToolError):
            mgr.check_access("unowned", mcp_session_id="session-B")


class CrossSessionToolTests(unittest.TestCase):
    """B cannot use A's instance_id on tools that previously skipped check_access."""

    def setUp(self) -> None:
        self.mgr = mcp_sessions.mcp_session_manager
        self._sessions = dict(self.mgr._session_to_instance)
        self._instances = dict(self.mgr._instance_to_session)
        self.mgr._session_to_instance.clear()
        self.mgr._instance_to_session.clear()
        self.inst_a = "inst-A"
        self.inst_b = "inst-B"
        # B has not spawned anything, matching the review scenario.
        self.mgr.register_session("session-A", self.inst_a)

    def tearDown(self) -> None:
        self.mgr._session_to_instance.clear()
        self.mgr._instance_to_session.clear()
        self.mgr._session_to_instance.update(self._sessions)
        self.mgr._instance_to_session.update(self._instances)

    def _call(self, session_id, factory):
        with mcp_sessions.set_current_mcp_session_id(session_id):
            return asyncio.run(factory())

    def test_list_instances_hides_other_session(self) -> None:
        class Inst:
            def __init__(self, instance_id: str) -> None:
                self.instance_id = instance_id
                self.state = "open"
                self.current_url = "https://example.com"
                self.title = "Example"
                self.attached = False

        async def fake_list():
            return [Inst(self.inst_a), Inst(self.inst_b)]

        with mock.patch.object(server.browser_manager, "list_instances", fake_list), mock.patch(
            "stealth_browser_mcp.server.persistent_storage.list_instances",
            return_value={"instances": {}},
        ):
            listed_b = self._call("session-B", lambda: server.list_instances.fn())
            listed_a = self._call("session-A", lambda: server.list_instances.fn())
        self.assertEqual(listed_b, [])
        self.assertEqual([row["instance_id"] for row in listed_a], [self.inst_a])

    def test_cross_session_tools_use_same_error_as_execute_script(self) -> None:
        async def fake_tab(instance_id: str):
            return object()

        def deny(session_id, coro_fn):
            with mock.patch.object(server.browser_manager, "get_tab", fake_tab):
                with self.assertRaises(ToolError) as raised:
                    self._call(session_id, coro_fn)
            return raised.exception

        script_err = deny(
            "session-B",
            lambda: server.execute_script.fn(instance_id=self.inst_a, script="1+1", args=None),
        )
        cookie_err = deny(
            "session-B",
            lambda: server.get_cookies.fn(instance_id=self.inst_a),
        )
        cdp_err = deny(
            "session-B",
            lambda: server.execute_cdp_command.fn(
                instance_id=self.inst_a,
                command="Runtime.evaluate",
            ),
        )
        save_err = deny(
            "session-B",
            lambda: server.save_session.fn(instance_id=self.inst_a, file_path="/tmp/x"),
        )
        for error in (cookie_err, cdp_err, save_err):
            self.assertIsInstance(error, ToolError)
            self.assertEqual(type(error), type(script_err))
            self.assertIn("Instance not found", str(error))

    def test_owner_can_still_call_get_cookies(self) -> None:
        tab = object()

        async def fake_tab(instance_id: str):
            self.assertEqual(instance_id, self.inst_a)
            return tab

        async def fake_cookies(tab_arg, urls=None):
            return [{"name": "secret", "value": "A-ONLY-COOKIE"}]

        with mock.patch.object(server.browser_manager, "get_tab", fake_tab), mock.patch.object(
            server.network_interceptor, "get_cookies", fake_cookies
        ):
            result = self._call("session-A", lambda: server.get_cookies.fn(instance_id=self.inst_a))
        self.assertEqual(result, [{"name": "secret", "value": "A-ONLY-COOKIE"}])


class HookOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = mcp_sessions.mcp_session_manager
        self._sessions = dict(self.mgr._session_to_instance)
        self._instances = dict(self.mgr._instance_to_session)
        self._hooks = dict(dynamic_hook_system.hooks)
        self._instance_hooks = {key: list(value) for key, value in dynamic_hook_system.instance_hooks.items()}
        self.mgr._session_to_instance.clear()
        self.mgr._instance_to_session.clear()
        self.inst_a = "hook-inst-A"
        self.inst_b = "hook-inst-B"
        self.mgr.register_session("session-A", self.inst_a)
        self.mgr.register_session("session-B", self.inst_b)
        self.ai = DynamicHookAIInterface()
        self.created = []

    def tearDown(self) -> None:
        async def _cleanup():
            for hook_id in list(self.created):
                await dynamic_hook_system.remove_hook(hook_id)

        with mcp_sessions.set_current_mcp_session_id(None):
            asyncio.run(_cleanup())
        self.mgr._session_to_instance.clear()
        self.mgr._instance_to_session.clear()
        self.mgr._session_to_instance.update(self._sessions)
        self.mgr._instance_to_session.update(self._instances)
        dynamic_hook_system.hooks.clear()
        dynamic_hook_system.hooks.update(self._hooks)
        dynamic_hook_system.instance_hooks.clear()
        dynamic_hook_system.instance_hooks.update(self._instance_hooks)

    def test_empty_instance_ids_means_my_instances_only(self) -> None:
        with mcp_sessions.set_current_mcp_session_id("session-A"):
            result = asyncio.run(
                self.ai.create_simple_hook("block-ads", "*ads*", "block")
            )
            self.assertTrue(result["success"])
            hook_id = result["hook_id"]
            self.created.append(hook_id)
            hook = dynamic_hook_system.hooks[hook_id]
            self.assertEqual(hook.owner_session_id, "session-A")
            self.assertEqual(hook.instance_ids, [])
            self.assertTrue(dynamic_hook_system.hook_applies_to_instance(hook, self.inst_a))
            self.assertFalse(dynamic_hook_system.hook_applies_to_instance(hook, self.inst_b))
            listed_a = asyncio.run(self.ai.list_dynamic_hooks())

        with mcp_sessions.set_current_mcp_session_id("session-B"):
            listed_b = asyncio.run(self.ai.list_dynamic_hooks())
            details = asyncio.run(self.ai.get_hook_details(hook_id))
            removed = asyncio.run(self.ai.remove_dynamic_hook(hook_id))

        self.assertTrue(any(row.get("hook_id") == hook_id for row in listed_a["hooks"]))
        self.assertEqual(listed_b["hooks"], [])
        self.assertFalse(details.get("success"))
        self.assertFalse(removed.get("success"))
        self.assertIn(hook_id, dynamic_hook_system.hooks)
