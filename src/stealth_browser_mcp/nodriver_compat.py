"""Compatibility patches for nodriver.

nodriver's Transaction future raises InvalidStateError when the browser replies
to a command whose waiter was already cancelled (for example by
asyncio.wait_for timing out). The exception escapes the connection listener
loop and leaves the tab unresponsive. The patch drops replies for transactions
that are already done.
"""

import json

_PATCHED_ATTR = "_sbm_ignore_done_patch"


def apply() -> bool:
    try:
        from nodriver.core.connection import Transaction
    except Exception:
        return False
    if getattr(Transaction, _PATCHED_ATTR, False):
        return False
    original = Transaction.__call__

    def guarded_call(self, **response):
        if self.done():
            return None
        return original(self, **response)

    Transaction.__call__ = guarded_call

    def message_with_session(self):
        payload = {"method": self.method, "params": self.params, "id": self.id}
        session_id = getattr(self.__cdp_obj__, "session_id", None)
        if session_id:
            payload["sessionId"] = session_id
        return json.dumps(payload)

    Transaction.message = property(message_with_session)
    setattr(Transaction, _PATCHED_ATTR, True)
    return True
