"""Element-kind probe wiring and pure classifier for the input path.

The JavaScript probe (``js/element_kind_probe.js``) gathers facts about a DOM
element: its tag, input type, ``contenteditable`` state, ARIA role, editable
flags, iframe nesting, and framework/editor class hints. This module loads that
probe as package data, evaluates it against an element, and exposes a pure
``classify()`` function that maps the facts onto a concrete element kind used
downstream by ``fill``.

``classify()`` never touches the browser, so it is safe to unit test with
plain dictionaries.
"""

import re
from importlib import resources
from typing import Any, Dict, List, Optional, Tuple

from .js_values import evaluate_to_python

JS_DIR = resources.files(__package__) / "js"
PROBE_JS = "element_kind_probe.js"
PLACEHOLDER_PATTERN = re.compile(r"\$([A-Z_]+)\$")


class ElementKind:
    """Element kinds the classifier can return."""

    TEXT = "text"
    PASSWORD = "password"
    EMAIL = "email"
    NUMBER = "number"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    DATE = "date"
    TIME = "time"
    DATETIME = "datetime"
    MONTH = "month"
    WEEK = "week"
    COLOR = "color"
    RANGE = "range"
    FILE = "file"
    CONTENTEDITABLE = "contenteditable"
    RICH_EDITOR = "rich_editor"
    COMBOBOX = "combobox"
    BUTTON = "button"
    UNKNOWN = "unknown"

    #: Every kind the classifier may return, plus what ``type=""`` falls back to.
    KINDS: List[str] = [
        TEXT,
        PASSWORD,
        EMAIL,
        NUMBER,
        TEXTAREA,
        SELECT,
        CHECKBOX,
        RADIO,
        DATE,
        TIME,
        DATETIME,
        MONTH,
        WEEK,
        COLOR,
        RANGE,
        FILE,
        CONTENTEDITABLE,
        RICH_EDITOR,
        COMBOBOX,
        BUTTON,
        UNKNOWN,
    ]


# Input type strings that render as a button rather than a field.
_BUTTON_TYPES = {"button", "submit", "reset", "image"}


def _as_facts(facts: Any) -> Dict[str, Any]:
    """Return ``facts`` as a dict, substituting an empty dict when invalid.

    Args:
        facts (Any): Caller-supplied facts mapping.

    Returns:
        Dict[str, Any]: The mapping, or an empty dict when ``facts`` is not a dict.
    """
    return facts if isinstance(facts, dict) else {}


def classify(facts: Any) -> str:
    """Classify one element's facts into a single kind string.

    The probe reports raw signals; this function turns them into the stable kind
    used by the input layer. Rich editors and content-editable content win over
    ``combobox`` (many WYSIWYG editors set ``role=combobox``); ``select`` beats a
    framework combobox hint; and ``type=""`` on an ``<input>`` falls back to text.

    Args:
        facts (Any): A mapping of probe facts. Non-dict input is treated as empty.

    Returns:
        str: One kind from :class:`ElementKind`.
    """
    f = _as_facts(facts)
    tag = str(f.get("tag_name") or "").strip().lower()
    hints = f.get("hints") or []
    hint_names = " ".join(hints).lower()

    is_content_editable = bool(f.get("is_content_editable") or f.get("is_contenteditable_element"))
    # ``is_content_editable`` is the authoritative DOM flag. When the probe does
    # not report it as true, fall back to the raw ``contenteditable`` attribute:
    # present-but-empty (or "true", "plaintext-only") means editing is on, "false"
    # means off, and absent means no state. The blocklist keeps inline elements
    # like ``<span>`` from being guessed editable.
    if not is_content_editable:
        ce_value = str(f.get("contenteditable") or "").strip().lower()
        if ce_value in ("", "true", "plaintext-only") and tag in ("div", "td", "li", "p"):
            is_content_editable = True

    # A content-editable custom combobox (e.g. react-select) reports both
    # editing and the framework hint; prefer the combobox kind.
    if is_content_editable and "react-select" in hint_names:
        return ElementKind.COMBOBOX

    if is_content_editable:
        for editor in ("prosemirror", "ql-editor", "codemirror", "monaco", "trix", "ckeditor"):
            if editor in hint_names:
                return ElementKind.RICH_EDITOR
        return ElementKind.CONTENTEDITABLE

    role = str(f.get("aria_role") or "").strip().lower()
    if role == "combobox" and tag != "select":
        return ElementKind.COMBOBOX

    if tag == "select":
        if "react-select" in hint_names:
            return ElementKind.COMBOBOX
        return ElementKind.SELECT

    if tag == "textarea":
        return ElementKind.TEXTAREA

    input_type = str(f.get("input_type") or "").strip().lower()
    attr_type = str(f.get("type") or "").strip().lower()
    kind = input_type or attr_type

    if kind in _BUTTON_TYPES:
        return ElementKind.BUTTON

    known = {
        "text": ElementKind.TEXT,
        "password": ElementKind.PASSWORD,
        "email": ElementKind.EMAIL,
        "number": ElementKind.NUMBER,
        "date": ElementKind.DATE,
        "time": ElementKind.TIME,
        "datetime": ElementKind.DATETIME,
        "datetime-local": ElementKind.DATETIME,
        "month": ElementKind.MONTH,
        "week": ElementKind.WEEK,
        "color": ElementKind.COLOR,
        "range": ElementKind.RANGE,
        "file": ElementKind.FILE,
        "checkbox": ElementKind.CHECKBOX,
        "radio": ElementKind.RADIO,
    }
    if kind in known:
        return known[kind]

    if kind == "":
        if tag == "input":
            return ElementKind.TEXT
        return ElementKind.UNKNOWN

    return ElementKind.UNKNOWN


def render_probe() -> str:
    """Load and return the element-kind probe JavaScript source.

    Returns:
        str: The probe file contents as read from package data.

    Raises:
        FileNotFoundError: When the probe file is missing from the package.
    """
    probe_path = JS_DIR / PROBE_JS
    if not probe_path.is_file():
        raise FileNotFoundError(f"Element-kind probe not found: {probe_path}")
    return probe_path.read_text(encoding="utf-8")


async def probe_facts(tab: Any, element: Any) -> Optional[Dict[str, Any]]:
    """Evaluate the probe against one element and return its facts.

    Args:
        tab (Any): A nodriver tab able to evaluate JavaScript.
        element (Any): A resolved element object or nodriver element handle.

    Returns:
        Optional[Dict[str, Any]]: Fact mapping on success, or ``None`` when the
        probe reported an error or threw.
    """
    probe_js = render_probe()
    literals = {name.upper(): _json_for(value) for name, value in {"element": element}.items()}

    def substitute(match: "re.Match") -> str:
        name = match.group(1)
        if name not in literals:
            raise KeyError(f"Missing value for placeholder ${name}$ in {PROBE_JS}")
        return literals[name]

    code = PLACEHOLDER_PATTERN.sub(substitute, probe_js)
    data, error = await evaluate_to_python(tab, code)
    if error or not isinstance(data, dict):
        return None
    if "error" in data:
        return None
    return data


def _json_for(value: Any) -> Any:
    """Encode a value as a JSON string for safe injection into the probe.

    Args:
        value (Any): Value to inject as a literal.

    Returns:
        Any: The JSON encoding (already a string stays untouched).
    """
    import json

    return json.dumps(value) if not isinstance(value, str) else value
