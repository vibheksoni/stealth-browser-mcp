"""Keyboard key tables and CDP key event field resolution."""

from typing import Any, Dict, List, Optional

MODIFIER_ALT = 1
MODIFIER_CONTROL = 2
MODIFIER_META = 4
MODIFIER_SHIFT = 8

MODIFIER_ALIASES: Dict[str, tuple] = {
    "alt": ("Alt", MODIFIER_ALT),
    "option": ("Alt", MODIFIER_ALT),
    "control": ("Control", MODIFIER_CONTROL),
    "ctrl": ("Control", MODIFIER_CONTROL),
    "meta": ("Meta", MODIFIER_META),
    "cmd": ("Meta", MODIFIER_META),
    "command": ("Meta", MODIFIER_META),
    "shift": ("Shift", MODIFIER_SHIFT),
}

NAMED_KEYS: Dict[str, tuple] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, None),
    "escape": ("Escape", "Escape", 27, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "space": (" ", "Space", 32, " "),
}

NAMED_KEYS.update({
    f"f{n}": (f"F{n}", f"F{n}", 111 + n, None) for n in range(1, 13)
})

PRINTABLE_KEY_CODES: Dict[str, tuple] = {
    " ": ("Space", 32),
    "-": ("Minus", 189), "_": ("Minus", 189),
    "=": ("Equal", 187), "+": ("Equal", 187),
    "[": ("BracketLeft", 219), "{": ("BracketLeft", 219),
    "]": ("BracketRight", 221), "}": ("BracketRight", 221),
    "\\": ("Backslash", 220), "|": ("Backslash", 220),
    ";": ("Semicolon", 186), ":": ("Semicolon", 186),
    "'": ("Quote", 222), '"': ("Quote", 222),
    ",": ("Comma", 188), "<": ("Comma", 188),
    ".": ("Period", 190), ">": ("Period", 190),
    "/": ("Slash", 191), "?": ("Slash", 191),
    "`": ("Backquote", 192), "~": ("Backquote", 192),
    "!": ("Digit1", 49), "@": ("Digit2", 50), "#": ("Digit3", 51),
    "$": ("Digit4", 52), "%": ("Digit5", 53), "^": ("Digit6", 54),
    "&": ("Digit7", 55), "*": ("Digit8", 56), "(": ("Digit9", 57),
    ")": ("Digit0", 48),
}


def supported_key_names() -> List[str]:
    """List the named keys accepted by press_key, for error messages."""
    named = [
        "Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight",
        "Backspace", "Delete", "Home", "End", "PageUp", "PageDown", "Space",
    ]
    return named + [f"F{n}" for n in range(1, 13)]


def resolve_key_descriptor(key: str) -> Dict[str, Any]:
    """Map a named key or supported ASCII printable character to CDP fields."""
    if not isinstance(key, str) or key == "":
        raise Exception(
            "Invalid key. Use a supported named key or one ASCII printable character."
        )

    if len(key) == 1 and key != " ":
        code, virtual_key_code = PRINTABLE_KEY_CODES.get(key, ("", 0))
        if not code and "a" <= key.lower() <= "z":
            code = f"Key{key.upper()}"
            virtual_key_code = ord(key.upper())
        elif not code and "0" <= key <= "9":
            code = f"Digit{key}"
            virtual_key_code = ord(key)
        if not code or not key.isascii() or not key.isprintable():
            raise Exception(
                f"Unsupported key {key!r}. Use a supported named key or one ASCII printable character."
            )
        return {"key": key, "code": code, "virtual_key_code": virtual_key_code, "text": key}

    lookup = "space" if key == " " else key.strip().lower()
    named = NAMED_KEYS.get(lookup)
    if named is None:
        raise Exception(
            f"Unsupported key {key!r}. Supported named keys: {', '.join(supported_key_names())}."
        )

    dom_key, code, virtual_key_code, text = named
    return {"key": dom_key, "code": code, "virtual_key_code": virtual_key_code, "text": text}


def resolve_modifiers(modifiers: Optional[List[str]]) -> tuple:
    """
    Combine modifier names into the CDP modifiers bitmask.

    Args:
        modifiers (Optional[List[str]]): Any of Shift, Control/Ctrl, Alt, Meta/Cmd.

    Returns:
        tuple: (bitmask int, canonical modifier names list).
    """
    if not modifiers:
        return 0, []

    if isinstance(modifiers, str):
        modifiers = [modifiers]

    mask = 0
    canonical: List[str] = []
    for modifier in modifiers:
        entry = MODIFIER_ALIASES.get(str(modifier).strip().lower())
        if entry is None:
            raise Exception(
                f"Unsupported modifier: {modifier!r}. "
                "Supported modifiers: Shift, Control (Ctrl), Alt, Meta (Cmd)."
            )
        name, bit = entry
        mask |= bit
        if name not in canonical:
            canonical.append(name)

    return mask, canonical
