"""Unit tests for HAR 1.2 export functionality."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.har_export import build_har, export_to_json
from stealth_browser_mcp.models import NetworkRequest, NetworkResponse
from stealth_browser_mcp import server


class HarExportTests(unittest.TestCase):
    """Test pure function build_har and export_to_json."""

    def test_har_shape_and_required_keys(self):
        """Verify all required HAR 1.2 keys, types, and structure."""
        now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        entries = [
            {
                "request": {
                    "request_id": "req-1",
                    "url": "https://example.com/api/items?search=test&page=2&empty=",
                    "method": "GET",
                    "httpVersion": "HTTP/1.1",
                    "headers": {
                        "Host": "example.com",
                        "Accept": "application/json",
                    },
                    "cookies": {"session_id": "abc123xyz"},
                    "timestamp": now.isoformat(),
                    "headersSize": 128,
                    "bodySize": 0,
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "httpVersion": "HTTP/1.1",
                    "headers": {
                        "Content-Type": "application/json; charset=utf-8",
                        "Content-Length": "19",
                    },
                    "content": {
                        "size": 19,
                        "mimeType": "application/json",
                        "text": '{"success": true}',
                    },
                    "redirectURL": "",
                    "headersSize": 150,
                    "bodySize": 19,
                    "timings": {"send": 5, "wait": 45, "receive": 10},
                    "time": 60,
                },
            }
        ]

        har = build_har(entries, browser_name="stealth-browser-mcp", version="1.0.0")

        # Root and log object
        self.assertIn("log", har)
        log = har["log"]
        self.assertEqual(log["version"], "1.2")
        self.assertEqual(log["creator"], {"name": "stealth-browser-mcp", "version": "1.0.0"})
        self.assertNotIn("pages", log)
        self.assertIsInstance(log["entries"], list)
        self.assertEqual(len(log["entries"]), 1)

        entry = log["entries"][0]
        # Entry top-level keys
        self.assertEqual(entry["startedDateTime"], now.isoformat())
        self.assertEqual(entry["time"], 60)
        self.assertEqual(entry["cache"], {})
        self.assertEqual(entry["timings"], {"send": 5, "wait": 45, "receive": 10})

        # Request keys and types
        req = entry["request"]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["url"], "https://example.com/api/items?search=test&page=2&empty=")
        self.assertEqual(req["httpVersion"], "HTTP/1.1")
        self.assertIsInstance(req["headers"], list)
        self.assertEqual(
            req["headers"],
            [
                {"name": "Host", "value": "example.com"},
                {"name": "Accept", "value": "application/json"},
            ],
        )
        # Query string parsed from URL
        self.assertEqual(
            req["queryString"],
            [
                {"name": "search", "value": "test"},
                {"name": "page", "value": "2"},
                {"name": "empty", "value": ""},
            ],
        )
        self.assertIsInstance(req["cookies"], list)
        # Redacted by default
        self.assertEqual(req["cookies"], [{"name": "session_id", "value": "[REDACTED]"}])
        self.assertEqual(req["headersSize"], 128)
        self.assertEqual(req["bodySize"], 0)
        self.assertNotIn("postData", req)

        # Response keys and types
        resp = entry["response"]
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["statusText"], "OK")
        self.assertEqual(resp["httpVersion"], "HTTP/1.1")
        self.assertIsInstance(resp["headers"], list)
        self.assertIsInstance(resp["cookies"], list)
        self.assertEqual(resp["redirectURL"], "")
        self.assertEqual(resp["headersSize"], 150)
        self.assertEqual(resp["bodySize"], 19)

        # Content keys and types
        content = resp["content"]
        self.assertEqual(content["size"], 19)
        self.assertEqual(content["mimeType"], "application/json")
        self.assertEqual(content["text"], '{"success": true}')

    def test_post_data_urlencoded_and_json(self):
        """Verify request postData parsing for urlencoded and plain/json payloads."""
        entries = [
            {
                "request": {
                    "method": "POST",
                    "url": "https://example.com/login",
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "post_data": "username=alice&password=secretpassword",
                },
            },
            {
                "request": {
                    "method": "POST",
                    "url": "https://example.com/api/data",
                    "headers": {"Content-Type": "application/json"},
                    "post_data": '{"action": "test"}',
                },
            },
        ]

        har = build_har(entries)
        req0 = har["log"]["entries"][0]["request"]
        self.assertIn("postData", req0)
        self.assertEqual(req0["postData"]["mimeType"], "application/x-www-form-urlencoded")
        self.assertEqual(req0["postData"]["text"], "username=alice&password=secretpassword")
        self.assertEqual(
            req0["postData"]["params"],
            [
                {"name": "username", "value": "alice"},
                {"name": "password", "value": "secretpassword"},
            ],
        )

        req1 = har["log"]["entries"][1]["request"]
        self.assertIn("postData", req1)
        self.assertEqual(req1["postData"]["mimeType"], "application/json")
        self.assertEqual(req1["postData"]["text"], '{"action": "test"}')
        self.assertEqual(req1["postData"]["params"], [])

    def test_redaction_default_and_include_sensitive(self):
        """Verify sensitive headers and cookies are redacted unless include_sensitive=True."""
        entries = [
            {
                "request": {
                    "url": "https://example.com/protected",
                    "method": "GET",
                    "headers": {
                        "Authorization": "Bearer super-secret-token",
                        "Cookie": "session=sensitive_value; tracker=123",
                        "X-Public-Header": "visible-data",
                    },
                    "cookies": {"session": "sensitive_value"},
                },
                "response": {
                    "status": 200,
                    "headers": {
                        "Set-Cookie": "auth_token=jwt_secret_val; Secure; HttpOnly",
                        "Content-Type": "text/html",
                    },
                    "cookies": [{"name": "auth_token", "value": "jwt_secret_val"}],
                },
            }
        ]

        # Redacted by default (include_sensitive=False)
        har_redacted = build_har(entries, include_sensitive=False)
        req_headers = {h["name"]: h["value"] for h in har_redacted["log"]["entries"][0]["request"]["headers"]}
        self.assertEqual(req_headers["Authorization"], "[REDACTED]")
        self.assertEqual(req_headers["Cookie"], "[REDACTED]")
        self.assertEqual(req_headers["X-Public-Header"], "visible-data")
        self.assertEqual(
            har_redacted["log"]["entries"][0]["request"]["cookies"],
            [{"name": "session", "value": "[REDACTED]"}],
        )

        resp_headers = {h["name"]: h["value"] for h in har_redacted["log"]["entries"][0]["response"]["headers"]}
        self.assertEqual(resp_headers["Set-Cookie"], "[REDACTED]")
        self.assertEqual(resp_headers["Content-Type"], "text/html")
        self.assertEqual(
            har_redacted["log"]["entries"][0]["response"]["cookies"],
            [{"name": "auth_token", "value": "[REDACTED]"}],
        )

        # Unredacted when include_sensitive=True
        har_clear = build_har(entries, include_sensitive=True)
        clear_req_headers = {h["name"]: h["value"] for h in har_clear["log"]["entries"][0]["request"]["headers"]}
        self.assertEqual(clear_req_headers["Authorization"], "Bearer super-secret-token")
        self.assertEqual(clear_req_headers["Cookie"], "session=sensitive_value; tracker=123")
        self.assertEqual(
            har_clear["log"]["entries"][0]["request"]["cookies"],
            [{"name": "session", "value": "sensitive_value"}],
        )

        clear_resp_headers = {h["name"]: h["value"] for h in har_clear["log"]["entries"][0]["response"]["headers"]}
        self.assertEqual(clear_resp_headers["Set-Cookie"], "auth_token=jwt_secret_val; Secure; HttpOnly")
        self.assertEqual(
            har_clear["log"]["entries"][0]["response"]["cookies"],
            [{"name": "auth_token", "value": "jwt_secret_val"}],
        )

    def test_case_insensitive_header_redaction(self):
        """Sensitive header matching must be case-insensitive."""
        entries = [
            {
                "request": {
                    "url": "https://example.com",
                    "headers": {
                        "authorization": "token1",
                        "COOKIE": "token2",
                        "sEt-cOoKiE": "token3",
                    },
                }
            }
        ]
        har = build_har(entries, include_sensitive=False)
        headers = {h["name"].lower(): h["value"] for h in har["log"]["entries"][0]["request"]["headers"]}
        self.assertEqual(headers["authorization"], "[REDACTED]")
        self.assertEqual(headers["cookie"], "[REDACTED]")
        self.assertEqual(headers["set-cookie"], "[REDACTED]")

    def test_missing_fields_tolerance_and_defaults(self):
        """Missing or partial fields must fall back to valid HAR defaults without exceptions."""
        entries = [{}]
        har = build_har(entries)
        log = har["log"]
        self.assertEqual(log["version"], "1.2")
        self.assertEqual(len(log["entries"]), 1)

        entry = log["entries"][0]
        self.assertTrue(entry["startedDateTime"])
        self.assertEqual(entry["time"], -1)
        self.assertEqual(entry["cache"], {})
        self.assertEqual(entry["timings"], {"send": -1, "wait": -1, "receive": -1})

        req = entry["request"]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["url"], "")
        self.assertEqual(req["httpVersion"], "HTTP/1.1")
        self.assertEqual(req["headers"], [])
        self.assertEqual(req["queryString"], [])
        self.assertEqual(req["cookies"], [])
        self.assertEqual(req["headersSize"], -1)
        self.assertEqual(req["bodySize"], -1)
        self.assertNotIn("postData", req)

        resp = entry["response"]
        self.assertEqual(resp["status"], -1)
        self.assertEqual(resp["statusText"], "")
        self.assertEqual(resp["httpVersion"], "HTTP/1.1")
        self.assertEqual(resp["headers"], [])
        self.assertEqual(resp["cookies"], [])
        self.assertEqual(resp["content"], {"size": -1, "mimeType": ""})
        self.assertEqual(resp["redirectURL"], "")
        self.assertEqual(resp["headersSize"], -1)
        self.assertEqual(resp["bodySize"], -1)

    def test_pages_optional(self):
        """Test optional pages array in log."""
        pages = [{"id": "page_0", "title": "Home", "startedDateTime": "2026-11-19T00:00:00Z"}]
        har = build_har([], pages=pages)
        self.assertEqual(har["log"]["pages"], pages)

        har_no_pages = build_har([])
        self.assertNotIn("pages", har_no_pages["log"])

    def test_custom_creator_name_and_version(self):
        """Test custom creator browser name and version."""
        har = build_har([], browser_name="custom-browser", version="2.5.0")
        self.assertEqual(har["log"]["creator"]["name"], "custom-browser")
        self.assertEqual(har["log"]["creator"]["version"], "2.5.0")

    def test_export_to_json(self):
        """Test serializing HAR document to a JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "nested" / "capture.har")
            har = build_har([{"request": {"url": "https://example.com"}}])
            success = export_to_json(har, filepath)
            self.assertTrue(success)
            self.assertTrue(Path(filepath).exists())

            loaded = json.loads(Path(filepath).read_text(encoding="utf-8"))
            self.assertEqual(loaded["log"]["version"], "1.2")
            self.assertEqual(loaded["log"]["entries"][0]["request"]["url"], "https://example.com")

    def test_network_interceptor_models_compatibility(self):
        """Test compatibility with NetworkRequest and NetworkResponse data shapes."""
        req = NetworkRequest(
            request_id="cdp-req-42",
            instance_id="inst-1",
            url="https://api.test/v1/users?limit=10",
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "SecretKey123"},
            cookies={"auth": "secret_cookie"},
            post_data='{"name": "test"}',
        )
        resp = NetworkResponse(
            request_id="cdp-req-42",
            status=201,
            headers={"Content-Type": "application/json"},
            content_type="application/json",
            body=b'{"id": 42, "created": true}',
        )

        entry = {
            "request": {
                "request_id": req.request_id,
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "cookies": dict(req.cookies),
                "post_data": req.post_data,
                "timestamp": req.timestamp.isoformat(),
            },
            "response": {
                "status": resp.status,
                "content_type": resp.content_type,
                "headers": dict(resp.headers),
                "body": resp.body,
                "timestamp": resp.timestamp.isoformat(),
            },
        }

        har = build_har([entry], include_sensitive=False)
        logged_req = har["log"]["entries"][0]["request"]
        logged_resp = har["log"]["entries"][0]["response"]

        self.assertEqual(logged_req["method"], "POST")
        self.assertEqual(logged_req["url"], "https://api.test/v1/users?limit=10")
        self.assertEqual(logged_req["queryString"], [{"name": "limit", "value": "10"}])
        self.assertEqual(logged_req["postData"]["text"], '{"name": "test"}')

        # Redaction check
        req_hdrs = {h["name"]: h["value"] for h in logged_req["headers"]}
        self.assertEqual(req_hdrs["Authorization"], "[REDACTED]")

        # Response body & content check
        self.assertEqual(logged_resp["status"], 201)
        self.assertEqual(logged_resp["content"]["text"], '{"id": 42, "created": true}')
        self.assertEqual(logged_resp["content"]["size"], len(b'{"id": 42, "created": true}'))


