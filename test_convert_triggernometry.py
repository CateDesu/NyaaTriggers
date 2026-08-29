"""path_to_fight word boundary tests for convert_triggernometry.

The Hunts, Party Finder, BA, TOP and job category checks used plain
substring matching, so a trial folder like "The Hunt Line" filed into
Hunts 6.3 and a DESKTOP share mistagged as TOP. The checks are
word-bounded now, same idiom as the unsorted raid tag scan, and the
legitimate folder names still file where they always did.

Also covers the repeat-count guard: Python 3.11 raises ValueError on an
int string past 4300 digits, so a corrupt XML carrying a repeat count
that long must not abort the whole scan.

Run directly:  python test_convert_triggernometry.py   (exit 0 = all pass)
        or:    python -m pytest test_convert_triggernometry.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from convert_triggernometry import (
    convert_xml, expand_id_expr, extract_ids, load_zone_map, path_to_fight,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)
        # Under pytest this fails the calling test; the direct-run loop below
        # catches it and moves on to the next test function.
        raise AssertionError(name)


# ── Hunts, the word boundary keeps trial folders out ────────────────────
def test_hunts_boundary_keeps_trials_out():
    check("trial folder named The Hunt Line stays a trial",
          path_to_fight("Trials/6.3/The Hunt Line/some trigger") == "The Hunt Line")
    check("trial folder named The Hunting Lodge stays a trial",
          path_to_fight("Trials/6.3/The Hunting Lodge/some trigger") == "The Hunting Lodge")


def test_hunts_legit_paths_unchanged():
    check("hunts root with era folder still files as hunts",
          path_to_fight("Hunts/6.3/S Rank/some trigger") == "Hunts 6.3")
    check("hunts root without era still files as hunts",
          path_to_fight("Hunts/Elite Marks/some trigger") == "Hunts")
    check("mid path hunts folder with era in one name still files as hunts",
          path_to_fight("Downloads/Hunts 6.0/Some Mark/some trigger") == "Hunts 6.0")


# ── Trials run after Party Finder, so the boundary matters there too ────
def test_party_finder_boundary():
    check("trial folder named Partywide Mechanics stays a trial",
          path_to_fight("Trials/6.3/Partywide Mechanics/some trigger") == "Partywide Mechanics")
    check("duty partyfinder root still files as party finder",
          path_to_fight("Duty_PartyFinder/some trigger") == "Party Finder")
    check("mid path party finder folder still files as party finder",
          path_to_fight("Downloads/Party Finder/some trigger") == "Party Finder")


# ── sharing channel TOP, STOP and DESKTOP can't mistag ──────────────────
def test_top_boundary():
    check("TOP share still tags TOP",
          path_to_fight("Sharing Channel/Ultimate/TOP/some trigger") == "TOP")
    check("Omega Protocol share still tags TOP",
          path_to_fight("Sharing Channel/Ultimate/Omega Protocol/some trigger") == "TOP")
    check("DESKTOP share does not tag TOP",
          path_to_fight("Sharing Channel/Ultimate/DESKTOP Icons/some trigger") == "")


# ── sharing channel job category, a name like Edwards stays out ─────────
def test_job_category_boundary():
    check("disciples of war category still finds the job",
          path_to_fight("Sharing Channel/Disciples of War/WHM/some trigger") == "WHM")
    check("singular disciple category still finds the job",
          path_to_fight("Sharing Channel/Disciple of Magic/BLM/some trigger") == "BLM")
    check("Edwards category is not a job category",
          path_to_fight("Sharing Channel/Edwards Stuff/Some Fight") == "Some Fight")


# ── Eureka BA, uppercase words like ZABAN can't mistag ──────────────────
def test_ba_boundary():
    check("BA folder still tags BA",
          path_to_fight("Eureka-Like/BA/some trigger") == "BA")
    check("Baldesion Arsenal still tags BA",
          path_to_fight("Eureka-Like/Baldesion Arsenal/some trigger") == "BA")
    check("ZABAN does not tag BA",
          path_to_fight("Eureka-Like/ZABAN/some trigger") == "ZABAN")


# ── snake_case folders file like their spaced forms ─────────────────────
def test_word_bounds_treat_underscore_as_separator():
    check("snake_case job category finds the job",
          path_to_fight("Sharing Channel/disciples_of_war/1 - WHM/stuff") == "WHM")
    check("snake_case BA folder tags BA",
          path_to_fight("Eureka-Like/BA_Raid/thing") == "BA")
    check("snake_case party folder files as party finder",
          path_to_fight("Downloads/My_Party_Runs/thing") == "Party Finder")
    check("the spaced forms file the same as before",
          path_to_fight("Sharing Channel/disciples of war/1 - WHM/stuff") == "WHM"
          and path_to_fight("Eureka-Like/BA Raid/thing") == "BA")


# ── a repeat count past 4300 digits must not kill the scan ───────────────
_GIANT_RX = r'^21\|(?:[^|]*\|){' + '9' * 5000 + r'}8B5F\|'


def test_giant_repeat_count_extracts_nothing():
    check("over 4300 digit repeat count stays literal, no ValueError escapes",
          extract_ids(_GIANT_RX) == [])


def test_giant_repeat_count_file_still_scans():
    xml = ('<TriggernometryExport><Folder Name="Trials">'
           '<Trigger Name="Giant Repeat" RegularExpression="' + _GIANT_RX + '">'
           '</Trigger></Folder></TriggernometryExport>')
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "giant.xml"
        p.write_text(xml, encoding="utf-8")
        res = convert_xml(p, {})
    check("file with a giant repeat count scans without raising", res == [])


def test_sane_repeat_counts_expand_as_before():
    check("count of 3 expands and the id still extracts",
          extract_ids(r'^21\|(?:[^|]*\|){3}8B5F\|') == [('21', '8B5F')])
    check("count over 10 stays literal and extracts nothing",
          extract_ids(r'^21\|(?:[^|]*\|){11}8B5F\|') == [])


# ── non scalar fight or zone rows are skipped, the map still builds ──────
def test_load_zone_map_skips_non_scalar_rows():
    existing = [
        {"fight": "DSR", "zone_regex": " Dragonsong"},
        {"fight": ["DSR"], "zone_regex": " Dragonsong"},   # list fight, junk
        {"fight": "TOP", "zone_regex": 42},                # int zone, junk
        "not a dict",
        {"fight": "", "zone_regex": "x"},                  # empty fight ignored
    ]
    check("non scalar rows are skipped and good rows build the map",
          load_zone_map(existing) == {"DSR": " Dragonsong"})


# ── every enclosing group form strips before the id expands ──────────────
def test_named_group_forms_strip_in_expand():
    check(".NET single quote named group strips",
          expand_id_expr("(?'id'8B5F)") == ["8B5F"])
    check("P named group still strips",
          expand_id_expr("(?P<id>8B5F)") == ["8B5F"])
    check("angle bracket named group still strips",
          expand_id_expr("(?<id>8B5F)") == ["8B5F"])
    check("non capturing and plain groups still strip",
          expand_id_expr("(?:8B5F)") == ["8B5F"]
          and expand_id_expr("(8B5F)") == ["8B5F"])


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass            # check() already recorded the failed step
            except Exception as exc:
                print(f"FAIL  {_name}: {exc!r}")
                FAILS.append(_name)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("all passed")
