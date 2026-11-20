"""
Progressive Element Cloner System
=================================

Stores comprehensive element clone data in memory and returns a compact handle
(`element_id`) so clients can progressively expand specific portions later.
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .comprehensive_element_cloner import comprehensive_element_cloner
from .debug_logger import debug_logger
from .persistent_storage import persistent_storage
from .mcp_sessions import get_current_mcp_session_id

MAX_STORED_ELEMENTS = 50
STYLE_CATEGORIES = {
    "layout": [
        "display",
        "position",
        "width",
        "height",
        "max-width",
        "max-height",
        "min-width",
        "min-height",
    ],
    "typography": [
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "line-height",
        "text-align",
    ],
    "colors": ["color", "background-color", "border-color"],
}


class ProgressiveElementCloner:
    """Progressive element cloner with in-memory store."""

    def __init__(self):
        self.STORAGE_KEY = "progressive_elements"

    def _get_store(self) -> Dict[str, Dict[str, Any]]:
        """
        Read the element store.

        Returns:
            Dict[str, Dict[str, Any]]: Element store keyed by element id
        """
        return persistent_storage.get(self.STORAGE_KEY, {})

    def _save_store(self, data: Dict[str, Dict[str, Any]]) -> None:
        """
        Persist the element store, evicting the oldest entries past the cap.

        Args:
            data (Dict[str, Dict[str, Any]]): Element store keyed by element id

        Returns:
            None
        """
        if len(data) > MAX_STORED_ELEMENTS:
            newest = sorted(data.items(), key=lambda item: item[1].get("timestamp", 0), reverse=True)
            data = dict(newest[:MAX_STORED_ELEMENTS])
        persistent_storage.set(self.STORAGE_KEY, data)

    def _lookup(self, element_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Find a stored clone and return its root element data.

        Args:
            element_id (str): Handle returned by clone_element_progressive

        Returns:
            Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]: (full_data, element_data),
            or (None, None) when the handle is unknown
        """
        entry = self._get_store().get(element_id)
        if entry is None or not self._visible_to_caller(entry):
            return None, None
        full_data = entry["full_data"]
        element_data = full_data.get("element")
        return full_data, element_data if isinstance(element_data, dict) else {}

    @staticmethod
    def _visible_to_caller(entry: Dict[str, Any]) -> bool:
        """True if the current MCP session may see this stored element."""
        sid = get_current_mcp_session_id()
        if not sid:
            return True
        owner = entry.get("owner_session_id")
        if owner is None:
            return False
        return owner == sid

    @staticmethod
    def _not_found(element_id: str) -> Dict[str, Any]:
        """
        Build the error returned for an unknown element handle.

        Args:
            element_id (str): Handle that was not found

        Returns:
            Dict[str, Any]: Error payload
        """
        return {"error": f"Element {element_id} not found"}

    async def clone_element_progressive(
        self,
        tab,
        selector: str,
        include_children: bool = True,
    ) -> Dict[str, Any]:
        """
        Clone an element, store the full payload, and return a compact handle.

        Args:
            tab (Any): Browser tab instance
            selector (str): CSS selector for the element
            include_children (bool): Include descendant elements in the clone

        Returns:
            Dict[str, Any]: Handle, summary counts, and available data sections
        """
        try:
            element_id = f"elem_{uuid.uuid4().hex[:12]}"
            debug_logger.log_info("progressive_cloner", "clone_progressive", f"Cloning {selector} -> {element_id}")

            full_data = await comprehensive_element_cloner.extract_complete_element(
                tab, selector, include_children
            )
            if not isinstance(full_data, dict) or "error" in full_data:
                error = full_data.get("error") if isinstance(full_data, dict) else None
                return {"error": error or "Element not found or extraction failed", "selector": selector}

            store = self._get_store()
            store[element_id] = {
                "full_data": full_data,
                "url": getattr(tab, "url", ""),
                "selector": selector,
                "timestamp": time.time(),
                "include_children": include_children,
                "owner_session_id": get_current_mcp_session_id(),
            }
            self._save_store(store)

            element_data = full_data.get("element") or {}
            html = element_data.get("html") or {}
            base = {
                "tagName": html.get("tagName", "unknown"),
                "attributes_count": len(html.get("attributes") or []),
                "children_count": len(full_data.get("children") or []),
                "summary": {
                    "styles_count": len(element_data.get("styles") or {}),
                    "event_listeners_count": len(element_data.get("eventListeners") or []),
                    "css_rules_count": len(element_data.get("cssRules") or []),
                },
            }

            return {
                "element_id": element_id,
                "base": base,
                "available_data": [
                    "styles",
                    "events",
                    "children",
                    "css_rules",
                    "pseudo_elements",
                    "animations",
                    "fonts",
                    "html",
                ],
                "url": getattr(tab, "url", ""),
                "selector": selector,
                "timestamp": time.time(),
            }
        except Exception as e:
            debug_logger.log_error("progressive_cloner", "clone_progressive", e)
            return {"error": str(e)}

    def expand_styles(
        self, element_id: str, categories: Optional[List[str]] = None, properties: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Return computed styles for a stored element, optionally filtered.

        Args:
            element_id (str): Handle returned by clone_element_progressive
            categories (Optional[List[str]]): Style categories (layout, typography, colors)
            properties (Optional[List[str]]): Exact CSS property names, takes priority over categories

        Returns:
            Dict[str, Any]: Filtered styles with counts
        """
        _, element_data = self._lookup(element_id)
        if element_data is None:
            return self._not_found(element_id)
        styles = element_data.get("styles") or {}
        if properties:
            filtered = {k: v for k, v in styles.items() if k in properties}
        elif categories:
            keys = {k for c in categories for k in STYLE_CATEGORIES.get(c, [])}
            filtered = {k: v for k, v in styles.items() if k in keys}
        else:
            filtered = styles
        return {
            "element_id": element_id,
            "data_type": "styles",
            "styles": filtered,
            "total_available": len(styles),
            "returned_count": len(filtered),
        }

    def expand_events(self, element_id: str, event_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Return event listeners for a stored element, optionally filtered.

        Args:
            element_id (str): Handle returned by clone_element_progressive
            event_types (Optional[List[str]]): Event types or sources to keep

        Returns:
            Dict[str, Any]: Event listeners with counts
        """
        _, element_data = self._lookup(element_id)
        if element_data is None:
            return self._not_found(element_id)
        all_events = element_data.get("eventListeners") or []
        events = all_events
        if event_types:
            events = [e for e in events if e.get("type") in event_types or e.get("source") in event_types]
        return {
            "element_id": element_id,
            "data_type": "events",
            "event_listeners": events,
            "total_available": len(all_events),
            "returned_count": len(events),
        }

    def expand_children(
        self, element_id: str, depth_range: Optional[Tuple[int, int]] = None, max_count: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Return descendant elements for a stored element, optionally filtered.

        Args:
            element_id (str): Handle returned by clone_element_progressive
            depth_range (Optional[Tuple[int, int]]): Inclusive (min, max) depth range
            max_count (Optional[int]): Maximum number of children to return

        Returns:
            Dict[str, Any]: Children with counts
        """
        data, _ = self._lookup(element_id)
        if data is None:
            return self._not_found(element_id)
        all_children = data.get("children") or []
        children = all_children
        if depth_range:
            min_d, max_d = depth_range
            children = [c for c in children if isinstance(c, dict) and min_d <= c.get("depth", 0) <= max_d]
        if isinstance(max_count, int) and max_count > 0:
            children = children[:max_count]
        return {
            "element_id": element_id,
            "data_type": "children",
            "children": children,
            "total_available": len(all_children),
            "returned_count": len(children),
        }

    def expand_css_rules(self, element_id: str, source_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Return matched CSS rules for a stored element, optionally filtered.

        Args:
            element_id (str): Handle returned by clone_element_progressive
            source_types (Optional[List[str]]): Substrings matched against each rule source

        Returns:
            Dict[str, Any]: CSS rules with counts
        """
        _, element_data = self._lookup(element_id)
        if element_data is None:
            return self._not_found(element_id)
        all_rules = element_data.get("cssRules") or []
        rules = all_rules
        if source_types:
            rules = [r for r in rules if any(s in r.get("source", "") for s in source_types)]
        return {
            "element_id": element_id,
            "data_type": "css_rules",
            "css_rules": rules,
            "total_available": len(all_rules),
            "returned_count": len(rules),
        }

    def expand_pseudo_elements(self, element_id: str) -> Dict[str, Any]:
        """
        Return pseudo-element styles for a stored element.

        Args:
            element_id (str): Handle returned by clone_element_progressive

        Returns:
            Dict[str, Any]: Pseudo-element data keyed by pseudo selector
        """
        _, element_data = self._lookup(element_id)
        if element_data is None:
            return self._not_found(element_id)
        pseudos = element_data.get("pseudoElements") or {}
        return {
            "element_id": element_id,
            "data_type": "pseudo_elements",
            "pseudo_elements": pseudos,
            "available_pseudos": list(pseudos.keys()),
        }

    def expand_animations(self, element_id: str) -> Dict[str, Any]:
        """
        Return animation and font data for a stored element.

        Args:
            element_id (str): Handle returned by clone_element_progressive

        Returns:
            Dict[str, Any]: Animation, transition, transform, and font data
        """
        _, element_data = self._lookup(element_id)
        if element_data is None:
            return self._not_found(element_id)
        return {
            "element_id": element_id,
            "data_type": "animations",
            "animations": element_data.get("animations") or {},
            "fonts": element_data.get("fonts") or {},
        }

    def list_stored_elements(self) -> Dict[str, Any]:
        """
        List stored element handles with short summaries.

        Returns:
            Dict[str, Any]: Stored element summaries and total count
        """
        items = []
        for element_id, meta in self._get_store().items():
            if not self._visible_to_caller(meta):
                continue
            fd = meta.get("full_data", {})
            element_data = fd.get("element") or {}
            items.append(
                {
                    "element_id": element_id,
                    "selector": meta.get("selector"),
                    "url": meta.get("url"),
                    "tagName": (element_data.get("html") or {}).get("tagName", "unknown"),
                    "children_count": len(fd.get("children") or []),
                    "styles_count": len(element_data.get("styles") or {}),
                    "timestamp": meta.get("timestamp"),
                }
            )
        return {"stored_elements": items, "total_count": len(items)}

    def clear_stored_element(self, element_id: str) -> Dict[str, Any]:
        """
        Remove one stored element.

        Args:
            element_id (str): Handle to remove

        Returns:
            Dict[str, Any]: Success message or not found error
        """
        store = self._get_store()
        entry = store.get(element_id)
        if entry is not None and self._visible_to_caller(entry):
            del store[element_id]
            self._save_store(store)
            return {"success": True, "message": f"Element {element_id} cleared"}
        return self._not_found(element_id)

    def clear_all_elements(self) -> Dict[str, Any]:
        """
        Remove stored elements owned by the current session.

        Returns:
            Dict[str, Any]: Success message
        """
        store = self._get_store()
        sid = get_current_mcp_session_id()
        if not sid:
            self._save_store({})
            return {"success": True, "message": "All stored elements cleared"}
        kept = {
            element_id: meta
            for element_id, meta in store.items()
            if meta.get("owner_session_id") != sid
        }
        removed = len(store) - len(kept)
        self._save_store(kept)
        return {"success": True, "message": f"{removed} stored elements cleared"}


progressive_element_cloner = ProgressiveElementCloner()
