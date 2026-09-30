"""Offline icons, recap filters and health rendering in Qt."""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PyQt6.QtCore import QByteArray, QEvent, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QHelpEvent
from PyQt6.QtNetwork import QNetworkReply
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QListWidget, QPushButton, QStyleOptionViewItem, QWidget, QVBoxLayout

from nyaatriggers import app_common as ac, theme
from nyaatriggers.death_recap import DeathRecap
from nyaatriggers.game_locale import localized_metadata
from nyaatriggers.locale_util import set_locale
from nyaatriggers.recap_filters import DEFAULT_HIDDEN_STATUSES, hidden_statuses
from nyaatriggers.ui.death_recap_tab import DeathRecapTabMixin
from nyaatriggers.ui.recap_widgets import RecapIcons
from nyaatriggers.ui.recap_browser import LogImportDialog, SavedRecapDialog


class RecapHost(DeathRecapTabMixin, QWidget):
    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings or {}
        self._death_recap = DeathRecap()
        self.saves = 0
        QVBoxLayout(self).addWidget(self._build_death_recap_tab())

    def _save_settings_debounced(self):
        self.saves += 1


def record(name="Player", actor=0x10000001):
    status = {"id": 202, "name": "Vulnerability Up", "source": "Boss", "stacks": 2, "remaining": 12}
    event = {"time": -2.6, "kind": "damage", "name": "Spelldriver", "source": "Kefka", "amount": 50000,
             "hp": 80000, "max_hp": 100000, "shield": 20, "action_id": 3570, "statuses": [status]}
    return {"id": str(uuid4()), "actor": actor, "when": 1000000, "name": name, "zone": "Duty",
            "statuses": [status], "events": [event, event | {"time": -1.5, "kind": "heal", "name": "Cure",
                                                         "amount": 10000, "hp_after": 90000}]}


class FakeReply(QObject):
    readyRead = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.raw = b""
        self.running = True
        self.problem = QNetworkReply.NetworkError.NoError

    def setReadBufferSize(self, _size):
        pass

    def readAll(self):
        raw, self.raw = self.raw, b""
        return QByteArray(raw)

    def isRunning(self):
        return self.running

    def error(self):
        return self.problem

    def abort(self):
        self.running = False
        self.problem = QNetworkReply.NetworkError.OperationCanceledError
        self.finished.emit()

    def deliver(self, raw):
        self.raw = raw
        self.readyRead.emit()
        if self.running:
            self.running = False
            self.finished.emit()


