from contextlib import contextmanager
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QFileDialog
from PyQt6.QtGui import QTextBlockUserData

from nyaatriggers import updater
from nyaatriggers.paths import bundle_root, data_root
from nyaatriggers.drop_log import log_drop
from nyaatriggers.locale_util import N_
from nyaatriggers.umad_chains import canon_status_key as _canon_status


_BUNDLE_DIR = bundle_root()
_ASSETS_DIR = _BUNDLE_DIR / "assets"
_DATA_DIR   = data_root()
TRIGGERS_FILE       = _ASSETS_DIR / "triggers.json"
TRIGGERS_LOCAL_FILE = _DATA_DIR   / "triggers.local.json"
# Retired IDs stop local edits from reviving withdrawn triggers.
RETIRED_FILE        = _ASSETS_DIR / "retired.json"
ZONE_NAMES_FILE     = _ASSETS_DIR / "zone_names.json"
CACTBOT_TIMELINES_FILE = _ASSETS_DIR / "cactbot_timelines.json"
# Version stamps let newer bundled triggers replace cached downloads.
_REPO_TRIGGERS_FILE    = _DATA_DIR / "triggers.repo.json"
_REPO_RETIRED_FILE     = _DATA_DIR / "retired.repo.json"
_REPO_TRIGGERS_VERSION = _DATA_DIR / "triggers.repo.version"
_REPO_TRIGGERS_BRANCH  = "main"


def _watched_trigger_files() -> tuple:
    """Converter output is loaded only at import."""
    return (TRIGGERS_FILE, _REPO_TRIGGERS_FILE, TRIGGERS_LOCAL_FILE)
# Update callout wording without rebuilding engines.
CALLOUT_DEFAULTS_FILE = _ASSETS_DIR / "callout_defaults.json"
TIMELINES_DIR       = _DATA_DIR   / "timelines"
# Keep downloads separate from bundled timelines.
_BUNDLE_TIMELINES_DIR = _BUNDLE_DIR / "timelines"
_SETTINGS_FILE              = _DATA_DIR   / "nyaatriggers_settings.json"
_TRIGGEVENT_INVENTORY_CACHE = _DATA_DIR   / "triggevent_inventory.json"
_TRIGGEVENT_INVENTORY_SEED  = _BUNDLE_DIR / "triggevent_inventory.seed.json"
_TRIGGEVENT_AUTOMARK_INVENTORY_CACHE = _DATA_DIR / "triggevent_automarkers.json"
_TRIGGEVENT_AUTOMARK_INVENTORY_SEED = _ASSETS_DIR / "triggevent_automarkers.seed.json"
# Triggernometry inventory has no bundled seed because packs are imported by users.
_TRIGGERNOMETRY_INVENTORY_CACHE = _DATA_DIR / "triggernometry_inventory.json"
# Imported sounds live with user data so updates preserve them.
_USER_SOUNDS_DIR = _DATA_DIR / "sounds"
_USER_VOICES_DIR = _DATA_DIR / "voices"
_CALLOUTS_JA_BUNDLE = _ASSETS_DIR / "callouts_ja.json"
_CALLOUTS_JA_CACHE  = _DATA_DIR   / "callouts_ja.cache.json"
_CALLOUTS_JA_MAX_BYTES = 4_000_000
_REPO_JSON_MAX_BYTES = 8_000_000
_TIMELINE_MAX_BYTES = 2_000_000
# Serve stale timelines while refreshing in the background.
_CACTBOT_TIMELINE_TTL_S = 7 * 24 * 3600
# Longer claims could suppress separate mechanics with the same text.
_CALLOUT_CLAIM_S = 0.5
# Give enabled local triggers time to claim guest text.
_GUEST_CALLOUT_DEFER_MS = 200
# cactbotSay may arrive as info before an alarm popup.
_GUEST_SEVERITY_RANK = {"info": 0, "alert": 1, "alarm": 2}
_VERSION            = "1.4.0"
_DISPLAY_VERSION    = updater.display_version(_VERSION)
if (_DATA_DIR / ".nyaa-version-x").exists():
    _DISPLAY_VERSION = "X"


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_strdict(value) -> dict:
    return {k: v for k, v in _as_dict(value).items()
            if isinstance(k, str) and isinstance(v, str)}


