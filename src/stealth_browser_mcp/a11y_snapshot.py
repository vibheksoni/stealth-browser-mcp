"""Accessibility snapshot extraction and ref= targeting."""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import nodriver as uc
from nodriver import Tab, cdp

from .cdp_raw import raw_command

INTERACTIVE_ROLES: Set[str] = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "checkbox",
    "radio",
    "combobox",
    "listbox",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "switch",
    "slider",
    "spinbutton",
    "treeitem",
    "option",
    "scrollbar",
    "colorwell",
    "date",
    "time",
    "input",
    "textarea",
    "select",
}

NON_INTERACTIVE_ROLES: Set[str] = {
    "rootwebarea",
    "webarea",
    "root",
    "none",
    "presentation",
    "inlinetextbox",
}

DOM_WALK_JS = """function (frameRef) {
  let counter = 0;

  function getAccessibleName(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();

    const ariaLabelledby = el.getAttribute('aria-labelledby');
    if (ariaLabelledby) {
      const parts = ariaLabelledby.split(/\\s+/).map(id => {
        const target = document.getElementById(id);
        return target ? (target.innerText || target.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }

    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      if (el.labels && el.labels.length > 0) {
        const text = Array.from(el.labels).map(l => (l.innerText || l.textContent || '').trim()).filter(Boolean).join(' ');
        if (text) return text;
      }
      if (el.id) {
        try {
          const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
          if (label) {
            const text = (label.innerText || label.textContent || '').trim();
            if (text) return text;
          }
        } catch (e) {}
      }
      if (el.placeholder) return el.placeholder;
      if (el.getAttribute('title')) return el.getAttribute('title').trim();
    }

    if (el.tagName === 'IMG' && el.getAttribute('alt')) {
      return el.getAttribute('alt').trim();
    }

    if (el.getAttribute('title')) return el.getAttribute('title').trim();

    return '';
  }

  function getRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'button' || type === 'submit' || type === 'reset') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'number') return 'spinbutton';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'p' || tag === 'span' || tag === 'label') return 'text';
    if (tag === 'form') return 'form';
    return tag;
  }

  function isInteractive(el, role) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'select' || tag === 'textarea') return true;
    if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() !== 'hidden') return true;
    if (tag === 'a' && el.hasAttribute('href')) return true;
    const interRoles = ['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox', 'listbox', 'menuitem', 'tab', 'switch', 'slider', 'spinbutton', 'option'];
    if (interRoles.includes(role)) return true;
    if (el.hasAttribute('tabindex') && parseInt(el.getAttribute('tabindex'), 10) >= 0) return true;
    return false;
  }

  function walk(node) {
    if (!node || node.nodeType !== 1) return null;
    try {
      const style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return null;
    } catch (e) {}

    const role = getRole(node);
    const interactive = isInteractive(node, role);
    let name = getAccessibleName(node);
    if (!name && (role === 'button' || role === 'link' || role === 'heading' || role === 'text' || node.children.length === 0)) {
      name = (node.innerText || node.textContent || '').trim();
    }

    const value = (node.value !== undefined && node.tagName !== 'BUTTON') ? String(node.value) : '';
    let level = 0;
    if (/^h[1-6]$/.test(node.tagName.toLowerCase())) {
      level = parseInt(node.tagName.substring(1), 10);
    }

    let marker = null;
    if (interactive) {
      counter++;
      marker = 'a11y-m-' + counter;
      node.setAttribute('data-a11y-marker', marker);
    }

    const children = [];
    for (let i = 0; i < node.children.length; i++) {
      const childObj = walk(node.children[i]);
      if (childObj) children.push(childObj);
    }

    return {
      role: role,
      name: name,
      value: value,
      level: level,
      checked: Boolean(node.checked),
      disabled: Boolean(node.disabled),
      interactive: interactive,
      marker: marker,
      children: children
    };
  }

  let targetDoc = document;
  if (frameRef) {
    let iframe = null;
    if (frameRef.startsWith('//') || frameRef.toLowerCase().startsWith('xpath=')) {
      const fXpath = frameRef.toLowerCase().startsWith('xpath=') ? frameRef.substring(6).trim() : frameRef;
      try {
        const res = document.evaluate(fXpath, document, null, 9, null);
        if (res && res.singleNodeValue) iframe = res.singleNodeValue;
      } catch (e) {}
    }
    if (!iframe) {
      try { iframe = document.querySelector(frameRef); } catch (e) {}
    }
    if (!iframe) {
      try { iframe = document.querySelector('iframe[name="' + frameRef + '"], iframe#' + frameRef + ', #' + frameRef); } catch (e) {}
    }
    if (!iframe) {
      try {
        const frames = document.querySelectorAll('iframe, frame');
        for (let i = 0; i < frames.length; i++) {
          const f = frames[i];
          if (f.name === frameRef || f.id === frameRef || (f.src && f.src.includes(frameRef))) {
            iframe = f;
            break;
          }
        }
      } catch (e) {}
    }
    if (!iframe) {
      try {
        let frameWin = window.frames[frameRef];
        if (!frameWin && /^\\d+$/.test(frameRef)) {
          frameWin = window.frames[parseInt(frameRef, 10)];
        }
        if (frameWin && frameWin.document) {
          targetDoc = frameWin.document;
        }
      } catch (e) {}
    } else {
      try {
        targetDoc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document) || document;
      } catch (e) {}
    }
  }

  const rootObj = walk(targetDoc.body || targetDoc.documentElement);
  return {
    root: rootObj,
    title: targetDoc.title || document.title || '',
    interactiveCount: counter
  };
}"""


