"""Restore system library paths for child processes in frozen builds. PyInstaller prepends
its bundled libraries and saves the original path in LD_LIBRARY_PATH_ORIG. Inheriting
the bundle path can make Java, Mono or their shells load incompatible libraries.
"""
import os
import sys


def child_env() -> dict:
    """Copy the environment with PyInstaller library path changes removed."""
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return env
    for var in ("LD_LIBRARY_PATH",):
        orig = env.get(var + "_ORIG")
        if orig is not None:
            env[var] = orig
        else:
            env.pop(var, None)
    return env
