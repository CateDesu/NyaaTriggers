import json
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile
import zlib


def engine_commit(jar: Path) -> str | None:
    jar = Path(jar)
    try:
        with ZipFile(jar) as archive:
            info = archive.getinfo("META-INF/MANIFEST.MF")
            if info.file_size > 65536:
                return None
            manifest = archive.read(info).decode("utf-8")
        main = manifest.replace("\r\n", "\n").replace("\n ", "").split("\n\n", 1)[0]
        for line in main.splitlines():
            name, separator, value = line.partition(": ")
            if separator and name.lower() == "nyaa-engine-commit":
                return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    except (OSError, ValueError, KeyError, RuntimeError, BadZipFile, zlib.error):
        pass
    try:
        with jar.with_name(jar.name + ".built-from").open(encoding="utf-8") as stream:
            stamp = json.loads(stream.read(65536))
        commit = stamp.get("engine") if isinstance(stamp, dict) else None
        return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None
    except (OSError, ValueError, RecursionError):
        return None