def is_ref_selector(selector: str, known_refs: Optional[Set[str]] = None) -> bool:
    """Return True if selector is a ref targeting selector (e.g. ref=e12, ref="e1", @e1)."""
    s = selector.strip()
    lower = s.lower()
    if lower.startswith("ref=") or lower.startswith("ref:"):
        return True
    if s.startswith("@"):
        return True
    if known_refs and s in known_refs:
        return True
    return False


def extract_ref_id(selector: str) -> str:
    """Extract ref ID from a ref targeting selector string."""
    s = selector.strip()
    lower = s.lower()
    if lower.startswith("ref=") or lower.startswith("ref:"):
        return s[4:].strip().strip("'\"")
    if s.startswith("@"):
        return s[1:].strip()
    return s.strip("'\"")


def _extract_prop_name(name_obj: Any) -> str:
    """Extract property name string from an AXProperty name field."""
    while True:
        if hasattr(name_obj, "value"):
            name_obj = name_obj.value
        elif isinstance(name_obj, dict) and "value" in name_obj:
            name_obj = name_obj.get("value")
        else:
            break
    return str(name_obj or "")


def _extract_val(v: Any) -> Any:
    """Unpack nested AXValue or dict value to raw primitive."""
    while True:
        if hasattr(v, "value"):
            v = v.value
        elif isinstance(v, dict) and "value" in v:
            v = v.get("value")
        else:
            break
    return v


def is_interactive_node(node: Any) -> bool:
    """Determine whether an accessibility node is interactive."""
    if not node:
        return False

    if isinstance(node, dict):
        role_val = node.get("role")
        raw_props = node.get("properties")
    else:
        role_val = getattr(node, "role", None)
        raw_props = getattr(node, "properties", None)

    role_str = ""
    if role_val is not None:
        role_raw = _extract_val(role_val)
        role_str = "" if role_raw is None else str(role_raw)
    role = role_str.strip().lower()

    if role in NON_INTERACTIVE_ROLES:
        return False
    if role in INTERACTIVE_ROLES:
        return True

    props_dict: Dict[str, Any] = {}
    if isinstance(raw_props, dict):
        props_dict = {str(k).lower(): _extract_val(v) for k, v in raw_props.items()}
    elif isinstance(raw_props, list):
        for p in raw_props:
            if isinstance(p, dict):
                p_name = p.get("name")
                p_val = p.get("value")
            else:
                p_name = getattr(p, "name", None)
                p_val = getattr(p, "value", None)
            name_key = _extract_prop_name(p_name).lower()
            val_out = _extract_val(p_val)
            if name_key:
                props_dict[name_key] = val_out

    def _is_true(v: Any) -> bool:
        if v is True:
            return True
        if isinstance(v, str) and v.lower() == "true":
            return True
        return False

    if _is_true(props_dict.get("focusable")):
        return True
    if _is_true(props_dict.get("editable")):
        return True
    if _is_true(props_dict.get("settable")):
        return True
    return False


