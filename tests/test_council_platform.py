import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, Qt, pyqtSlot

from nyaatriggers import triggernometry_bridge as tb
from nyaatriggers.ui import voice_tab


class SoundReceiver(QObject, voice_tab.VoiceTabMixin):
    def __init__(self, bridge):
        super().__init__()
        self._triggernometry = bridge
        self._triggernometry_mode = True
        self._connected = True
        self.received = []

    @pyqtSlot(str, int, int)
    def receive(self, path, volume, generation):
        self.received.append((path, volume, generation))
        self._on_triggernometry_sound(path, volume, generation)


class TriggernometrySoundPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sound = self.root / "声 #%20.wav"
        self.sound.write_bytes(b"sound fixture")
        self.bridge = tb.TriggernometryBridge()
        # Simulate a ready bridge without native runtime discovery or startup.
        self.bridge._active = True
        self.bridge._gen = 11
        self.receiver = SoundReceiver(self.bridge)
        self.bridge.sound.connect(self.receiver.receive, Qt.ConnectionType.QueuedConnection)
        self.addCleanup(self.bridge.sound.disconnect)
        self.sink = self.enterContext(patch.object(voice_tab, "play_sound"))
        self.packs = self.enterContext(patch.object(tb, "_packs_dir", return_value=self.root))
        self.addCleanup(QCoreApplication.removePostedEvents, self.receiver, QEvent.Type.MetaCall)

    def dispatch(self, path, volume=37, generation=None, flush=True):
        errors = []

        def send():
            try:
                self.bridge._dispatch({"t": "sound", "file": path, "volume": volume},
                                      self.bridge.generation() if generation is None else generation)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "sound dispatch did not finish")
        if errors:
            raise errors[0]
        if flush:
            self.flush()

    def flush(self):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.MetaCall)

    def test_local_file_uri_reaches_production_sound_slot(self):
        uri = self.sound.as_uri()
        for path in (uri, "FILE:" + uri[5:], "file:" + uri[7:],
                     "file://localhost" + uri[7:]):
            with self.subTest(path=path):
                self.sink.reset_mock()
                self.dispatch(path)
                self.assertEqual(self.receiver.received[-1], (self.sound.as_posix(), 37, 11))
                self.sink.assert_called_once_with(self.sound.as_posix(), 0.37)
        self.packs.assert_not_called()

    def test_plain_paths_preserve_percent_characters_and_pack_resolution(self):
        relative = Path("nested") / "literal%20音.wav"
        (self.root / relative).parent.mkdir()
        (self.root / relative).write_bytes(b"relative fixture")
        for path, expected in ((str(self.sound), self.sound),
                               (str(relative), self.root / relative)):
            with self.subTest(path=path):
                self.sink.reset_mock()
                self.dispatch(path)
                self.sink.assert_called_once_with(str(expected), 0.37)
        self.packs.assert_called_once_with()

    def test_windows_uri_syntax_keeps_drive_and_unc_paths(self):
        for uri, expected in (
                ("file:///C:/Voices/%E5%A3%B0%20one.wav",
                 ("" if os.name == "nt" else "/") + "C:/Voices/声 one.wav"),
                ("file://server/share/%E5%A3%B0%20one.wav", "//server/share/声 one.wav")):
            with self.subTest(uri=uri):
                self.dispatch(uri)
                self.assertEqual(self.receiver.received[-1], (expected, 37, 11))
        self.packs.assert_not_called()

    def test_uri_delivery_keeps_volume_validation(self):
        for volume, expected in ((150, 1.0), (-4, 0.0), ("bad", 1.0)):
            with self.subTest(volume=volume):
                self.sink.reset_mock()
                self.dispatch(self.sound.as_uri(), volume=volume)
                self.sink.assert_called_once_with(self.sound.as_posix(), expected)

    def test_stale_and_disabled_sounds_keep_live_positive_controls(self):
        self.dispatch(str(self.sound))
        self.sink.assert_called_once()
        self.sink.reset_mock()
        self.receiver.received.clear()
        self.dispatch(str(self.sound), generation=10)
        self.assertEqual(self.receiver.received, [])
        self.sink.assert_not_called()

        self.dispatch(str(self.sound), flush=False)
        self.bridge._gen = 12
        self.flush()
        self.assertEqual(self.receiver.received, [(str(self.sound), 37, 11)])
        self.sink.assert_not_called()
        self.dispatch(str(self.sound))
        self.sink.assert_called_once_with(str(self.sound), 0.37)

        for attribute in ("_connected", "_triggernometry_mode"):
            with self.subTest(attribute=attribute):
                self.sink.reset_mock()
                setattr(self.receiver, attribute, False)
                self.dispatch(str(self.sound))
                self.sink.assert_not_called()
                setattr(self.receiver, attribute, True)
                self.dispatch(str(self.sound))
                self.sink.assert_called_once_with(str(self.sound), 0.37)

    def test_http_uri_does_not_become_remote_playback(self):
        self.dispatch("https://example.invalid/sound.wav")
        self.sink.assert_not_called()
        self.dispatch(str(self.sound))
        self.sink.assert_called_once_with(str(self.sound), 0.37)


if __name__ == "__main__":
    unittest.main()
