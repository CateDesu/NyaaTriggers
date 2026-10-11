import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyaatriggers.convert_triggernometry import (
    convert_xml, expand_id_expr, extract_ids, load_zone_map, path_to_fight,
)

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)
        # Fail this pytest case, or let the direct runner continue to the next group.
        raise AssertionError(name)


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


def test_party_finder_boundary():
    check("trial folder named Partywide Mechanics stays a trial",
          path_to_fight("Trials/6.3/Partywide Mechanics/some trigger") == "Partywide Mechanics")
    check("duty partyfinder root still files as party finder",
          path_to_fight("Duty_PartyFinder/some trigger") == "Party Finder")
    check("mid path party finder folder still files as party finder",
          path_to_fight("Downloads/Party Finder/some trigger") == "Party Finder")


def test_top_boundary():
    check("TOP share still tags TOP",
          path_to_fight("Sharing Channel/Ultimate/TOP/some trigger") == "TOP")
    check("Omega Protocol share still tags TOP",
          path_to_fight("Sharing Channel/Ultimate/Omega Protocol/some trigger") == "TOP")
    check("DESKTOP share does not tag TOP",
          path_to_fight("Sharing Channel/Ultimate/DESKTOP Icons/some trigger") == "")


def test_job_category_boundary():
    check("disciples of war category still finds the job",
          path_to_fight("Sharing Channel/Disciples of War/WHM/some trigger") == "WHM")
    check("singular disciple category still finds the job",
          path_to_fight("Sharing Channel/Disciple of Magic/BLM/some trigger") == "BLM")
    check("Edwards category is not a job category",
          path_to_fight("Sharing Channel/Edwards Stuff/Some Fight") == "Some Fight")


def test_ba_boundary():
    check("BA folder still tags BA",
          path_to_fight("Eureka-Like/BA/some trigger") == "BA")
    check("Baldesion Arsenal still tags BA",
          path_to_fight("Eureka-Like/Baldesion Arsenal/some trigger") == "BA")
    check("ZABAN does not tag BA",
          path_to_fight("Eureka-Like/ZABAN/some trigger") == "ZABAN")


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


_GIANT_RX = r'^21\|(?:[^|]*\|){' + '9' * 5000 + r'}8B5F\|'


def test_giant_repeat_count_extracts_nothing():
    check("over 4300 digit repeat count stays literal, no ValueError escapes",
          extract_ids(_GIANT_RX) == [])


def test_giant_repeat_count_file_still_scans():
    root, trigger, _action = simple_pack()
    trigger.set("RegularExpression", _GIANT_RX)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "giant.xml"
        p.write_bytes(ET.tostring(root))
        res = convert_xml(p, {})
    check("file with a giant repeat count scans without raising", res == [])


def test_sane_repeat_counts_expand_as_before():
    check("count of 3 expands and the id still extracts",
          extract_ids(r'^21\|(?:[^|]*\|){3}8B5F\|') == [('21', '8B5F')])
    check("count over 10 stays literal and extracts nothing",
          extract_ids(r'^21\|(?:[^|]*\|){11}8B5F\|') == [])


def test_load_zone_map_skips_non_scalar_rows():
    existing = [
        {"fight": "DSR", "zone_regex": " Dragonsong"},
        {"fight": ["DSR"], "zone_regex": " Dragonsong"},
        {"fight": "TOP", "zone_regex": 42},
        "not a dict",
        {"fight": "", "zone_regex": "x"},
    ]
    check("non scalar rows are skipped and good rows build the map",
          load_zone_map(existing) == {"DSR": " Dragonsong"})


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


def simple_pack():
    root = ET.Element("TriggernometryExport", Version="1")
    folder = ET.SubElement(root, "ExportedFolder", Name="Trials", Enabled="true")
    triggers = ET.SubElement(folder, "Triggers")
    trigger = ET.SubElement(triggers, "Trigger", Name="Call", Enabled="true",
                            Source="FFXIVNetwork", RegularExpression=r"^21\|(?:[^|]*\|){3}ABCD\|")
    actions = ET.SubElement(trigger, "Actions")
    action = ET.SubElement(actions, "Action", ActionType="UseTTS", UseTTSTextExpression="Move")
    return root, trigger, action