def _extract_ax_node_data(raw_node: Any) -> Dict[str, Any]:
    """Normalize raw AXNode dataclass, SimpleNamespace, or dict into a standard dictionary."""
    if isinstance(raw_node, dict):
        node_id = str(raw_node.get("nodeId", ""))
        ignored = bool(raw_node.get("ignored", False))
        role_val = raw_node.get("role")
        name_val = raw_node.get("name")
        val_val = raw_node.get("value")
        desc_val = raw_node.get("description")
        parent_id = raw_node.get("parentId")
        child_ids = raw_node.get("childIds") or []
        backend_id = raw_node.get("backendDOMNodeId") or raw_node.get("backend_dom_node_id") or raw_node.get("backendNodeId")
        raw_props = raw_node.get("properties") or []
    else:
        node_id = str(getattr(raw_node, "node_id", ""))
        ignored = bool(getattr(raw_node, "ignored", False))
        role_val = getattr(raw_node, "role", None)
        name_val = getattr(raw_node, "name", None)
        val_val = getattr(raw_node, "value", None)
        desc_val = getattr(raw_node, "description", None)
        parent_id = getattr(raw_node, "parent_id", None)
        child_ids = getattr(raw_node, "child_ids", None) or []
        backend_id = (
            getattr(raw_node, "backend_dom_node_id", None)
            or getattr(raw_node, "backendDOMNodeId", None)
            or getattr(raw_node, "backend_node_id", None)
        )
        raw_props = getattr(raw_node, "properties", None) or []

    def _extract_text(obj: Any) -> str:
        if obj is None:
            return ""
        v = _extract_val(obj)
        return str(v) if v is not None else ""

    role_str = _extract_text(role_val)
    name_str = _extract_text(name_val)
    value_str = _extract_text(val_val)
    desc_str = _extract_text(desc_val)

    properties: Dict[str, Any] = {}
    if isinstance(raw_props, dict):
        properties = {str(k): _extract_val(v) for k, v in raw_props.items()}
    elif isinstance(raw_props, list):
        for p in raw_props:
            if isinstance(p, dict):
                p_name = p.get("name")
                p_val = p.get("value")
            else:
                p_name = getattr(p, "name", None)
                p_val = getattr(p, "value", None)

            name_key = _extract_prop_name(p_name)
            val_out = _extract_val(p_val)
            if name_key:
                properties[name_key] = val_out

    b_id = None
    if backend_id is not None:
        try:
            b_id = int(backend_id)
        except (ValueError, TypeError):
            if hasattr(backend_id, "value"):
                try:
                    b_id = int(backend_id.value)
                except (ValueError, TypeError):
                    b_id = None

    return {
        "node_id": str(node_id),
        "ignored": ignored,
        "role": role_str,
        "name": name_str,
        "value": value_str,
        "description": desc_str,
        "parent_id": str(parent_id) if parent_id is not None else None,
        "child_ids": [str(c) for c in child_ids],
        "backend_node_id": b_id,
        "properties": properties,
    }


