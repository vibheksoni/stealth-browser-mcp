import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stealth_browser_mcp import nodriver_compat, oopif
from stealth_browser_mcp.browser_manager import BrowserManager
import nodriver as uc


class PickMainFrameStatusTests(unittest.TestCase):
    pick = staticmethod(BrowserManager._pick_main_frame_status)

    def test_empty(self):
        self.assertIsNone(self.pick([], "F", "L"))

    def test_ignores_other_frames(self):
        responses = [("F", "L", 404), ("IFRAME", "L2", 200)]
        self.assertEqual(self.pick(responses, "F", "L"), 404)

    def test_redirect_uses_last_document(self):
        responses = [("F", "L1", 302), ("F", "L2", 200)]
        self.assertEqual(self.pick(responses, "F", "L2"), 200)

    def test_unknown_frame_falls_back_to_last_document(self):
        self.assertEqual(self.pick([("A", "L", 500)], None, None), 500)

    def test_no_match_for_known_frame(self):
        self.assertIsNone(self.pick([("IFRAME", "L", 200)], "F", "L"))


class TransactionPatchTests(unittest.TestCase):
    def setUp(self):
        nodriver_compat.apply()
        from nodriver.core.connection import Transaction
        self.Transaction = Transaction

    def test_reply_for_cancelled_transaction_is_ignored(self):
        async def run():
            tx = self.Transaction(uc.cdp.runtime.evaluate("1"))
            tx.cancel()
            return tx(result={"result": {"type": "number", "value": 1}})
        self.assertIsNone(asyncio.run(run()))

    def test_session_id_is_included_in_message(self):
        async def run():
            cmd = oopif.SessionCommand(uc.cdp.runtime.evaluate("1"), "SESS")
            tx = self.Transaction(cmd)
            return json.loads(tx.message)
        payload = asyncio.run(run())
        self.assertEqual(payload["sessionId"], "SESS")
        self.assertEqual(payload["method"], "Runtime.evaluate")

    def test_plain_message_has_no_session_id(self):
        async def run():
            return json.loads(self.Transaction(uc.cdp.runtime.evaluate("1")).message)
        self.assertNotIn("sessionId", asyncio.run(run()))


class SendWithinTests(unittest.TestCase):
    def test_timeout_does_not_cancel_send(self):
        async def run():
            fut = asyncio.get_running_loop().create_future()

            class FakeTab:
                async def send(self, command):
                    return await fut

            with self.assertRaises(asyncio.TimeoutError):
                await BrowserManager._send_within(FakeTab(), object(), 0.05)
            self.assertFalse(fut.cancelled())
            fut.set_result("late")
            await asyncio.sleep(0)
            return True
        self.assertTrue(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
