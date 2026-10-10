"""Benchmark target registry."""

from typing import Dict, List, Optional

from .base import Target
from .cloudflare import CloudflareTarget
from .creepjs import CreepJSTarget
from .input_fidelity import InputFidelityTarget
from .launch_flags import LaunchFlagsTarget
from .local_probe import InitScriptTarget, LocalProbeTarget
from .result_tables import IntoliTarget, SannysoftTarget
from .tls_fingerprint import TLSFingerprintTarget

ALL_TARGETS: List[Target] = [
    LaunchFlagsTarget(),
    LocalProbeTarget(),
    InitScriptTarget(),
    InputFidelityTarget(),
    SannysoftTarget(),
    IntoliTarget(),
    CreepJSTarget(),
    CloudflareTarget(),
    TLSFingerprintTarget(),
]
TARGETS_BY_NAME: Dict[str, Target] = {target.name: target for target in ALL_TARGETS}


def select_targets(names: Optional[List[str]] = None) -> List[Target]:
    """
    Resolve target names to target objects.

    Args:
        names (Optional[List[str]]): Target names, or None for every target

    Returns:
        List[Target]: Targets in registry order

    Raises:
        ValueError: When a name is unknown
    """
    if not names:
        return list(ALL_TARGETS)
    unknown = [name for name in names if name not in TARGETS_BY_NAME]
    if unknown:
        raise ValueError(f"Unknown targets: {', '.join(unknown)}. Known: {', '.join(TARGETS_BY_NAME)}")
    wanted = set(names)
    return [target for target in ALL_TARGETS if target.name in wanted]