def converted(root):
    return convert_xml(Path("pack.xml"), {}, content=ET.tostring(root))


def test_plain_speech_remains_convertible():
    root, trigger, action = simple_pack()
    ET.SubElement(trigger, "Condition", Enabled="false")
    ET.SubElement(action, "Condition", Enabled="false")
    action.set("ExecutionDelayExpression", "0")
    rows = converted(root)
    check("plain immediate speech converts as a disabled local ability row",
          len(rows) == 1 and rows[0]["ability_id"] == "ABCD"
          and rows[0]["tts_text"] == "Move" and not rows[0]["enabled"])


def test_conditions_cannot_be_discarded_by_local_conversion():
    for owner in ("trigger", "action"):
        root, trigger, action = simple_pack()
        condition = ET.SubElement(trigger if owner == "trigger" else action,
                                  "Condition", Enabled="true", Grouping="And")
        ET.SubElement(condition, "ConditionSingle", Enabled="true", ExpressionL="${_incombat}",
                      ExpressionTypeL="Numeric", ExpressionR="1", ExpressionTypeR="Numeric",
                      ConditionType="NumericEqual")
        check(owner + " condition requires the native engine", converted(root) == [])


def test_delays_and_scripted_actions_require_the_native_engine():
    for expression in ("1000", "${var:delay}"):
        root, trigger, action = simple_pack()
        action.set("ExecutionDelayExpression", expression)
        check("speech delay is not converted into an immediate call: " + expression,
              converted(root) == [])
    for kind in ("ExecuteScript", "Variable", "Placeholder", "Trigger"):
        root, trigger, action = simple_pack()
        ET.SubElement(trigger.find("Actions"), "Action", ActionType=kind)
        check(kind + " actions are not stripped from a speech trigger", converted(root) == [])


def test_multiple_speech_actions_are_not_truncated():
    root, trigger, action = simple_pack()
    ET.SubElement(trigger.find("Actions"), "Action", ActionType="UseTTS", UseTTSTextExpression="Then stop")
    check("multiple speech actions require the native engine", converted(root) == [])


def test_non_log_sources_cannot_become_ability_matches():
    for source in ("None", "Endpoint", "ACT"):
        root, trigger, action = simple_pack()
        trigger.set("Source", source)
        check(source + " source is not reinterpreted as a combat log trigger", converted(root) == [])


def test_disabled_extra_actions_do_not_block_plain_speech():
    root, trigger, action = simple_pack()
    extra = ET.SubElement(trigger.find("Actions"), "Action", ActionType="ExecuteScript", Enabled=" False ")
    ET.SubElement(extra, "Condition", Enabled="true")
    action.set("ExecutionDelayExpression", "0.0")
    rows = converted(root)
    check("disabled actions leave the immediate plain speech fallback available",
          len(rows) == 1 and rows[0]["tts_text"] == "Move")


def test_legacy_conditions_cannot_be_discarded():
    for owner in ("trigger", "action"):
        root, trigger, action = simple_pack()
        conditions = ET.SubElement(trigger if owner == "trigger" else action, "Conditions")
        ET.SubElement(conditions, "Condition", ExpressionL="0", ExpressionTypeL="Numeric",
                      ExpressionR="1", ExpressionTypeR="Numeric", ConditionType="NumericEqual")
        check(owner + " legacy conditions require the native engine", converted(root) == [])
    root, trigger, action = simple_pack()
    ET.SubElement(trigger, "Conditions")
    ET.SubElement(action, "Conditions")
    check("empty legacy condition containers do not prevent plain speech", len(converted(root)) == 1)


def test_ancestor_folder_filters_are_not_discarded():
    for setting in ("ZoneFilterEnabled", "FFXIVZoneFilterEnabled", "FFXIVJobFilterEnabled", "EventFilterEnabled"):
        root, trigger, action = simple_pack()
        folder = root.find("ExportedFolder")
        folder.set(setting, "True")
        folder.set("ZoneFilterRegularExpression", "^Allowed$")
        check(setting + " requires native filtering", converted(root) == [])
    root, trigger, action = simple_pack()
    root.find("ExportedFolder").set("ZoneFilterEnabled", "False")
    check("disabled folder filters do not prevent plain speech", len(converted(root)) == 1)
    root.find("ExportedFolder").set("ZoneFilterEnabled", " True ")
    check("native whitespace in enabled filter flags is recognized", converted(root) == [])