def _as_text_overrides(value) -> dict:
    return {k: v for k, v in _as_dict(value).items()
            if isinstance(k, str) and isinstance(v, dict)
            and all(isinstance(f, str) for f in v.values())}


def _as_strset(value) -> set:
    if not isinstance(value, (list, set, tuple)):
        return set()
    return {x for x in value if isinstance(x, str)}


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def persistence_warning(window, title, text, *, conflict=False):
    pending = getattr(window, "_deferred_persistence_warnings", None)
    if pending is not None:
        pending.append((title, text, conflict))
        return
    if conflict:
        window._local_conflict_dialog = True
    try:
        QMessageBox.warning(window, title, text)
    finally:
        if conflict:
            window._local_conflict_dialog = False


@contextmanager
def defer_persistence_warnings(window):
    if getattr(window, "_deferred_persistence_warnings", None) is not None:
        yield
        return
    window._deferred_persistence_warnings = []
    try:
        yield
    finally:
        pending = window._deferred_persistence_warnings
        window._deferred_persistence_warnings = None
        for title, text, conflict in pending:
            persistence_warning(window, title, text, conflict=conflict)


def _atomic_write_json(path: "Path", data, *, indent: "int | None" = None) -> None:
    _atomic_write_bytes(path, json.dumps(data, indent=indent, ensure_ascii=False).encode("utf-8"))


