import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from nyaatriggers import fight_catalog as fc

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


FIGHT_TREE = [
    ("Ultimates", [
        ("Dawntrail",      ["FRU", "UMAD"]),
        ("Endwalker",      ["TOP", "DSR"]),
        ("Shadowbringers", ["TEA"]),
        ("Stormblood",     ["UwU", "UCoB"]),
    ]),
]

LIVE_PATHS = [
    "ui/raidboss/data/04-sb/ultimate/unending_coil_ultimate.ts",
    "ui/raidboss/data/04-sb/ultimate/ultima_weapon_ultimate.ts",
    "ui/raidboss/data/05-shb/ultimate/the_epic_of_alexander.ts",
    "ui/raidboss/data/06-ew/ultimate/dragonsongs_reprise_ultimate.ts",
    "ui/raidboss/data/06-ew/ultimate/the_omega_protocol.ts",
    "ui/raidboss/data/07-dt/ultimate/dancing_mad.ts",
    "ui/raidboss/data/07-dt/ultimate/futures_rewritten.ts",
]

check("every mapped stem resolves to a known tag",
      all(tag in fc._ULTIMATE_INFO for tag in fc._ULTIMATE_STEM_TO_TAG.values()))

stems = {p.rsplit("/", 1)[1][:-3] for p in LIVE_PATHS}
check("every live ultimate stem is mapped", stems <= set(fc._ULTIMATE_STEM_TO_TAG))

offline = fc.build_offline(FIGHT_TREE, set())
online = fc.parse_cactbot_paths(LIVE_PATHS)
offline_keys = {(e["difficulty"], e["name"]) for e in offline}
check("each derived ultimate row matches an offline entry",
      all((e["difficulty"], e["name"]) in offline_keys for e in online))
check("derived rows use the fight tag as folder",
      all(e["folder_name"] == fc._ULTIMATE_STEM_TO_TAG[p.rsplit("/", 1)[1][:-3]]
          for e, p in zip(online, LIVE_PATHS)))

with tempfile.TemporaryDirectory() as td:
    cache = Path(td) / "fight_catalog.json"
    cache.write_text(json.dumps(online), encoding="utf-8")
    merged = fc.load_catalog(FIGHT_TREE, set(), cache)
    ult = [(e["difficulty"], e["name"]) for e in merged if e["difficulty"] == "Ultimate"]
    check("no duplicate ultimate rows after merge", len(ult) == len(set(ult)))
    check("all seven ultimates present", len(ult) == 7)

    cache.write_text(json.dumps([
        {"difficulty": "Ultimate", "expansion": "Dawntrail",
         "name": "Futures Rewritten", "folder_name": "bogus", "has_triggers": False},
        {"difficulty": "Ultimate", "expansion": "Dawntrail",
         "name": "Some New Ultimate", "folder_name": "Some New Ultimate",
         "has_triggers": False},
        {"difficulty": "Ultimate", "expansion": "Dawntrail",
         "name": "Some New Ultimate", "folder_name": "Duplicate",
         "has_triggers": False},
    ]), encoding="utf-8")
    merged = fc.load_catalog(FIGHT_TREE, set(), cache)
    names = [e["name"] for e in merged if e["difficulty"] == "Ultimate"]
    check("cache row naming an offline fight is dropped",
          names.count("Futures Rewritten") == 1)
    check("cache row with a fresh name is kept", "Some New Ultimate" in names)
    check("duplicate cache rows are dropped", names.count("Some New Ultimate") == 1)

TRIAL_TREE = [
    ("Extreme Trials", [
        ("Dawntrail",      ["Zelenia EX", "Enuo EX", "Doomtrain EX",
                            "Queen EX", "Valigarmanda EX", "Zoraal Ja EX"]),
        ("Endwalker",      ["Zeromus EX", "Golbez EX", "Rubicante EX"]),
        ("A Realm Reborn", ["Ultima's Bane EX"]),
    ]),
]