def build_a11y_tree(
    raw_nodes: List[Any],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int], Dict[str, Dict[str, Any]]]:
    """
    Construct a clean hierarchical tree from flat CDP AXNodes and assign short refs.

    Returns:
        Tuple: (root_node_dict, refs_dict, ref_details_dict)
    """
    if not raw_nodes:
        return None, {}, {}

    nodes_list = [_extract_ax_node_data(n) for n in raw_nodes]
    nodes_by_id = {n["node_id"]: n for n in nodes_list}

    children_map: Dict[str, List[str]] = {n["node_id"]: [] for n in nodes_list}
    for n in nodes_list:
        nid = n["node_id"]
        for cid in n.get("child_ids", []):
            if cid in nodes_by_id and cid not in children_map[nid]:
                children_map[nid].append(cid)

    for n in nodes_list:
        pid = n.get("parent_id")
        nid = n["node_id"]
        if pid and pid in children_map:
            if nid not in children_map[pid]:
                children_map[pid].append(nid)

    root_id = None
    for n in nodes_list:
        if n["role"] in ("RootWebArea", "WebArea"):
            root_id = n["node_id"]
            break
    if not root_id:
        for n in nodes_list:
            if not n["parent_id"] or n["parent_id"] not in nodes_by_id:
                root_id = n["node_id"]
                break
    if not root_id:
        root_id = nodes_list[0]["node_id"]

    def get_effective_children(nid: str, visited: Set[str]) -> List[str]:
        if nid in visited:
            return []
        visited.add(nid)
        effective = []
        for cid in children_map.get(nid, []):
            child = nodes_by_id.get(cid)
            if not child:
                continue
            c_role = child.get("role") or ""
            c_name = child.get("name") or ""
            is_interact = is_interactive_node(child)
            is_ignored = child.get("ignored", False)
            if is_ignored or (c_role in ("generic", "none", "presentation", "InlineTextBox") and not c_name and not is_interact):
                effective.extend(get_effective_children(cid, visited.copy()))
            else:
                effective.append(cid)
        return effective

    ref_counter = 1
    refs: Dict[str, int] = {}
    ref_details: Dict[str, Dict[str, Any]] = {}
    tree_visited: Set[str] = set()

    def build_node(nid: str) -> Optional[Dict[str, Any]]:
        nonlocal ref_counter
        if nid in tree_visited:
            return None
        tree_visited.add(nid)

        node = nodes_by_id.get(nid)
        if not node:
            return None

        role = node.get("role") or ""
        name = node.get("name") or ""
        value = node.get("value") or ""
        backend_id = node.get("backend_node_id")
        properties = node.get("properties") or {}

        ref = None
        if is_interactive_node(node) and backend_id is not None:
            ref = f"e{ref_counter}"
            ref_counter += 1
            refs[ref] = backend_id
            ref_details[ref] = {
                "role": role,
                "name": name,
                "value": value,
                "backend_node_id": backend_id,
            }

        effective_cids = get_effective_children(nid, set())
        children = []
        has_interactive = bool(ref)

        for cid in effective_cids:
            c_dict = build_node(cid)
            if c_dict:
                children.append(c_dict)
                if c_dict.get("_has_interactive"):
                    has_interactive = True

        return {
            "role": role,
            "name": name,
            "value": value,
            "ref": ref,
            "properties": properties,
            "backend_node_id": backend_id,
            "children": children,
            "_has_interactive": has_interactive,
        }

    root_tree = build_node(root_id)
    return root_tree, refs, ref_details


def format_text_tree(
    root: Optional[Dict[str, Any]],
    max_nodes: int = 500,
    max_chars: int = 50000,
    interactive_only: bool = False,
) -> Tuple[str, int, bool]:
    """
    Format hierarchical tree into a compact indented text representation with size caps.

    Returns:
        Tuple: (text_representation, node_count, is_truncated)
    """
    if not root:
        return "", 0, False

    lines: List[str] = []
    node_count = 0
    truncated = False

    def traverse(node: Dict[str, Any], depth: int = 0) -> None:
        nonlocal node_count, truncated
        if node_count >= max_nodes:
            truncated = True
            return

        ref = node.get("ref")
        is_interact = bool(ref)
        if interactive_only and not is_interact and not node.get("_has_interactive"):
            return

        role = node.get("role") or "element"
        name = node.get("name") or ""
        value = node.get("value") or ""
        props = node.get("properties") or {}

        parts = [role]
        if name:
            clean_name = " ".join(name.split())
            if len(clean_name) > 80:
                clean_name = clean_name[:77] + "..."
            parts.append(f'"{clean_name}"')

        if value and role != "button":
            clean_val = " ".join(value.split())
            if len(clean_val) > 40:
                clean_val = clean_val[:37] + "..."
            parts.append(f'value="{clean_val}"')

        level = props.get("level") or node.get("level")
        if level:
            parts.append(f"[level={level}]")

        if props.get("checked") is True or props.get("checked") == "true":
            parts.append("[checked]")

        if props.get("disabled") is True or props.get("disabled") == "true":
            parts.append("[disabled]")

        if props.get("expanded") is True or props.get("expanded") == "true":
            parts.append("[expanded]")
        elif props.get("expanded") is False or props.get("expanded") == "false":
            parts.append("[collapsed]")

        if ref:
            parts.append(f"(ref={ref})")

        indent = "  " * depth
        lines.append(f"{indent}{' '.join(parts)}")
        node_count += 1

        for child in node.get("children", []):
            if node_count >= max_nodes:
                truncated = True
                break
            traverse(child, depth + 1)

    traverse(root, depth=0)
    text = "\n".join(lines)

    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
        truncated = True

    return text, node_count, truncated


