from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nyaatriggers import app_common as ac
from nyaatriggers.record_store import read_record, write_record
from nyaatriggers.trigger_profiles import DEFAULT_PROFILE_ID, capture_profile
from tests import test_session_ui as session_fixture


def fixture(root=None):
    case = session_fixture.SessionUiTests()
    case.setUpClass()
    try:
        if root is None:
            case.setUp()
        else:
            with patch("tests.test_session_ui.tempfile.TemporaryDirectory", return_value=nullcontext(str(root))):
                case.setUp()
    except BaseException:
        case.doCleanups()
        raise
    return case


def profile_pair(case):
    trigger, tank = case.saved_profile()
    tank["engines"]["triggevent"]["saved-call"] = {"enabled": False, "text": "Tank engine"}
    write_record(case.window._profiles_dir, tank)
    healer = capture_profile(case.window, "Healer")
    healer["local"][trigger.id].update(enabled=True, text="Healer setup")
    healer["engines"]["triggevent"]["saved-call"] = {"enabled": True, "text": "Healer engine"}
    write_record(case.window._profiles_dir, healer)
    case.window._profiles.append(healer)
    case.window._save_settings()
    case.window._save_triggers()
    return trigger, tank, healer


def crash_fixture(directory, transition, boundary):
    root = Path(directory)
    case = fixture(root)
    window = case.window
    trigger, tank, healer = profile_pair(case)
    if transition != "default-named":
        assert window._activate_profile(tank)
    previous = capture_profile(window, "Previous", window._active_profile_id)
    target = window._default_profile if transition == "named-default" else (
        healer if transition == "named-named" else tank)
    (root / "expected.json").write_text(json.dumps({"previous": previous, "target": target}))
    save_settings, save_triggers = window._save_settings, window._save_triggers
    write_intent = window._write_profile_intent
    settings_calls = 0
    trigger_calls = 0
    intent_calls = 0

    def settings():
        nonlocal settings_calls
        result = save_settings()
        settings_calls += 1
        if boundary.removeprefix("rollback-") == f"settings-{settings_calls}" and result:
            os._exit(23)
        return result

    def triggers():
        nonlocal trigger_calls
        trigger_calls += 1
        if boundary.startswith("rollback-") and trigger_calls == 1:
            return False
        result = save_triggers()
        if boundary in ("triggers", "rollback-triggers") and result:
            os._exit(23)
        return result

    def intent(profile):
        nonlocal intent_calls
        intent_calls += 1
        write_intent(profile)
        if boundary == "intent" or boundary == "rollback-intent" and intent_calls == 2:
            os._exit(23)

    window._save_settings = settings
    window._save_triggers = triggers
    window._write_profile_intent = intent
    window._activate_profile(target)
    raise AssertionError("interruption boundary was not reached")


