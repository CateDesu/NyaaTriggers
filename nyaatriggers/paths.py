"""Locations shared by source runs and frozen builds."""

import sys
import sysconfig
from pathlib import Path

from nyaatriggers.voice_config import voice_config_ok


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", source_root()))


def data_root() -> Path:
    """Keep user files beside the executable or source entry points."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return source_root()


def resolve_voice_venv(value, default: Path | None = None) -> Path:
    if not isinstance(value, str) or not value.strip():
        return default if default is not None else Path.home() / ".venv" / "ffxiv"
    return Path(value.strip()).expanduser()


def voice_venv_version(directory: Path) -> tuple[int, int] | None:
    """Read the interpreter version recorded when the voice environment was created."""
    try:
        config = Path(directory) / "pyvenv.cfg"
        if not config.is_file():
            return None
        with config.open(encoding="utf-8") as stream:
            lines = stream.read(16384).splitlines()
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "version":
                major, minor, *_ = value.strip().split(".")
                return int(major), int(minor)
    except (OSError, ValueError):
        pass
    return None


def voice_site_packages(directory: Path) -> list[str]:
    """Use only packages built for the running interpreter."""
    version = voice_venv_version(directory)
    if version is not None and version != sys.version_info[:2]:
        return []
    variables = {"base": str(directory), "platbase": str(directory)}
    paths = [sysconfig.get_path(kind, scheme="venv", vars=variables)
             for kind in ("purelib", "platlib")]
    return list(dict.fromkeys(path for path in paths if Path(path).is_dir()))


def default_voice_dir() -> Path:
    """Find a complete default voice or the user directory for its repair."""
    user = data_root() / "voices"
    bundled = bundle_root() / "voices"
    for directory in (user, bundled):
        if ((directory / "en_US-arctic-medium.onnx").is_file()
                and voice_config_ok(directory / "en_US-arctic-medium.onnx.json")):
            return directory
    return user
