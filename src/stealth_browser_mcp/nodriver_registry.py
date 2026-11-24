"""Release nodriver's global Browser registry entries.

nodriver adds every Browser it creates to ``nodriver.core.util.__registered__instances__``
(a module level set used for atexit cleanup) and never removes it. Each retained
Browser keeps its Tabs, websocket connections and compressor buffers alive, so a
long running server leaks roughly 0.3 MB per browser opened (found by the 1 hour
soak: 120 MB to 1 GB in 33 minutes). Call :func:`release_browser` once a browser
is closed or disconnected.
"""
from typing import Any


def registered_count() -> int:
    """Return how many Browser objects nodriver is still holding (for tests and diagnostics)."""
    try:
        from nodriver.core import util
        return len(getattr(util, "__registered__instances__", ()))
    except Exception:
        return 0


def release_browser(browser: Any) -> bool:
    """Drop ``browser`` from nodriver's global registry. Returns True if it was registered."""
    if browser is None:
        return False
    try:
        from nodriver.core import util
        registry = getattr(util, "__registered__instances__", None)
        if registry is None or browser not in registry:
            return False
        registry.discard(browser)
        return True
    except Exception:
        return False
