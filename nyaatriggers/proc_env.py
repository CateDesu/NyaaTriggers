"""Environment sanitising for child processes spawned by the frozen app.

PyInstaller's onedir bootloader prepends the bundle directory, ``_internal``
aka ``sys._MEIPASS``, to ``LD_LIBRARY_PATH`` so the app resolves its own
bundled shared objects, and stashes the caller's original value in
``LD_LIBRARY_PATH_ORIG``. That injected path is inherited by every process we
spawn, so a child that dlopen's a system library sharing a soname with one we
ship loads OUR possibly-stale copy and dies with an undefined-symbol error.
``/bin/sh`` pulling in ``libreadline.so.8`` is the usual victim. This is what
breaks the trigger-engine sidecars. ``java`` and Mono, plus any ``/bin/sh``
they invoke, pick up our bundled ``libreadline`` and the shell aborts before
the engine can start.

Hand children the pre-bundle environment so they resolve system libraries.
Only frozen builds carry the injection, so from source this does nothing.
"""
import os
import sys


def child_env() -> dict:
    """A copy of ``os.environ`` with PyInstaller's library-path injection undone."""
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return env
    for var in ("LD_LIBRARY_PATH",):
        orig = env.get(var + "_ORIG")
        if orig is not None:
            env[var] = orig          # restore what the user had before launch
        else:
            env.pop(var, None)       # PyInstaller added it from nothing, drop it
    return env