class ServerExportHarToolTests(unittest.IsolatedAsyncioTestCase):
    """Test export_har MCP tool registered in server.py."""

    async def test_export_har_returns_dict_when_no_path(self):
        """Calling export_har without a path returns the HAR dict."""
        instance_id = "test-inst-none-path"
        await server.network_interceptor.clear_instance_data(instance_id)

        # Seed network interceptor with requests
        req = NetworkRequest(
            request_id="r1",
            instance_id=instance_id,
            url="https://example.com/page",
            method="GET",
            headers={"Accept": "text/html"},
        )
        resp = NetworkResponse(
            request_id="r1",
            status=200,
            headers={"Content-Type": "text/html"},
            body=b"<html>ok</html>",
        )
        async with server.network_interceptor._lock:
            server.network_interceptor._requests["r1"] = req
            server.network_interceptor._responses["r1"] = resp
            server.network_interceptor._instance_requests[instance_id] = ["r1"]

        try:
            result = await server.export_har.fn(instance_id=instance_id)
            self.assertIsInstance(result, dict)
            self.assertIn("log", result)
            self.assertEqual(result["log"]["version"], "1.2")
            self.assertEqual(len(result["log"]["entries"]), 1)
            self.assertEqual(result["log"]["entries"][0]["request"]["url"], "https://example.com/page")
            self.assertEqual(result["log"]["entries"][0]["response"]["content"]["text"], "<html>ok</html>")
        finally:
            await server.network_interceptor.clear_instance_data(instance_id)

    async def test_export_har_writes_file_when_path_provided(self):
        """Calling export_har with a path writes the file and returns the path string."""
        instance_id = "test-inst-with-path"
        await server.network_interceptor.clear_instance_data(instance_id)

        req = NetworkRequest(
            request_id="r2",
            instance_id=instance_id,
            url="https://example.com/api?q=1",
            method="GET",
            headers={"Host": "example.com"},
        )
        async with server.network_interceptor._lock:
            server.network_interceptor._requests["r2"] = req
            server.network_interceptor._instance_requests[instance_id] = ["r2"]

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                out_path = str(Path(tmpdir) / "output.har")
                result = await server.export_har.fn(instance_id=instance_id, path=out_path)
                self.assertEqual(result, out_path)
                self.assertTrue(Path(out_path).exists())

                data = json.loads(Path(out_path).read_text(encoding="utf-8"))
                self.assertEqual(data["log"]["version"], "1.2")
                self.assertEqual(data["log"]["entries"][0]["request"]["url"], "https://example.com/api?q=1")
                self.assertEqual(data["log"]["entries"][0]["request"]["queryString"], [{"name": "q", "value": "1"}])
        finally:
            await server.network_interceptor.clear_instance_data(instance_id)


if __name__ == "__main__":
    unittest.main()
