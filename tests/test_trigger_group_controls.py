from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QStyle, QStyleOptionButton

from nyaatriggers import app_common as ac
from nyaatriggers import main_window as mw
from nyaatriggers.locale_util import set_locale
from nyaatriggers.telesto_client import TelestoClient
from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.triggernometry_bridge import TriggernometryBridge
from nyaatriggers.ui.settings_tab import SettingsTabMixin
from tests import test_session_ui as fixture


class TriggerGroupControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionUiTests.setUpClass()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = fixture.SessionUiTests()
        self.addCleanup(self.case.doCleanups)
        self.stack.enter_context(patch.object(TelestoClient, "start"))
        self.case.setUp()
        self.window = self.case.window
        self.window._triggers = [
            Trigger(id="local-umad", name="Local UMAD", fight="UMAD", zone_regex="."),
            Trigger(id="local-m1s", name="Local M1S", fight="M1S", zone_regex="."),
        ]
        self.window._local_ids = {trigger.id for trigger in self.window._triggers}
        self.window._official_ids = set()
        self.window._official_triggers = {}
        self.window._deleted_ids = set()
        self.window._local_trigger_baseline = None
        self.window._folders = []
        self.window._custom_triggevent = []
        self.window._engine_inventory = [
            self.engine("triggevent", "tv-umad", "UMAD"),
            self.engine("triggevent", "tv-m1s", "M1S"),
            self.engine("triggernometry", "tn-umad#0", "UMAD"),
            self.engine("triggernometry", "tn-umad#1", "UMAD"),
            self.engine("triggernometry", "tn-m1s#0", "M1S"),
        ]
        for disabled in self.window._engine_disabled.values():
            disabled.clear()
        self.window._triggernometry_callout_edits["tn-umad#0"] = "Custom ${_me.name}"
        self.window._settings["triggernometry_callout_edits"] = self.window._triggernometry_callout_edits
        self.window._src_collapsed = {}
        self.window._refresh_table()
        self.select("UMAD")

    @staticmethod
    def engine(source, ident, fight):
        return {"source": source, "id": ident, "fight": fight, "name": ident,
                "group": "", "text": "Original " + ident}

    def select(self, fight, kind=None):
        self.assertTrue(self.window._restore_tree_selection(fight, kind))
        self.window._apply_tab_filter()
        self.window._update_fight_controls()

    def settings(self):
        return json.loads((self.case.temp / "settings.json").read_text())

    def test_compact_table_keeps_ability_type_source_and_fight_search(self):
        self.window._triggers[0].ability_id = "ABCD"
        self.window._refresh_table()
        for query, expected in (
            ("ABCD", {"local-umad"}),
            ("20", {"local-umad", "local-m1s"}),
            ("Triggevent", {"triggevent:tv-umad", "triggevent:tv-m1s"}),
            ("M1S", {"local-m1s", "triggevent:tv-m1s", "triggernometry:tn-m1s#0"}),
        ):
            with self.subTest(query=query):
                self.window._search_edit.setText(query)
                self.window._apply_tab_filter()
                visible = {self.window._table.item(row, ac._C_EN).data(Qt.ItemDataRole.UserRole)
                           for row in range(self.window._table.rowCount())
                           if not self.window._table.isRowHidden(row)}
                self.assertEqual(visible, expected)
        self.window._search_edit.clear()
        self.select("UMAD")
        self.assertFalse(self.window._table.isColumnHidden(0))
        self.assertEqual(self.window._table.columnCount(), 4)

    def test_source_group_clicks_and_rebuilds_keep_trigger_cells_independent(self):
        self.window.show()
        self.case.app.processEvents()
        table = self.window._table

        def row(key):
            return next(index for index in range(table.rowCount())
                        if table.item(index, ac._C_EN).data(Qt.ItemDataRole.UserRole) == key)

        for key, child in (("local", "local-umad"), ("engine", "triggevent:tv-umad"),
                           ("triggernometry", "triggernometry:tn-umad#0")):
            header = table.item(row("__hdr__:" + key), ac._C_NAME)
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                             pos=table.visualItemRect(header).center())
            self.assertTrue(table.isRowHidden(row(child)))
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                             pos=table.visualItemRect(header).center())
            self.assertFalse(table.isRowHidden(row(child)))
        self.window._triggers.insert(0, Trigger(id="extra", name="An extra trigger", fight="UMAD"))
        self.window._refresh_table()
        self.select("UMAD")
        self.case.app.processEvents()
        for key in ("extra", "local-umad", "triggevent:tv-umad", "triggernometry:tn-umad#0"):
            index = row(key)
            self.assertEqual(table.columnSpan(index, ac._C_NAME), 1)
            self.assertTrue(table.item(index, ac._C_NAME).text())
        cell = table.item(row("local-umad"), ac._C_EN)
        cell.setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(next(trigger for trigger in self.window._triggers if trigger.id == "local-umad").enabled)

    def test_triggernometry_only_fight_exposes_a_working_checkbox(self):
        self.window._triggers = []
        self.window._engine_inventory = [self.engine("triggernometry", "tn-umad#0", "UMAD")]
        self.window._refresh_table()
        self.select("UMAD")
        self.window.show()
        self.case.app.processEvents()
        self.assertFalse(self.window._fight_bar.isHidden())
        self.assertTrue(self.window._cb_tn.isEnabled())
        self.assertTrue(self.window._cb_tn.isChecked())
        self.assertFalse(self.window._cb_local.isEnabled())
        self.assertFalse(self.window._cb_tv.isEnabled())
        QTest.mouseClick(self.window._cb_tn, Qt.MouseButton.LeftButton)
        self.assertFalse(self.window._cb_tn.isChecked())
        self.assertEqual(self.window._engine_disabled["triggernometry"], {"tn-umad#0"})
        self.assertEqual(self.settings()["triggernometry_disabled_triggers"], ["tn-umad#0"])
        QTest.mouseClick(self.window._cb_tn, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window._cb_tn.isChecked())
        self.assertEqual(self.window._engine_disabled["triggernometry"], set())

    def test_global_and_fight_controls_fit_the_minimum_window_in_both_languages(self):
        load_settings = mw.MainWindow._load_settings
        self.addCleanup(set_locale, "en")
        for language in ("en", "ja"):
            def localized_settings(window):
                load_settings(window)
                window._settings["ui_language"] = language

            with patch.object(mw.MainWindow, "_load_settings", localized_settings):
                window = mw.MainWindow()
            self.addCleanup(window._prog_sessions.close)
            self.addCleanup(window.close)
            for field in ("_triggers", "_local_ids", "_official_ids", "_official_triggers", "_folders",
                          "_custom_triggevent", "_engine_inventory", "_src_collapsed"):
                setattr(window, field, getattr(self.window, field).copy())
            window._refresh_table()
            self.assertTrue(window._restore_tree_selection("UMAD"))
            window._update_fight_controls()
            window.show()
            for width, height in ((900, 600), (1280, 800)):
                with self.subTest(language=language, size=(width, height)):
                    window.resize(width, height)
                    self.case.app.processEvents()
                    self.assertEqual((window.width(), window.height()), (width, height))
                    buttons = (window._add_btn, window._edit_btn, window._dup_btn, window._del_btn,
                               window._test_btn, window._disable_btn, window._enable_btn, window._global_tn_btn)
                    checks = (window._cb_local, window._cb_tv, window._cb_tn)
                    for control in (*buttons, *checks):
                        option = QStyleOptionButton()
                        option.initFrom(control)
                        option.text = control.text()
                        element = (QStyle.SubElement.SE_PushButtonContents if control in buttons
                                   else QStyle.SubElement.SE_CheckBoxContents)
                        contents = control.style().subElementRect(element, option, control)
                        self.assertTrue(control.isVisible())
                        self.assertGreaterEqual(contents.width(), control.fontMetrics().horizontalAdvance(control.text()),
                                                control.text())
                        self.assertTrue(control.parentWidget().rect().contains(control.geometry()), control.text())
                    for label in window._fight_bar.findChildren(QLabel):
                        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                        if not label.wordWrap():
                            self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(label.text()))
                        self.assertTrue(label.parentWidget().rect().contains(label.geometry()), label.text())

    def test_fight_checkbox_changes_only_its_triggernometry_actions_and_keeps_edits(self):
        self.window._cb_tn.click()
        self.assertEqual(self.window._engine_disabled["triggernometry"], {"tn-umad#0", "tn-umad#1"})
        self.assertEqual(self.window._engine_disabled["triggevent"], set())
        self.assertTrue(all(trigger.enabled for trigger in self.window._triggers))
        self.assertTrue(self.window._src_collapsed["triggernometry"])
        saved = self.settings()
        self.assertEqual(saved["triggernometry_callout_edits"]["tn-umad#0"], "Custom ${_me.name}")
        self.select("M1S")
        self.assertTrue(self.window._cb_tn.isChecked())
        self.select("UMAD")
        self.assertFalse(self.window._cb_tn.isChecked())
        self.window._cb_tn.click()
        self.assertEqual(self.window._engine_disabled["triggernometry"], set())
        self.assertFalse(self.window._src_collapsed["triggernometry"])
        self.assertEqual(self.settings()["triggernometry_callout_edits"], saved["triggernometry_callout_edits"])

    def test_mixed_fight_checkbox_enables_every_action_without_touching_other_fights(self):
        self.window._engine_disabled["triggernometry"].update({"tn-umad#1", "tn-m1s#0"})
        self.window._engine_disabled["triggevent"].add("tv-umad")
        self.window._update_fight_controls()
        self.assertFalse(self.window._cb_tn.isChecked())
        self.assertFalse(self.window._cb_tv.isChecked())
        self.assertTrue(self.window._cb_local.isChecked())
        self.window._cb_tn.click()
        self.assertTrue(self.window._cb_tn.isChecked())
        self.assertEqual(self.window._engine_disabled["triggernometry"], {"tn-m1s#0"})
        self.assertEqual(self.window._engine_disabled["triggevent"], {"tv-umad"})
        self.assertTrue(all(trigger.enabled for trigger in self.window._triggers))

    def test_global_triggernometry_toggle_spans_fights_and_retains_its_direction(self):
        self.window._global_tn_btn.click()
        self.assertTrue(self.window._global_tn_on_flag)
        self.window._global_tn_btn.click()
        all_tn = {"tn-umad#0", "tn-umad#1", "tn-m1s#0"}
        self.assertEqual(self.window._engine_disabled["triggernometry"], all_tn)
        self.assertFalse(self.settings()["global_tn_on"])
        key = "triggernometry:tn-umad#0"
        row = next(index for index in range(self.window._table.rowCount())
                   if self.window._table.item(index, ac._C_EN).data(Qt.ItemDataRole.UserRole) == key)
        self.window._table.item(row, ac._C_EN).setCheckState(Qt.CheckState.Checked)
        self.assertFalse(self.window._global_tn_on_flag)
        self.assertEqual(self.window._engine_disabled["triggernometry"], all_tn - {"tn-umad#0"})
        self.window._global_tn_btn.click()
        self.assertTrue(self.window._global_tn_on_flag)
        self.assertEqual(self.window._engine_disabled["triggernometry"], set())
        self.assertEqual(self.window._engine_disabled["triggevent"], set())
        self.assertTrue(all(trigger.enabled for trigger in self.window._triggers))
        self.assertEqual(self.settings()["triggernometry_callout_edits"]["tn-umad#0"], "Custom ${_me.name}")

    def test_saved_global_triggernometry_direction_survives_window_reload(self):
        self.window._global_tn_btn.click()
        self.assertTrue(self.settings()["global_tn_on"])
        with patch.object(mw.MainWindow, "_load_settings", SettingsTabMixin._load_settings):
            reloaded = mw.MainWindow()
        self.addCleanup(reloaded._prog_sessions.close)
        self.addCleanup(reloaded.close)
        self.assertTrue(reloaded._global_tn_on_flag)
        self.assertEqual(reloaded._triggernometry_callout_edits["tn-umad#0"], "Custom ${_me.name}")
        reloaded._global_tn_btn.click()
        self.assertFalse(self.settings()["global_tn_on"])

    def test_reset_clears_every_group_and_the_new_global_direction_without_losing_edits(self):
        self.window._global_tn_btn.click()
        self.window._reset_all_btn.click()
        self.assertFalse(self.window._global_tn_on_flag)
        self.assertFalse(self.settings()["global_tn_on"])
        self.assertEqual(self.window._engine_disabled["triggernometry"],
                         {"tn-umad#0", "tn-umad#1", "tn-m1s#0"})
        self.assertEqual(self.window._engine_disabled["triggevent"], {"tv-umad", "tv-m1s"})
        self.assertTrue(all(not trigger.enabled for trigger in self.window._triggers))
        self.assertEqual(self.settings()["triggernometry_callout_edits"]["tn-umad#0"], "Custom ${_me.name}")
        self.window._global_tn_btn.click()
        self.assertEqual(self.window._engine_disabled["triggernometry"], set())
        self.assertEqual(self.window._engine_disabled["triggevent"], {"tv-umad", "tv-m1s"})
        self.assertTrue(all(not trigger.enabled for trigger in self.window._triggers))

    def test_general_controls_leave_unassigned_triggernometry_in_unsorted(self):
        general = Trigger(id="general", name="General")
        self.window._triggers = [general]
        self.window._local_ids = set()
        self.window._official_ids = {general.id}
        self.window._engine_inventory = [self.engine("triggernometry", "unassigned#0", "Unsorted")]
        self.window._refresh_table()
        self.select("")
        self.assertFalse(self.window._fight_bar.isHidden())
        self.assertFalse(self.window._cb_tn.isEnabled())
        self.assertFalse(self.window._cb_tn.isChecked())
        self.window._cb_local.click()
        self.assertFalse(general.enabled)
        self.assertEqual(self.window._engine_disabled["triggernometry"], set())
        self.select("", "custom_group")
        self.assertTrue(self.window._fight_bar.isHidden())

    def test_real_engine_controls_preserve_scripts_and_templates_with_simulated_logs(self):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            self.skipTest("Requires Mono and Xvfb")
        core = Path(__file__).resolve().parents[1] / "triggernometry-core"
        packs = self.case.temp / "triggernometry-packs"
        tree = ET.parse(core / "test" / "spike-script-pack.xml")
        tree.find("ExportedFolder").set("Name", "UMAD")
        trigger = tree.find(".//Trigger")
        actions = trigger.find("Actions")
        actions[0].set("ExecScriptExpression",
                       'int count; int.TryParse(Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, '
                       '"spikevar"), out count); Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, '
                       '"spikevar", (count + 1).ToString());')
        actions[1].set("ExecutionDelayExpression", "0")
        actions[1].set("Asynchronous", "False")
        ET.SubElement(actions, "Action", OrderNumber="3", ActionType="UseTTS", Asynchronous="False",
                      UseTTSTextExpression="finished ${var:spikevar}")
        ET.SubElement(actions, "Action", OrderNumber="4", ActionType="ExecuteScript", Asynchronous="False",
                      ExecScriptExpression='Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, '
                      '"donevar", Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, "spikevar"));')
        tree.write(packs / "controls.xml", encoding="utf-8")
        probe = ET.parse(core / "test" / "spike-script-pack.xml")
        probe.find("ExportedFolder").set("Name", "M1S")
        probe.find("ExportedFolder").set("Id", "66666666-6666-6666-6666-666666666666")
        probe_trigger = probe.find(".//Trigger")
        probe_trigger.set("Id", "55555555-5555-5555-5555-555555555555")
        probe_trigger.set("RegularExpression", "CONTROL_PROBE")
        probe_actions = probe_trigger.find("Actions")
        probe_actions[0].set("ExecScriptExpression",
                            'int expected; int.TryParse(Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, '
                            '"probevar"), out expected); expected++; '
                            'Triggernometry.Interpreter.StaticHelpers.SetScalarVariable(false, "probevar", expected.ToString()); '
                            'var deadline = System.DateTime.UtcNow.AddSeconds(5); int actual; '
                            'while (System.DateTime.UtcNow < deadline) { '
                            'int.TryParse(Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, "donevar"), out actual); '
                            'if (actual >= expected) break; System.Threading.Thread.Sleep(10); }')
        probe_actions[1].set("ExecutionDelayExpression", "0")
        probe_actions[1].set("UseTTSTextExpression", "counter ${var:spikevar}")
        probe.write(packs / "probe.xml", encoding="utf-8")
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.case.temp / "runtime"),
            "NYAA_TRIGGERNOMETRY_PACKS": str(packs),
            "NYAA_TRIGGERNOMETRY_EXE": str(core / "bin" / "triggernometry-core.exe"),
        }))
        bridge = TriggernometryBridge()
        self.window._triggernometry = bridge
        self.window._triggernometry_mode = True
        self.window._engine_inventory = []
        spoken, inventories = [], []
        bridge.tts.connect(lambda text, generation: spoken.append(text))
        bridge.inventory.connect(self.window._on_triggernometry_inventory)
        bridge.inventory.connect(lambda payload, generation: inventories.append(json.loads(payload)))

        def stop_bridge():
            bridge.stop(wait=True)
            for worker in (bridge._reader, bridge._errpump, bridge._writer):
                if worker is not None:
                    worker.join(timeout=8)
                    self.assertFalse(worker.is_alive())

        self.addCleanup(stop_bridge)

        def wait_for(predicate):
            deadline = time.monotonic() + 20
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(predicate(), spoken)

        expected_run = []

        def fire(number, expected):
            start = len(spoken)
            expected_run.extend([*expected, f"counter {number}"])
            bridge.feed_log("00|2026-10-16T12:00:00Z|0038|Player|SPIKESCRIPT|test")
            bridge.feed_log("00|2026-10-16T12:00:00Z|0038|Player|CONTROL_PROBE|test")
            wait_for(lambda: all(text in spoken[start:] for text in (*expected, f"counter {number}")))
            self.assertCountEqual(spoken, expected_run)

        bridge.start()
        wait_for(lambda: bool(inventories))
        generation = bridge.generation()
        ident = trigger.get("Id") + "#0"
        self.select("UMAD")
        self.window._set_triggernometry_callout_edit(ident, "Custom ${var:spikevar}")
        fire(1, ["Custom 1", "finished 1"])
        self.window._cb_tn.click()
        fire(2, [])
        self.window._cb_tn.click()
        fire(3, ["Custom 3", "finished 3"])
        self.window._global_tn_btn.click()
        self.window._global_tn_btn.click()
        probe_key = "triggernometry:" + probe_trigger.get("Id") + "#0"
        row = next(index for index in range(self.window._table.rowCount())
                   if self.window._table.item(index, ac._C_EN).data(Qt.ItemDataRole.UserRole) == probe_key)
        self.window._table.item(row, ac._C_EN).setCheckState(Qt.CheckState.Checked)
        fire(4, [])
        self.window._global_tn_btn.click()
        fire(5, ["Custom 5", "finished 5"])
        self.assertCountEqual(spoken, expected_run)
        self.assertEqual(bridge.generation(), generation)
        self.assertTrue(bridge.is_active())
        self.assertEqual(self.settings()["triggernometry_callout_edits"][ident], "Custom ${var:spikevar}")


if __name__ == "__main__":
    unittest.main()
