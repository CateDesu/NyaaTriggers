from copy import deepcopy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QGroupBox, QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox

from nyaatriggers.ui.native_automarkers import NativeAutomarkersPanel


_QT_APP = None


def native_inventory():
    jobs = ["DRG", "WAR", "SCH"]
    markers = ([f"ATTACK{number}" for number in range(1, 9)]
               + [f"BIND{number}" for number in range(1, 4)]
               + ["IGNORE1", "IGNORE2", "CIRCLE", "CROSS", "SQUARE", "TRIANGLE",
                  "ATTACK_NEXT", "BIND_NEXT", "IGNORE_NEXT", "CLEAR"])
    settings = []

    def add(ident, kind, value, **extra):
        settings.append({"id": ident, "type": kind, "label": ident,
                         "value": deepcopy(value), "default": deepcopy(value), **extra})

    for ident, value in (("uwu.enabled", True), ("uwu.override_zone_lock", False),
                         ("dsr.p5.enabled", False), ("dsr.p6.enabled", False),
                         ("dsr.p6.rot_priority", True), ("dsr.p6.reverse_priority", True)):
        add(ident, "boolean", value)
    add("uwu.priority", "jobs", jobs, effective_order=jobs)
    add("uwu.clear_delay_ms", "integer", 10000)
    add("dsr.p6.priority", "jobs", jobs, effective_order=jobs)
    add("top.priority", "jobs", jobs, effective_order=jobs)
    for mechanic in ("looper", "panto", "ps", "sniper", "monitor", "delta", "sigma", "omega"):
        add(f"top.{mechanic}.enabled", "boolean", False)
    add("top.omega.first_enabled", "boolean", True)
    add("top.omega.second_enabled", "boolean", True)
    for mechanic in ("p1", "ps", "sniper", "monitor", "sigma", "omega"):
        add(f"top.{mechanic}.priority_override", "boolean", False)
        add(f"top.{mechanic}.priority", "jobs", list(reversed(jobs)), parent="top.priority",
            override_enabled=f"top.{mechanic}.priority_override", effective_order=jobs)
    for ident, value, maximum in (("top.sigma.delay_seconds", 0, 50),
                                  ("top.omega.first_delay_seconds", 1, 28),
                                  ("top.omega.second_delay_seconds", 0, 20),
                                  ("telesto.delay_base_ms", 100, 5000),
                                  ("telesto.delay_plus_ms", 100, 5000)):
        add(ident, "integer", value, min=0, max=maximum)
    map_slots = {
        "dsr.p6.markers": ["Spread_1", "Spread_2", "Spread_3", "Spread_4", "Stack_Buff_1", "Stack_Buff_2", "Nothing_1", "Nothing_2"],
        "top.p1.markers": [f"GROUP{group}_NUM{number}" for group in (1, 2) for number in range(1, 5)],
        "top.ps.mid_markers": [f"GROUP{group}_{shape}" for group in (1, 2) for shape in ("CIRCLE", "TRIANGLE", "SQUARE", "X")],
        "top.ps.far_markers": [f"GROUP{group}_{shape}" for group in (1, 2) for shape in ("CIRCLE", "TRIANGLE", "SQUARE", "X")],
        "top.sniper.markers": ["SPREAD_1", "SPREAD_2", "SPREAD_3", "SPREAD_4", "STACK_1", "STACK_2", "NOTHING_1", "NOTHING_2"],
        "top.delta.markers": ["NearWorld", "DistantWorld"],
        "top.sigma.markers": ["NearWorld", "DistantWorld", "OneStack1", "OneStack2", "OneStack3", "OneStack4", "Remaining1", "Remaining2"],
        "top.omega.markers": ["NearWorld", "DistantWorld", "Baiter1", "Baiter2", "Baiter3", "Baiter4", "Remaining1", "Remaining2"],
    }
    for ident, slots in map_slots.items():
        value = {slot: {"enabled": True, "marker": markers[index]} for index, slot in enumerate(slots)}
        preset = {slot: {"enabled": index % 2 == 0, "marker": "CLEAR" if index == 0 else "BIND_NEXT"}
                  for index, slot in enumerate(slots)}
        add(ident, "marker_map", value, slots=[{"id": slot, "label": slot} for slot in slots],
            presets=[{"name": "Defaults", "value": value}, {"name": "Alternate", "value": preset}])
    add("native_umad", "boolean", True, managed="frontend")
    mechanics = []

    def mechanic(ident, fight, controls):
        mechanics.append({"id": ident, "fight": fight, "name": ident,
                          "settings": controls + ["telesto.delay_base_ms", "telesto.delay_plus_ms"]})

    mechanic("uwu.gaols", "UWU", ["uwu.enabled", "uwu.override_zone_lock", "uwu.priority", "uwu.clear_delay_ms"])
    mechanic("dsr.p5", "DSR", ["dsr.p5.enabled"])
    mechanic("dsr.p6", "DSR", ["dsr.p6.enabled", "dsr.p6.rot_priority", "dsr.p6.reverse_priority", "dsr.p6.priority", "dsr.p6.markers"])
    for ident, priority, maps in (("looper", "p1", ["top.p1.markers"]),
                                ("panto", "p1", ["top.p1.markers"]),
                                ("ps", "ps", ["top.ps.mid_markers", "top.ps.far_markers"]),
                                ("sniper", "sniper", ["top.sniper.markers"]),
                                ("monitor", "monitor", []),
                                ("delta", None, ["top.delta.markers"]),
                                ("sigma", "sigma", ["top.sigma.markers"])):
        controls = ["top.priority", f"top.{ident}.enabled"] + maps
        if priority:
            controls += [f"top.{priority}.priority_override", f"top.{priority}.priority"]
        if ident == "sigma":
            controls.append("top.sigma.delay_seconds")
        mechanic(f"top.{ident}", "TOP", controls)
    for part in ("first", "second"):
        mechanic(f"top.omega.{part}", "TOP", ["top.priority", "top.omega.enabled", f"top.omega.{part}_enabled",
                                                   f"top.omega.{part}_delay_seconds", "top.omega.priority_override",
                                                   "top.omega.priority", "top.omega.markers"])
    mechanic("umad.p4", "UMAD", ["native_umad"])
    return {"t": "automark_inventory", "settings": settings, "mechanics": mechanics,
            "jobs": [{"id": job, "label": job} for job in jobs],
            "markers": [{"id": marker, "label": marker} for marker in markers]}


class NativeAutomarkersUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _QT_APP
        _QT_APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = NativeAutomarkersPanel()
        self.inventory = native_inventory()
        self.changes = []
        self.panel.changed.connect(lambda ident, value: self.changes.append((ident, value)))
        self.panel.set_inventory(self.inventory, {}, True, False)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        _QT_APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.panel))

    def control(self, cls, name):
        widget = self.panel.findChild(cls, name)
        self.assertIsNotNone(widget, name)
        return widget

    def test_all_native_controls_render_once_without_changes(self):
        self.assertEqual(len(self.inventory["settings"]), 46)
        self.assertEqual(len(self.inventory["mechanics"]), 13)
        self.assertEqual(self.panel._tabs.count(), 4)
        self.assertEqual([self.panel._tabs.tabText(index) for index in range(4)], ["UWU", "DSR", "TOP", "UMAD"])
        self.assertEqual(set(self.panel._editors), {setting["id"] for setting in self.inventory["settings"]})
        self.assertEqual(len([setting for setting in self.inventory["settings"] if setting["type"] == "marker_map"]), 8)
        self.assertEqual(len([group for group in self.panel.findChildren(QGroupBox)
                              if group.objectName().startswith("native_mechanic.")]), 13)
        self.assertEqual(len(self.panel.findChildren(QSpinBox, "telesto.delay_base_ms.value")), 1)
        self.assertEqual(self.changes, [])

    def test_engine_status_keeps_controls_editable_and_explains_saved_changes(self):
        self.panel.set_engine_status("Sidecar exited")
        self.assertTrue(self.panel.isEnabled())
        self.assertIn("Sidecar exited", self.panel._engine_note.text())
        self.assertIn("Changes are saved", self.panel._engine_note.text())
        self.control(QCheckBox, "top.looper.enabled.value").click()
        self.assertEqual(self.changes, [("top.looper.enabled", True)])
        self.panel.set_engine_status("")
        self.assertTrue(self.panel._engine_note.isHidden())

    def test_saved_values_and_requested_umad_do_not_emit(self):
        overrides = {"top.looper.enabled": True, "top.sigma.delay_seconds": 17, "native_umad": True}
        self.panel.set_inventory(self.inventory, overrides, False, True)
        self.assertTrue(self.control(QCheckBox, "top.looper.enabled.value").isChecked())
        self.assertEqual(self.control(QSpinBox, "top.sigma.delay_seconds.value").value(), 17)
        self.assertFalse(self.control(QCheckBox, "native_umad.value").isChecked())
        self.assertFalse(self.panel._umad_note.isHidden())
        self.assertEqual(self.changes, [])
        self.panel.set_umad_state(True, False)
        self.assertTrue(self.control(QCheckBox, "native_umad.value").isChecked())
        self.assertTrue(self.panel._umad_note.isHidden())
        self.assertEqual(self.changes, [])

    def test_boolean_and_bounded_integer_emit_typed_ids(self):
        self.control(QCheckBox, "top.looper.enabled.value").click()
        spin = self.control(QSpinBox, "top.sigma.delay_seconds.value")
        self.assertEqual((spin.minimum(), spin.maximum()), (0, 50))
        spin.setValue(18)
        self.assertEqual(self.changes, [("top.looper.enabled", True), ("top.sigma.delay_seconds", 18)])
        self.assertIs(type(self.changes[-1][1]), int)

    def test_unbounded_long_retains_large_value_and_rejects_invalid_edits(self):
        large = (1 << 40) + 123
        self.panel.set_inventory(self.inventory, {"uwu.clear_delay_ms": large}, True, False)
        edit = self.control(QLineEdit, "uwu.clear_delay_ms.value")
        self.assertEqual(edit.text(), str(large))
        edit.setText("-150")
        edit.editingFinished.emit()
        self.assertEqual(self.changes, [("uwu.clear_delay_ms", -150)])
        for invalid in ("1.5", "bad", str(1 << 63)):
            edit.setText(invalid)
            edit.editingFinished.emit()
            self.assertEqual(edit.text(), "-150")
            self.assertTrue(edit.toolTip())
        self.assertEqual(len(self.changes), 1)

    def test_map_edits_emit_complete_slot_values_and_retain_disabled_sign(self):
        ident = "top.delta.markers"
        marker = self.control(QComboBox, ident + ".NearWorld.marker")
        self.assertGreaterEqual(marker.findData("IGNORE_NEXT"), 0)
        self.assertGreaterEqual(marker.findData("CLEAR"), 0)
        marker.setCurrentIndex(marker.findData("IGNORE_NEXT"))
        enabled = self.control(QCheckBox, ident + ".NearWorld.enabled")
        enabled.click()
        self.assertFalse(marker.isEnabled())
        self.assertEqual(self.changes[-1], (ident, {"NearWorld": {"enabled": False, "marker": "IGNORE_NEXT"},
                                                  "DistantWorld": {"enabled": True, "marker": "ATTACK2"}}))

    def test_presets_and_resets_emit_one_complete_change(self):
        ident = "top.sigma.markers"
        presets = self.control(QComboBox, ident + ".preset")
        preset = presets.itemData(2)
        presets.setCurrentIndex(2)
        presets.activated.emit(2)
        self.assertEqual(self.changes, [(ident, preset)])
        self.assertEqual(self.control(QComboBox, ident + ".NearWorld.marker").currentData(), "CLEAR")
        self.assertFalse(self.control(QCheckBox, ident + ".DistantWorld.enabled").isChecked())
        self.control(QPushButton, ident + ".reset").click()
        default = next(setting["default"] for setting in self.inventory["settings"] if setting["id"] == ident)
        self.assertEqual(self.changes[-1], (ident, default))
        self.assertEqual(len(self.changes), 2)
        self.control(QPushButton, ident + ".reset").click()
        self.assertEqual(len(self.changes), 2)

    def test_disabled_priority_override_retains_own_order_and_parent_preview(self):
        ident = "top.ps.priority"
        jobs = self.control(QListWidget, ident + ".value")
        self.assertEqual([jobs.item(index).text() for index in range(jobs.count())], ["SCH", "WAR", "DRG"])
        self.assertFalse(jobs.isEnabled())
        self.assertIn("DRG, WAR, SCH", self.control(QLabel, ident + ".effective").text())
        self.control(QPushButton, ident + ".up").click()
        self.assertEqual(self.changes, [])
        override = self.control(QCheckBox, "top.ps.priority_override.value")
        override.click()
        jobs.setCurrentRow(1)
        self.control(QPushButton, ident + ".up").click()
        self.assertEqual(self.changes, [("top.ps.priority_override", True), (ident, ["WAR", "SCH", "DRG"])])
        override.click()
        self.assertEqual([jobs.item(index).text() for index in range(jobs.count())], ["WAR", "SCH", "DRG"])
        self.assertIn("DRG, WAR, SCH", self.control(QLabel, ident + ".effective").text())
        parent = self.control(QListWidget, "top.priority.value")
        parent.setCurrentRow(2)
        self.control(QPushButton, "top.priority.up").click()
        self.assertIn("DRG, SCH, WAR", self.control(QLabel, ident + ".effective").text())
        self.assertEqual([jobs.item(index).text() for index in range(jobs.count())], ["WAR", "SCH", "DRG"])

    def test_refresh_preserves_selected_fight_and_job_without_signals(self):
        self.panel._tabs.setCurrentIndex(2)
        jobs = self.control(QListWidget, "top.priority.value")
        jobs.setCurrentRow(1)
        self.control(QPushButton, "top.priority.up").click()
        values = {ident: deepcopy(value) for ident, value in self.changes}
        self.panel.set_inventory(self.inventory, values, True, False)
        self.assertEqual(self.panel._tabs.tabText(self.panel._tabs.currentIndex()), "TOP")
        self.assertEqual(self.control(QListWidget, "top.priority.value").currentItem().text(), "WAR")
        self.assertEqual(self.changes, [("top.priority", ["WAR", "DRG", "SCH"])])

    def test_full_inventory_recovers_after_empty_controls_are_deleted(self):
        previous_tabs = self.panel._tabs
        empty = {"t": "automark_inventory", "version": 1, "settings": [],
                 "mechanics": [], "jobs": [], "markers": []}
        self.panel.set_inventory(empty, {}, True, False)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.panel._editors, {})
        self.panel.set_umad_state(False, True)
        self.panel.set_inventory(self.inventory, {"top.looper.enabled": True}, False, True)
        self.assertIsNot(self.panel._tabs, previous_tabs)
        self.assertEqual(self.panel._tabs.count(), 4)
        self.assertEqual(len(self.panel._editors), 46)
        self.assertTrue(self.control(QCheckBox, "top.looper.enabled.value").isChecked())
        self.assertFalse(self.control(QCheckBox, "native_umad.value").isChecked())
        self.assertFalse(self.panel._umad_note.isHidden())
        self.assertEqual(self.changes, [])
        self.control(QCheckBox, "top.looper.enabled.value").click()
        self.assertEqual(self.changes, [("top.looper.enabled", False)])

    def test_invalid_saved_preferences_fall_back_without_autosaving(self):
        bad_map = {"NearWorld": {"enabled": True, "marker": ["ATTACK1"]},
                   "DistantWorld": {"enabled": True, "marker": "ATTACK2"}}
        self.panel.set_inventory(self.inventory, {"top.looper.enabled": "true", "top.sigma.delay_seconds": True,
                                                  "top.priority": ["DRG", "DRG", "SCH"], "top.delta.markers": bad_map}, True, False)
        self.assertFalse(self.control(QCheckBox, "top.looper.enabled.value").isChecked())
        self.assertEqual(self.control(QSpinBox, "top.sigma.delay_seconds.value").value(), 0)
        self.assertEqual(self.control(QListWidget, "top.priority.value").item(1).text(), "WAR")
        self.assertEqual(self.control(QComboBox, "top.delta.markers.NearWorld.marker").currentData(), "ATTACK1")
        self.assertEqual(self.changes, [])

    def test_error_is_plain_text_and_clears_on_valid_inventory(self):
        payload = deepcopy(self.inventory)
        payload["error"] = "Rejected marker <bad>"
        self.panel.set_inventory(payload, {}, True, False)
        error = self.control(QLabel, "native_automarker_error")
        self.assertEqual(error.text(), "Rejected marker <bad>")
        self.assertEqual(error.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(error.isHidden())
        self.panel.set_inventory(self.inventory, {}, True, False)
        self.assertTrue(self.control(QLabel, "native_automarker_error").isHidden())
        self.assertEqual(self.changes, [])

    def test_incomplete_metadata_does_not_hide_valid_or_unreferenced_controls(self):
        payload = deepcopy(self.inventory)
        payload["settings"].extend([None, {"id": "bad", "type": "unsupported", "value": {}}, payload["settings"][0]])
        payload["mechanics"][0]["settings"].extend([{}, "unknown"])
        payload["mechanics"] = payload["mechanics"][:-1]
        self.panel.set_inventory(payload, {}, True, False)
        self.assertEqual(len(self.panel._editors), 46)
        self.assertIsNotNone(self.control(QCheckBox, "native_umad.value"))
        self.assertEqual(self.changes, [])

    def test_changes_do_not_mutate_native_inventory_or_panel_values(self):
        before = deepcopy(self.inventory)
        marker = self.control(QComboBox, "top.delta.markers.NearWorld.marker")
        marker.setCurrentIndex(marker.findData("CROSS"))
        self.changes[-1][1]["NearWorld"]["marker"] = "BIND1"
        self.assertEqual(self.panel._values["top.delta.markers"]["NearWorld"]["marker"], "CROSS")
        self.assertEqual(self.inventory, before)


if __name__ == "__main__":
    unittest.main()