def _atomic_write_bytes(path: "Path", payload: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".nyaa-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_file(path: "Path") -> None:
    with open(path, "r+b") as f:
        os.fsync(f.fileno())


def _next_bad_name(path: "Path", cap: int = 100) -> "Path":
    candidate = path.with_name(path.name + ".bad")
    for n in range(1, cap):
        if not candidate.exists():
            break
        candidate = path.with_name(f"{path.name}.bad.{n}")
    return candidate


def _repo_download_version() -> "str | None":
    try:
        v = json.loads(_REPO_TRIGGERS_VERSION.read_text(encoding="utf-8"))
        # Source updates may keep the same version.
        if not updater.is_frozen() and any(
                bundled.exists() and bundled.stat().st_mtime_ns > _REPO_TRIGGERS_VERSION.stat().st_mtime_ns
                for bundled in (TRIGGERS_FILE, RETIRED_FILE)):
            return None
    except (OSError, ValueError, RecursionError):
        return None
    return v if isinstance(v, str) else None


def _sweep_stale_update_parts(tmpdir: "Path", older_than_s: float = 3600.0) -> None:
    cutoff = time.time() - older_than_s
    try:
        for part in Path(tmpdir).glob("NyaaTriggers-*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
        for directory in Path(tmpdir).glob("nyaatriggers-download-*"):
            try:
                if not directory.is_dir() or directory.is_symlink():
                    continue
                entries = list(directory.iterdir())
                if not entries or directory.stat().st_mtime >= cutoff:
                    continue
                names = (updater.LINUX_ASSET, updater.WINDOWS_ASSET)
                if all(entry.is_file() and not entry.is_symlink()
                       and entry.stat().st_mtime < cutoff
                       and (entry.name in names or any(
                           re.fullmatch(re.escape(name) + r"\.[0-9]+\.[0-9]+\.part", entry.name)
                           for name in names)) for entry in entries):
                    for entry in entries:
                        entry.unlink()
                    directory.rmdir()
            except OSError:
                pass
    except OSError:
        pass


def _hex_id(value: str) -> int:
    try:
        return int(str(value).strip(), 16)
    except (TypeError, ValueError):
        return 0


def _bare_fight_tag(tag: str) -> str:
    """Normalize fight tags and reject path traversal."""
    if "/" in tag or "\\" in tag or ".." in tag:
        return ""
    return tag


_zone_names_cache: "dict | None" = None


def canonical_zone_name(zone_id: int) -> str:
    """Return the English zone name, or an empty string if unavailable."""
    global _zone_names_cache
    if _zone_names_cache is None:
        try:
            data = json.loads(ZONE_NAMES_FILE.read_text(encoding="utf-8"))
            _zone_names_cache = {str(k): v for k, v in data.items()
                                 if isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError, AttributeError, RecursionError):
            _zone_names_cache = {}
    try:
        return _zone_names_cache.get(str(int(zone_id)), "")
    except (TypeError, ValueError):
        return ""


_cactbot_tl_cache: "dict | None" = None


def cactbot_timeline_for_zone(zone_id: int) -> "tuple[str, str]":
    """Return the cactbot timeline tag and path, or an empty tuple if unavailable."""
    global _cactbot_tl_cache
    if _cactbot_tl_cache is None:
        try:
            data = json.loads(CACTBOT_TIMELINES_FILE.read_text(encoding="utf-8"))
            _cactbot_tl_cache = {
                str(k): (v["tag"], v["txt_path"]) for k, v in data.items()
                if isinstance(v, dict)
                   and isinstance(v.get("tag"), str)
                   and isinstance(v.get("txt_path"), str)
            } if isinstance(data, dict) else {}
        except (OSError, ValueError, AttributeError, RecursionError):
            _cactbot_tl_cache = {}
    try:
        return _cactbot_tl_cache.get(str(int(zone_id)), ())
    except (TypeError, ValueError):
        return ()


class _PhrasePattern:
    """Match literal fragments in order without backtracking through token values."""

    def __init__(self, parts, tokens=(), restrict_choices=True):
        self.parts = tuple(parts)
        self.tokens = tuple(tokens)
        self.durations = tuple("duration" in token.lower() for token in tokens)
        self.choices = []
        for token in tokens:
            branches = token.rsplit("?", 1)[-1] if "?" in token else ""
            choice = re.fullmatch(r'''\s*(['"])([^'"]*)\1\s*:\s*(['"])([^'"]*)\3\s*\}+''', branches)
            self.choices.append((choice[2], choice[4]) if choice and restrict_choices else None)

    def values(self, text: str):
        if not text.startswith(self.parts[0]):
            return None
        values = []
        position = len(self.parts[0])
        for index, part in enumerate(self.parts[1:-1], 1):
            markers = [part]
            if index < len(self.choices) and self.choices[index]:
                markers = [part + choice + self.parts[index + 1]
                           for choice in self.choices[index]]
            found = min((offset for marker in markers
                         if (offset := text.find(marker, position)) >= 0), default=-1)
            if found < 0:
                return None
            values.append(text[position:found])
            position = found + len(part)
        suffix = self.parts[-1]
        end = len(text)
        if not text.endswith(suffix, position):
            if not text.endswith("\n") or not text.endswith(suffix, position, end - 1):
                return None
            end -= 1
        values.append(text[position:end - len(suffix) if suffix else end])
        if any(choices is not None and value not in choices
               for value, choices in zip(values, self.choices)):
            return None
        if any(duration and not re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*(?:s|sec|seconds))?", value)
               for value, duration in zip(values, self.durations)):
            return None
        return values

    def match(self, text: str) -> bool:
        return self.values(text) is not None

    def render(self, text, translated, phrases):
        values = self.values(text)
        if values is None:
            return None
        replacements = dict(zip(self.tokens, values))
        parts, tokens = _phrase_template(translated)
        out = [parts[0]]
        for token, tail in zip(tokens, parts[1:]):
            value = replacements.get(token, token)
            translated_value = phrases.get(value)
            if translated_value is None:
                items = re.split(r"[,、/]\s*", value.strip("[]"))
                if len(items) > 1 and all(item in phrases for item in items):
                    translated_value = "、".join(phrases[item] for item in items)
            out.extend((translated_value or value, tail))
        return "".join(out)


def _phrase_template(text: str):
    parts = []
    tokens = []
    start = 0
    while True:
        left = text.find("{", start)
        if left < 0:
            parts.append(text[start:])
            return parts, tokens
        depth, right = 1, left + 1
        while right < len(text) and depth:
            if text[right] == "{":
                depth += 1
            elif text[right] == "}":
                depth -= 1
            right += 1
        if depth:
            parts.append(text[start:])
            return parts, tokens
        parts.append(text[start:left])
        tokens.append(text[left:right])
        start = right


def _compile_phrase_patterns(phrases: dict, *, minimum_literal=6, restrict_choices=True) -> list:
    """Match specific templates and substitute captured values without evaluation."""
    out = []
    for en, ja in phrases.items():
        parts, tokens = _phrase_template(en)
        if len(parts) == 1:
            continue
        ja_tokens = _phrase_template(ja)[1]
        if not set(ja_tokens) <= set(tokens):
            continue
        if any(not part for part in parts[1:-1]):
            continue
        literal = "".join(parts)
        if len(re.sub(r"[\W_]+", "", literal)) < minimum_literal:
            continue
        out.append((_PhrasePattern(parts, tokens, restrict_choices), ja))
    out.sort(key=lambda item: sum(len(part) for part in item[0].parts), reverse=True)
    return out

MAX_ABILITY_LINES = 200
MAX_RAW_CAPTURE = 20000

# Bound matching time to keep the GUI responsive.
_DISPATCH_BUDGET_S = 1.0

try:
    from nyaatriggers.convert_cactbot import TARGETS as _CB_TARGETS
    FIGHT_TO_CACTBOT_TXT = {
        tag: (rel[:-3] + ".txt") if rel.endswith(".ts") else rel
        for rel, tag in _CB_TARGETS if tag
    }
except Exception as _cb_exc:  # noqa: BLE001
    print(f"[NyaaTriggers] convert_cactbot unavailable, no cactbot timeline map: "
          f"{_cb_exc!r}", file=sys.stderr)
    FIGHT_TO_CACTBOT_TXT = {}

_CACTBOT_DATA_RAW = (
    "https://raw.githubusercontent.com/OverlayPlugin/cactbot/main/ui/raidboss/data"
)

_C_EN    = 0
_C_ZONE  = 1
_C_NAME  = 2
_C_FIGHT = 3
_C_TYPE  = 4
_C_RE    = 5
_C_TTS   = 6
_HEADERS = ("", N_("Zone"), N_("Name"), N_("Fight"), N_("Type"),
            N_("Ability / ID"), N_("TTS Text"))

_DOT_GREEN = "#a6e3a1"
_DOT_RED   = "#f38ba8"
_DOT_GREY  = "#585b70"

_ABILITY_TYPES = {"20", "21", "22", "23"}
_GENERAL_TAB   = "General"

_ITEM_TYPE_ROLE   = Qt.ItemDataRole.UserRole + 1   # str, one of folder, custom_hdr, custom_group
_ITEM_ID_ROLE     = Qt.ItemDataRole.UserRole + 2   # str, folder UUID
_SECTION_ROLE     = Qt.ItemDataRole.UserRole + 4   # str, row's source group, general/dot/local/engine

_GITHUB_URL  = "https://github.com/CateDesu/NyaaTriggers"
_DISCORD_URL = "https://discord.com/invite/TQJrbZcgKF"

_PIPER_VOICES_URL = "https://rhasspy.github.io/piper-samples/"

DEFAULT_TELESTO_URI = "http://localhost:45678/"

# Compound statuses require both effects on one player and defer to the chain controller.
# UMAD preset rules start unassigned. Status evidence is in docs/UMAD-DEBUFFS.md.
_UMAD_FIGHT_TAG = "UMAD"
_UMAD_FIGHT_TAG_CF = _UMAD_FIGHT_TAG.casefold()
_UMAD_AUTOMARK_PRESET: "list[tuple[str, str]]" = [
    # Distinguish Accretion carriers by their line order status.
    ("644+BBC", "Accretion (1st in Line) - cleansed first"),
    ("644+BBD", "Accretion (2nd in Line) - cleansed second"),
    # Use real wounds 15A5 and 15A6, excluding fake variants 1317 and 1318.
    ("15A7", "Cursed Shriek - gaze (real: look away / fake: look at)"),
    ("15A8", "Forked Lightning - real: spread / fake: stack"),
    ("15A9", "Compressed Water - stack marker"),
    ("15AA", "Acceleration Bomb - real: stay still / fake: keep moving"),
    ("15A5", "White Wound - real: lethal in White Antilight / fake: Black"),
    ("15A6", "Black Wound - real: lethal in Black Antilight / fake: White"),
    ("566",  "Beyond Death - real: must take lethal / fake: avoid lethal"),
    ("1C6",  "Allagan Field - real: avoid lethal / fake: must take lethal"),
]

_UMAD_STATUS_LABELS: "dict[str, str]" = {
    _canon_status(h): label for h, label in _UMAD_AUTOMARK_PRESET
}

# Fight tags must match triggers.json.
_FIGHT_TREE = [
    (N_("Ultimates"), [
        (N_("Dawntrail"),      ["FRU", "UMAD"]),
        (N_("Endwalker"),      ["TOP", "DSR"]),
        (N_("Shadowbringers"), ["TEA"]),
        (N_("Stormblood"),     ["UwU", "UCoB"]),
    ]),
    (N_("Savage Raids"), [
        (N_("Dawntrail"),      ["M12S", "M11S", "M10S", "M9S", "M8S", "M7S",
                            "M6S", "M5S", "M4S", "M3S", "M2S", "M1S"]),
        (N_("Endwalker"),      ["P12S", "P11S", "P10S", "P9S", "P8S", "P7S",
                            "P6S", "P5S", "P4S", "P3S", "P2S", "P1S"]),
        (N_("Shadowbringers"), ["E12S", "E8S", "E4S", "E3S"]),
        (N_("Stormblood"),     ["O12S", "O11S", "O10S", "O8S", "O7S", "O4S", "O3S"]),
        (N_("Heavensward"),    ["A11S", "A10S"]),
    ]),
    (N_("Extreme Trials"), [
        (N_("Dawntrail"),      ["Zelenia EX", "Enuo EX", "Doomtrain EX",
                            "Queen EX", "Valigarmanda EX", "Zoraal Ja EX"]),
        (N_("Endwalker"),      ["Zeromus EX", "Golbez EX", "Rubicante EX",
                            "Endsinger EX", "Hydaelyn EX", "Zodiark EX"]),
        (N_("Shadowbringers"), ["Warrior of Light EX", "Hades EX", "Innocence EX"]),
        (N_("Stormblood"),     ["Seiryu EX"]),
        (N_("A Realm Reborn"), ["Ultima's Bane EX"]),
    ]),
    (N_("Unreal Trials"), [
        (N_("Dawntrail"),      ["Shinryu Unreal"]),
        (N_("Endwalker"),      ["Ultima Unreal"]),
    ]),
    (N_("Criterion Dungeons"), [
        (N_("Endwalker"),      ["Another Aloalo Island", "Another Sil'dihn Subterrane"]),
    ]),
    (N_("Deep Dungeons"), [
        (N_("Endwalker"),      ["Eureka Orthos"]),
    ]),
    (N_("Field Operations"), [
        (N_("Shadowbringers"), ["Zadnor", "Delubrum Reginae", "Bozja"]),
    ]),
    (N_("Normal Raids"), [
        (N_("Dawntrail"),      ["M12N", "M11N", "M10N", "M9N", "M8N", "M7N",
                            "M6N", "M5N", "M4N", "M3N", "M2N", "M1N"]),
        (N_("Endwalker"),      ["P10N", "P9N", "P8N", "P7N", "P6N", "P5N",
                            "P4N", "P3N", "P2N", "P1N"]),
        (N_("Shadowbringers"), ["E8N", "E4N"]),
        (N_("Stormblood"),     ["O11N", "O10N", "O8N", "O7N", "O4N", "O3N"]),
        (N_("Heavensward"),    ["A11N", "A10N", "A6N", "A1N"]),
    ]),
    (N_("Normal Trials"), [
        (N_("Endwalker"),      ["The Final Day", "The Mothercrystal", "The Dark Inside"]),
        (N_("Shadowbringers"), ["Seat of Sacrifice", "The Dying Gasp",
                            "The Dancing Plague", "The Crown of the Immaculate"]),
        (N_("Stormblood"),     ["The Wreath of Snakes"]),
    ]),
    (N_("Alliance Raids"), [
        (N_("Dawntrail"),      ["Windurst: The Third Walk"]),
        (N_("Endwalker"),      ["Euphrosyne", "Aglaia"]),
        (N_("Shadowbringers"), ["The Tower at Paradigm's Breach", "The Puppets' Bunker",
                            "The Copied Factory"]),
        (N_("Stormblood"),     ["The Orbonne Monastery", "The Ridorana Lighthouse",
                            "The Royal City of Rabanastre"]),
        (N_("Heavensward"),    ["Dun Scaith", "The Weeping City of Mhach", "The Void Ark"]),
        (N_("A Realm Reborn"), ["The World of Darkness", "Syrcus Tower",
                            "The Labyrinth of the Ancients"]),
    ]),
]

_TREE_FIGHTS = {fight for _cat, _exps in _FIGHT_TREE
                for _exp, _fights in _exps for fight in _fights}


# Keep real names such as Dead or Face. Hex placeholders need a digit and four characters.
_UNKNOWN_NAME_RE = re.compile(r"^\s*unknown_[0-9a-f]+\s*$", re.IGNORECASE)
_BARE_HEX_RE     = re.compile(r"^\s*(0x[0-9a-f]+|(?=[0-9a-f]*[0-9])[0-9a-f]{4,})\s*$", re.IGNORECASE)


def _clean_ability_name(raw: str, fallback: str = "New Trigger") -> str:
    name = (raw or "").strip()
    if not name:
        return fallback
    if _UNKNOWN_NAME_RE.match(name) or _BARE_HEX_RE.match(name):
        return fallback
    return " ".join(name.split())


def _prefill_name_tts(raw_name: str, source: str = "", target: str = "",
                      me: str = "") -> tuple[str, str]:
    """Qualify distinct targets, omitting self casts and missing targets."""
    name = _clean_ability_name(raw_name)
    tgt  = (target or "").strip()
    src  = (source or "").strip()
    suffix = ""
    if tgt:
        if me and tgt.casefold() == me.casefold():
            suffix = " on you"
        elif tgt.casefold() != src.casefold() and tgt.casefold() != name.casefold():
            suffix = " on {target}"
    return name, name + suffix


_TV_PREVIEW_TOKENS = [
    (re.compile(r"\{event\.estimatedRemainingDuration[^{}]*\}", re.IGNORECASE), "5 seconds"),
    (re.compile(r"\{event\.target(?:\.[\w().]+)?\}", re.IGNORECASE), "you"),
    (re.compile(r"\{event\.source(?:\.[\w().]+)?\}", re.IGNORECASE), "the boss"),
    (re.compile(r"\{event\.ability(?:\.[\w().]+)?\}", re.IGNORECASE), "the ability"),
    (re.compile(r"\{event\.buff(?:\.[\w().]+)?\}", re.IGNORECASE), "the buff"),
    (re.compile(r"\{target\}", re.IGNORECASE), "you"),
    (re.compile(r"\{source\}", re.IGNORECASE), "the boss"),
]


_JP_NEURAL_VOICES = (("jf_alpha", "(JPN) Alpha"), ("jm_kumo", "(JPN) Kumo"))

# ISO 639-1 to the 3 letter tag shown in the Model dropdown.
_VOICE_LANG_TAGS = {"en": "ENG", "ja": "JPN", "de": "GER", "fr": "FRE", "es": "SPA"}


def _voice_display(stem: str) -> str:
    """Keep unrecognized voice filenames unchanged."""
    m = re.match(r"([a-z]{2})_[A-Z]{2}-([A-Za-z0-9]+)", stem)
    if m:
        lang = _VOICE_LANG_TAGS.get(m.group(1), m.group(1).upper())
        return f"({lang}) {m.group(2).capitalize()}"
    return stem


def _engine_preview_text(s: str) -> str:
    s = s or ""
    for pat, val in _TV_PREVIEW_TOKENS:
        s = pat.sub(val, s)
    s = re.sub(r"\{[^{}]*\}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _stale_gen(bridge, gen) -> bool:
    """Reject stale bridge output while allowing internal calls without a generation."""
    return gen is not None and (bridge is None or gen != bridge.generation())


class _AbilityData(QTextBlockUserData):
    __slots__ = ("log_type", "ability_name", "ability_id", "source", "target")

    def __init__(self, log_type: str, ability_name: str, ability_id: str = "",
                 source: str = "", target: str = "") -> None:
        super().__init__()
        self.log_type     = log_type
        self.ability_name = ability_name
        self.ability_id   = ability_id
        self.source       = source
        self.target       = target
