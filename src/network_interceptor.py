"""Network interception and traffic monitoring using CDP."""

import asyncio
import base64
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import nodriver as uc
from nodriver import Tab

from models import NetworkRequest, NetworkResponse


class NetworkInterceptor:
    """Intercepts and manages network traffic for browser instances."""

    def __init__(self):
        self._requests: Dict[str, NetworkRequest] = {}
        self._responses: Dict[str, NetworkResponse] = {}
        self._instance_requests: Dict[str, List[str]] = {}
        self._instance_filters: Dict[str, Dict[str, List[str]]] = {}
        self._lock = asyncio.Lock()

    async def setup_interception(self, tab: Tab, instance_id: str, block_resources: List[str] = None):
        """
        Set up network interception for a tab.

        tab: Tab - The browser tab to intercept.
        instance_id: str - The browser instance identifier.
        block_resources: List[str] - List of resource types or URL patterns to block.
        """
        try:
            await tab.send(uc.cdp.network.enable())
            
            if block_resources:
                # Convert resource types to URL patterns for blocking
                url_patterns = []
                for resource_type in block_resources:
                    # Map resource types to URL patterns that typically identify these resources
                    resource_patterns = {
                        'image': ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.webp', '*.svg', '*.bmp', '*.ico'],
                        'stylesheet': ['*.css'],
                        'font': ['*.woff', '*.woff2', '*.ttf', '*.otf', '*.eot'],
                        'script': ['*.js', '*.mjs'],
                        'media': ['*.mp4', '*.mp3', '*.wav', '*.avi', '*.webm']
                    }
                    
                    if resource_type.lower() in resource_patterns:
                        url_patterns.extend(resource_patterns[resource_type.lower()])
                        print(f"[DEBUG] Added URL patterns for {resource_type}: {resource_patterns[resource_type.lower()]}", file=sys.stderr)
                    else:
                        # Assume it's already a URL pattern
                        url_patterns.append(resource_type)
                        print(f"[DEBUG] Added custom URL pattern: {resource_type}", file=sys.stderr)
                
                # Use network.set_blocked_ur_ls to block the URL patterns
                if url_patterns:
                    await tab.send(uc.cdp.network.set_blocked_ur_ls(urls=url_patterns))
                    print(f"[DEBUG] Blocked {len(url_patterns)} URL patterns: {url_patterns}", file=sys.stderr)
            
            tab.add_handler(
                uc.cdp.network.RequestWillBeSent,
                lambda event: asyncio.create_task(self._on_request(event, instance_id)),
            )
            tab.add_handler(
                uc.cdp.network.ResponseReceived,
                lambda event: asyncio.create_task(self._on_response(event, instance_id, tab)),
            )
            
            async with self._lock:
                if instance_id not in self._instance_requests:
                    self._instance_requests[instance_id] = []
        except Exception as e:
            print(f"[DEBUG] Error in setup_interception: {e}", file=sys.stderr)
            raise Exception(f"Failed to setup network interception: {str(e)}")

    async def _on_request(self, event, instance_id: str):
        """
        Handle request event.

        event: Any - The event object containing request data.
        instance_id: str - The browser instance identifier.
        """
        try:
            request_id = event.request_id
            request = event.request
            resource_type = event.type.value if hasattr(event, "type") else None

            async with self._lock:
                filters = self._instance_filters.get(instance_id, {})
                include = filters.get("include", [])
                exclude = filters.get("exclude", [])

                if include and resource_type and resource_type.lower() not in [t.lower() for t in include]:
                    return
                if exclude and resource_type and resource_type.lower() in [t.lower() for t in exclude]:
                    return

            cookies = {}
            if hasattr(request, "headers") and "Cookie" in request.headers:
                cookie_str = request.headers["Cookie"]
                for cookie in cookie_str.split("; "):
                    if "=" in cookie:
                        key, value = cookie.split("=", 1)
                        cookies[key] = value

            network_request = NetworkRequest(
                request_id=request_id,
                instance_id=instance_id,
                url=request.url,
                method=request.method,
                headers=dict(request.headers) if hasattr(request, "headers") else {},
                cookies=cookies,
                post_data=request.post_data if hasattr(request, "post_data") else None,
                resource_type=resource_type,
            )
            async with self._lock:
                self._requests[request_id] = network_request
                self._instance_requests[instance_id].append(request_id)
        except Exception:
            pass

    async def _on_response(self, event, instance_id: str, tab: Tab = None):
        """
        Handle response event.

        event: Any - The event object containing response data.
        instance_id: str - The browser instance identifier.
        tab: Tab - The browser tab (optional, for body capture).
        """
        try:
            request_id = event.request_id
            response = event.response

            body = None
            if tab:
                try:
                    result = await tab.send(uc.cdp.network.get_response_body(request_id=request_id))
                    if result:
                        body_str, base64_encoded = result
                        if base64_encoded:
                            body = base64.b64decode(body_str)
                        else:
                            body = body_str.encode("utf-8")
                except Exception:
                    pass

            network_response = NetworkResponse(
                request_id=request_id,
                status=response.status,
                headers=dict(response.headers) if hasattr(response, "headers") else {},
                content_type=response.mime_type if hasattr(response, "mime_type") else None,
                body=body,
            )
            async with self._lock:
                self._responses[request_id] = network_response
        except Exception:
            pass


    async def set_capture_filters(
        self,
        instance_id: str,
        include_types: Optional[List[str]] = None,
        exclude_types: Optional[List[str]] = None,
    ):
        """
        Set resource type filters for network capture.

        instance_id: str - The browser instance identifier.
        include_types: Optional[List[str]] - Only capture these types (Document, Stylesheet, Image, Media, Font, Script, XHR, Fetch, etc).
        exclude_types: Optional[List[str]] - Exclude these types from capture.
        """
        async with self._lock:
            self._instance_filters[instance_id] = {
                "include": include_types or [],
                "exclude": exclude_types or [],
            }

    async def get_capture_filters(self, instance_id: str) -> Dict[str, List[str]]:
        """
        Get current capture filters.

        instance_id: str - The browser instance identifier.
        Returns: Dict[str, List[str]] - Current filters.
        """
        async with self._lock:
            return self._instance_filters.get(instance_id, {"include": [], "exclude": []})

    async def search_requests(
        self,
        instance_id: str,
        url_pattern: Optional[str] = None,
        method: Optional[str] = None,
        status_code: Optional[int] = None,
        response_contains: Optional[str] = None,
        payload_contains: Optional[str] = None,
        resource_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Search requests with advanced filters and pagination.

        instance_id: str - The browser instance identifier.
        url_pattern: Optional[str] - Filter by URL pattern (substring match).
        method: Optional[str] - Filter by HTTP method.
        status_code: Optional[int] - Filter by response status code.
        response_contains: Optional[str] - Search in response body.
        payload_contains: Optional[str] - Search in request payload.
        resource_type: Optional[str] - Filter by resource type.
        limit: int - Max results per page.
        offset: int - Starting index for pagination.
        Returns: Dict[str, Any] - Paginated results with metadata.
        """
        async with self._lock:
            request_ids = self._instance_requests.get(instance_id, [])
            matches = []

            for req_id in request_ids:
                if req_id not in self._requests:
                    continue

                request = self._requests[req_id]
                response = self._responses.get(req_id)

                if url_pattern and url_pattern.lower() not in request.url.lower():
                    continue
                if method and request.method.upper() != method.upper():
                    continue
                if resource_type and (not request.resource_type or resource_type.lower() not in request.resource_type.lower()):
                    continue
                if status_code and (not response or response.status != status_code):
                    continue
                if payload_contains and (not request.post_data or payload_contains.lower() not in request.post_data.lower()):
                    continue
                if response_contains and response and response.body:
                    try:
                        body_str = response.body.decode('utf-8', errors='ignore')
                        if response_contains.lower() not in body_str.lower():
                            continue
                    except:
                        continue

                matches.append({
                    "request_id": req_id,
                    "url": request.url,
                    "method": request.method,
                    "status": response.status if response else None,
                    "resource_type": request.resource_type,
                })

            total = len(matches)
            paginated = matches[offset:offset + limit]

            return {
                "results": paginated,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total,
            }

    async def list_requests(self, instance_id: str, filter_type: Optional[str] = None) -> List[NetworkRequest]:
        """
        List all requests for an instance.

        instance_id: str - The browser instance identifier.
        filter_type: Optional[str] - Filter requests by resource type.
        Returns: List[NetworkRequest] - List of network requests.
        """
        async with self._lock:
            request_ids = self._instance_requests.get(instance_id, [])
            requests = []
            for req_id in request_ids:
                if req_id in self._requests:
                    request = self._requests[req_id]
                    if filter_type:
                        if request.resource_type and filter_type.lower() in request.resource_type.lower():
                            requests.append(request)
                    else:
                        requests.append(request)
            return requests

    async def get_request(self, request_id: str) -> Optional[NetworkRequest]:
        """
        Get specific request by ID.

        request_id: str - The request identifier.
        Returns: Optional[NetworkRequest] - The network request object or None.
        """
        async with self._lock:
            return self._requests.get(request_id)

    async def get_response(self, request_id: str) -> Optional[NetworkResponse]:
        """
        Get response for a request.

        request_id: str - The request identifier.
        Returns: Optional[NetworkResponse] - The network response object or None.
        """
        async with self._lock:
            return self._responses.get(request_id)

    async def get_response_body(self, tab: Tab, request_id: str) -> Optional[bytes]:
        """
        Get response body content.

        tab: Tab - The browser tab.
        request_id: str - The request identifier.
        Returns: Optional[bytes] - The response body as bytes, or None.
        """
        try:
            # Convert string to RequestId object
            request_id_obj = uc.cdp.network.RequestId(request_id)
            result = await tab.send(uc.cdp.network.get_response_body(request_id=request_id_obj))
            if result:
                body, base64_encoded = result  # Result is a tuple (body, base64Encoded)
                if base64_encoded:
                    return base64.b64decode(body)
                else:
                    return body.encode("utf-8")
        except Exception:
            pass
        return None

    async def modify_headers(self, tab: Tab, headers: Dict[str, str]):
        """
        Modify request headers for future requests.

        tab: Tab - The browser tab.
        headers: Dict[str, str] - Headers to set.
        Returns: bool - True if successful.
        """
        try:
            # Convert dict to Headers object
            headers_obj = uc.cdp.network.Headers(headers)
            await tab.send(uc.cdp.network.set_extra_http_headers(headers=headers_obj))
            return True
        except Exception as e:
            raise Exception(f"Failed to modify headers: {str(e)}")

    async def set_user_agent(self, tab: Tab, user_agent: str):
        """
        Set custom user agent.

        tab: Tab - The browser tab.
        user_agent: str - The user agent string to set.
        Returns: bool - True if successful.
        """
        try:
            await tab.send(uc.cdp.network.set_user_agent_override(user_agent=user_agent))
            return True
        except Exception as e:
            raise Exception(f"Failed to set user agent: {str(e)}")

    async def export_to_json(self, instance_id: str, filepath: str) -> bool:
        """
        Export network data to JSON file.

        instance_id: str - The browser instance identifier.
        filepath: str - Path to save JSON file.
        Returns: bool - True if successful.
        """
        async with self._lock:
            request_ids = self._instance_requests.get(instance_id, [])
            data = {"requests": [], "responses": []}

            for req_id in request_ids:
                if req_id in self._requests:
                    req = self._requests[req_id]
                    data["requests"].append({
                        "request_id": req.request_id,
                        "url": req.url,
                        "method": req.method,
                        "headers": req.headers,
                        "cookies": req.cookies,
                        "post_data": req.post_data,
                        "resource_type": req.resource_type,
                        "timestamp": req.timestamp.isoformat(),
                    })

                if req_id in self._responses:
                    resp = self._responses[req_id]
                    data["responses"].append({
                        "request_id": resp.request_id,
                        "status": resp.status,
                        "headers": resp.headers,
                        "content_type": resp.content_type,
                        "body": base64.b64encode(resp.body).decode('utf-8') if resp.body else None,
                        "timestamp": resp.timestamp.isoformat(),
                    })

            Path(filepath).write_text(json.dumps(data, indent=2))
            return True

    async def import_from_json(self, instance_id: str, filepath: str) -> bool:
        """
        Import network data from JSON file.

        instance_id: str - The browser instance identifier.
        filepath: str - Path to JSON file.
        Returns: bool - True if successful.
        """
        data = json.loads(Path(filepath).read_text())

        async with self._lock:
            if instance_id not in self._instance_requests:
                self._instance_requests[instance_id] = []

            for req_data in data.get("requests", []):
                req = NetworkRequest(
                    request_id=req_data["request_id"],
                    instance_id=instance_id,
                    url=req_data["url"],
                    method=req_data["method"],
                    headers=req_data["headers"],
                    cookies=req_data["cookies"],
                    post_data=req_data.get("post_data"),
                    resource_type=req_data.get("resource_type"),
                    timestamp=datetime.fromisoformat(req_data["timestamp"]),
                )
                self._requests[req.request_id] = req
                if req.request_id not in self._instance_requests[instance_id]:
                    self._instance_requests[instance_id].append(req.request_id)

            for resp_data in data.get("responses", []):
                resp = NetworkResponse(
                    request_id=resp_data["request_id"],
                    status=resp_data["status"],
                    headers=resp_data["headers"],
                    content_type=resp_data.get("content_type"),
                    body=base64.b64decode(resp_data["body"]) if resp_data.get("body") else None,
                    timestamp=datetime.fromisoformat(resp_data["timestamp"]),
                )
                self._responses[resp.request_id] = resp

            return True

    async def enable_cache(self, tab: Tab, enabled: bool = True):
        """
        Enable or disable cache.

        tab: Tab - The browser tab.
        enabled: bool - True to enable cache, False to disable.
        Returns: bool - True if successful.
        """
        try:
            await tab.send(uc.cdp.network.set_cache_disabled(cache_disabled=not enabled))
            return True
        except Exception as e:
            raise Exception(f"Failed to set cache state: {str(e)}")

    async def clear_browser_cache(self, tab: Tab):
        """
        Clear browser cache.

        tab: Tab - The browser tab.
        Returns: bool - True if successful.
        """
        try:
            await tab.send(uc.cdp.network.clear_browser_cache())
            return True
        except Exception as e:
            raise Exception(f"Failed to clear cache: {str(e)}")

    async def clear_cookies(self, tab: Tab, url: Optional[str] = None):
        """
        Clear cookies.

        tab: Tab - The browser tab.
        url: Optional[str] - The URL for which to clear cookies, or None to clear all.
        Returns: bool - True if successful.
        """
        try:
            if url:
                # For specific URL, get all cookies for that URL and delete them
                cookies = await tab.send(uc.cdp.network.get_cookies(urls=[url]))
                for cookie in cookies:
                    await tab.send(
                        uc.cdp.network.delete_cookies(
                            name=cookie.name,
                            url=url
                        )
                    )
            else:
                # Clear all browser cookies using the proper method
                await tab.send(uc.cdp.network.clear_browser_cookies())
            return True
        except Exception as e:
            raise Exception(f"Failed to clear cookies: {str(e)}")

    async def set_cookie(self, tab: Tab, cookie: Dict[str, Any]):
        """
        Set a cookie.

        tab: Tab - The browser tab.
        cookie: Dict[str, Any] - Cookie parameters.
        Returns: bool - True if successful.
        """
        try:
            await tab.send(uc.cdp.network.set_cookie(**cookie))
            return True
        except Exception as e:
            raise Exception(f"Failed to set cookie: {str(e)}")

    async def get_cookies(self, tab: Tab, urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Get cookies.

        tab: Tab - The browser tab.
        urls: Optional[List[str]] - List of URLs to get cookies for, or None for all.
        Returns: List[Dict[str, Any]] - List of cookies.
        """
        try:
            if urls:
                result = await tab.send(uc.cdp.network.get_cookies(urls=urls))
            else:
                result = await tab.send(uc.cdp.network.get_all_cookies())
            if isinstance(result, dict):
                return result.get("cookies", [])
            elif isinstance(result, list):
                return result
            else:
                return []
        except Exception as e:
            raise Exception(f"Failed to get cookies: {str(e)}")

    async def emulate_network_conditions(
        self,
        tab: Tab,
        offline: bool = False,
        latency: int = 0,
        download_throughput: int = -1,
        upload_throughput: int = -1,
    ):
        """
        Emulate network conditions.

        tab: Tab - The browser tab.
        offline: bool - Whether to emulate offline mode.
        latency: int - Additional latency (ms).
        download_throughput: int - Download speed (bytes/sec).
        upload_throughput: int - Upload speed (bytes/sec).
        Returns: bool - True if successful.
        """
        try:
            await tab.send(
                uc.cdp.network.emulate_network_conditions(
                    offline=offline,
                    latency=latency,
                    download_throughput=download_throughput,
                    upload_throughput=upload_throughput,
                )
            )
            return True
        except Exception as e:
            raise Exception(f"Failed to emulate network conditions: {str(e)}")

    async def clear_instance_data(self, instance_id: str):
        """
        Clear all network data for an instance.

        instance_id: str - The browser instance identifier.
        """
        async with self._lock:
            if instance_id in self._instance_requests:
                for req_id in self._instance_requests[instance_id]:
                    self._requests.pop(req_id, None)
                    self._responses.pop(req_id, None)
                del self._instance_requests[instance_id]