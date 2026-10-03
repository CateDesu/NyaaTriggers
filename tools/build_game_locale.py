"""Build Japanese metadata from paired game CSVs.
Use --data-dir for Action, Status, BNpcName and PlaceName CSVs from xivapi/ffxiv-datamining.
Use --cactbot-dir for zone patterns. Curated labels live in tools/game_text_ja.json."""

import argparse
import csv
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nyaatriggers.convert_cactbot import _unescape_js


def build(data_dir, cactbot_dir):
    catalog, names = {}, {}
    for sheet in ("Action", "Status", "BNpcName", "PlaceName"):
        rows = {}
        for language in ("en", "ja"):
            with (data_dir / f"{sheet}-{language}.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows[language] = {row["#"]: row for row in csv.DictReader(stream)}
        field = "Singular" if sheet == "BNpcName" else "Name"
        translated = {}
        for ident, row in rows["ja"].items():
            name = row[field]
            if not name or name.startswith("_rsv_"):
                continue
            english = rows["en"].get(ident, {}).get(field, "")
            if english and not english.startswith("_rsv_"):
                names.setdefault(english, name)
            translated[ident] = {"name": name}
            if sheet == "Status":
                translated[ident]["description"] = row["Description"]
        if sheet in ("Action", "Status"):
            catalog[sheet] = translated
    source = (cactbot_dir / "resources" / "zone_info.ts").read_text(encoding="utf-8")
    for match in re.finditer(r"^  (\d+): \{(.*?)^  \},", source, re.M | re.S):
        english = re.search(r"'en': '((?:[^'\\]|\\.)*)'", match[2])
        japanese = re.search(r"'ja': '((?:[^'\\]|\\.)*)'", match[2])
        if english and japanese:
            names[_unescape_js(english[1])] = _unescape_js(japanese[1])
    curated = json.loads((ROOT / "tools" / "game_text_ja.json").read_text(encoding="utf-8"))
    catalog.update(names=names, **curated)
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cactbot-dir", type=Path, required=True)
    args = parser.parse_args()
    catalog = build(args.data_dir, args.cactbot_dir)
    output = ROOT / "assets" / "game_data_ja.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"Wrote {output.name}: " + ", ".join(f"{key} {len(value)}" for key, value in catalog.items()))


if __name__ == "__main__":
    main()