def test_regex_actor_and_status_constraints_are_not_discarded():
    for pattern in (r"^21\|[^|]*\|40001234\|[^|]*\|ABCD\|",
                    r"^21\|(?:[^|]*\|){3}ABCD\|[^|]*\|10001234\|",
                    r"^21\|(?:[^|]*\|){3}ABCD\|[^|]*\|[^|]*\|[^|]+\|",
                    r"^26\|[^|]*\|ABCD\|[^|]*\|10\|"):
        root, trigger, action = simple_pack()
        trigger.set("RegularExpression", pattern)
        check("constrained log fields require the native engine: " + pattern,
              converted(root) == [])
    root, trigger, action = simple_pack()
    trigger.set("Source", "Log")
    trigger.set("RegularExpression", r"^.{15}\S+ 15:[^:]*:[^:]*:ABCD:")
    check("simple formatted ACT ability matches remain convertible", len(converted(root)) == 1)
    for pattern in (r"^21\|[^|]*\|\|[^|]*\|ABCD\|",
                    r"^21\|[^|]*$\|[^|]*\|[^|]*\|ABCD\|"):
        trigger.set("Source", "FFXIVNetwork")
        trigger.set("RegularExpression", pattern)
        check("literal empty fields and premature anchors are not discarded: " + pattern,
              converted(root) == [])
    for pattern in (r"^21\|(?:[^|]*\|){3}ABCD\|.*$",
                    r"^21\|(?<time>[^|]*)\|(?<source>[^|]*)\|([^|]*)\|ABCD\|",
                    r"^21\|(?:[^|]*\|){3}ABCD\|(?<tail>.*)$"):
        trigger.set("RegularExpression", pattern)
        check("plain wildcard fields and unused captures remain convertible: " + pattern,
              len(converted(root)) == 1)


def test_regex_balancing_groups_require_native_matching():
    for pattern in (r"^21\|(?<field>[^|]*)\|(?<-field>[^|]*)\|(?<-field>[^|]*)\|ABCD\|",
                    r"^21\|(?:[^|]*\|){3}(?<-field>ABCD)\|",
                    r"^21\|(?:[^|]*\|){3}(?'new-field'ABCD)\|"):
        root, trigger, action = simple_pack()
        trigger.set("RegularExpression", pattern)
        check("balancing capture conditions are not discarded: " + pattern, converted(root) == [])


def test_source_and_formatted_log_prefixes_keep_native_meaning():
    for source, pattern in (("Log", r"^21\|(?:[^|]*\|){3}ABCD\|"),
                            ("FFXIVNetwork", r"^.{15}\S+ 15:[^:]*:[^:]*:ABCD:"),
                            ("Log", r"^.{15}WrongType 15:[^:]*:[^:]*:ABCD:"),
                            ("Log", r"^.{99}\S+ 15:[^:]*:[^:]*:ABCD:")):
        root, trigger, action = simple_pack()
        trigger.set("Source", source)
        trigger.set("RegularExpression", pattern)
        check("unmatched native source or ACT prefix is not discarded: " + source + " " + pattern,
              converted(root) == [])
    root, trigger, action = simple_pack()
    del trigger.attrib["Source"]
    check("missing Source defaults to formatted Log rather than network", converted(root) == [])
    for pattern, kind in ((r"^.{15}ActionEffect 15:[^:]*:[^:]*:ABCD:", "21"),
                          (r"^.{28}15:[^:]*:[^:]*:ABCD:", "21"),
                          (r"^.{15}\S+ 1[56]:[^:]*:[^:]*:ABCD:", "21|22"),
                          (r"^.{15}ActionEffect 1[56]:[^:]*:[^:]*:ABCD:", "21"),
                          (r"^.{15}StatusAdd 1A:ABCD:", "26")):
        trigger.set("RegularExpression", pattern)
        rows = converted(root)
        check("actual formatted ACT prefix remains convertible: " + pattern,
              len(rows) == 1 and rows[0]["log_type"] == kind)


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