class FakeNetwork:
    def __init__(self, parent):
        self.parent = parent
        self.requests = []

    def get(self, request):
        reply = FakeReply(self.parent)
        self.requests.append((request.url().toString(), reply))
        return reply


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(theme.STYLESHEET)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.patch = patch.object(ac, "_DATA_DIR", self.directory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.network = patch.object(RecapIcons, "_pump")
        self.network.start()
        self.addCleanup(self.network.stop)
        self.host = RecapHost()
        self.addCleanup(self.host.close)
        self.host._recap_records = [record(), record("Other", 0x10000002)]
        self.host._refresh_recap_list()

    def test_player_filter_keeps_list_and_event_selection_together(self):
        index = self.host._recap_player.findData(0x10000002)
        self.host._recap_player.setCurrentIndex(index)
        self.assertEqual(self.host._recap_list.count(), 1)
        self.assertEqual(self.host._recap_visible[0]["name"], "Other")
        self.assertEqual(self.host._recap_list.item(0).data(Qt.ItemDataRole.UserRole),
                         self.host._recap_records[1]["id"])

    def test_filters_persist_without_changing_recorded_events(self):
        before = json.dumps(self.host._recap_records)
        self.host._recap_healing.setChecked(False)
        self.assertEqual(self.host._recap_table.rowCount(), 1)
        self.assertEqual(self.host._recap_table.item(0, 1).text(), "-50,000")
        self.assertFalse(self.host._settings["recap_show_healing"])
        self.assertEqual(before, json.dumps(self.host._recap_records))
        self.assertEqual(self.host.saves, 1)

    def test_japanese_action_names_preserve_instant_death_label(self):
        set_locale("ja")
        self.addCleanup(set_locale, "en")
        death = record()
        event = death["events"][0]
        death["events"] = [event | {"kind": "instant-death", "amount": None}, event]
        original = json.dumps(death)
        self.host._recap_records = [death]
        self.host._refresh_recap_list()
        self.assertEqual(self.host._recap_table.item(0, 2).text(),
                         localized_metadata("Action", event["action_id"], {})["name"])
        self.assertEqual(self.host._recap_table.item(1, 2).text(), "即死")
        self.assertEqual(json.dumps(death), original)

    def test_buff_filter_hides_icons_and_can_restore_them(self):
        def edit():
            dialog = QApplication.activeModalWidget()
            listing = dialog.findChild(QListWidget)
            item = next(listing.item(row) for row in range(listing.count())
                        if listing.item(row).data(Qt.ItemDataRole.UserRole) == "202")
            item.setCheckState(Qt.CheckState.Checked)
            dialog.accept()
        QTimer.singleShot(0, edit)
        self.host._filter_recap_buffs()
        self.assertEqual(set(self.host._settings["recap_hidden_statuses"]), DEFAULT_HIDDEN_STATUSES | {"202"})
        self.assertEqual(hidden_statuses(self.host._settings), self.host._recap_hidden)
        event = self.host._recap_records[0]["events"][0]
        self.assertEqual(self.host._recap_delegate.statuses(event), [])
        self.assertEqual(len(event["statuses"]), 1)
        self.host._recap_hidden.clear()
        self.assertEqual(len(self.host._recap_delegate.statuses(event)), 1)

    def test_cancelled_buff_filter_does_not_change_preferences(self):
        def cancel():
            dialog = QApplication.activeModalWidget()
            dialog.findChild(QListWidget).item(0).setCheckState(Qt.CheckState.Checked)
            dialog.reject()
        QTimer.singleShot(0, cancel)
        self.host._filter_recap_buffs()
        self.assertEqual(self.host._recap_hidden, DEFAULT_HIDDEN_STATUSES)
        self.assertEqual(self.host.saves, 0)

    def test_damage_buff_and_debuff_filters_use_game_categories_and_keep_snapshots(self):
        death = record()
        death["events"].extend({"kind": "gained", "name": "Translated status", "source": "Player", "time": -1,
                                 "amount": None, "status_id": ident} for ident in (71, 43, 65530))
        self.host._recap_records = [death]
        self.host._recap_buffs.setChecked(True)
        self.host._recap_debuffs.setChecked(True)
        self.host._refresh_recap_list()
        before = json.dumps(death)
        self.host._recap_buffs.setChecked(False)
        events = [self.host._recap_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                  for row in range(self.host._recap_table.rowCount())]
        self.assertEqual({e["status_id"] for e in events if "status_id" in e}, {43, 65530})
        self.assertEqual(events[-1]["statuses"][0]["id"], 202)
        self.host._recap_debuffs.setChecked(False)
        self.host._recap_damage.setChecked(False)
        self.assertEqual(self.host._recap_table.rowCount(), 1)
        self.assertEqual(self.host._recap_table.item(0, 0).data(Qt.ItemDataRole.UserRole)["kind"], "heal")
        self.assertFalse(self.host._settings["recap_show_buffs"])
        self.assertFalse(self.host._settings["recap_show_debuffs"])
        self.assertFalse(self.host._settings["recap_show_damage"])
        self.assertEqual(json.dumps(death), before)

    def test_legacy_status_change_preferences_migrate_to_both_categories(self):
        host = RecapHost({"recap_show_status_changes": False})
        self.addCleanup(host.close)
        self.assertFalse(host._recap_buffs.isChecked())
        self.assertFalse(host._recap_debuffs.isChecked())
        host = RecapHost({"recap_show_status_changes": False, "recap_show_debuffs": True})
        self.addCleanup(host.close)
        self.assertFalse(host._recap_buffs.isChecked())
        self.assertTrue(host._recap_debuffs.isChecked())

    def test_attacker_mitigation_tooltip_and_filter_identify_the_correct_actor(self):
        status = {"id": 1193, "name": "Reprisal", "source": "Tank", "stacks": 0, "remaining": 12}
        death = record()
        death["events"][0]["source_statuses"] = [status]
        self.host._recap_records = [death]
        self.host._refresh_recap_list()
        table = self.host._recap_table
        delegate = self.host._recap_delegate
        self.host.resize(1500, 800)
        self.host.show()
        self.app.processEvents()
        index = table.model().index(1, 5)
        option = QStyleOptionViewItem()
        option.rect = table.visualRect(index)
        point = delegate._status_rect(option.rect, 1).center()
        event = QHelpEvent(QEvent.Type.ToolTip, point, table.viewport().mapToGlobal(point))
        with patch("nyaatriggers.ui.recap_widgets.QToolTip.showText") as tooltip:
            self.assertTrue(delegate.helpEvent(event, table, option, index))
        text = tooltip.call_args.args[1]
        self.assertIn("On attacker: Kefka", text)
        self.assertIn("Applied by: Tank", text)
        self.assertIn("Damage dealt is reduced", text)
        def hide():
            dialog = QApplication.activeModalWidget()
            listing = dialog.findChild(QListWidget)
            item = next(listing.item(i) for i in range(listing.count())
                        if listing.item(i).data(Qt.ItemDataRole.UserRole) == "1193")
            item.setCheckState(Qt.CheckState.Checked)
            dialog.accept()
        QTimer.singleShot(0, hide)
        self.host._filter_recap_buffs()
        self.assertEqual([s["id"] for s in delegate.statuses(death["events"][0])], [202])
        self.assertNotIn("Reprisal", self.host._recap_statuses.text())
        self.assertEqual(death["events"][0]["source_statuses"], [status])

    def test_gained_status_uses_stacked_icon_and_description(self):
        death = record()
        death["events"] = [{"kind": "gained", "name": "Vulnerability Up", "source": "Boss", "time": -1,
                             "amount": None, "status_id": 202, "status_stacks": 3, "status_duration": 20}]
        self.host._recap_records = [death]
        self.host._recap_debuffs.setChecked(True)
        self.host._refresh_recap_list()
        table = self.host._recap_table
        self.host.show()
        self.app.processEvents()
        with patch.object(self.host._recap_icons, "get", wraps=self.host._recap_icons.get) as lookup:
            table.grab()
            self.assertIn(unittest.mock.call("Status", 202, 3), lookup.call_args_list)
        index = table.model().index(0, 2)
        event = QHelpEvent(QEvent.Type.ToolTip, QPoint(), QPoint())
        with patch("nyaatriggers.ui.recap_widgets.QToolTip.showText") as tooltip:
            self.host._recap_delegate.helpEvent(event, table, QStyleOptionViewItem(), index)
        self.assertIn("Damage taken is increased", tooltip.call_args.args[1])
        self.assertIn("Stacks / value: 3", tooltip.call_args.args[1])
        self.assertEqual(table.item(0, 1).text(), "+20s")

    def test_default_hidden_statuses_are_all_beneficial_in_the_bundled_game_data(self):
        catalog = self.host._recap_icons.catalog["Status"]
        for ident in DEFAULT_HIDDEN_STATUSES:
            with self.subTest(ident=ident):
                self.assertEqual(catalog[ident]["category"], 1)
                self.assertTrue(catalog[ident]["description"])
        for ident in (1203, 1195, 1193, 860, 1715, 2115, 3642, 43, 44, 62):
            self.assertEqual(catalog[str(ident)]["category"], 2)
            self.assertNotIn(str(ident), DEFAULT_HIDDEN_STATUSES)

    def test_default_filter_keeps_defenses_debuffs_and_mixed_buff_variants(self):
        hidden = [1878, 786, 1239, 2599, 2703, 3685]
        visible = [71, 1191, 1202, 1826, 2707, 43, 44, 62, 202, 2034, 2282, 3042, 3186,
                   3888, 1303, 1320, 3177, 65530]
        event = {"statuses": [{"id": ident, "name": "翻訳された名前"} for ident in hidden + visible]}
        self.assertEqual([s["id"] for s in self.host._recap_delegate.statuses(event)], visible)
        death = record()
        death["statuses"] = event["statuses"]
        death["events"].extend({"kind": "gained", "name": "Test", "source": "Player", "time": -1,
                                 "amount": None, "status_id": ident} for ident in (1878, 43))
        self.host._recap_records = [death]
        self.host._recap_buffs.setChecked(True)
        self.host._recap_debuffs.setChecked(True)
        self.host._refresh_recap_list()
        events = [self.host._recap_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                  for row in range(self.host._recap_table.rowCount())]
        self.assertEqual([event["status_id"] for event in events if event["kind"] == "gained"], [43])
        self.assertEqual(len(death["events"]), 4)

    def test_show_all_and_restore_defaults_are_explicit_saved_choices(self):
        def click_button(text):
            dialog = QApplication.activeModalWidget()
            next(button for button in dialog.findChildren(QPushButton) if button.text() == text).click()
            dialog.accept()
        QTimer.singleShot(0, lambda: click_button("Show all statuses"))
        self.host._filter_recap_buffs()
        self.assertEqual(self.host._recap_hidden, set())
        self.assertEqual(hidden_statuses(self.host._settings), set())
        QTimer.singleShot(0, lambda: click_button("Restore defaults"))
        self.host._filter_recap_buffs()
        self.assertEqual(self.host._recap_hidden, DEFAULT_HIDDEN_STATUSES)
        self.assertEqual(hidden_statuses(self.host._settings), DEFAULT_HIDDEN_STATUSES)

    def test_rendered_health_and_status_icons_survive_column_resize(self):
        self.host.resize(1600, 600)
        self.host.show()
        self.app.processEvents()
        table = self.host._recap_table
        self.assertEqual(table.item(0, 0).text(), "-1.5s")
        self.assertEqual(table.item(0, 1).text(), "+10,000")
        self.assertFalse(self.host.grab().isNull())
        table.setColumnWidth(5, 80)
        self.app.processEvents()
        self.assertGreaterEqual(table.rowHeight(0), 30)

    def test_import_cancel_closes_any_completed_result(self):
        path = self.directory / "Network.log"
        path.write_text("25|2026-09-27T12:00:00+00:00|10000001|Player\n")
        dialog = LogImportDialog(path, self.host)
        QTimer.singleShot(0, dialog.reject)
        self.assertEqual(dialog.exec(), QDialog.DialogCode.Rejected)
        self.assertTrue(dialog.job.done.is_set())
        self.assertIsNone(dialog.result)
        self.assertIsNone(dialog.job.result)
        dialog.deleteLater()

    def test_empty_saved_browser_cannot_open_a_missing_pull(self):
        dialog = SavedRecapDialog([], self.host)
        self.assertIsNone(dialog.selection)
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
        dialog.close()

    def test_missing_legacy_snapshots_stay_explicit(self):
        old = record()
        for event in old["events"]:
            for key in ("hp", "max_hp", "shield", "hp_after", "statuses"):
                event.pop(key, None)
        self.host._recap_records = [old]
        self.host._refresh_recap_list()
        self.assertEqual(self.host._recap_table.item(0, 4).text(), "Not recorded")
        self.assertEqual(self.host._recap_table.item(0, 5).text(), "Not recorded")

    def test_bundled_statuses_and_stacks_have_real_icons_offline(self):
        cache = self.host._recap_icons
        self.assertGreater(len(cache.catalog.get("Status", {})), 4700)
        first, image = cache.get("Status", 202, 1)
        _second, stacked = cache.get("Status", 202, 2)
        self.assertEqual(first["name"], "Vulnerability Up")
        self.assertFalse(image.isNull())
        self.assertNotEqual(image.toImage(), cache.placeholder.toImage())
        self.assertNotEqual(image.toImage(), stacked.toImage())
        _raw, invalid_stack = cache.get("Status", 202, 65535)
        self.assertEqual(image.toImage(), invalid_stack.toImage())
        self.assertEqual(cache.waiting, {})

    def test_every_catalog_icon_is_bundled_including_stack_variants(self):
        cache = self.host._recap_icons
        available = set(cache.archive.namelist())
        missing = []
        for sheet in ("Status", "Action"):
            for ident, row in cache.catalog[sheet].items():
                if not row["icon"]:
                    continue
                for icon in range(row["icon"], row["icon"] + max(1, row.get("max_stacks", 1))):
                    if f"{icon}.png" not in available and str(icon) not in cache.catalog.get("unavailable_icons", {}):
                        missing.append((sheet, ident, icon))
        self.assertEqual(missing, [])
        self.assertEqual(cache.catalog["unavailable_icons"], {"215049": "Battle Efficiency Down"})

    def test_hidden_game_statuses_do_not_show_unknown_icon_boxes(self):
        event = {"statuses": [{"id": 5084, "name": "Unknown_13DC", "source": ""},
                              {"id": 202, "name": "Vulnerability Up", "source": "Boss"}]}
        self.assertEqual([s["id"] for s in self.host._recap_delegate.statuses(event)], [202])

    def test_all_bundled_images_decode(self):
        cache = self.host._recap_icons
        for name in cache.archive.namelist():
            if name.endswith(".png"):
                with self.subTest(icon=name):
                    self.assertFalse(cache._picture(cache.archive.read(name)).isNull())

    def fake_network(self):
        self.network.stop()
        cache = self.host._recap_icons
        cache.manager = FakeNetwork(cache)
        return cache

    def test_unknown_icon_download_keeps_stack_variant_and_then_uses_memory(self):
        cache = self.fake_network()
        cache.get("Status", 60000, 3)
        manager = cache.manager
        self.assertEqual(len(manager.requests), 1)
        manager.requests[0][1].deliver(json.dumps({"fields": {"Name": "New status", "Icon": {"id": 215001},
                                                                  "MaxStacks": 4}}).encode())
        self.assertIn("215003.tex", manager.requests[1][0])
        png = cache.archive.read("215001.png")
        manager.requests[1][1].deliver(png)
        metadata, pixmap = cache.get("Status", 60000, 3)
        self.assertEqual(metadata["name"], "New status")
        self.assertFalse(pixmap.isNull())
        self.assertEqual(len(manager.requests), 2)
        self.assertEqual(cache.active, set())

    def test_downloaded_status_metadata_refreshes_names_and_event_filters(self):
        cache = self.fake_network()
        entry = cache.catalog["Status"].pop("202")
        self.host._recap_hidden.clear()
        self.host._recap_buffs.setChecked(True)
        self.host._recap_debuffs.setChecked(False)
        death = record()
        death["statuses"][0]["name"] = "Status CA"
        death["events"] = [{"kind": "gained", "time": -1, "source": "Boss",
                            "name": "Status CA", "amount": None, "status_id": 202}]
        self.host._recap_records = [death]
        self.host._refresh_recap_list()
        cache.get("Status", 202)
        cache.manager.requests[0][1].deliver(json.dumps({"fields": {
            "Name": entry["name"], "Icon": {"id": entry["icon"]},
            "MaxStacks": entry["max_stacks"], "Description": entry["description"],
            "StatusCategory": entry["category"], "IsPermanent": False}}).encode())
        cache.manager.requests[1][1].deliver(cache.archive.read(f"{entry['icon']}.png"))
        self.assertEqual(self.host._recap_table.rowCount(), 0)
        self.assertIn("Vulnerability Up", self.host._recap_statuses.text())
        self.host._recap_debuffs.setChecked(True)
        self.assertEqual(self.host._recap_table.item(0, 2).text(), "Vulnerability Up")

    def test_downloaded_status_metadata_survives_an_unavailable_icon(self):
        cache = self.fake_network()
        cache.get("Status", 60000)
        cache.manager.requests[0][1].deliver(json.dumps({"fields": {
            "Name": "New debuff", "Icon": {"id": 215001}, "MaxStacks": 0,
            "Description": "Damage taken is increased.", "StatusCategory": 2}}).encode())
        cache.manager.requests[1][1].deliver(b"missing image")
        metadata, picture = cache.get("Status", 60000)
        self.assertEqual(metadata.get("category"), 2)
        self.assertEqual(metadata.get("name"), "New debuff")
        self.assertEqual(picture.toImage(), cache.placeholder.toImage())

    def test_failed_or_oversized_download_has_a_cooldown(self):
        cache = self.fake_network()
        for ident, raw in ((60000, b"not JSON"), (60001, b"x" * (512 * 1024 + 1))):
            cache.get("Status", ident)
            cache.manager.requests[-1][1].deliver(raw)
            count = len(cache.manager.requests)
            metadata, picture = cache.get("Status", ident)
            self.assertEqual(metadata, {})
            self.assertEqual(picture.toImage(), cache.placeholder.toImage())
            self.assertEqual(len(cache.manager.requests), count)
            self.assertIn(("Status", ident, 0), cache.failed)
        self.assertEqual(cache.active, set())

    def test_icon_requests_and_images_stay_bounded(self):
        cache = self.fake_network()
        for ident in range(60000, 60700):
            cache.get("Status", ident)
        self.assertEqual(len(cache.active), 4)
        self.assertEqual(len(cache.waiting), 512)
        self.assertEqual(len(cache.manager.requests), 4)
        for ident in range(700):
            cache._remember(("Action", ident, 0), {}, cache.placeholder)
        self.assertEqual(len(cache.entries), 512)


if __name__ == "__main__":
    unittest.main()
