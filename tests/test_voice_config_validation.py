from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install
from nyaatriggers import paths
from nyaatriggers.voice_config import validate_voice_config, voice_config_ok


VALID = (Path(__file__).resolve().parents[1] / "voices" /
         "en_US-arctic-medium.onnx.json").read_bytes()


def changed_config(keys, value):
    config = json.loads(VALID)
    target = config
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    return json.dumps(config).encode()


def unusable_configs():
    for value in (None, "bad", 0, -1, float("inf"), float("nan"), 2**32):
        yield "sample rate " + repr(value), changed_config(("audio", "sample_rate"), value)
    for value in (None, {}, "18", float("nan")):
        yield "speakers " + repr(value), changed_config(("num_speakers",), value)
    for missing in ("^", "_", "$"):
        config = json.loads(VALID)
        del config["phoneme_id_map"][missing]
        yield "missing " + missing, json.dumps(config).encode()
    for value in (None, 14, [None], [{}], [-1], [1.5], [2**63], [float("nan")]):
        yield "phoneme IDs " + repr(value), changed_config(("phoneme_id_map", "a"), value)
    for key, value in (("noise_scale", {}), ("noise_w", float("inf")),
                       ("length_scale", "bad"), ("length_scale", 0)):
        yield key + " " + repr(value), changed_config(("inference", key), value)


class VoiceConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = self.root / "en_US-arctic-medium.onnx.json"

    def test_configs_that_fail_synthesis_are_rejected_before_use(self):
        for label, raw in unusable_configs():
            with self.subTest(config=label):
                self.config.write_bytes(raw)
                self.assertFalse(voice_config_ok(self.config))
                with self.assertRaises(ValueError):
                    validate_voice_config(self.config)

    def test_working_numeric_and_optional_metadata_remains_supported(self):
        for phoneme_type in (None, "espeak", "text", "pinyin"):
            for inference in (None, {"noise_scale": "0.667", "length_scale": 1.0,
                                      "noise_w": 0}):
                with self.subTest(phoneme_type=phoneme_type, inference=inference):
                    config = json.loads(VALID)
                    config["num_symbols"] = 0
                    config["num_speakers"] = float(config["num_speakers"])
                    config["audio"]["sample_rate"] = float(config["audio"]["sample_rate"])
                    config["phoneme_id_map"]["a"] = [14.0]
                    config.pop("phoneme_type", None)
                    config.pop("inference", None)
                    if phoneme_type is not None:
                        config["phoneme_type"] = phoneme_type
                    if phoneme_type == "text":
                        config["espeak"]["voice"] = ""
                    if inference is not None:
                        config["inference"] = inference
                    self.config.write_text(json.dumps(config))
                    validate_voice_config(self.config)

    def test_default_selection_uses_the_working_bundled_config(self):
        user, bundle = self.root / "user", self.root / "bundle"
        for directory in (user, bundle):
            (directory / "voices").mkdir(parents=True)
            (directory / "voices/en_US-arctic-medium.onnx").write_bytes(b"model")
        bad = changed_config(("audio", "sample_rate"), "bad")
        (user / "voices" / self.config.name).write_bytes(bad)
        (bundle / "voices" / self.config.name).write_bytes(VALID)
        with patch.object(paths, "data_root", return_value=user), \
                patch.object(paths, "bundle_root", return_value=bundle):
            self.assertEqual(paths.default_voice_dir(), bundle / "voices")
        self.assertEqual((user / "voices" / self.config.name).read_bytes(), bad)

    def test_installer_repairs_invalid_values_and_rejects_a_bad_replacement(self):
        model = self.root / "en_US-arctic-medium.onnx"
        model.write_bytes(b"existing model")
        previous = changed_config(("audio", "sample_rate"), "bad")
        self.config.write_bytes(previous)

        def response(raw):
            stream = io.BytesIO(raw)
            stream.headers = {"Content-Length": str(len(raw))}
            return stream

        with patch.multiple(install, VOICES_DIR=self.root, VOICE_FILE=model), \
                patch.object(install, "_voice_model_ok", return_value=True), \
                patch.object(install, "open_response") as download, \
                redirect_stdout(io.StringIO()):
            download.return_value = response(changed_config(("num_speakers",), {}))
            with self.assertRaises(ValueError):
                install.download_voice()
            self.assertEqual(self.config.read_bytes(), previous)
            self.assertFalse(list(self.root.glob("*.part")))
            download.return_value = response(VALID)
            install.download_voice()
            self.assertEqual(self.config.read_bytes(), VALID)
            self.assertEqual(download.call_count, 2)
            self.assertTrue(all(call.args[0].endswith(".onnx.json")
                                for call in download.call_args_list))
            self.assertEqual(model.read_bytes(), b"existing model")


if __name__ == "__main__":
    unittest.main()
