from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from nyaatriggers import triggevent_custom as custom
from nyaatriggers.trigger_conversion import (
    ConversionAdjustmentRequired, PortableTrigger, from_local, from_triggevent,
    from_triggernometry, to_local, to_triggevent, to_triggernometry,
)
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggernometry_editor import PackDocument, validate_native, xml_bytes
from nyaatriggers.ui.instance_tab import InstanceTabMixin


def cast(ident="A123", source="Boss", target="Player", kind="20"):
    return [kind, "2026-10-16T12:00:00Z", "40000001", source, ident, "Ability",
            "10000001", target, "2.500", "0", "0", "0"]


def status(ident="A123", source="Boss", target="Player", kind="26"):
    return [kind, "2026-10-16T12:00:00Z", ident, "Effect", "20.0",
            "40000001", source, "10000001", target, "01", "0", "0"]


def cancel(ident="A123"):
    return ["23", "2026-10-16T12:00:00Z", "40000001", "Boss", ident,
            "Ability", "Cancelled", "48816cb0ad2272d1"]


def portable(**changes):
    return replace(PortableTrigger("Probe", "20", "A123|A124", "{source} targets {target}"), **changes)


class TriggerConversionTests(unittest.TestCase):
    def test_local_and_triggernometry_roundtrip_keeps_matching_and_wording(self):
        for kind in ("20", "21", "22", "21|22", "26", "30", "26|30"):
            with self.subTest(kind=kind):
                original = Trigger(name="Probe", log_type=kind, ability_id="A123|A124",
                                   tts_text="{source} targets {target}", cooldown_s=0,
                                   status_scope="any", fight="Example", enabled=False)
                before = deepcopy(original.to_dict())
                root = to_triggernometry(from_local(original))
                trigger = root.find(".//Trigger")
                roundtrip = to_local(from_triggernometry(root, trigger.get("Id"), fight="Example"))
                self.assertEqual(original.to_dict(), before)
                self.assertFalse(roundtrip.enabled)
                self.assertEqual(roundtrip.fight, "Example")
                self.assertEqual(roundtrip.tts_text, original.tts_text)
                roundtrip.enabled = original.enabled = True
                line = status if kind.split("|")[0] in ("26", "30") else cast
                for event_kind in kind.split("|"):
                    for ident in ("A123", "A124", "A125", "A1230"):
                        fields = line(ident, kind=event_kind)
                        self.assertEqual(original.matches(fields), roundtrip.matches(fields))

    def test_cancel_conversion_preserves_the_short_network_log_layout(self):
        for kind in ("23", "20|23"):
            with self.subTest(kind=kind):
                original = Trigger(name="Cancel", log_type=kind, ability_id="A123|A124",
                                   tts_text="Cancelled {source}", cooldown_s=0)
                root = to_triggernometry(from_local(original))
                trigger = root.find(".//Trigger")
                local = to_local(from_triggernometry(root, trigger.get("Id")))
                self.assertEqual(local.matches(cancel()), original.matches(cancel()))
                self.assertIsNone(local.matches(cancel("A125")))
                self.assertEqual(local.tts_text, "Cancelled {source}")
                for text in ("On {target}", "{source} targets {target}"):
                    with self.subTest(text=text), self.assertRaises(ValueError):
                        to_triggernometry(portable(log_type=kind, tts_text=text))

    def test_native_cancel_pattern_cannot_require_missing_target_fields(self):
        root = to_triggernometry(portable())
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression", trigger.get("RegularExpression").replace("^20", "^23"))
        before = xml_bytes(root)
        with self.assertRaises(ValueError):
            from_triggernometry(root, trigger.get("Id"))
        self.assertEqual(xml_bytes(root), before)

    def test_cancel_checksum_capture_cannot_become_a_local_target(self):
        for header in ("23", "(?:20|23)"):
            with self.subTest(header=header):
                root = to_triggernometry(portable())
                trigger = root.find(".//Trigger")
                trigger.set("RegularExpression", "(?i)^" + header +
                            r"\|[^|]*\|[^|]*\|(?<source>[^|]*)\|A123\|[^|]*\|[^|]*\|(?<target>[^|]*)")
                before = xml_bytes(root)
                with self.assertRaises(ValueError):
                    from_triggernometry(root, trigger.get("Id"))
                self.assertEqual(xml_bytes(root), before)

    def test_required_suffix_columns_cannot_be_removed_by_a_move(self):
        for kind in ("20", "21", "22", "21|22", "26", "30", "26|30"):
            with self.subTest(kind=kind):
                root = to_triggernometry(portable(log_type=kind))
                trigger = root.find(".//Trigger")
                trigger.set("RegularExpression", trigger.get("RegularExpression") + r"[^|]*\|" * 20)
                before = xml_bytes(root)
                with self.assertRaises(ValueError):
                    from_triggernometry(root, trigger.get("Id"))
                self.assertEqual(xml_bytes(root), before)

    def test_local_and_custom_triggevent_share_common_cast_and_status_events(self):
        for kind, event in (("20", "cast"), ("26", "status_gain"), ("30", "status_loss")):
            for scope in (("any",) if kind == "20" else ("any", "self")):
                with self.subTest(kind=kind, scope=scope):
                    definition = to_triggevent(portable(log_type=kind, status_scope=scope))
                    custom.validate_trigger(definition)
                    self.assertEqual(definition["start"]["event"], event)
                    self.assertEqual(definition["start"]["target"], "me" if scope == "self" else "any")
                    trigger = to_local(from_triggevent(definition))
                    line = status if kind != "20" else cast
                    self.assertIsNotNone(trigger.matches(line(kind=kind), me="Player", me_id="10000001"))
                    self.assertIsNone(trigger.matches(line("A125", kind=kind), me="Player", me_id="10000001"))
                    if scope == "self":
                        fields = line(kind=kind)
                        fields[7], fields[8] = "10000002", "Other player"
                        self.assertIsNone(trigger.matches(fields, me="Player", me_id="10000001"))

    def test_common_triggernometry_to_custom_triggevent_preserves_tokens_and_ids(self):
        root = to_triggernometry(portable())
        ident = root.find(".//Trigger").get("Id")
        definition = to_triggevent(from_triggernometry(root, ident))
        self.assertEqual(definition["start"], {"event": "cast", "ids": "A123|A124", "target": "any"})
        self.assertEqual(definition["steps"][0]["text"], "{source} targets {target}")
        definition["steps"][0]["text"] = "{start.source} targets {start.target}"
        self.assertEqual(from_triggevent(definition).tts_text, "{source} targets {target}")

    def test_custom_numeric_ids_keep_observed_short_iinact_format(self):
        definition = to_triggevent(portable())
        definition["start"]["ids"] = "5|0006|0x000A"
        value = from_triggevent(definition)
        local = to_local(value)
        for ident in ("05", "06", "0A"):
            with self.subTest(ident=ident):
                self.assertIsNotNone(local.matches(cast(ident)))
        self.assertIsNone(local.matches(cast("07")))
        root = to_triggernometry(value)
        native = root.find(".//Trigger")
        recovered = to_local(from_triggernometry(root, native.get("Id")))
        self.assertIsNotNone(recovered.matches(cast("05")))
        custom.validate_trigger(to_triggevent(from_local(local)))

    def test_cooldown_changes_require_an_explicit_adjusted_value(self):
        source = Trigger(name="Default cooldown", ability_id="A123", tts_text="Move")
        before = source.to_dict()
        value = from_local(source)
        with self.assertRaises(ConversionAdjustmentRequired) as raised:
            to_triggernometry(value)
        adjusted = raised.exception.adjusted
        self.assertIn("whole trigger", raised.exception.description)
        self.assertEqual((adjusted.cooldown_s, adjusted.cooldown_scope), (5.0, "trigger"))
        root = to_triggernometry(adjusted)
        restored = from_triggernometry(root, root.find(".//Trigger").get("Id"))
        self.assertEqual(restored.cooldown_s, 5.0)
        with self.assertRaises(ConversionAdjustmentRequired) as reset_review:
            to_local(restored)
        self.assertEqual(to_local(reset_review.exception.adjusted).cooldown_s, 5.0)
        self.assertIn("reset", reset_review.exception.description)
        with self.assertRaises(ConversionAdjustmentRequired) as reset_review:
            to_triggernometry(replace(value, cooldown_scope="trigger"))
        self.assertEqual(reset_review.exception.adjusted.cooldown_s, 5.0)
        with self.assertRaises(ConversionAdjustmentRequired) as raised:
            to_triggevent(value)
        self.assertEqual(raised.exception.adjusted.cooldown_s, 0)
        custom.validate_trigger(to_triggevent(raised.exception.adjusted))
        self.assertEqual(source.to_dict(), before)

    def test_zone_and_fight_restrictions_survive_destination_roundtrips(self):
        value = portable(fight="FRU", zone_id=1238)
        local = to_local(value)
        self.assertIn("Futures", local.zone_regex)
        with self.assertRaises(ConversionAdjustmentRequired) as raised:
            to_triggevent(from_local(local))
        definition = to_triggevent(raised.exception.adjusted)
        self.assertEqual((definition["fight"], definition["zone_id"]), ("FRU", 1238))
        root = to_triggernometry(from_triggevent(definition))
        loaded = from_triggernometry(root, root.find(".//Trigger").get("Id"), fight="FRU")
        self.assertEqual((loaded.fight, loaded.zone_id), ("FRU", 1238))
        for pattern in (".*", "Unknown unlisted duty", " ", "(a+)+$"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                to_triggevent(portable(zone_regex=pattern))

    def test_zone_readiness_differences_require_review_in_both_directions(self):
        local = to_local(portable(zone_id=1238))
        state = SimpleNamespace(_zone_aliases=(), _awaiting_zone_metadata=True)
        self.assertTrue(InstanceTabMixin._trigger_zone_matches(state, local))
        for converter in (to_triggernometry, to_triggevent):
            with self.subTest(destination=converter.__name__):
                with self.assertRaises(ConversionAdjustmentRequired) as raised:
                    converter(from_local(local))
                self.assertIn("zone metadata", raised.exception.description)
                converter(raised.exception.adjusted)
                with self.assertRaises(ConversionAdjustmentRequired) as combined:
                    converter(replace(from_local(local), cooldown_s=5, cooldown_scope="source"))
                self.assertIn("cooldown", combined.exception.description)
                self.assertIn("zone metadata", combined.exception.description)
                converter(combined.exception.adjusted)
        definition = to_triggevent(portable(zone_id=1238))
        with self.assertRaises(ConversionAdjustmentRequired) as raised:
            to_local(from_triggevent(definition))
        self.assertIn("zone metadata", raised.exception.description)
        self.assertEqual(to_local(raised.exception.adjusted).zone_regex, local.zone_regex)

    def test_triggernometry_uses_effective_speech_and_disabled_ancestors(self):
        root = to_triggernometry(portable())
        root.find("ExportedFolder").set("Enabled", "false")
        ident = root.find(".//Trigger").get("Id")
        before = xml_bytes(root)
        value = from_triggernometry(root, ident.upper() + "#0", text_overrides={ident + "#0": "Changed ${target}"})
        self.assertEqual(value.tts_text, "Changed {target}")
        self.assertFalse(value.enabled)
        self.assertEqual(xml_bytes(root), before)
        document = PackDocument("unused.xml", before)
        self.assertFalse(from_triggernometry(document, ident).enabled)
        root.find("ExportedFolder").set("Enabled", "true")
        del root.find(".//Trigger").attrib["Enabled"]
        self.assertFalse(from_triggernometry(root, ident).enabled)

    def test_complex_sources_are_refused_without_dropping_their_behavior(self):
        changes = (
            lambda root: ET.SubElement(root.find(".//Actions"), "Action", ActionType="ExecuteScript", ExecScriptExpression="DoWork();"),
            lambda root: ET.SubElement(root.find(".//Trigger"), "Condition", Enabled="true"),
            lambda root: root.find(".//Action").set("UseTTSTextExpression", "${var:state}"),
            lambda root: root.find(".//Action").set("ExecutionDelayExpression", "500"),
            lambda root: root.find(".//Trigger").set("MutexToCapture", "shared"),
            lambda root: root.find("ExportedFolder").set("FFXIVJobFilterEnabled", "true"),
            lambda root: root.find(".//Action").set("UseTTSRateExpression", "3"),
            lambda root: root.find(".//Action").set("UseTTSTextExpression", "literal {source}"),
        )
        for change in changes:
            with self.subTest(change=changes.index(change)):
                root = to_triggernometry(portable())
                change(root)
                before = ET.tostring(root)
                with self.assertRaises(ValueError):
                    from_triggernometry(root, root.find(".//Trigger").get("Id"))
                self.assertEqual(ET.tostring(root), before)
        for key, value in (("sequence", [{"log_type": "20"}]), ("sound_file", "sound.wav"),
                           ("expiry_warn_s", 5), ("count_min", 2), ("speed", 1.5), ("interrupt", True)):
            trigger = Trigger(name="Complex local", ability_id="A123", tts_text="Move")
            setattr(trigger, key, value)
            before = trigger.to_dict()
            with self.subTest(field=key), self.assertRaises(ValueError):
                from_local(trigger)
            self.assertEqual(trigger.to_dict(), before)

    def test_triggevent_sequences_and_native_inventory_are_not_guessed(self):
        with self.assertRaises(ValueError):
            from_triggevent({"id": "native.id", "name": "Inventory display", "text": "Move"})
        definition = to_triggevent(portable())
        definition["steps"].insert(0, {"kind": "wait", "event": "cast", "ids": "B123", "target": "any"})
        before = deepcopy(definition)
        with self.assertRaises(ValueError):
            from_triggevent(definition)
        self.assertEqual(definition, before)

    def test_incompatible_event_target_and_timing_semantics_are_refused(self):
        for value in (portable(log_type="21"), portable(log_type="21|22"),
                      portable(log_type="26", status_scope="by_me"), portable(delay_s=1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                to_triggevent(value)
        for value in (portable(log_type="26", status_scope="self"), portable(delay_s=1),
                      portable(tts_text="literal ${expression}")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                to_triggernometry(value)

    def test_network_regex_conversion_requires_exact_field_constraints(self):
        for transform in (lambda pattern: pattern.replace("[^|]*", ".*", 1),
                          lambda pattern: pattern.replace("A123|A124", "A123.*"),
                          lambda pattern: pattern.split(r"\|(?:A123|A124)")[0] + r"\|(?:A123|A124)",
                          lambda pattern: pattern.replace("(?i)", "").replace("A123", "a123")):
            root = to_triggernometry(portable())
            trigger = root.find(".//Trigger")
            trigger.set("RegularExpression", transform(trigger.get("RegularExpression")))
            with self.subTest(pattern=trigger.get("RegularExpression")), self.assertRaises(ValueError):
                from_triggernometry(root, trigger.get("Id"))
        with self.assertRaises(ValueError):
            to_triggevent(portable(ability_id="0000A123"))

    def test_non_field_id_expressions_cannot_be_moved_as_field_local_matches(self):
        for kind in ("20", "26"):
            for expression in ("A123|A124", "(?:A123)|A124", "A123|(?:A124)",
                               " A123 ", "(?: A123|A124 )"):
                with self.subTest(kind=kind, expression=expression):
                    root = to_triggernometry(portable(log_type=kind))
                    trigger = root.find(".//Trigger")
                    trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
                        "(?:A123|A124)", expression))
                    before = xml_bytes(root)
                    with self.assertRaises(ValueError):
                        from_triggernometry(root, trigger.get("Id"))
                    self.assertEqual(xml_bytes(root), before)

    def test_finite_id_patterns_preserve_all_short_and_long_matches(self):
        for kind in ("20", "26"):
            for expression, identifiers in (
                    ("0[5-6]", {"05", "06"}),
                    ("(?:0[5-6]|A123|1234567[8-9])", {"05", "06", "A123", "12345678", "12345679"})):
                with self.subTest(kind=kind, expression=expression):
                    root = to_triggernometry(portable(log_type=kind))
                    trigger = root.find(".//Trigger")
                    trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
                        "(?:A123|A124)", expression))
                    value = from_triggernometry(root, trigger.get("Id"))
                    self.assertEqual(set(value.ability_id.split("|")), identifiers)
                    local = to_local(value)
                    event = cast if kind == "20" else status
                    for ident in identifiers:
                        self.assertIsNotNone(local.matches(event(ident, kind=kind)))
                    for ident in ("04", "07", "A124", "1234567A"):
                        self.assertIsNone(local.matches(event(ident, kind=kind)))

    def test_unsupported_finite_alternative_is_refused_as_a_whole(self):
        root = to_triggernometry(portable())
        trigger = root.find(".//Trigger")
        pattern = trigger.get("RegularExpression")
        for expression in ("(?:A123|12345678[9])", "(?:A123|)", "(?:A123|[0-9A-F][0-9A-F])"):
            with self.subTest(expression=expression):
                trigger.set("RegularExpression", pattern.replace("(?:A123|A124)", expression))
                with self.assertRaises(ValueError):
                    from_triggernometry(root, trigger.get("Id"))

    def test_id_captures_cannot_replace_or_remove_spoken_field_captures(self):
        for expression in ("(?<source>A123|A124)", "(?'source'A123|A124)",
                           "(?<id-source>A123|A124)", "(?<-source>A123|A124)",
                           "(?<source-id>A123|A124)", "(?P<id>A123|A124)"):
            with self.subTest(expression=expression):
                root = to_triggernometry(portable())
                trigger = root.find(".//Trigger")
                trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
                    "(?:A123|A124)", expression))
                before = xml_bytes(root)
                with self.assertRaises(ValueError):
                    from_triggernometry(root, trigger.get("Id"))
                self.assertEqual(xml_bytes(root), before)

    def test_unused_id_captures_keep_supported_matching(self):
        for expression in ("(?<id>A123|A124)", "(?'id'A123|A124)", "(A123|A124)"):
            with self.subTest(expression=expression):
                root = to_triggernometry(portable())
                trigger = root.find(".//Trigger")
                trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
                    "(?:A123|A124)", expression))
                value = from_triggernometry(root, trigger.get("Id"))
                self.assertEqual(to_local(value).matches(cast()),
                                 {"source": "Boss", "target": "Player", "count": ""})

    def test_literal_placeholder_and_malformed_expression_text_are_refused(self):
        for text in ("literal {source}", "${source", "${var:state}", "${_ffxivplayer}", "${env:source}"):
            root = to_triggernometry(portable())
            root.find(".//Action").set("UseTTSTextExpression", text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                from_triggernometry(root, root.find(".//Trigger").get("Id"))


class NativeTriggerConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            raise unittest.SkipTest("Requires real Mono and Xvfb")

    def test_native_character_range_keeps_every_matching_hex_id(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable(tts_text="Range {source}"))
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
            "(?:A123|A124)", "[0-a]123"))
        barrier = to_triggernometry(portable(ability_id="FFFF", tts_text="Barrier"))
        root.find("ExportedFolder/Triggers").append(barrier.find(".//Trigger"))
        raw = xml_bytes(root)
        validate_native(raw)
        identifiers = [char + "123" for char in "0123456789ABCDEF"]
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "range.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                for ident in identifiers:
                    host.send(t="log", line="|".join(cast(ident, source=ident)))
                    self.assertEqual(host.call(), "Range " + ident)
                for ident in ("0124", "B124", "B1230"):
                    host.send(t="log", line="|".join(cast(ident)))
                host.send(t="log", line="|".join(cast("FFFF")))
                self.assertEqual(host.call(), "Barrier")
                self.assertEqual(host.calls, ["Range " + ident for ident in identifiers] + ["Barrier"])
        local = to_local(from_triggernometry(root, trigger.get("Id")))
        for ident in identifiers:
            self.assertIsNotNone(local.matches(cast(ident)), ident)
        for ident in ("0124", "B124", "B1230"):
            self.assertIsNone(local.matches(cast(ident)), ident)

    def test_generated_triggernometry_pack_executes_the_same_static_id_callouts(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable())
        root.find("ExportedFolder").set("RawEnvironmentVariables", "source=Replaced\ntarget=Other")
        folder = root.find("ExportedFolder/Triggers")
        status_root = to_triggernometry(portable(name="Status probe", log_type="26", ability_id="ABC", tts_text="Status on {target}"))
        folder.append(status_root.find(".//Trigger"))
        short_definition = to_triggevent(portable())
        short_definition["start"]["ids"] = "5|0006"
        short_definition["steps"][0]["text"] = "Short ID {source}"
        short_root = to_triggernometry(from_triggevent(short_definition))
        folder.append(short_root.find(".//Trigger"))
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "converted.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                fields = cast("A1230")
                host.send(t="log", line="|".join(fields))
                fields = cast("A123")
                host.send(t="log", line="|".join(fields))
                self.assertEqual(host.call(), "Boss targets Player")
                fields = status("ABC")
                host.send(t="log", line="|".join(fields))
                self.assertEqual(host.call(), "Status on Player")
                host.send(t="log", line="|".join(cast("A124", source="Boss 猫", target="Player 星")))
                self.assertEqual(host.call(), "Boss 猫 targets Player 星")
                host.send(t="log", line="|".join(cast("05")))
                self.assertEqual(host.call(), "Short ID Boss")
                self.assertEqual(host.calls, ["Boss targets Player", "Status on Player", "Boss 猫 targets Player 星", "Short ID Boss"])

    def test_native_suffix_constraints_remain_silent_on_a_recorded_cast(self):
        from tests.test_triggernometry_host import replay
        from nyaatriggers.convert_triggernometry import convert_xml

        # First cast from the fork's m1s_anon.log replay.
        line = ("20|2024-07-30T18:12:26.0070000-05:00|10FF0008|Pupuqeto Fufuqeto|650F|"
                "Fall Malefic|4000AE94|Black Cat|1.477|99.17|103.99|0.00|2.96|")
        root = to_triggernometry(portable(ability_id="650F", tts_text="Network"))
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression", r"^20\|[^|]*\|[^|]*\|[^|]*\|650F\|" + r"[^|]*\|" * 20)
        act = to_triggernometry(portable(ability_id="650F", tts_text="ACT")).find(".//Trigger")
        act.set("Source", "Log")
        act.set("RegularExpression", r"^.{15}StartsCasting 14:[^:]*:[^:]*:650F:" + "[^:]*:" * 20)
        root.find("ExportedFolder/Triggers").append(act)
        barrier = to_triggernometry(portable(ability_id="FFFF", tts_text="Barrier"))
        barrier_trigger = barrier.find(".//Trigger")
        barrier_trigger.set("RegularExpression", barrier_trigger.get("RegularExpression").removeprefix("(?i)"))
        root.find("ExportedFolder/Triggers").append(barrier_trigger)
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "suffix.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line=line)
                host.send(t="log", line=line.replace("|650F|", "|FFFF|"))
                self.assertEqual(host.call(), "Barrier")
                self.assertEqual(host.calls, ["Barrier"])
            self.assertEqual([(row["ability_id"], row["tts_text"]) for row in convert_xml(pack, {})],
                             [("FFFF", "Barrier")])
            self.assertEqual(pack.read_bytes(), raw)
        with self.assertRaises(ValueError):
            from_triggernometry(root, trigger.get("Id"))
        self.assertEqual(xml_bytes(root), raw)

    def test_native_checksum_target_speech_requires_conversion_refusal(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable())
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression",
                    r"(?i)^23\|[^|]*\|[^|]*\|(?<source>[^|]*)\|A123\|[^|]*\|[^|]*\|(?<target>[^|]*)")
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "checksum-target.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cancel()))
                self.assertEqual(host.call(), "Boss targets " + cancel()[-1])
            with self.assertRaises(ValueError):
                from_triggernometry(root, trigger.get("Id"))
            self.assertEqual(pack.read_bytes(), raw)
        self.assertEqual(xml_bytes(root), raw)

    def test_native_cancel_conversion_executes_recorded_network_field_layout(self):
        from tests.test_triggernometry_host import replay

        for kind in ("23", "20|23"):
            with self.subTest(kind=kind):
                original = Trigger(name="Cancel", log_type=kind, ability_id="A123",
                                   tts_text="Cancelled {source}", cooldown_s=0)
                self.assertIsNotNone(original.matches(cancel()))
                root = to_triggernometry(from_local(original))
                raw = xml_bytes(root)
                validate_native(raw)
                with tempfile.TemporaryDirectory() as temp:
                    pack = Path(temp) / "cancel.xml"
                    pack.write_bytes(raw)
                    with replay(pack) as host:
                        host.send(t="log", line="|".join(cancel("A125")))
                        host.send(t="log", line="|".join(cancel()))
                        self.assertEqual(host.call(), "Cancelled Boss")
                        if kind == "20|23":
                            host.send(t="log", line="|".join(cast(source="Cast boss")))
                            self.assertEqual(host.call(), "Cancelled Cast boss")

    def test_native_cross_field_wildcard_requires_fallback_refusal(self):
        from tests.test_triggernometry_host import replay
        from nyaatriggers.convert_triggernometry import convert_xml

        root = to_triggernometry(portable(log_type="21", ability_id="ABCD", tts_text="Move"))
        root.find(".//Trigger").set("RegularExpression", r"^21\|(?:.*\|){3}ABCD\|")
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "wildcard.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cast("A125", target="ABCD", kind="21")))
                self.assertEqual(host.call(), "Move")
            self.assertEqual(convert_xml(pack, {}), [])
            self.assertEqual(pack.read_bytes(), raw)

    def test_native_final_field_anchor_requires_fallback_refusal(self):
        from tests.test_triggernometry_host import replay
        from nyaatriggers.convert_triggernometry import convert_xml

        root = to_triggernometry(portable(log_type="21", ability_id="ABCD", tts_text="Anchored"))
        root.find(".//Trigger").set("RegularExpression", r"^21\|(?:[^|]*\|){3}ABCD\|[^|]*$")
        barrier = to_triggernometry(portable(log_type="21", ability_id="B123", tts_text="Barrier"))
        root.find("ExportedFolder/Triggers").append(barrier.find(".//Trigger"))
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "anchored.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cast("ABCD", kind="21")))
                host.send(t="log", line="|".join(cast("B123", kind="21")))
                self.assertEqual(host.call(), "Barrier")
                self.assertEqual(host.calls, ["Barrier"])
            self.assertEqual(convert_xml(pack, {}), [])
            self.assertEqual(pack.read_bytes(), raw)

    def test_native_case_sensitive_id_requires_fallback_refusal(self):
        from tests.test_triggernometry_host import replay
        from nyaatriggers.convert_triggernometry import convert_xml

        root = to_triggernometry(portable(log_type="21", ability_id="ABCD", tts_text="Lower"))
        root.find(".//Trigger").set("RegularExpression", r"^21\|(?:[^|]*\|){3}abcd\|")
        barrier = to_triggernometry(portable(ability_id="B123", tts_text="Barrier"))
        root.find("ExportedFolder/Triggers").append(barrier.find(".//Trigger"))
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "lowercase.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cast("ABCD", kind="21")))
                host.send(t="log", line="|".join(cast("B123")))
                self.assertEqual(host.call(), "Barrier")
                self.assertEqual(host.calls, ["Barrier"])
            self.assertEqual(convert_xml(pack, {}), [])
            self.assertEqual(pack.read_bytes(), raw)

    def test_generated_triggernometry_cooldown_survives_combat_and_blocks_another_source(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable(cooldown_s=5))
        barrier = to_triggernometry(portable(name="Barrier", ability_id="B123", tts_text="Barrier"))
        barrier.find(".//Action").set("UseTTSTextExpression", "Barrier ${_incombat}")
        root.find("ExportedFolder/Triggers").append(barrier.find(".//Trigger"))
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "converted.xml"
            pack.write_bytes(xml_bytes(root))
            with replay(pack) as host:
                host.send(t="combat", active=True)
                host.send(t="log", line="|".join(cast()))
                self.assertEqual(host.call(), "Boss targets Player")
                host.send(t="combat", active=False)
                host.send(t="log", line="|".join(cast("B123")))
                self.assertEqual(host.call(), "Barrier 0")
                host.send(t="combat", active=True)
                host.send(t="log", line="|".join(cast(source="Other boss")))
                host.send(t="log", line="|".join(cast("B123")))
                self.assertEqual(host.call(), "Barrier 1")
                self.assertEqual(host.calls, ["Boss targets Player", "Barrier 0", "Barrier 1"])

    def test_native_unscoped_alternatives_change_captures_and_require_refusal(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable(tts_text="source={source} target={target}"))
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
            "(?:A123|A124)", "A123|A124"))
        before = xml_bytes(root)
        validate_native(before)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "unscoped.xml"
            pack.write_bytes(before)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cast("A123")))
                self.assertEqual(host.call(), "source=Boss target=")
                host.send(t="log", line="|".join(cast("A124")))
                self.assertEqual(host.call(), "source= target=Player")
        with self.assertRaises(ValueError):
            from_triggernometry(root, trigger.get("Id"))
        self.assertEqual(xml_bytes(root), before)

    def test_native_finite_short_and_long_ids_keep_matching_after_conversion(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable())
        trigger = root.find(".//Trigger")
        trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
            "(?:A123|A124)", "(?:0[5-6]|A123|1234567[8-9])"))
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "finite.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                for ident in ("05", "06", "A123", "12345678", "12345679"):
                    fields = cast(ident, source="Boss " + ident, target="Player " + ident)
                    host.send(t="log", line="|".join(fields))
                    self.assertEqual(host.call(), "Boss " + ident + " targets Player " + ident)
        local = to_local(from_triggernometry(root, trigger.get("Id")))
        for ident in ("05", "06", "A123", "12345678", "12345679"):
            with self.subTest(ident=ident):
                self.assertIsNotNone(local.matches(cast(ident)))

    def test_native_id_captures_change_spoken_source_and_require_refusal(self):
        from tests.test_triggernometry_host import replay

        root = to_triggernometry(portable())
        triggers = root.find("ExportedFolder/Triggers")
        triggers.clear()
        expressions = (("(?<source>A123|A124)", "A123 targets Player"),
                       ("(?'source'A123|A124)", "A123 targets Player"),
                       ("(?<id-source>A123|A124)", " targets Player"))
        for index, (expression, _expected) in enumerate(expressions):
            candidate = to_triggernometry(portable())
            trigger = candidate.find(".//Trigger")
            trigger.set("RegularExpression", trigger.get("RegularExpression").replace(
                "(?:A123|A124)", expression))
            trigger.find("Actions/Action").set("UseTTSTextExpression", str(index) + " ${source} targets ${target}")
            triggers.append(trigger)
        raw = xml_bytes(root)
        validate_native(raw)
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "captures.xml"
            pack.write_bytes(raw)
            with replay(pack) as host:
                host.send(t="log", line="|".join(cast()))
                self.assertCountEqual([host.call() for _ in expressions],
                                      [str(index) + " " + expected
                                       for index, (_expression, expected) in enumerate(expressions)])
        for trigger in triggers:
            with self.subTest(pattern=trigger.get("RegularExpression")), self.assertRaises(ValueError):
                from_triggernometry(root, trigger.get("Id"))
        self.assertEqual(xml_bytes(root), raw)


if __name__ == "__main__":
    unittest.main()
