import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from nyaatriggers.main_window import MainWindow
from nyaatriggers.ui.automarkers_tab import AutomarkersTabMixin
from nyaatriggers.umad_chains import CursedShriekPairs


class Host(AutomarkersTabMixin):
    _norm_hex = staticmethod(MainWindow._norm_hex)

    def __init__(self):
        self._settings = {"telesto_enabled": True}
        self._current_fight_tag = "UMAD"
        self._umad_gaze_enabled = True
        self._umad_gaze = CursedShriekPairs()
        self._umad_gaze_pending = []
        self._umad_actor_names = {}
        self._umad_gaze_flush_timer = types.SimpleNamespace(start=lambda: None)
        self.signs = {}

    def _mark_player(self, actor, marker, name="", *, expires_at=None):
        if marker in self.signs and self.signs[marker] != actor:
            raise AssertionError(f"{marker} stolen from {self.signs[marker]} by {actor}")
        self.signs[marker] = actor
        return True

    def _clear_player(self, actor, name="", force=False):
        self.signs = {sign: who for sign, who in self.signs.items() if who != actor}
        return True

    def _is_me_actor(self, actor, name=""):
        return False


class GazeReplayTests(unittest.TestCase):
    def test_all_recorded_gaze_waves(self):
        cases = json.loads((Path(__file__).parent / "fixtures/umad_gazes.json").read_text())
        self.assertEqual(len(cases), 44)
        self.assertEqual(sum(len(case["waves"]) for case in cases), 85)
        for case in cases:
            with self.subTest(pull=case["pull"]):
                host = Host()
                seen = set()
                expires = {}
                complete = set()
                for now, raw in case["events"]:
                    fields = raw.split("|")
                    with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now):
                        host._on_umad_gaze_flush()
                        if fields[0] == "20":
                            host._umad_gaze_cast(fields)
                        else:
                            host._umad_gaze_line(fields)
                    if fields[0] in ("26", "30") and fields[2] == "15A7":
                        actor = fields[7]
                        if fields[0] == "26":
                            seen.add(actor)
                            expires.setdefault(actor, now + float(fields[4]))
                        else:
                            expires.pop(actor, None)
                    expected = {}
                    for index, wave in enumerate(case["waves"]):
                        if all(actor in seen for actor in wave["actors"]):
                            complete.add(index)
                        if index not in complete:
                            continue
                        prefix = "ignore" if wave["real"] else "bind"
                        for slot, actor in enumerate(wave["actors"], 1):
                            if now < expires.get(actor, -1):
                                expected.setdefault(f"{prefix}{slot}", actor)
                    self.assertEqual(host.signs, expected, f"{now}: {raw}")
                with patch("nyaatriggers.ui.automarkers_tab.time.monotonic", return_value=now + 100):
                    host._on_umad_gaze_flush()
                self.assertEqual(host.signs, {})
                self.assertFalse(host._umad_gaze_pending)


if __name__ == "__main__":
    unittest.main()
