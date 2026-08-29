"""Regression tests for the trigger editor's zone-regex guards (M-4/H-3),
plus step-row round trips, null step log types, and the clamp warning.

The live zone-match dot must compile and search through the engine's
guarded helpers (compile_user_regex + _safe_search), so a catastrophic
intermediate pattern typed character-by-character can't hang the dialog,
and accept() must refuse to save a zone regex the engine rejects (at
runtime compile_user_regex -> None would make the trigger silently dead).

Run directly:  python test_trigger_dialog.py   (exit 0 = all pass)
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QDialog

import trigger_dialog
from trigger_dialog import TriggerDialog

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


_app = QApplication.instance() or QApplication(sys.argv)

ZONE = "The Voidcast Dais"
GREEN, RED, GREY = "#a6e3a1", "#f38ba8", "#5e6480"


def new_dialog(zone=ZONE):
    dlg = TriggerDialog(current_zone=zone)
    return dlg


# ── the zone dot goes through the engine's guarded compile/search ─────────
dlg = new_dialog()
dlg._zone.setText("")
check("blank zone pattern shows no dot", dlg._zone_dot.text() == "")

dlg._zone.setText("Voidcast")
check("matching zone pattern is green", GREEN in dlg._zone_dot.text())
check("matching zone tooltip names the zone",
      ZONE in dlg._zone_dot.toolTip())

dlg._zone.setText("Everkeep")
check("non-matching zone pattern is red", RED in dlg._zone_dot.text())

dlg._zone.setText("[")
check("uncompilable zone pattern is grey", GREY in dlg._zone_dot.text())
check("uncompilable zone pattern says invalid",
      dlg._zone_dot.toolTip() == "Invalid regex")

# The ReDoS case itself: typing a catastrophic pattern must not hang.
t0 = time.monotonic()
dlg._zone.setText("(.*)*x")
elapsed = time.monotonic() - t0
check("catastrophic zone pattern is rejected, not run",
      GREY in dlg._zone_dot.text()
      and dlg._zone_dot.toolTip() == "Invalid regex")
check("catastrophic zone pattern returns immediately", elapsed < 2.0)

# With no current zone there is nothing to match against: grey, no tooltip.
dlg_nozone = new_dialog(zone="")
dlg_nozone._zone.setText("Voidcast")
check("no current zone is grey", GREY in dlg_nozone._zone_dot.text())
check("no current zone has an empty tooltip",
      dlg_nozone._zone_dot.toolTip() == "")

# ── accept() refuses a zone regex the engine can't compile ────────────────
class _WarnBox:
    calls = []

    @staticmethod
    def warning(*args, **kwargs):
        _WarnBox.calls.append(args)


_real_qmessagebox = trigger_dialog.QMessageBox
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._zone.setText("(.*)*x")
    _WarnBox.calls.clear()
    dlg.accept()
    check("catastrophic zone regex cannot be saved",
      len(_WarnBox.calls) == 1
      and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._zone.setText("[")
    _WarnBox.calls.clear()
    dlg.accept()
    check("uncompilable zone regex cannot be saved",
      len(_WarnBox.calls) == 1
      and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._zone.setText("Voidcast")
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("valid zone regex saves",
      not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._zone.setText("")
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("blank zone (any zone) saves",
      not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a step row keeps both ability_id and ability_regex on round trip ──────
from trigger_engine import Trigger

row = trigger_dialog._StepRow(data={"log_type": "21", "ability_id": "A55B",
                                    "ability_regex": "Exaflare"})
d = row.to_dict()
check("step row keeps both id and regex",
      d.get("ability_id") == "A55B" and d.get("ability_regex") == "Exaflare")

# ── a null step log_type loads as the "20" default, not the text "None" ───
row = trigger_dialog._StepRow(data={"log_type": None})
check("null step log_type falls back to 20", row._type.currentData() == "20")

# ── out of range persisted values warn before OK saves them clamped ───────
class _ClampBox:
    StandardButton = _real_qmessagebox.StandardButton
    answer = None
    calls = []

    @staticmethod
    def warning(*args, **kwargs):
        _ClampBox.calls.append(args)
        return _ClampBox.answer


trigger_dialog.QMessageBox = _ClampBox
try:
    t = Trigger(name="clamped", log_type="26", ability_id="A55B",
                cooldown_s=7200.0, speed=5.0, duration_max=50000.0,
                sequence=[{"log_type": "21", "timeout_s": 0.2}])
    dlg = TriggerDialog(trigger=t)
    _ClampBox.calls.clear()
    _ClampBox.answer = None
    dlg.accept()
    text = _ClampBox.calls[0][2] if _ClampBox.calls else ""
    check("clamped values block the save with one warning",
          len(_ClampBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)
    check("warning lists each field with old and clamped value",
          all(s in text for s in ("7200", "3600", "5.0", "3.0",
                                  "50000", "9999", "0.2", "0.5")))

    _ClampBox.calls.clear()
    _ClampBox.answer = _ClampBox.StandardButton.Ok
    dlg.accept()
    check("proceeding saves the clamped values",
          len(_ClampBox.calls) == 1
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").cooldown_s == 3600.0)

    t = Trigger(name="windows dropped", log_type="21", ability_id="A55B",
                cooldown_s=7200.0, duration_max=50000.0)
    dlg = TriggerDialog(trigger=t)
    _ClampBox.calls.clear()
    _ClampBox.answer = None
    dlg.accept()
    text = _ClampBox.calls[0][2] if _ClampBox.calls else ""
    check("duration the type drops on save is not listed",
          len(_ClampBox.calls) == 1 and "7200" in text and "50000" not in text)

    t = Trigger(name="fine", log_type="21", ability_id="A55B",
                cooldown_s=30.0, speed=1.5)
    dlg = TriggerDialog(trigger=t)
    _ClampBox.calls.clear()
    dlg.accept()
    check("in range values save with no warning",
          not _ClampBox.calls and dlg.result() == QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a piped 26|30 trigger keeps its status fields through the dialog ──────
t = Trigger(name="piped", log_type="26|30", duration_min=1.5, duration_max=12.0,
            count_min=1, count_max=3, status_scope="any", expiry_warn_s=4.5)
dlg = TriggerDialog(trigger=t)
check("piped 26|30 shows the duration row", not dlg._dur_row.isHidden())
check("piped 26|30 shows the reapply warning row", not dlg._warn_row.isHidden())
out = dlg.get_trigger("x")
check("piped 26|30 keeps the duration window",
      out.duration_min == 1.5 and out.duration_max == 12.0)
check("piped 26|30 keeps the stacks window",
      out.count_min == 1 and out.count_max == 3)
check("piped 26|30 keeps the scope", out.status_scope == "any")
check("piped 26|30 keeps the expiry warning", out.expiry_warn_s == 4.5)

# ── editing a clamped spin afterwards clears the pending clamp ────────────
t = Trigger(name="clamp cleared", log_type="21", cooldown_s=7200.0)
dlg = TriggerDialog(trigger=t)
check("clamped cooldown load is pending", len(dlg._pending_clamps()) == 1)
dlg._cooldown.setValue(30.0)
check("editing a clamped spin clears the pending clamp",
      dlg._pending_clamps() == [])

t = Trigger(name="step clamp cleared", log_type="21",
            sequence=[{"log_type": "21", "timeout_s": 0.2}])
dlg = TriggerDialog(trigger=t)
check("clamped step timeout load is pending", len(dlg._pending_clamps()) == 1)
dlg._sequence._rows[0]._timeout.setValue(5.0)
check("editing a clamped step timeout clears the pending clamp",
      dlg._pending_clamps() == []
      and dlg._sequence._rows[0]._timeout_clamped is None)

# ── step timeout default matches the 10s runtime fallback ─────────────────
row = trigger_dialog._StepRow()
check("new step row defaults to the 10s runtime timeout",
      row._timeout.value() == 10.0)
row = trigger_dialog._StepRow(data={"log_type": "21"})
check("step without a saved timeout loads as 10s",
      row._timeout.value() == 10.0)

# nan and inf literals from hand-edited JSON load as the 10s default, not a
# clamped spinbox value with a pending clamp warning.
row = trigger_dialog._StepRow(data={"log_type": "21", "timeout_s": "nan"})
check("nan step timeout loads as 10s",
      row._timeout.value() == 10.0 and row._timeout_clamped is None)
row = trigger_dialog._StepRow(data={"log_type": "21", "timeout_s": "inf"})
check("infinite step timeout loads as 10s",
      row._timeout.value() == 10.0 and row._timeout_clamped is None)

# ── accept() refuses a step ability regex the engine can't compile ────────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._sequence._add_row({"log_type": "21", "ability_regex": "["})
    _WarnBox.calls.clear()
    dlg.accept()
    text = _WarnBox.calls[0][2] if _WarnBox.calls else ""
    check("uncompilable step regex cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)
    check("step regex warning names the step", "step 1" in text)

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._sequence._add_row({"log_type": "21", "ability_regex": "Exaflare"})
    _WarnBox.calls.clear()
    dlg.accept()
    check("valid step regex saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._sequence._add_row({"log_type": "21"})
    _WarnBox.calls.clear()
    dlg.accept()
    check("blank step regex saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── accept() refuses a custom log type the engine can never match ─────────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText("2O")
    _WarnBox.calls.clear()
    dlg.accept()
    check("letter lookalike custom type cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText("２６")   # full-width digits
    _WarnBox.calls.clear()
    dlg.accept()
    check("full-width digit custom type cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText("")
    _WarnBox.calls.clear()
    dlg.accept()
    check("blank custom type cannot silently save as 20",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText(" 26|30 ")
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("piped custom type saves, stripped",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").log_type == "26|30")
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── accept() refuses windows whose min tops the max, dead on save ─────────
trigger_dialog.QMessageBox = _WarnBox
try:
    t = Trigger(name="bad duration window", log_type="26",
                duration_min=30.0, duration_max=10.0)
    dlg = TriggerDialog(trigger=t)
    _WarnBox.calls.clear()
    dlg.accept()
    check("duration min above max cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    t = Trigger(name="bad stacks window", log_type="26",
                count_min=5, count_max=2)
    dlg = TriggerDialog(trigger=t)
    _WarnBox.calls.clear()
    dlg.accept()
    check("stacks min above max cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    t = Trigger(name="open ended window", log_type="26", ability_id="A55B",
                duration_min=5.0, duration_max=0.0, count_min=2, count_max=0)
    dlg = TriggerDialog(trigger=t)
    _WarnBox.calls.clear()
    dlg.accept()
    check("zero max means no upper bound and saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    t = Trigger(name="equal window", log_type="26", ability_id="A55B",
                duration_min=8.0, duration_max=8.0)
    dlg = TriggerDialog(trigger=t)
    _WarnBox.calls.clear()
    dlg.accept()
    check("equal min and max saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    t = Trigger(name="windows on ability type", log_type="21", ability_id="A55B",
                duration_min=30.0, duration_max=10.0)
    dlg = TriggerDialog(trigger=t)
    _WarnBox.calls.clear()
    dlg.accept()
    check("window values the type drops on save are not gated",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── accept() refuses a malformed ability id, same silent death class ──────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._ability_id.setText("A5SB")   # S is not a hex digit
    _WarnBox.calls.clear()
    dlg.accept()
    text = _WarnBox.calls[0][2] if _WarnBox.calls else ""
    check("non-hex ability id cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)
    check("ability id warning says hex", "hex" in text)

    dlg = new_dialog()
    dlg._ability_id.setText("A55D|a55e")   # piped, lowercase is fine
    _WarnBox.calls.clear()
    dlg.accept()
    check("piped hex ability id saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._sequence._add_row({"log_type": "21", "ability_id": "A5SB"})
    _WarnBox.calls.clear()
    dlg.accept()
    text = _WarnBox.calls[0][2] if _WarnBox.calls else ""
    check("non-hex step ability id cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)
    check("step id warning names the step", "step 1" in text)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── accept() refuses a custom type that is not exactly 2 digits ───────────
trigger_dialog.QMessageBox = _WarnBox
try:
    for bad in ("0", "026"):
        dlg = new_dialog()
        dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
        dlg._type_custom.setText(bad)
        dlg._ability_id.setText("A55B")
        _WarnBox.calls.clear()
        dlg.accept()
        check(f"wrong-width custom type {bad!r} cannot be saved",
              len(_WarnBox.calls) == 1
              and dlg.result() != QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── accept() refuses a trigger with no matcher at all, a spam trigger ─────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    _WarnBox.calls.clear()
    dlg.accept()
    check("blank id and blank regex cannot be saved",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._regex.setText("Exaflare")
    _WarnBox.calls.clear()
    dlg.accept()
    check("regex alone saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("ability id alone saves",
          not _WarnBox.calls and dlg.result() == QDialog.DialogCode.Accepted)
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a stale id in a greyed box never reaches a saved trigger ──────────────
trigger_dialog.QMessageBox = _WarnBox
try:
    idx_00 = next(i for i, (_lbl, val) in enumerate(trigger_dialog._LOG_TYPES)
                  if val == "00")
    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._type_combo.setCurrentIndex(idx_00)
    check("00 chat type greys the ability id box",
          not dlg._ability_id.isEnabled())
    _WarnBox.calls.clear()
    dlg.accept()
    check("a stale id is no matcher, the refusal still fires",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg._regex.setText("Exaflare")
    _WarnBox.calls.clear()
    dlg.accept()
    check("a stale id drops on save, the regex stays the matcher",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").log_type == "00"
          and dlg.get_trigger("x").ability_id == ""
          and dlg.get_trigger("x").ability_regex == "Exaflare")

    dlg = new_dialog()
    dlg._ability_id.setText("A5SB")   # malformed, greyed out after the switch
    dlg._type_combo.setCurrentIndex(idx_00)
    dlg._regex.setText("Exaflare")
    _WarnBox.calls.clear()
    dlg.accept()
    check("a malformed stale id drops on save instead of blocking it",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").ability_id == "")

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("a type with an id field keeps the id",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").ability_id == "A55B")
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a policy-rejected regex is not reported as a syntax error ─────────────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._regex.setText("(.*)*x")   # compiles fine, the ReDoS heuristic refuses it
    _WarnBox.calls.clear()
    dlg.accept()
    check("catastrophic ability regex is called unsafe, not invalid",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Unsafe regex"
          and "potentially catastrophic" in _WarnBox.calls[0][2]
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._regex.setText("[")
    _WarnBox.calls.clear()
    dlg.accept()
    check("malformed ability regex is still called invalid",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Invalid regex"
          and "does not compile" in _WarnBox.calls[0][2]
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._zone.setText("(.*)*x")
    _WarnBox.calls.clear()
    dlg.accept()
    check("catastrophic zone regex is called unsafe, not invalid",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Unsafe regex"
          and "potentially catastrophic" in _WarnBox.calls[0][2])

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._zone.setText("[")
    _WarnBox.calls.clear()
    dlg.accept()
    check("malformed zone regex is still called invalid",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Invalid regex"
          and "does not compile" in _WarnBox.calls[0][2])

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._sequence._add_row({"log_type": "21", "ability_regex": "(.*)*x"})
    _WarnBox.calls.clear()
    dlg.accept()
    check("catastrophic step regex is called unsafe and names the step",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Unsafe regex"
          and "potentially catastrophic" in _WarnBox.calls[0][2]
          and "step 1" in _WarnBox.calls[0][2])

    dlg = new_dialog()
    dlg._ability_id.setText("A55B")
    dlg._sequence._add_row({"log_type": "21", "ability_regex": "["})
    _WarnBox.calls.clear()
    dlg.accept()
    check("malformed step regex is still called invalid",
          len(_WarnBox.calls) == 1
          and _WarnBox.calls[0][1] == "Invalid regex"
          and "does not compile" in _WarnBox.calls[0][2])
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a mixed pipe with a chat half drops the id like a plain 00 type ───────
trigger_dialog.QMessageBox = _WarnBox
try:
    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText("00|21")
    check("00|21 greys the ability id box",
          not dlg._ability_id.isEnabled())
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("a stale id on 00|21 is no matcher, the refusal still fires",
          len(_WarnBox.calls) == 1
          and dlg.result() != QDialog.DialogCode.Accepted)

    dlg._regex.setText("Exaflare")
    _WarnBox.calls.clear()
    dlg.accept()
    check("a stale id on 00|21 drops on save, the regex stays the matcher",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").log_type == "00|21"
          and dlg.get_trigger("x").ability_id == ""
          and dlg.get_trigger("x").ability_regex == "Exaflare")

    dlg = new_dialog()
    dlg._type_combo.setCurrentIndex(trigger_dialog._CUSTOM_IDX)
    dlg._type_custom.setText("21|22")
    dlg._ability_id.setText("A55B")
    _WarnBox.calls.clear()
    dlg.accept()
    check("an all-indexed pipe keeps the id",
          not _WarnBox.calls
          and dlg.result() == QDialog.DialogCode.Accepted
          and dlg.get_trigger("x").ability_id == "A55B")
finally:
    trigger_dialog.QMessageBox = _real_qmessagebox

# ── a mixed 26|21 pipe drops the reapply warning the dialog once saved ────
t = Trigger(name="mixed pipe", log_type="26|21", ability_id="A55B",
            expiry_warn_s=4.5)
dlg = TriggerDialog(trigger=t)
check("26|21 hides the reapply warning row", dlg._warn_row.isHidden())
check("26|21 drops the reapply warning on save",
      dlg.get_trigger("x").expiry_warn_s == 0.0)

t = Trigger(name="clamped mixed", log_type="26|21", ability_id="A55B",
            expiry_warn_s=90.0)
dlg = TriggerDialog(trigger=t)
check("a clamped warning a 26|21 type drops on save is not pending",
      dlg._pending_clamps() == [])

t = Trigger(name="clamped status", log_type="26|30", ability_id="A55B",
            expiry_warn_s=90.0)
dlg = TriggerDialog(trigger=t)
check("a clamped warning a 26|30 type keeps stays pending",
      dlg._pending_clamps() == [("Reapply warning", 90.0, 60.0)])

# ── _regex_syntax_error diagnoses with the engine's own compiler ──────────
# The helper mirrors compile_user_regex's broad catch. A deeply nested
# pattern over the length cap raises RecursionError in a plain compile,
# which escaped the helper and aborted the app from the Save path.
check("a deeply nested pattern is a refusal, not a crash",
      trigger_dialog._regex_syntax_error("(" * 500 + "a" + ")" * 500) is True)
check("a genuinely malformed pattern is a syntax error",
      trigger_dialog._regex_syntax_error("(") is True)
if trigger_dialog._HAVE_REGEX:
    # stdlib re rejects syntax the regex module accepts, like \p, so a
    # policy refusal of one was mislabeled "does not compile".
    check("regex module only syntax is not mislabeled a syntax error",
          trigger_dialog._regex_syntax_error("(?:\\p{L}+)+") is False)

# ── a stray pipe in the log type keeps the id and warn, same as from_dict ──
# The engine filters empty pipe parts at load, so a hand edited "21|" keeps
# its ability id. The dialog's parts computation must filter them the same
# way or editing such a trigger greys the id box and blanks the id on save.
t = Trigger(name="stray pipe", log_type="21|", ability_id="A55B")
dlg = TriggerDialog(trigger=t)
check("a stray pipe keeps the ability id field live",
      dlg._ability_id.isEnabled())
check("a stray pipe saves the ability id",
      dlg.get_trigger("x").ability_id == "A55B")

t = Trigger(name="stray status pipe", log_type="26|", ability_id="A1B2",
            expiry_warn_s=4.5)
dlg = TriggerDialog(trigger=t)
check("a stray pipe on 26 keeps the reapply warning row visible",
      not dlg._warn_row.isHidden())
check("a stray pipe on 26 saves the reapply warning",
      dlg.get_trigger("x").expiry_warn_s == 4.5)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
