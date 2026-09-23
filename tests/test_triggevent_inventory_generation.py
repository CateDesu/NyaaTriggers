"""Inventory from a stopped Triggevent process must not replace current rows."""

import json
import unittest
from unittest.mock import Mock

from nyaatriggers.triggevent_bridge import TriggeventBridge
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


if __name__ == "__main__":
    unittest.main()
