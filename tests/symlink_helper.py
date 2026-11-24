"""Helper for creating symlinks with scoped Windows privilege error 1314 handling."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Union


def create_symlink(
    link: Path,
    target: Union[Path, str],
    target_is_directory: bool = False,
) -> None:
    """Create a symlink at `link` pointing to `target`.

    If the OS raises WinError 1314 (privilege not held), skip the current test
    with an explanatory message. Normal creation succeeds, and other errors
    are re-raised.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as err:
        if getattr(err, "winerror", None) == 1314 or getattr(err, "errno", None) == 1314:
            raise unittest.SkipTest("Symlink privilege not held (WinError 1314)") from err
        raise
