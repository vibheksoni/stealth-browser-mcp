"""Build HAR 1.2 documents from in-memory network capture data.

The capture format mirrors the request/response records held by
``network_interceptor.NetworkInterceptor`` (see ``models.py``): a list of
request dicts optionally paired with a matching response dict. This module
converts that into a HAR 1.2 structure suitable for export.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
}

DEFAULT_HAR_VERSION = "1.2"
UNKNOWN_SIZE = -1


def _redact_header_value(name: str, value: str) -> str:
    """Return [REDACTED] when the header name is sensitive, else the original value."""
    if name.lower() in SENSITIVE_HEADERS:
        return "[REDACTED]"
    return value


def _build_headers(headers: Any, include_sensitive: bool) -> List[Dict[str, str]]:
    """Convert captured headers mapping or list to a HAR headers list."""
    if not headers:
        return []
    if isinstance(headers, dict):
        return [
            {
                "name": str(name),
                "value": str(value) if include_sensitive else _redact_header_value(str(name), str(value)),
            }
            for name, value in headers.items()
        ]
    if isinstance(headers, list):
        result = []
        for h in headers:
            if isinstance(h, dict):
                name = str(h.get("name", ""))
                value = str(h.get("value", ""))
                result.append({
                    "name": name,
                    "value": value if include_sensitive else _redact_header_value(name, value),
                })
        return result
    return []


def _build_cookies(cookies: Any, include_sensitive: bool) -> List[Dict[str, str]]:
    """Convert captured cookies mapping or list to a HAR cookies list."""
    if not cookies:
        return []
    if isinstance(cookies, dict):
        return [
            {
                "name": str(name),
                "value": str(value) if include_sensitive else "[REDACTED]",
            }
            for name, value in cookies.items()
        ]
    if isinstance(cookies, list):
        result = []
        for c in cookies:
            if isinstance(c, dict):
                name = str(c.get("name", ""))
                val = str(c.get("value", ""))
                result.append({
                    "name": name,
                    "value": val if include_sensitive else "[REDACTED]",
                })
        return result
    return []


def _parse_query(url: str) -> List[Dict[str, str]]:
    """Split a URL into HAR query string parameters."""
    if not url:
        return []
    try:
        query = urlsplit(url).query
        if not query:
            return []
        return [{"name": k, "value": v} for k, v in parse_qsl(query, keep_blank_values=True)]
    except Exception:
        return []


def _find_header_value(headers: Any, name: str) -> str:
    """Return the first header value matching name (case-insensitive)."""
    target = name.lower()
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == target:
                return str(v)
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and str(h.get("name", "")).lower() == target:
                return str(h.get("value", ""))
    return ""


def _encode_post_data(data: Any, headers: Any) -> Optional[Dict[str, Any]]:
    """Build a HAR postData object for a request payload, or None if no payload."""
    if not data:
        return None
    if isinstance(data, dict):
        mime_type = data.get("mimeType") or _find_header_value(headers, "content-type") or "application/octet-stream"
        text = str(data.get("text", ""))
        params = data.get("params", [])
        return {
            "mimeType": mime_type,
            "text": text,
            "params": params if isinstance(params, list) else [],
        }

    if isinstance(data, bytes):
        try:
            data_str = data.decode("utf-8")
        except UnicodeDecodeError:
            data_str = data.decode("latin1", errors="replace")
    else:
        data_str = str(data)

    content_type = _find_header_value(headers, "content-type")
    is_form = "x-www-form-urlencoded" in content_type.lower()
    params = []
    if is_form:
        try:
            params = [{"name": part, "value": val} for part, val in parse_qsl(data_str, keep_blank_values=True)]
        except Exception:
            params = []

    return {
        "mimeType": content_type or "application/x-www-form-urlencoded",
        "text": data_str,
        "params": params,
    }


def _build_timings(timings: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Normalize a timings mapping, tolerating missing or partial input."""
    timings = timings or {}
    send = timings.get("send", UNKNOWN_SIZE)
    wait = timings.get("wait", UNKNOWN_SIZE)
    receive = timings.get("receive", UNKNOWN_SIZE)
    return {
        "send": int(send) if isinstance(send, (int, float)) else UNKNOWN_SIZE,
        "wait": int(wait) if isinstance(wait, (int, float)) else UNKNOWN_SIZE,
        "receive": int(receive) if isinstance(receive, (int, float)) else UNKNOWN_SIZE,
    }


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Safely convert a model, mapping, or object to a dictionary."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _build_request(request: Dict[str, Any], include_sensitive: bool) -> Dict[str, Any]:
    """Convert a single captured request into a HAR request object."""
    url = str(request.get("url", ""))
    method = str(request.get("method", "GET")).upper() if request.get("method") else "GET"
    http_version = str(request.get("httpVersion", "HTTP/1.1"))

    headers = _build_headers(request.get("headers"), include_sensitive)
    cookies = _build_cookies(request.get("cookies"), include_sensitive)

    query_input = request.get("queryString")
    if query_input is None:
        query_string = _parse_query(url)
    elif isinstance(query_input, list):
        query_string = [
            {"name": str(q.get("name", "")), "value": str(q.get("value", ""))}
            for q in query_input if isinstance(q, dict)
        ]
    elif isinstance(query_input, dict):
        query_string = [{"name": str(k), "value": str(v)} for k, v in query_input.items()]
    else:
        query_string = _parse_query(url)

    post_data_raw = request.get("postData")
    if post_data_raw is None:
        post_data_raw = request.get("post_data")

    post_data = _encode_post_data(post_data_raw, request.get("headers"))

    headers_size = request.get("headersSize", UNKNOWN_SIZE)
    body_size = request.get("bodySize")
    if body_size is None:
        body_size = request.get("requestBodySize")
    if body_size is None:
        if post_data is not None and "text" in post_data:
            body_size = len(post_data["text"].encode("utf-8"))
        else:
            body_size = UNKNOWN_SIZE

    req_obj: Dict[str, Any] = {
        "method": method,
        "url": url,
        "httpVersion": http_version,
        "headers": headers,
        "queryString": query_string,
        "cookies": cookies,
        "headersSize": headers_size if isinstance(headers_size, int) else UNKNOWN_SIZE,
        "bodySize": body_size if isinstance(body_size, int) else UNKNOWN_SIZE,
    }
    if post_data is not None:
        req_obj["postData"] = post_data

    return req_obj


