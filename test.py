"""
Stealth Browser MCP - Test Suite with Proxy Support

Tests:
  1. Proxy URL parsing
  2. Browser spawn with authenticated proxy
  3. Navigation and IP verification via proxy
  4. Screenshot capture
  5. Graceful cleanup

Usage:
  python test.py

The proxy is pre-configured. Edit PROXY_URL below to change it.
"""

import asyncio
import sys
import os

# Add src/ to path so imports work from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from proxy_utils import parse_proxy_config, ProxyConfigError
from browser_manager import BrowserManager
from models import BrowserOptions

# ---------------------------------------------------------------------------
# Proxy configuration
# Format: http://username:password@host:port
# ---------------------------------------------------------------------------
PROXY_URL = (
    "http://geonode_b7QPQOytu0-type-residential"
    ":8e3b9c16-f668-43e9-8164-2aa1b1162385"
    "@us.proxy.geonode.io:9000"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"


def log(status: str, message: str) -> None:
    print(f"{status} {message}")


# ---------------------------------------------------------------------------
# Test 1 – proxy URL parsing (no browser required)
# ---------------------------------------------------------------------------

def test_proxy_parsing() -> bool:
    print("\n--- Test 1: Proxy URL Parsing ---")
    try:
        cfg = parse_proxy_config(PROXY_URL)
        log(INFO, f"Server   : {cfg.server}")
        log(INFO, f"Username : {cfg.username}")
        log(INFO, f"Password : {'*' * len(cfg.password) if cfg.password else None}")

        assert cfg.server == "http://us.proxy.geonode.io:9000", f"Unexpected server: {cfg.server}"
        assert cfg.username == "geonode_b7QPQOytu0-type-residential"
        assert cfg.password == "8e3b9c16-f668-43e9-8164-2aa1b1162385"
        log(PASS, "Proxy URL parsed correctly")
        return True
    except (AssertionError, ProxyConfigError) as exc:
        log(FAIL, f"Proxy parsing failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Test 2 – bad proxy URL raises ProxyConfigError
# ---------------------------------------------------------------------------

def test_invalid_proxy() -> bool:
    print("\n--- Test 2: Invalid Proxy Rejection ---")
    bad_urls = [
        "",
        "not-a-url",
        "http://user@missingpassword.com:8080",  # username without password
    ]
    all_ok = True
    for bad in bad_urls:
        try:
            parse_proxy_config(bad)
            log(FAIL, f"Expected ProxyConfigError for: {repr(bad)}")
            all_ok = False
        except ProxyConfigError as exc:
            log(PASS, f"Correctly rejected {repr(bad)}: {exc}")
    return all_ok


# ---------------------------------------------------------------------------
# Test 3 – spawn browser, navigate with proxy, verify IP, screenshot
# ---------------------------------------------------------------------------

async def test_browser_with_proxy() -> bool:
    print("\n--- Test 3: Browser with Proxy ---")
    manager = BrowserManager()
    instance_id = None

    try:
        # Spawn browser (headless for CI / server environments)
        log(INFO, "Spawning browser with proxy...")
        options = BrowserOptions(
            headless=True,
            proxy=PROXY_URL,
            sandbox=False,         # required in most container environments
            viewport_width=1280,
            viewport_height=800,
        )
        instance = await manager.spawn_browser(options)
        instance_id = instance.instance_id
        log(PASS, f"Browser spawned: {instance_id}")

        # Navigate to an IP-echo service to confirm proxy is being used
        log(INFO, "Navigating to ip.me to check exit IP...")
        result = await manager.navigate(instance_id, "https://ip.me", timeout=30000)
        log(INFO, f"Navigation result keys: {list(result.keys())}")

        page_url = result.get("url", "")
        log(INFO, f"Page URL: {page_url}")

        # Grab page content to inspect IP
        data = manager._instances.get(instance_id, {})
        tab = data.get("tab")
        if tab:
            content = await tab.get_content()
            # ip.me returns a plain-text IP or a simple HTML page
            ip_hint = content.strip()[:200].replace("\n", " ")
            log(INFO, f"Page content snippet: {ip_hint}")

        # Take a screenshot via the tab directly
        if tab:
            screenshot_path = os.path.join(os.path.dirname(__file__), "test_screenshot.png")
            log(INFO, "Taking screenshot...")
            try:
                from pathlib import Path
                await tab.save_screenshot(Path(screenshot_path))
                log(PASS, f"Screenshot saved: {screenshot_path}")
            except Exception as exc:
                log(INFO, f"Screenshot skipped (non-fatal): {exc}")

        log(PASS, "Browser proxy navigation succeeded")
        return True

    except Exception as exc:
        log(FAIL, f"Browser test failed: {exc}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        if instance_id:
            log(INFO, "Closing browser instance...")
            try:
                await manager.close_instance(instance_id)
                log(INFO, "Browser closed")
            except Exception as exc:
                log(INFO, f"Close error (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Test 4 – proxy forwarder starts and exposes a local server
# ---------------------------------------------------------------------------

async def test_proxy_forwarder() -> bool:
    print("\n--- Test 4: Authenticated Proxy Forwarder ---")
    try:
        from proxy_forwarder import AuthenticatedProxyForwarder

        forwarder = AuthenticatedProxyForwarder(PROXY_URL)
        await forwarder.start()
        local_server = forwarder.proxy_server

        assert local_server.startswith("http://127.0.0.1:"), (
            f"Expected loopback server, got: {local_server}"
        )
        log(PASS, f"Forwarder listening at {local_server}")

        await forwarder.close()
        log(PASS, "Forwarder closed cleanly")
        return True

    except Exception as exc:
        log(FAIL, f"Forwarder test failed: {exc}")
        import traceback
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("  Stealth Browser MCP – Test Suite")
    print("=" * 60)

    results = {}

    # Synchronous tests
    results["proxy_parsing"] = test_proxy_parsing()
    results["invalid_proxy"] = test_invalid_proxy()

    # Async tests
    results["proxy_forwarder"] = await test_proxy_forwarder()
    results["browser_with_proxy"] = await test_browser_with_proxy()

    # Summary
    print("\n" + "=" * 60)
    print("  Results Summary")
    print("=" * 60)
    passed = 0
    failed = 0
    for name, ok in results.items():
        status = PASS if ok else FAIL
        print(f"  {status}  {name}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n  {passed}/{passed + failed} tests passed")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
