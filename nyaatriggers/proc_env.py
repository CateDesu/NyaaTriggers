"""Remove PyInstaller library paths so Java and Mono use compatible system libraries."""
import os
import sys


def child_env() -> dict:
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
