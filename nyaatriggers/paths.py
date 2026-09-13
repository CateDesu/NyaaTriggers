"""Locations shared by source runs and frozen builds."""

import sys
from pathlib import Path


def source_root() -> Path:
    """Return the directory containing the source entry points."""
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Return the directory containing the bundled resources."""
    return Path(getattr(sys, "_MEIPASS", source_root()))


def data_root() -> Path:
    """Keep user files beside the executable or source entry points."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return source_root()
