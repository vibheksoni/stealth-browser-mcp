"""Seed identity defaults merged with spawn args.

A seed may carry default browser-identity values (user agent, language,
timezone, window size, platform, proxy). When a chat spawns a browser the
caller can override any of them. This module decides the final value: a
caller-supplied non-``None`` argument always wins, otherwise the seed default
is used.

The merge is a pure function: it never mutates its inputs and ignores any
seed key that is not a recognized identity field.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

# The identity fields a seed may default. Only these are carried into the
# merged result; any other key in the seed is ignored.
KNOWN_KEYS = ("user_agent", "lang", "timezone", "window_size", "platform", "proxy")

IDENTITY_FILE = "identity.json"


def merge_identity_defaults(seed_identity: Dict[str, Any], explicit: Dict[str, Any]) -> Dict[str, Any]:
    """Merge spawn defaults with explicit spawn arguments.

    ``explicit`` is what the caller passed at spawn time. A non-``None``
    explicit value always wins. When an explicit value is ``None`` or the key is
    absent, the seed default is used instead. Only the recognized identity
    fields are kept, and neither input is mutated.

    Args:
        seed_identity: Default identity values keyed by the fields in
            :data:`KNOWN_KEYS`.
        explicit: Values the caller supplied at spawn time.

    Returns:
        A new dict with every field in :data:`KNOWN_KEYS`, filled from
        ``explicit`` when present or from ``seed_identity`` otherwise.
    """
    seed = seed_identity or {}
    explicit = explicit or {}
    merged: Dict[str, Any] = {}
    for key in KNOWN_KEYS:
        value = explicit.get(key)
        if value is None:
            value = seed.get(key)
        merged[key] = value
    return merged


def load_identity(seed_dir: Union[Path, str]) -> Dict[str, Any]:
    """Return identity defaults from ``seed_dir/identity.json``.

    Returns an empty dict when the file is missing or does not contain valid
    JSON.
    """
    path = Path(seed_dir) / IDENTITY_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
