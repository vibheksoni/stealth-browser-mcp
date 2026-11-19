"""Session save and restore functionality (storageState-like JSON, mode 0600 POSIX)."""

import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import cdp_raw
from .debug_logger import debug_logger
from .paths import data_dir


def default_session_path(instance_id: Optional[str] = None) -> Path:
    """
    Return the default file path for session state JSON.

    Args:
        instance_id (Optional[str]): Browser instance identifier.

    Returns:
        Path: Target session file location under the data directory.
    """
    sessions_dir = data_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(instance_id)) if instance_id else "default"
    return sessions_dir / f"session_{slug}.json"


def write_session_file(file_path: Union[str, Path], state: Dict[str, Any]) -> Path:
    """
    Write session state to a JSON file with POSIX mode 0600 (owner read/write only).

    Args:
        file_path (Union[str, Path]): Destination file path.
        state (Dict[str, Any]): Session state dictionary.

    Returns:
        Path: Absolute resolved path of the written file.
    """
    path = Path(file_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    content = json.dumps(state, indent=2, ensure_ascii=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    fd = os.open(str(path), flags, 0o600)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
    finally:
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    return path


def read_session_file(file_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Read session state from a JSON file.

    Args:
        file_path (Union[str, Path]): Source file path.

    Returns:
        Dict[str, Any]: Parsed session state dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If file content is not a JSON object.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Session file not found: {path}")

    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Session state must be a JSON object, got {type(data).__name__}")
    return data


def sanitize_cookie_for_cdp(cookie: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a cookie dictionary into the parameter shape expected by CDP Network.setCookies.

    Args:
        cookie (Dict[str, Any]): Cookie dictionary from storageState or CDP.

    Returns:
        Dict[str, Any]: Sanitized dictionary conforming to CDP CookieParam.
    """
    out: Dict[str, Any] = {
        "name": str(cookie["name"]),
        "value": str(cookie["value"]),
    }

    if "domain" in cookie and cookie["domain"]:
        out["domain"] = str(cookie["domain"])
    if "path" in cookie and cookie["path"]:
        out["path"] = str(cookie["path"])
    if "secure" in cookie and cookie["secure"] is not None:
        out["secure"] = bool(cookie["secure"])

    http_only = cookie.get("httpOnly") if "httpOnly" in cookie else cookie.get("http_only")
    if http_only is not None:
        out["httpOnly"] = bool(http_only)

    same_site = cookie.get("sameSite") or cookie.get("same_site")
    if same_site:
        val = str(same_site).capitalize()
        if val in ("Strict", "Lax", "None"):
            out["sameSite"] = val

    expires = cookie.get("expires")
    if expires is not None and not cookie.get("session", False):
        try:
            exp_float = float(expires)
            if exp_float > 0:
                out["expires"] = exp_float
        except (ValueError, TypeError):
            pass

    if "priority" in cookie and cookie["priority"]:
        val = str(cookie["priority"]).capitalize()
        if val in ("Low", "Medium", "High"):
            out["priority"] = val

    if "sameParty" in cookie and cookie["sameParty"] is not None:
        out["sameParty"] = bool(cookie["sameParty"])

    if "sourceScheme" in cookie and cookie["sourceScheme"]:
        out["sourceScheme"] = str(cookie["sourceScheme"])

    if "sourcePort" in cookie and cookie["sourcePort"] is not None:
        try:
            port = int(cookie["sourcePort"])
            if port > 0:
                out["sourcePort"] = port
        except (ValueError, TypeError):
            pass

    if "partitionKey" in cookie and isinstance(cookie["partitionKey"], dict):
        out["partitionKey"] = cookie["partitionKey"]

    if "url" in cookie and cookie["url"]:
        out["url"] = str(cookie["url"])
    elif "domain" in out:
        domain = out["domain"].lstrip(".")
        scheme = "https" if out.get("secure") else "http"
        port = out.get("sourcePort")
        port_suffix = f":{port}" if port and port not in (-1, 80, 443) else ""
        path = out.get("path") or "/"
        out["url"] = f"{scheme}://{domain}{port_suffix}{path}"

    return out


async def save_session_state(
    tab: Any,
    browser: Optional[Any] = None,
    origins: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Extract session state (cookies via CDP Network.getAllCookies, localStorage per origin).

    Args:
        tab (Any): Current active nodriver Tab.
        browser (Optional[Any]): nodriver Browser instance.
        origins (Optional[List[str]]): Specific origins to extract localStorage for.

    Returns:
        Dict[str, Any]: StorageState-like dictionary with 'cookies' and 'origins'.
    """
    cookies: List[Dict[str, Any]] = []
    try:
        raw_cookies = await cdp_raw.get_cookies(tab, all_cookies=True)
        for c in raw_cookies:
            if isinstance(c, dict) and "name" in c and "value" in c:
                cookie_entry: Dict[str, Any] = {
                    "name": str(c["name"]),
                    "value": str(c["value"]),
                    "domain": str(c.get("domain", "")),
                    "path": str(c.get("path", "/")),
                    "expires": float(c["expires"]) if c.get("expires") is not None else -1,
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "sameSite": str(c["sameSite"]).capitalize() if c.get("sameSite") else "Lax",
                }
                for extra in ("priority", "sameParty", "sourceScheme", "sourcePort", "partitionKey"):
                    if extra in c and c[extra] is not None:
                        cookie_entry[extra] = c[extra]
                cookies.append(cookie_entry)
    except Exception as exc:
        debug_logger.log_warning("session_state", "get_cookies", f"Failed to get cookies: {exc}")

    candidate_origins = set()
    if origins:
        origin_list = [origins] if isinstance(origins, str) else origins
        for o in origin_list:
            cleaned = str(o).strip().rstrip("/")
            if cleaned:
                candidate_origins.add(cleaned)

    if browser is None:
        browser = getattr(tab, "browser", None)
    all_tabs = list(getattr(browser, "tabs", []) or [tab])
    if tab not in all_tabs:
        all_tabs.append(tab)

    for t in all_tabs:
        url_val = getattr(t, "url", None) or getattr(getattr(t, "target", None), "url", None)
        if url_val and isinstance(url_val, str):
            parsed = urllib.parse.urlsplit(url_val)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                candidate_origins.add(f"{parsed.scheme}://{parsed.netloc}")

    for c in cookies:
        domain = str(c.get("domain", "")).lstrip(".")
        if domain:
            scheme = "https" if c.get("secure") or c.get("sourceScheme") == "Secure" else "http"
            port = c.get("sourcePort")
            port_suffix = f":{port}" if port and port not in (-1, 80, 443) else ""
            candidate_origins.add(f"{scheme}://{domain}{port_suffix}")

    saved_origins: List[Dict[str, Any]] = []

    try:
        await tab.send(cdp_raw.raw_command("DOMStorage.enable"))
    except Exception:
        pass

    for origin in sorted(candidate_origins):
        origin_storage: Dict[str, str] = {}

        try:
            storage_id = {"securityOrigin": origin, "isLocalStorage": True}
            resp = await tab.send(cdp_raw.raw_command("DOMStorage.getDOMStorageItems", {"storageId": storage_id}))
            if isinstance(resp, dict):
                entries = resp.get("entries", [])
                for entry in entries:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        origin_storage[str(entry[0])] = str(entry[1])
        except Exception as exc:
            debug_logger.log_info("session_state", "dom_storage_items", f"DOMStorage query for {origin}: {exc}")

        for t in all_tabs:
            u = getattr(t, "url", None) or getattr(getattr(t, "target", None), "url", None)
            if u and isinstance(u, str):
                parsed = urllib.parse.urlsplit(u)
                if f"{parsed.scheme}://{parsed.netloc}" == origin:
                    try:
                        eval_resp = await t.send(cdp_raw.raw_command("Runtime.evaluate", {
                            "expression": """
                                (() => {
                                    try {
                                        const res = [];
                                        for (let i = 0; i < localStorage.length; i++) {
                                            const k = localStorage.key(i);
                                            res.push([k, localStorage.getItem(k)]);
                                        }
                                        return res;
                                    } catch (e) {
                                        return [];
                                    }
                                })()
                            """,
                            "returnByValue": True,
                        }))
                        if isinstance(eval_resp, dict):
                            val = eval_resp.get("result", {}).get("value")
                            if isinstance(val, list):
                                for item in val:
                                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                                        origin_storage[str(item[0])] = str(item[1])
                    except Exception as exc:
                        debug_logger.log_info("session_state", "runtime_eval_storage", f"Evaluate for {origin}: {exc}")
                    break

        if origin_storage:
            saved_origins.append({
                "origin": origin,
                "localStorage": [
                    {"name": k, "value": v}
                    for k, v in sorted(origin_storage.items())
                ],
            })

    return {
        "cookies": cookies,
        "origins": saved_origins,
    }


async def restore_session_state(
    tab: Any,
    state: Dict[str, Any],
    browser: Optional[Any] = None,
    clear_existing: bool = True,
    instance_id: Optional[str] = None,
    browser_manager: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Restore session state (cookies via CDP Network.setCookies, localStorage per origin).

    Args:
        tab (Any): Current active nodriver Tab.
        state (Dict[str, Any]): Session state dictionary.
        browser (Optional[Any]): nodriver Browser instance.
        clear_existing (bool): Whether to clear existing cookies and storage.
        instance_id (Optional[str]): Browser instance identifier for init scripts.
        browser_manager (Optional[Any]): BrowserManager instance.

    Returns:
        Dict[str, Any]: Summary with cookies_restored and origins_restored.
    """
    if not isinstance(state, dict):
        raise ValueError(f"Session state must be a dict, got {type(state).__name__}")

    raw_cookies = state.get("cookies", [])
    raw_origins = state.get("origins", [])

    if browser is None:
        browser = getattr(tab, "browser", None)
    all_tabs = list(getattr(browser, "tabs", []) or [tab])
    if tab not in all_tabs:
        all_tabs.append(tab)

    if clear_existing:
        try:
            await tab.send(cdp_raw.raw_command("Network.clearBrowserCookies"))
        except Exception as exc:
            debug_logger.log_warning("session_state", "clear_cookies", f"Failed clearing cookies: {exc}")

    cookies_restored = 0
    if raw_cookies:
        sanitized = [sanitize_cookie_for_cdp(c) for c in raw_cookies if isinstance(c, dict)]
        if sanitized:
            try:
                await tab.send(cdp_raw.raw_command("Network.setCookies", {"cookies": sanitized}))
                cookies_restored = len(sanitized)
            except Exception as exc:
                debug_logger.log_warning("session_state", "set_cookies_batch", f"Batch setCookies failed: {exc}, using fallback")
                for sc in sanitized:
                    try:
                        await tab.send(cdp_raw.raw_command("Network.setCookie", sc))
                        cookies_restored += 1
                    except Exception as s_exc:
                        debug_logger.log_warning("session_state", "set_cookie_single", f"Failed setting cookie {sc.get('name')}: {s_exc}")

    origins_restored = 0
    all_origin_data: Dict[str, Dict[str, str]] = {}

    try:
        await tab.send(cdp_raw.raw_command("DOMStorage.enable"))
    except Exception:
        pass

    for origin_entry in raw_origins:
        if not isinstance(origin_entry, dict) or "origin" not in origin_entry:
            continue
        origin = str(origin_entry["origin"]).strip().rstrip("/")
        raw_items = origin_entry.get("localStorage", [])
        items_dict: Dict[str, str] = {}
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    items_dict[str(item["name"])] = str(item["value"])
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    items_dict[str(item[0])] = str(item[1])
        elif isinstance(raw_items, dict):
            for k, v in raw_items.items():
                items_dict[str(k)] = str(v)

        all_origin_data[origin] = items_dict

        storage_id = {"securityOrigin": origin, "isLocalStorage": True}
        if clear_existing:
            try:
                await tab.send(cdp_raw.raw_command("DOMStorage.clear", {"storageId": storage_id}))
            except Exception:
                pass

        for k, v in items_dict.items():
            try:
                await tab.send(cdp_raw.raw_command("DOMStorage.setDOMStorageItem", {
                    "storageId": storage_id,
                    "key": k,
                    "value": v,
                }))
            except Exception as exc:
                debug_logger.log_info("session_state", "set_dom_storage", f"setDOMStorageItem {k} on {origin}: {exc}")

        for t in all_tabs:
            u = getattr(t, "url", None) or getattr(getattr(t, "target", None), "url", None)
            if u and isinstance(u, str):
                parsed = urllib.parse.urlsplit(u)
                if f"{parsed.scheme}://{parsed.netloc}" == origin:
                    try:
                        stmts = []
                        if clear_existing:
                            stmts.append("localStorage.clear();")
                        for k, v in items_dict.items():
                            stmts.append(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});")
                        await t.send(cdp_raw.raw_command("Runtime.evaluate", {
                            "expression": f"(() => {{ try {{ {' '.join(stmts)} }} catch(e) {{}} }})()",
                            "returnByValue": True,
                        }))
                    except Exception as exc:
                        debug_logger.log_info("session_state", "runtime_set_storage", f"Runtime evaluate for {origin}: {exc}")

        origins_restored += 1

    if all_origin_data:
        script_code = (
            "(function() {\n"
            "  try {\n"
            f"    const originData = {json.dumps(all_origin_data)};\n"
            "    const currentOrigin = window.location.origin;\n"
            "    if (originData[currentOrigin]) {\n"
            "      const items = originData[currentOrigin];\n"
            "      for (const [k, v] of Object.entries(items)) {\n"
            "        if (localStorage.getItem(k) !== v) {\n"
            "          localStorage.setItem(k, v);\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "  } catch (e) {}\n"
            "})();\n"
        )
        if browser_manager is not None and instance_id is not None:
            try:
                await browser_manager.add_init_script(instance_id, script_code)
            except Exception as exc:
                debug_logger.log_info("session_state", "add_init_script_manager", f"Failed registering init script: {exc}")
        else:
            try:
                await tab.send(cdp_raw.raw_command("Page.enable"))
                await tab.send(cdp_raw.raw_command("Page.addScriptToEvaluateOnNewDocument", {"source": script_code}))
            except Exception as exc:
                debug_logger.log_info("session_state", "add_init_script_tab", f"Failed registering init script on tab: {exc}")

    return {
        "success": True,
        "cookies_restored": cookies_restored,
        "origins_restored": origins_restored,
    }


async def save_session(
    instance_id: str,
    file_path: Optional[str] = None,
    origins: Optional[List[str]] = None,
    browser_manager: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Save browser session state to JSON file with mode 0600.

    Args:
        instance_id (str): Browser instance ID.
        file_path (Optional[str]): Target JSON file path.
        origins (Optional[List[str]]): Specific origins to save.
        browser_manager (Optional[Any]): BrowserManager instance.

    Returns:
        Dict[str, Any]: Result with file_path, cookies_count, origins_count, and session data.
    """
    if browser_manager is None:
        try:
            from . import server
            browser_manager = getattr(server, "browser_manager", None)
        except ImportError:
            pass
    if browser_manager is None:
        raise RuntimeError("Browser manager is not available.")

    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    browser = await browser_manager.get_browser(instance_id)
    state = await save_session_state(tab, browser=browser, origins=origins)

    target_path = Path(file_path).expanduser().resolve() if file_path else default_session_path(instance_id)
    written_path = write_session_file(target_path, state)

    return {
        "success": True,
        "file_path": str(written_path),
        "cookies_count": len(state.get("cookies", [])),
        "origins_count": len(state.get("origins", [])),
        "session": state,
    }


async def restore_session(
    instance_id: str,
    file_path: Optional[str] = None,
    session_data: Optional[Union[Dict[str, Any], str]] = None,
    clear_existing: bool = True,
    browser_manager: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Restore browser session state from JSON file or dictionary.

    Args:
        instance_id (str): Browser instance ID.
        file_path (Optional[str]): Source JSON file path.
        session_data (Optional[Union[Dict[str, Any], str]]): In-memory session data.
        clear_existing (bool): Clear existing cookies and storage before restore.
        browser_manager (Optional[Any]): BrowserManager instance.

    Returns:
        Dict[str, Any]: Result with success, cookies_restored, origins_restored.
    """
    if browser_manager is None:
        try:
            from . import server
            browser_manager = getattr(server, "browser_manager", None)
        except ImportError:
            pass
    if browser_manager is None:
        raise RuntimeError("Browser manager is not available.")

    source_file: Optional[str] = None
    if session_data is not None:
        if isinstance(session_data, str):
            state = json.loads(session_data)
        elif isinstance(session_data, dict):
            state = session_data
        else:
            raise ValueError(f"session_data must be dict or JSON string, got {type(session_data).__name__}")
    elif file_path:
        state = read_session_file(file_path)
        source_file = str(Path(file_path).expanduser().resolve())
    else:
        def_path = default_session_path(instance_id)
        if def_path.is_file():
            state = read_session_file(def_path)
            source_file = str(def_path)
        else:
            raise ValueError("Either file_path or session_data must be provided.")

    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    browser = await browser_manager.get_browser(instance_id)

    summary = await restore_session_state(
        tab=tab,
        state=state,
        browser=browser,
        clear_existing=clear_existing,
        instance_id=instance_id,
        browser_manager=browser_manager,
    )

    summary["file_path"] = source_file
    return summary
