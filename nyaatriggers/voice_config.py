"""Bounded Piper config checks shared by setup and voice selection."""

import json
import math
from pathlib import Path

MAX_VOICE_CONFIG_BYTES = 1 << 20


def _finite_number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def validate_voice_config(path: Path) -> None:
    path = Path(path)
    if not path.is_file():
        raise ValueError("Piper voice config is not a regular file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_VOICE_CONFIG_BYTES + 1)
    if len(raw) > MAX_VOICE_CONFIG_BYTES:
        raise ValueError("Piper voice config is too large")
    config = json.loads(raw.decode("utf-8"))
    if (not isinstance(config, dict)
            or any(not isinstance(config.get(key), dict)
                   for key in ("audio", "espeak", "phoneme_id_map"))):
        raise ValueError("Invalid Piper voice config")
    if (any(key not in config for key in ("num_symbols", "num_speakers"))
            or "sample_rate" not in config["audio"]
            or not isinstance(config["espeak"].get("voice"), str)
            or not config["phoneme_id_map"]
            or not isinstance(config.get("inference", {}), dict)
            or config.get("phoneme_type", "espeak") not in ("espeak", "text", "pinyin")):
        raise ValueError("Invalid Piper voice config")
    rate = config["audio"]["sample_rate"]
    if (not _finite_number(rate) or not 1 <= round(rate) <= 0x7FFFFFFF
            or not _finite_number(config["num_speakers"])):
        raise ValueError("Invalid Piper voice numeric settings")
    phonemes = config["phoneme_id_map"]
    if (not all(key in phonemes for key in ("^", "_", "$"))
            or any(not isinstance(ids, list) or any(
                not _finite_number(ident) or ident != int(ident)
                or not 0 <= ident < 2**63 for ident in ids)
                for ids in phonemes.values())):
        raise ValueError("Invalid Piper phoneme IDs")
    for key in ("noise_scale", "length_scale", "noise_w"):
        if key not in config.get("inference", {}):
            continue
        try:
            value = float(config["inference"][key])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Invalid Piper inference settings") from exc
        if (not math.isfinite(value) or not 0 <= value <= 3.4028234663852886e38
                or key == "length_scale" and value == 0):
            raise ValueError("Invalid Piper inference settings")


def voice_config_ok(path: Path) -> bool:
    try:
        validate_voice_config(path)
        return True
    except (OSError, ValueError, TypeError, RecursionError):
        return False
