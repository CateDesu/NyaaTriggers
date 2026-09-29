#!/usr/bin/env python3
"""Refresh the bundled action and status icons from XIVAPI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote
from zipfile import ZipFile, ZIP_STORED

ROOT = Path(__file__).resolve().parents[1]
API = "https://v2.xivapi.com/api"
CACHE = None
UNAVAILABLE_ICONS = {215049: "Battle Efficiency Down"}


def download(url):
    cached = CACHE / quote(url.removeprefix(API + "/"), safe="") if CACHE else None
    if cached and cached.exists():
        return cached.read_bytes()
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "NyaaTriggers icon catalog"}), timeout=30) as response:
                raw = response.read()
                if cached:
                    cached.write_bytes(raw)
                return raw
        except OSError as exc:
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def sheet(name):
    rows = {}
    after = -1
    version = None
    fields = "Name,Icon,MaxStacks,Description,StatusCategory,IsPermanent" if name == "Status" else "Name,Icon"
    with ThreadPoolExecutor(max_workers=3) as pool:
        while True:
            urls = [f"{API}/sheet/{name}?fields={fields}&limit=500"
                    + (f"&after={after + step * 500}" if after >= 0 else "")
                    + (f"&version={version}" if version else "") for step in range(3 if version else 1)]
            pages = [json.loads(raw) for raw in pool.map(download, urls)]
            count = 0
            for data in pages:
                version = data["version"]
                for row in data["rows"]:
                    count += 1
                    after = max(after, row["row_id"])
                    entry = row["fields"]
                    if entry["Icon"]["id"] or name == "Status":
                        rows[str(row["row_id"])] = {"name": entry["Name"], "icon": entry["Icon"]["id"]}
                        if name == "Status":
                            rows[str(row["row_id"])]["max_stacks"] = entry["MaxStacks"]
                            rows[str(row["row_id"])]["description"] = entry["Description"]
                            rows[str(row["row_id"])]["category"] = entry["StatusCategory"]
                            rows[str(row["row_id"])]["is_permanent"] = entry["IsPermanent"]
            if count < len(pages) * 500:
                break
    print(f"{name}: {len(rows)} entries", flush=True)
    return rows, version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-icons", type=Path,
                        help="Reuse an existing directory of game icon PNG files")
    parser.add_argument("--cache-dir", type=Path, help="Reuse downloads while resuming an interrupted refresh")
    args = parser.parse_args()
    global CACHE
    CACHE = args.cache_dir
    if CACHE:
        CACHE.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(("Status", "Action"), pool.map(sheet, ("Status", "Action"))))
    catalog = {name: result[0] for name, result in results.items()}
    catalog["versions"] = {name: result[1] for name, result in results.items()}
    catalog["unavailable_icons"] = UNAVAILABLE_ICONS
    icon_ids = set()
    for name in ("Status", "Action"):
        for row in catalog[name].values():
            if not row["icon"]:
                continue
            icon_ids.update(range(row["icon"], row["icon"] + max(1, row.get("max_stacks", 1))))
    def icon(ident):
        if ident in UNAVAILABLE_ICONS:
            return ident, None
        if args.local_icons:
            path = args.local_icons / f"{ident:06d}_hr1.png"
            if path.exists():
                return ident, path.read_bytes()
        path = f"ui/icon/{ident // 1000 * 1000:06d}/{ident:06d}_hr1.tex"
        try:
            return ident, download(f"{API}/asset?path={path}&format=png")
        except HTTPError as exc:
            if exc.code != 404:
                raise
            path = path.replace("_hr1.tex", ".tex")
            try:
                return ident, download(f"{API}/asset?path={path}&format=png")
            except HTTPError as missing:
                raise ValueError(f"Unavailable game icon {ident}") from missing
    output = ROOT / "assets" / "recap_icons.zip"
    temporary = output.with_suffix(".tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_STORED) as archive, ThreadPoolExecutor(max_workers=4) as pool:
            archive.writestr("catalog.json", json.dumps(catalog, ensure_ascii=False, separators=(",", ":")))
            for count, (ident, raw) in enumerate(pool.map(icon, sorted(icon_ids)), 1):
                if raw is None:
                    continue
                if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError(f"Invalid icon {ident}")
                archive.writestr(f"{ident}.png", raw)
                if count % 500 == 0:
                    print(f"Icons: {count}/{len(icon_ids)}", flush=True)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Bundled {len(icon_ids - UNAVAILABLE_ICONS.keys())} icons in {output.stat().st_size:,} bytes", flush=True)


if __name__ == "__main__":
    main()