LIVE_TRIAL_PATHS = [
    "ui/raidboss/data/02-arr/trial/levi-ex.ts",
    "ui/raidboss/data/02-arr/trial/shiva-ex.ts",
    "ui/raidboss/data/02-arr/trial/titan-ex.ts",
    "ui/raidboss/data/02-arr/trial/ultima-ex.ts",
    "ui/raidboss/data/03-hw/trial/bismarck-ex.ts",
    "ui/raidboss/data/03-hw/trial/ravana-ex.ts",
    "ui/raidboss/data/03-hw/trial/sephirot-ex.ts",
    "ui/raidboss/data/03-hw/trial/sophia-ex.ts",
    "ui/raidboss/data/03-hw/trial/thordan-ex.ts",
    "ui/raidboss/data/03-hw/trial/zurvan-ex.ts",
    "ui/raidboss/data/04-sb/trial/byakko-ex.ts",
    "ui/raidboss/data/04-sb/trial/lakshmi-ex.ts",
    "ui/raidboss/data/04-sb/trial/rathalos-ex.ts",
    "ui/raidboss/data/04-sb/trial/seiryu-ex.ts",
    "ui/raidboss/data/04-sb/trial/shinryu-ex.ts",
    "ui/raidboss/data/04-sb/trial/susano-ex.ts",
    "ui/raidboss/data/04-sb/trial/suzaku-ex.ts",
    "ui/raidboss/data/04-sb/trial/tsukuyomi-ex.ts",
    "ui/raidboss/data/05-shb/trial/diamond_weapon-ex.ts",
    "ui/raidboss/data/05-shb/trial/emerald_weapon-ex.ts",
    "ui/raidboss/data/05-shb/trial/hades-ex.ts",
    "ui/raidboss/data/05-shb/trial/innocence-ex.ts",
    "ui/raidboss/data/05-shb/trial/ruby_weapon-ex.ts",
    "ui/raidboss/data/05-shb/trial/titania-ex.ts",
    "ui/raidboss/data/05-shb/trial/varis-ex.ts",
    "ui/raidboss/data/05-shb/trial/wol-ex.ts",
    "ui/raidboss/data/06-ew/trial/barbariccia-ex.ts",
    "ui/raidboss/data/06-ew/trial/endsinger-ex.ts",
    "ui/raidboss/data/06-ew/trial/golbez-ex.ts",
    "ui/raidboss/data/06-ew/trial/hydaelyn-ex.ts",
    "ui/raidboss/data/06-ew/trial/rubicante-ex.ts",
    "ui/raidboss/data/06-ew/trial/zeromus-ex.ts",
    "ui/raidboss/data/06-ew/trial/zodiark-ex.ts",
    "ui/raidboss/data/07-dt/trial/arkveld-ex.ts",
    "ui/raidboss/data/07-dt/trial/doomtrain-ex.ts",
    "ui/raidboss/data/07-dt/trial/enuo-ex.ts",
    "ui/raidboss/data/07-dt/trial/necron-ex.ts",
    "ui/raidboss/data/07-dt/trial/queen-eternal-ex.ts",
    "ui/raidboss/data/07-dt/trial/valigarmanda-ex.ts",
    "ui/raidboss/data/07-dt/trial/zelenia-ex.ts",
    "ui/raidboss/data/07-dt/trial/zoraal-ja-ex.ts",
]

EX_TAGS = {tag for _cat, exps in TRIAL_TREE for _exp, tags in exps for tag in tags}

check("every mapped trial stem resolves to a shipped tag",
      all(tag in EX_TAGS for tag in fc._TRIAL_STEM_TO_TAG.values()))

trial_stems = {p.rsplit("/", 1)[1][:-3] for p in LIVE_TRIAL_PATHS}
check("every mapped trial stem is a live stem",
      set(fc._TRIAL_STEM_TO_TAG) <= trial_stems)

online_trials = fc.parse_cactbot_paths(LIVE_TRIAL_PATHS)
check("every shipped extreme fight survives derivation",
      EX_TAGS <= {e["name"] for e in online_trials})
check("derived extreme rows use the fight tag as folder",
      all(e["folder_name"] == e["name"] for e in online_trials))

with tempfile.TemporaryDirectory() as td:
    cache = Path(td) / "fight_catalog.json"
    cache.write_text(json.dumps(online_trials), encoding="utf-8")
    merged = fc.load_catalog(FIGHT_TREE + TRIAL_TREE, set(), cache)
    ext = [(e["difficulty"], e["name"]) for e in merged if e["difficulty"] == "Extreme"]
    check("no duplicate extreme rows after merge", len(ext) == len(set(ext)))
    check("all ten shipped extremes present",
          sum(1 for _d, n in ext if n in EX_TAGS) == 10)

print("\n" + (f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "all passed"))
sys.exit(1 if FAILS else 0)