class AccessibilitySnapshotManager:
    """Stores accessibility snapshots and resolves ref= targets per browser instance."""

    def __init__(self):
        self._snapshots_by_instance: Dict[str, Dict[str, Any]] = {}
        self._snapshots_by_tab: Dict[int, Dict[str, Any]] = {}

    def store_snapshot(
        self,
        instance_id: Optional[str],
        tab: Any,
        refs: Dict[str, int],
        tree_text: str,
        tree_json: Optional[Dict[str, Any]],
        ref_details: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Record latest snapshot and ref mappings for an instance and tab."""
        record = {
            "refs": dict(refs),
            "ref_details": dict(ref_details or {}),
            "text": tree_text,
            "json": tree_json,
            "timestamp": time.time(),
        }
        if instance_id:
            self._snapshots_by_instance[instance_id] = record
        if tab is not None:
            self._snapshots_by_tab[id(tab)] = record

    def get_snapshot(
        self,
        instance_id: Optional[str] = None,
        tab: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve stored snapshot record."""
        if instance_id and instance_id in self._snapshots_by_instance:
            return self._snapshots_by_instance[instance_id]
        if tab is not None and id(tab) in self._snapshots_by_tab:
            return self._snapshots_by_tab[id(tab)]
        return None

    def get_known_refs(
        self,
        instance_id: Optional[str] = None,
        tab: Optional[Any] = None,
    ) -> Set[str]:
        """Return the set of known ref IDs for this instance or tab."""
        record = self.get_snapshot(instance_id=instance_id, tab=tab)
        if not record:
            return set()
        return set(record["refs"].keys())

    def resolve_ref(
        self,
        ref_id: str,
        instance_id: Optional[str] = None,
        tab: Optional[Any] = None,
    ) -> int:
        """Resolve a ref ID to its backend DOM node ID, raising when stale or missing."""
        record = self.get_snapshot(instance_id=instance_id, tab=tab)
        if not record:
            inst_desc = f" for instance '{instance_id}'" if instance_id else ""
            raise Exception(f"Stale or invalid ref '{ref_id}': no accessibility snapshot has been taken{inst_desc}")

        clean_ref = ref_id.strip()
        if clean_ref not in record["refs"]:
            inst_desc = f" for instance '{instance_id}'" if instance_id else ""
            raise Exception(
                f"Stale or invalid ref '{ref_id}': ref '{clean_ref}' not found in latest accessibility snapshot{inst_desc}"
            )

        return record["refs"][clean_ref]

    def forget_instance(self, instance_id: str) -> None:
        """Clear stored snapshot data for an instance."""
        self._snapshots_by_instance.pop(instance_id, None)

    def forget_tab(self, tab: Any) -> None:
        """Clear stored snapshot data for a tab."""
        self._snapshots_by_tab.pop(id(tab), None)

    def clear_all(self) -> None:
        """Clear all stored snapshots."""
        self._snapshots_by_instance.clear()
        self._snapshots_by_tab.clear()


a11y_manager = AccessibilitySnapshotManager()


async def _dom_walk_fallback(
    tab: Tab,
    instance_id: Optional[str] = None,
    max_nodes: int = 500,
    max_chars: int = 50000,
    interactive_only: bool = False,
    frame: Optional[str] = None,
) -> Dict[str, Any]:
    """Fallback snapshot mechanism using a DOM-walk JS evaluation."""
    frame_arg = json.dumps(frame) if frame else "null"
    eval_res = await asyncio.wait_for(
        tab.send(cdp.runtime.evaluate(
            expression=f"({DOM_WALK_JS})({frame_arg})",
            return_by_value=True,
            await_promise=True,
        )),
        timeout=5.0,
    )
    remote_obj = eval_res[0] if isinstance(eval_res, tuple) else eval_res
    val = getattr(remote_obj, "value", None) or {}
    root_raw = val.get("root") if isinstance(val, dict) else None

    if not root_raw:
        return {
            "tree": "",
            "text": "",
            "nodes": None,
            "json": None,
            "node_count": 0,
            "truncated": False,
            "refs": {},
        }

    refs: Dict[str, int] = {}
    ref_details: Dict[str, Dict[str, Any]] = {}
    ref_counter = 1

    interactive_nodes: List[Dict[str, Any]] = []

    def collect_interactive(node: Dict[str, Any]) -> None:
        if node.get("interactive") and node.get("marker"):
            interactive_nodes.append(node)
        for child in node.get("children", []):
            collect_interactive(child)

    collect_interactive(root_raw)

    try:
        doc = await asyncio.wait_for(
            tab.send(cdp.dom.get_document(depth=0)),
            timeout=3.0,
        )
        doc_node_id = getattr(doc, "node_id", None)
        if doc_node_id is not None:
            for item in interactive_nodes:
                marker = item.get("marker")
                try:
                    nid = await asyncio.wait_for(
                        tab.send(cdp.dom.query_selector(doc_node_id, f'[data-a11y-marker="{marker}"]')),
                        timeout=1.0,
                    )
                    if nid:
                        d = await asyncio.wait_for(
                            tab.send(cdp.dom.describe_node(node_id=nid)),
                            timeout=1.0,
                        )
                        b_id = getattr(d, "backend_node_id", None)
                        if b_id is not None:
                            item["backend_node_id"] = int(b_id)
                        elif isinstance(d, dict) and "backendNodeId" in d.get("node", {}):
                            item["backend_node_id"] = int(d["node"]["backendNodeId"])
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            cleanup_expr = (
                f"(function(f) {{ "
                f"let d = document; "
                f"if (f) {{ "
                f"  try {{ "
                f"    let el = document.querySelector(f) || document.querySelector('iframe[name=\"' + f + '\"], iframe#' + f); "
                f"    if (el && (el.contentDocument || el.contentWindow)) d = el.contentDocument || el.contentWindow.document; "
                f"  }} catch(e) {{}} "
                f"}} "
                f"d.querySelectorAll('[data-a11y-marker]').forEach(e => e.removeAttribute('data-a11y-marker')); "
                f"}})({frame_arg})"
            )
            await asyncio.wait_for(
                tab.send(cdp.runtime.evaluate(
                    expression=cleanup_expr,
                    return_by_value=True,
                )),
                timeout=2.0,
            )
        except Exception:
            pass

    def convert_walk_node(node: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal ref_counter
        ref = None
        backend_id = node.get("backend_node_id")
        if node.get("interactive") and backend_id is not None:
            ref = f"e{ref_counter}"
            ref_counter += 1
            refs[ref] = backend_id
            ref_details[ref] = {
                "role": node.get("role", ""),
                "name": node.get("name", ""),
                "value": node.get("value", ""),
                "backend_node_id": backend_id,
            }

        c_list = []
        has_interactive = bool(ref)
        for c in node.get("children", []):
            converted_child = convert_walk_node(c)
            c_list.append(converted_child)
            if converted_child.get("_has_interactive"):
                has_interactive = True

        props = {}
        if node.get("level"):
            props["level"] = node["level"]
        if node.get("checked"):
            props["checked"] = True
        if node.get("disabled"):
            props["disabled"] = True

        return {
            "role": node.get("role", "element"),
            "name": node.get("name", ""),
            "value": node.get("value", ""),
            "ref": ref,
            "properties": props,
            "backend_node_id": backend_id,
            "children": c_list,
            "_has_interactive": has_interactive,
        }

    root_tree = convert_walk_node(root_raw)
    text, node_count, truncated = format_text_tree(
        root_tree,
        max_nodes=max_nodes,
        max_chars=max_chars,
        interactive_only=interactive_only,
    )

    a11y_manager.store_snapshot(
        instance_id=instance_id,
        tab=tab,
        refs=refs,
        tree_text=text,
        tree_json=root_tree,
        ref_details=ref_details,
    )

    return {
        "tree": text,
        "text": text,
        "nodes": root_tree,
        "json": root_tree,
        "node_count": node_count,
        "truncated": truncated,
        "refs": ref_details,
    }


async def get_accessibility_snapshot(
    tab: Tab,
    instance_id: Optional[str] = None,
    max_nodes: int = 500,
    max_chars: int = 50000,
    interactive_only: bool = False,
    frame: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate an accessibility snapshot of the page using CDP getFullAXTree or DOM-walk fallback.

    Args:
        tab (Tab): Browser tab.
        instance_id (Optional[str]): Browser instance ID.
        max_nodes (int): Maximum nodes to include in snapshot before truncating. Defaults to 500.
        max_chars (int): Maximum character length of text tree before truncating. Defaults to 50000.
        interactive_only (bool): Include only interactive nodes and their ancestors. Defaults to False.
        frame (Optional[str]): Target frame (omitted for root document).

    Returns:
        Dict[str, Any]: Result dictionary containing tree, text, nodes, truncated, node_count, refs.
    """
    raw_nodes = None
    resolved_frame_id: Optional[str] = None
    target_tab = tab
    f_ctx = None

    if frame:
        try:
            from .input_actions import resolve_frame_context
            f_ctx = await resolve_frame_context(tab, frame)
            if f_ctx:
                if f_ctx.frame_id:
                    resolved_frame_id = str(f_ctx.frame_id)
                if f_ctx.target_tab and hasattr(f_ctx.target_tab, "send"):
                    target_tab = f_ctx.target_tab
        except Exception:
            pass

        if not resolved_frame_id:
            try:
                tree = await tab.send(cdp.page.get_frame_tree())
                from .input_actions import _find_frame_id_in_tree
                fid = _find_frame_id_in_tree(tree, frame)
                if fid:
                    resolved_frame_id = str(fid)
            except Exception:
                pass

        if not resolved_frame_id:
            resolved_frame_id = str(frame)

    send_tab = target_tab if hasattr(target_tab, "send") else tab
    ax_params = {"frameId": resolved_frame_id} if resolved_frame_id else None

    try:
        try:
            await asyncio.wait_for(
                send_tab.send(raw_command("Accessibility.enable")),
                timeout=2.0,
            )
        except Exception:
            pass

        res = None
        try:
            res = await asyncio.wait_for(
                send_tab.send(raw_command("Accessibility.getFullAXTree", ax_params)),
                timeout=3.0,
            )
        except Exception:
            if send_tab is not tab and hasattr(tab, "send"):
                res = await asyncio.wait_for(
                    tab.send(raw_command("Accessibility.getFullAXTree", ax_params)),
                    timeout=3.0,
                )
            else:
                raise

        if isinstance(res, dict) and "nodes" in res:
            raw_nodes = res["nodes"]
        elif isinstance(res, list):
            raw_nodes = res
    except Exception:
        raw_nodes = None

    if not raw_nodes:
        return await _dom_walk_fallback(
            tab=tab,
            instance_id=instance_id,
            max_nodes=max_nodes,
            max_chars=max_chars,
            interactive_only=interactive_only,
            frame=frame,
        )

    root_tree, refs, ref_details = build_a11y_tree(raw_nodes)
    text, node_count, truncated = format_text_tree(
        root_tree,
        max_nodes=max_nodes,
        max_chars=max_chars,
        interactive_only=interactive_only,
    )

    if target_tab is not tab and refs:
        try:
            from .input_actions import _track_node
            ctx_id = f_ctx.context_id if f_ctx else None
            for bid in refs.values():
                if isinstance(bid, int):
                    _track_node(bid, ctx_id, target_tab)
        except Exception:
            pass

    a11y_manager.store_snapshot(
        instance_id=instance_id,
        tab=tab,
        refs=refs,
        tree_text=text,
        tree_json=root_tree,
        ref_details=ref_details,
    )

    return {
        "tree": text,
        "text": text,
        "nodes": root_tree,
        "json": root_tree,
        "node_count": node_count,
        "truncated": truncated,
        "refs": ref_details,
    }


take_accessibility_snapshot = get_accessibility_snapshot


def cap_snapshot_response(
    result: Dict[str, Any],
    max_chars: int,
    include_json: bool = False,
) -> Dict[str, Any]:
    """Build a compact, size-capped response dictionary for accessibility snapshot.

    Args:
        result (Dict[str, Any]): Raw snapshot result from get_accessibility_snapshot.
        max_chars (int): Maximum characters for text tree.
        include_json (bool): When True, include full node tree under 'nodes'.

    Returns:
        Dict[str, Any]: Capped response dictionary with tree, node_count, truncated, refs,
            and optional nodes.
    """
    text = result.get("tree") or result.get("text") or ""
    response: Dict[str, Any] = {
        "tree": text,
        "node_count": result.get("node_count", 0),
        "truncated": bool(result.get("truncated", False)),
    }

    raw_refs = result.get("refs") or {}
    filtered_refs: Dict[str, Any] = {}
    if isinstance(raw_refs, dict):
        for ref_id, details in raw_refs.items():
            needle = "ref=" + str(ref_id) + ")"
            if needle in text:
                filtered_refs[ref_id] = details
    response["refs"] = filtered_refs

    if include_json:
        response["nodes"] = result.get("nodes") or result.get("json")

    threshold = max(3 * max_chars, 20000)
    while response["refs"] and len(json.dumps(response, default=str)) > threshold:
        response["refs"].popitem()
        response["truncated"] = True

    if include_json and "nodes" in response and len(json.dumps(response, default=str)) > threshold:
        response.pop("nodes", None)
        response["truncated"] = True

    return response