def _build_response(response: Dict[str, Any], include_sensitive: bool) -> Dict[str, Any]:
    """Convert a single captured response into a HAR response object."""
    response = response or {}

    status_val = response.get("status")
    if status_val is None:
        status = UNKNOWN_SIZE
    else:
        try:
            status = int(status_val)
        except (ValueError, TypeError):
            status = UNKNOWN_SIZE

    status_text = str(response.get("statusText", ""))
    http_version = str(response.get("httpVersion", "HTTP/1.1"))

    headers = _build_headers(response.get("headers"), include_sensitive)
    cookies = _build_cookies(response.get("cookies"), include_sensitive)

    content_input = response.get("content") if isinstance(response.get("content"), dict) else {}
    body_text = content_input.get("text")

    if body_text is None:
        if isinstance(response.get("text"), str):
            body_text = response["text"]
        elif isinstance(response.get("body"), bytes):
            try:
                body_text = response["body"].decode("utf-8")
            except UnicodeDecodeError:
                body_text = response["body"].decode("latin1", errors="replace")
        elif isinstance(response.get("body"), str):
            body_text = response["body"]

    size = content_input.get("size")
    if size is None:
        size = response.get("content_length")
    if size is None:
        if isinstance(response.get("body"), bytes):
            size = len(response["body"])
        elif isinstance(body_text, str):
            size = len(body_text.encode("utf-8"))
        elif response.get("bodySize") is not None:
            size = response["bodySize"]
        elif response.get("responseBodySize") is not None:
            size = response["responseBodySize"]
        else:
            size = UNKNOWN_SIZE

    mime_type = (
        content_input.get("mimeType")
        or response.get("content_type")
        or response.get("mimeType")
        or _find_header_value(headers, "content-type")
        or ""
    )

    content_obj: Dict[str, Any] = {
        "size": size if isinstance(size, int) else UNKNOWN_SIZE,
        "mimeType": mime_type,
    }
    if body_text is not None:
        content_obj["text"] = body_text

    redirect_url = str(
        response.get("redirectURL")
        or response.get("redirect_url")
        or _find_header_value(headers, "location")
        or ""
    )
    headers_size = response.get("headersSize", UNKNOWN_SIZE)
    body_size = response.get(
        "bodySize",
        response.get("responseBodySize", size if size != UNKNOWN_SIZE else UNKNOWN_SIZE),
    )

    return {
        "status": status,
        "statusText": status_text,
        "httpVersion": http_version,
        "headers": headers,
        "cookies": cookies,
        "content": content_obj,
        "redirectURL": redirect_url,
        "headersSize": headers_size if isinstance(headers_size, int) else UNKNOWN_SIZE,
        "bodySize": body_size if isinstance(body_size, int) else UNKNOWN_SIZE,
    }