class ProfileRecoveryTests(unittest.TestCase):
    def test_failed_fixture_setup_restores_global_paths_and_ui_hooks(self):
        saved = (ac._DATA_DIR, ac._SETTINGS_FILE, ac.TRIGGERS_LOCAL_FILE,
                 session_fixture.mw.QTimer.singleShot, ac.QMessageBox.warning)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.txt"
            evidence.write_text("keep")
            for target in (None, root):
                with self.subTest(root=target):
                    case = session_fixture.SessionUiTests()
                    self.addCleanup(case.doCleanups)
                    with patch.object(session_fixture, "SessionUiTests", return_value=case), \
                            patch.object(session_fixture.mw, "MainWindow", side_effect=RuntimeError("startup failed")):
                        with self.assertRaisesRegex(RuntimeError, "startup failed"):
                            fixture(target)
                    self.assertEqual((ac._DATA_DIR, ac._SETTINGS_FILE, ac.TRIGGERS_LOCAL_FILE,
                                      session_fixture.mw.QTimer.singleShot, ac.QMessageBox.warning), saved)
            self.assertEqual(evidence.read_text(), "keep")

    def start(self):
        case = fixture()
        self.addCleanup(case.doCleanups)
        return case

    def interrupted(self, transition="named-default", boundary="settings-1"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        result = subprocess.run([sys.executable, "-c",
            "import sys; from tests.test_profile_recovery_audit import crash_fixture; crash_fixture(*sys.argv[1:])",
            str(root), transition, boundary], capture_output=True, text=True, timeout=30,
            cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        return root, json.loads((root / "expected.json").read_text())

    def restart(self, root):
        case = self.start()
        shutil.copytree(root, case.temp, dirs_exist_ok=True)
        window = case.restart_profile_window()
        return case, window

    def assert_choices(self, window, expected):
        actual = capture_profile(window, "Actual", window._active_profile_id)
        self.assertEqual(actual["id"], expected["id"])
        self.assertEqual(actual["local"], expected["local"])
        for source, choices in expected["engines"].items():
            for ident, choice in choices.items():
                self.assertEqual(actual["engines"][source].get(ident, {"enabled": True, "text": None}), choice)
        self.assertNotIn("pending_trigger_profile", window._settings)

    def test_external_local_edits_survive_failed_apply_and_restart(self):
        for phase in ("before-save", "during-rollback", "after-failed-rollback"):
            with self.subTest(phase=phase):
                case = self.start()
                _, tank, _ = profile_pair(case)
                window = case.window
                external = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
                external["triggers"][0].update(tts_text="External edit", name="Keep name", cooldown_s=17)
                external["external_metadata"] = {"keep": True}
                payload = json.dumps(external).encode()
                save, write = window._save_settings, window._write_profile_intent
                calls = [0, 0]

                def settings():
                    calls[0] += 1
                    return False if phase == "during-rollback" and calls[0] == 1 else save()

                def intent(profile, **kwargs):
                    calls[1] += 1
                    write(profile, **kwargs)
                    if phase == "during-rollback" and calls[1] == 2:
                        ac.TRIGGERS_LOCAL_FILE.write_bytes(payload)

                if phase == "before-save":
                    ac.TRIGGERS_LOCAL_FILE.write_bytes(payload)
                with patch.object(window, "_save_settings", settings), \
                        patch.object(window, "_write_profile_intent", intent):
                    if phase == "after-failed-rollback":
                        with patch.object(window, "_save_triggers", return_value=False):
                            self.assertFalse(window._activate_profile(tank))
                        self.assertTrue(window._settings["pending_trigger_profile"])
                        ac.TRIGGERS_LOCAL_FILE.write_bytes(payload)
                    else:
                        self.assertFalse(window._activate_profile(tank))
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), payload)
                restarted = case.restart_profile_window()
                self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), payload)
                self.assertEqual(restarted._triggers[0].tts_text, "External edit")
                self.assertEqual(restarted._active_profile_id, DEFAULT_PROFILE_ID)
                self.assertNotIn("pending_trigger_profile", restarted._settings)

    def test_external_edit_after_interrupted_commit_is_preserved(self):
        root, records = self.interrupted("default-named", "settings-1")
        path = root / "triggers.local.json"
        external = json.loads(path.read_text())
        external["triggers"][0]["tts_text"] = "External after process death"
        external["external_metadata"] = ["keep"]
        payload = json.dumps(external).encode()
        path.write_bytes(payload)
        case, window = self.restart(root)
        self.assertEqual((case.temp / path.name).read_bytes(), payload)
        self.assertEqual(window._triggers[0].tts_text, "External after process death")
        self.assertEqual(window._active_profile_id, records["target"]["id"])
        self.assertNotIn("pending_trigger_profile", window._settings)

    def test_successful_local_write_still_rolls_back_after_settings_failure(self):
        case = self.start()
        _, tank, _ = profile_pair(case)
        old = ac.TRIGGERS_LOCAL_FILE.read_bytes()
        save = case.window._save_settings
        calls = []
        def settings():
            calls.append(True)
            return False if len(calls) == 2 else save()
        with patch.object(case.window, "_save_settings", settings):
            self.assertFalse(case.window._activate_profile(tank))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), old)
        self.assertEqual(case.window._triggers[0].tts_text, "Normal setup")

    def test_missing_target_fallback_keeps_external_local_bytes(self):
        root, records = self.interrupted("default-named", "settings-1")
        path = root / "triggers.local.json"
        external = json.loads(path.read_text())
        external["triggers"][0].update(tts_text="External fallback", name="External name")
        external["external_metadata"] = {"keep": True}
        payload = json.dumps(external).encode()
        path.write_bytes(payload)
        (root / "trigger_profiles" / (records["target"]["id"] + ".json")).unlink()
        case, window = self.restart(root)
        self.assertEqual((case.temp / path.name).read_bytes(), payload)
        self.assertEqual(window._triggers[0].tts_text, "External fallback")
        self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertNotIn("pending_trigger_profile", window._settings)

    def test_failed_preserve_record_leaves_external_file_for_next_recovery(self):
        case = self.start()
        _, tank, _ = profile_pair(case)
        external = json.loads(ac.TRIGGERS_LOCAL_FILE.read_text())
        external["triggers"][0]["tts_text"] = "External edit"
        payload = json.dumps(external).encode()
        ac.TRIGGERS_LOCAL_FILE.write_bytes(payload)
        write = case.window._write_profile_intent
        def intent(profile, **kwargs):
            if kwargs.get("preserve_local"):
                raise OSError("recovery record unavailable")
            write(profile, **kwargs)
        with patch.object(case.window, "_write_profile_intent", intent):
            self.assertFalse(case.window._activate_profile(tank))
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), payload)
        restarted = case.restart_profile_window()
        self.assertEqual(ac.TRIGGERS_LOCAL_FILE.read_bytes(), payload)
        self.assertEqual(restarted._triggers[0].tts_text, "External edit")

    def test_process_death_at_every_write_recovers_all_profile_directions(self):
        for transition in ("default-named", "named-default", "named-named"):
            for boundary in ("intent", "settings-1", "triggers", "settings-2"):
                with self.subTest(transition=transition, boundary=boundary):
                    root, records = self.interrupted(transition, boundary)
                    case, window = self.restart(root)
                    expected = records["previous" if boundary == "intent" else "target"]
                    self.assert_choices(window, expected)
                    if window._default_profile is not None:
                        self.assertEqual(window._default_profile["local"]["local"]["text"], "Normal setup")

    def test_restored_default_survives_later_switches_and_user_edits(self):
        root, _ = self.interrupted()
        case, window = self.restart(root)
        tank = next(p for p in window._profiles if p["name"] == "Tank")
        self.assertTrue(window._activate_profile(tank))
        self.assertTrue(window._activate_profile(window._default_profile))
        self.assertEqual(window._triggers[0].tts_text, "Normal setup")
        window._triggers[0].tts_text = "Edited after recovery"
        self.assertTrue(window._save_triggers())
        case.window = window
        restarted = case.restart_profile_window()
        self.assertEqual(restarted._triggers[0].tts_text, "Edited after recovery")

    def test_interrupted_rollback_restores_the_previous_complete_choices(self):
        for boundary in ("rollback-intent", "rollback-settings-2", "rollback-triggers", "rollback-settings-3"):
            with self.subTest(boundary=boundary):
                root, records = self.interrupted("default-named", boundary)
                _, window = self.restart(root)
                self.assert_choices(window, records["previous"])

    def test_committed_change_ignores_snapshot_left_by_failed_cleanup(self):
        case = self.start()
        trigger, tank, _ = profile_pair(case)
        unlink = Path.unlink

        def fail_cleanup(path, *args, **kwargs):
            if path == case.window._profile_intent_path():
                raise OSError("cleanup unavailable")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_cleanup):
            self.assertTrue(case.window._activate_profile(tank))
        self.assertTrue(case.window._profile_intent_path().exists())
        trigger.tts_text = "Keep this later edit"
        self.assertTrue(case.window._save_triggers())
        restarted = case.restart_profile_window()
        self.assertEqual(restarted._triggers[0].tts_text, "Keep this later edit")

    def test_missing_or_corrupt_snapshot_preserves_evidence_and_blocks_activation(self):
        for content in (None, "broken recovery record"):
            with self.subTest(content=content):
                root, _ = self.interrupted()
                pending = root / "trigger_profiles" / ".pending" / (DEFAULT_PROFILE_ID + ".json")
                if content is None:
                    pending.unlink()
                else:
                    pending.write_text(content)
                case, window = self.restart(root)
                self.assertIn("Could not recover", window._profile_status.text())
                self.assertFalse(window._activate_profile(window._profiles[0]))
                self.assertTrue(window._settings["pending_trigger_profile"])
                self.assertEqual(window._triggers[0].tts_text, "Tank setup")
                if content is not None:
                    self.assertEqual(window._profile_intent_path().read_text(), content)

    def test_missing_or_corrupt_named_target_follows_existing_default_policy(self):
        for content in (None, "broken named profile"):
            with self.subTest(content=content):
                root, records = self.interrupted("default-named")
                target = root / "trigger_profiles" / (records["target"]["id"] + ".json")
                if content is None:
                    target.unlink()
                else:
                    target.write_text(content)
                case, window = self.restart(root)
                self.assertEqual(window._active_profile_id, DEFAULT_PROFILE_ID)
                self.assertEqual(window._triggers[0].tts_text, "Normal setup")
                self.assertIn("returned to Default", window._profile_status.text())
                saved = case.temp / "trigger_profiles" / target.name
                self.assertEqual(saved.exists(), content is not None)
                if content is not None:
                    self.assertEqual(saved.read_text(), content)

    def test_valid_external_profile_edits_are_not_replaced_by_recovery(self):
        root, records = self.interrupted("default-named")
        changed = deepcopy(records["target"])
        changed["local"]["local"]["text"] = "External saved edit"
        write_record(root / "trigger_profiles", changed)
        case, window = self.restart(root)
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        saved = read_record(case.temp / "trigger_profiles" / (changed["id"] + ".json"))
        self.assertEqual(saved["local"]["local"]["text"], "External saved edit")

    def test_unreadable_default_and_missing_target_keep_recovery_evidence(self):
        root, records = self.interrupted("default-named")
        profiles = root / "trigger_profiles"
        (profiles / (records["target"]["id"] + ".json")).unlink()
        (profiles / (DEFAULT_PROFILE_ID + ".json")).write_text("damaged Default")
        case, window = self.restart(root)
        self.assertEqual(window._triggers[0].tts_text, "Tank setup")
        self.assertIn("Default could not be loaded", window._profile_status.text())
        self.assertTrue(window._profile_intent_path().exists())
        self.assertEqual((case.temp / "trigger_profiles" / (DEFAULT_PROFILE_ID + ".json")).read_text(),
                         "damaged Default")
        self.assertFalse(window._activate_profile(window._profiles[0]))

    def test_failed_rollback_snapshot_keeps_the_original_intent_recoverable(self):
        case = self.start()
        trigger, tank, healer = profile_pair(case)
        write = case.window._write_profile_intent
        calls = 0

        def fail_rollback(profile):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("rollback snapshot disk full")
            write(profile)

        with patch.object(case.window, "_write_profile_intent", side_effect=fail_rollback), \
                patch.object(case.window, "_save_triggers", return_value=False):
            self.assertFalse(case.window._activate_profile(tank))
        self.assertEqual(trigger.tts_text, "Normal setup")
        self.assertFalse(case.window._activate_profile(healer))
        restarted = case.restart_profile_window()
        self.assertEqual(restarted._active_profile_id, tank["id"])
        self.assertEqual(restarted._triggers[0].tts_text, "Tank setup")
        self.assertEqual(restarted._default_profile["local"]["local"]["text"], "Normal setup")

    def test_failed_rollback_remains_recoverable_and_rejects_new_activation(self):
        case = self.start()
        trigger, tank, healer = profile_pair(case)
        with patch.object(case.window, "_save_triggers", return_value=False):
            self.assertFalse(case.window._activate_profile(tank))
        self.assertEqual(trigger.tts_text, "Normal setup")
        self.assertTrue(case.window._settings["pending_trigger_profile"])
        self.assertFalse(case.window._activate_profile(healer))
        restarted = case.restart_profile_window()
        self.assertEqual(restarted._active_profile_id, DEFAULT_PROFILE_ID)
        self.assertEqual(restarted._triggers[0].tts_text, "Normal setup")
        self.assertNotIn("pending_trigger_profile", restarted._settings)


if __name__ == "__main__":
    unittest.main()
