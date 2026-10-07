import json
import unittest
from unittest.mock import Mock

from nyaatriggers.triggevent_bridge import TriggeventBridge
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.ui.engines import EnginesMixin


class InventoryHost(EnginesMixin):
    def __init__(self, bridge):
        self._triggevent = bridge
        self._engine_inventory = []
        self._save_triggevent_inventory_cache = Mock()
        self._record_engine_seen = Mock()
        self._refresh_table = Mock()
        self._replay_triggevent_callout_edits = Mock()
        self._apply_automark_state = Mock()


class TriggeventInventoryGenerationTests(unittest.TestCase):
    def test_old_process_inventory_is_discarded(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 2
        host = InventoryHost(bridge)
        bridge.inventory.connect(host._on_triggevent_inventory)

        bridge._dispatch({"t": "inventory", "triggers": [{"id": "current"}]}, gen=2)
        self.assertEqual([row["id"] for row in host._engine_inventory], ["current"])
        bridge._dispatch({"t": "inventory", "triggers": [{"id": "old"}]}, gen=1)
        self.assertEqual([row["id"] for row in host._engine_inventory], ["current"])

    def test_queued_inventory_is_discarded_after_restart(self):
        bridge = TriggeventBridge()
        bridge._active = True
        bridge._gen = 3
        host = InventoryHost(bridge)
        host._engine_inventory = [{"source": "triggevent", "id": "current"}]

        host._on_triggevent_inventory(json.dumps([{"id": "old"}]), 2)
        self.assertEqual([row["id"] for row in host._engine_inventory], ["current"])
        host._save_triggevent_inventory_cache.assert_not_called()


class AutomarkHost(InventoryHost, AutomarkersTabMixin):
    def __init__(self, bridge):
        super().__init__(bridge)
        del self._apply_automark_state
        self._settings = {"telesto_enabled": True, "telesto_uri": "http://127.0.0.1:45678/"}
        self._telesto_client = Mock()
        self._umad_chain_reset = Mock()
        self._umad_gaze_reset = Mock()
        self._automark_active = {}
        self._automark_pending = []
        self._update_automark_status_label = Mock()


class NativeAutomarkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.bridge = TriggeventBridge()
        self.bridge._active = True
        self.bridge._gen = 1
        self.host = AutomarkHost(self.bridge)
        self.bridge.inventory.connect(self.host._on_triggevent_inventory)

    def commands(self):
        commands = []
        while not self.bridge._wq.empty():
            commands.append(json.loads(self.bridge._wq.get_nowait()))
        return commands

    def test_each_engine_inventory_restores_marking_with_callouts_off(self):
        self.host._triggevent_mode = False
        for generation in (1, 2):
            with self.subTest(generation=generation):
                self.bridge._gen = generation
                self.bridge._dispatch({"t": "inventory", "triggers": []}, gen=generation)
                self.assertEqual(self.commands(), [{"nyaa_cmd": "set_automark", "enable": True,
                                                    "uri": self.host._settings["telesto_uri"],
                                                    "native_umad": True}])

    def test_setting_changes_disable_native_output_and_replace_endpoint(self):
        for enabled, uri in ((True, "http://127.0.0.1:45678/"),
                             (False, "http://127.0.0.1:45679/")):
            with self.subTest(enabled=enabled):
                self.host._settings.update(telesto_enabled=enabled, telesto_uri=uri)
                self.host._apply_automark_state()
                expected = [{"nyaa_cmd": "set_automark", "enable": enabled,
                             "uri": uri, "native_umad": True}]
                if not enabled:
                    expected.insert(0, {"nyaa_cmd": "set_automark", "enable": False,
                                        "native_umad": True})
                self.assertEqual(self.commands(), expected)

    def test_retired_inventory_cannot_reconfigure_new_engine(self):
        self.bridge._gen = 2
        self.host._on_triggevent_inventory("[]", 1)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