def _build_entry(entry: Any, include_sensitive: bool) -> Dict[str, Any]:
    """Convert one captured entry (request + optional response) into a HAR entry."""
    entry_dict = _to_dict(entry)
    if "request" in entry_dict:
        req_raw = _to_dict(entry_dict["request"])
        resp_raw = _to_dict(entry_dict.get("response"))
    else:
        req_raw = entry_dict
        resp_raw = _to_dict(entry_dict.get("response"))

    ts = (
        entry_dict.get("startedDateTime")
        or req_raw.get("startedDateTime")
        or req_raw.get("timestamp")
        or resp_raw.get("startedDateTime")
        or resp_raw.get("timestamp")
    )
    if isinstance(ts, datetime):
        started_date_time = ts.isoformat()
    elif isinstance(ts, str) and ts:
        started_date_time = ts
    else:
        started_date_time = datetime.now(timezone.utc).isoformat()

    elapsed_time = entry_dict.get("time")
    if elapsed_time is None:
        elapsed_time = resp_raw.get("time")
    if elapsed_time is None:
        elapsed_time = UNKNOWN_SIZE

    timings_raw = resp_raw.get("timings") or entry_dict.get("timings") or req_raw.get("timings")
    timings = _build_timings(timings_raw)

    return {
        "startedDateTime": started_date_time,
        "time": elapsed_time,
        "request": _build_request(req_raw, include_sensitive),
        "response": _build_response(resp_raw, include_sensitive),
        "cache": {},
        "timings": timings,
    }


def build_har(
    entries: List[Dict[str, Any]],
    browser_name: str = "stealth-browser-mcp",
    version: str = "",
    include_sensitive: bool = False,
    pages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Convert captured network data into a HAR 1.2 document.

    Args:
        entries (List[Dict[str, Any]]): Captured requests, each optionally paired
            with a "response" dict (the capture format from
            ``network_interceptor``). Missing fields fall back to HAR defaults;
            unknown sizes use -1.
        browser_name (str): Creator tool name.
        version (str): Creator tool version.
        include_sensitive (bool): When True, do not redact Authorization,
            Cookie, and Set-Cookie header values.
        pages (Optional[List[Dict[str, Any]]]): Optional page records.

    Returns:
        Dict[str, Any]: A HAR 1.2 document ready for serialization.
    """
    log: Dict[str, Any] = {
        "version": DEFAULT_HAR_VERSION,
        "creator": {
            "name": browser_name,
            "version": version,
        },
        "entries": [
            _build_entry(entry, include_sensitive) for entry in entries
        ],
    }
    if pages is not None:
        log["pages"] = pages

    return {"log": log}


def export_to_json(har: Dict[str, Any], filepath: str) -> bool:
    """Write a HAR document to a JSON file.

    Args:
        har (Dict[str, Any]): HAR document to persist.
        filepath (str): Destination path.

    Returns:
        bool: True when the file was written successfully.
    """
    try:
        p = Path(filepath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(har, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False
