import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import network_interceptor as network_module
from network_interceptor import NetworkInterceptor


class FakeTab:
    def __init__(self, bodies):
        self.bodies = bodies
        self.fetched = []

    async def send(self, command):
        request = next(command)
        request_id = request["params"]["requestId"]
        self.fetched.append(request_id)
        return self.bodies[request_id], False


def request_event(request_id, url, resource_type):
    return SimpleNamespace(
        request_id=request_id,
        request=SimpleNamespace(url=url, method="GET", headers={}, post_data=None),
        type_=SimpleNamespace(value=resource_type),
    )


def response_event(request_id, mime_type):
    return SimpleNamespace(
        request_id=request_id,
        response=SimpleNamespace(status=200, headers={}, mime_type=mime_type),
    )


async def load(interceptor, tab, request_id, url, resource_type, mime_type):
    await interceptor._on_request(request_event(request_id, url, resource_type), "i")
    await interceptor._on_response(response_event(request_id, mime_type))
    await interceptor._on_loading_finished(SimpleNamespace(request_id=request_id, encoded_data_length=10), tab)


class BodyCaptureTests(unittest.TestCase):
    def test_only_text_bodies_are_stored(self):
        interceptor = NetworkInterceptor()
        tab = FakeTab({"api": '{"ok": 1}', "img": "binary", "doc": "<html>"})

        async def run():
            await load(interceptor, tab, "api", "https://x/api", "Fetch", "application/json")
            await load(interceptor, tab, "img", "https://x/a.png", "Image", "image/png")
            await load(interceptor, tab, "doc", "https://x/", "Document", "text/html")

        asyncio.run(run())
        self.assertEqual(tab.fetched, ["api", "doc"])
        self.assertEqual(interceptor._responses["api"].body, b'{"ok": 1}')
        self.assertIsNone(interceptor._responses["img"].body)

    def test_stored_bodies_are_capped_and_served(self):
        interceptor = NetworkInterceptor()
        tab = FakeTab({f"r{index}": "x" * 40 for index in range(5)})
        original = network_module.MAX_STORED_BODY_BYTES
        network_module.MAX_STORED_BODY_BYTES = 100

        async def run():
            for index in range(5):
                await load(interceptor, tab, f"r{index}", f"https://x/{index}", "XHR", "application/json")
            return await interceptor.get_response_body(tab, "r4")

        try:
            served = asyncio.run(run())
        finally:
            network_module.MAX_STORED_BODY_BYTES = original
        stored = [request_id for request_id in ("r0", "r1", "r2", "r3", "r4") if interceptor._responses[request_id].body]
        self.assertEqual(stored, ["r3", "r4"])
        self.assertEqual(interceptor._body_bytes["i"], 80)
        self.assertEqual(served, b"x" * 40)
        self.assertEqual(tab.fetched.count("r4"), 1)

    def test_internal_urls_are_ignored_and_clear_resets(self):
        interceptor = NetworkInterceptor()
        tab = FakeTab({"api": "{}"})

        async def run():
            await interceptor._on_request(request_event("ntp", "chrome://new-tab-page/", "Script"), "i")
            await load(interceptor, tab, "api", "https://x/api", "XHR", "application/json")
            await interceptor.clear_instance_data("i")

        asyncio.run(run())
        self.assertNotIn("ntp", interceptor._requests)
        self.assertNotIn("api", interceptor._requests)
        self.assertNotIn("i", interceptor._body_bytes)


if __name__ == "__main__":
    unittest.main()
