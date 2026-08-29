"""Shared module-level constants and helpers.

Everything main_window.py used to carry at module scope: data file paths,
size and time limits, the version stamps, the JSON coercion and atomic write
helpers, the zone and cactbot timeline caches, the fight tree and the UMAD
preset tables. main_window and the ui mixin modules all import from here so
the split never runs into a circular import.
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QFileDialog
from PyQt6.QtGui import QTextBlockUserData

import updater
from drop_log import log_drop
from locale_util import N_
from umad_chains import canon_status_key as _canon_status


_BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
_DATA_DIR   = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
TRIGGERS_FILE       = _BUNDLE_DIR / "triggers.json"
TRIGGERS_LOCAL_FILE = _DATA_DIR   / "triggers.local.json"
# Ids withdrawn from triggers.json. Dropping a row from triggers.json only hides it
# from clients that never touched it. The merge re-appends any local copy whose id is
# no longer official, so a toggled trigger would outlive its own removal. Kept out of
# triggers.json because that file must stay a bare list. An older client downloading
# a dict from main would parse zero triggers.
RETIRED_FILE        = _BUNDLE_DIR / "retired.json"
# Zone id to English zone name, from tools/gen_zone_names.py, sourced from cactbot.
# The feed reports the zone name in the client's language but every shipped
# zone_regex is English, so on a non-English client no Local trigger can ever
# match its zone. The sidecars key on the numeric zone id so they keep calling
# out. This map lets the local engine match the English name too.
ZONE_NAMES_FILE     = _BUNDLE_DIR / "zone_names.json"
# Zone id to cactbot timeline, from tools/gen_cactbot_timelines.py, generated
# from the cactbot source tree. Covers every fight cactbot ships a .txt
# timeline for. Keyed on the numeric zone id like ZONE_NAMES_FILE, so a fight
# with no local trigger file still gets its timeline bars whatever language
# the client reports. This is the primary cactbot source. The small
# FIGHT_TO_CACTBOT_TXT converter map below stays as fallback.
CACTBOT_TIMELINES_FILE = _BUNDLE_DIR / "cactbot_timelines.json"
# Downloaded repo trigger set, behind the Settings Update Triggers and Restore
# from Repo buttons. Distinct untracked names, mirroring the _CALLOUTS_JA_CACHE
# pattern below. On a source checkout _DATA_DIR == _BUNDLE_DIR, so writing
# triggers.json there clobbers the git tracked file and blocks git pull self
# updates. The .version stamp records the _VERSION that fetched the download.
# A newer build's freshly bundled set wins over a stale download.
_REPO_TRIGGERS_FILE    = _DATA_DIR / "triggers.repo.json"
_REPO_RETIRED_FILE     = _DATA_DIR / "retired.repo.json"
_REPO_TRIGGERS_VERSION = _DATA_DIR / "triggers.repo.version"
# Branch the download follows. master is the trunk. Trigger fixes land there
# first and the app's own update stream, Master pre-releases and git checkouts,
# tracks it. main only advances when a Stable is promoted off master, so
# fetching main would hand every install the set it already bundles.
_REPO_TRIGGERS_BRANCH  = "master"


def _watched_trigger_files() -> tuple:
    """The files _load_triggers merges, polled by the 30 s tick for on-disk
    changes. Hot reload. convert_cactbot.py output is import time by design
    and not watched on purpose."""
    return (TRIGGERS_FILE, _REPO_TRIGGERS_FILE, TRIGGERS_LOCAL_FILE)
# Shipped rewrites for sidecar callout text, keyed source then trigger id. Seeds the
# same path as a user's own callout edit, so wording fixes ship without a jar rebuild.
CALLOUT_DEFAULTS_FILE = _BUNDLE_DIR / "callout_defaults.json"
TIMELINES_DIR       = _DATA_DIR   / "timelines"
_SETTINGS_FILE              = _DATA_DIR   / "nyaatriggers_settings.json"
# Last Triggevent inventory harvest. Lets rows list before the engine starts.
_TRIGGEVENT_INVENTORY_CACHE = _DATA_DIR   / "triggevent_inventory.json"
# Read only fallback for fresh installs. The writable cache above supersedes it once
# the sidecar reports. Distinct filename so it can be committed and bundled without
# a dev run's cache write clobbering it.
_TRIGGEVENT_INVENTORY_SEED  = _BUNDLE_DIR / "triggevent_inventory.seed.json"
# Last Triggernometry inventory harvest. No bundled seed here, packs are user imported.
_TRIGGERNOMETRY_INVENTORY_CACHE = _DATA_DIR / "triggernometry_inventory.json"
# User imported alert SFX from the Import SFX button. Built ins ship read only in
# _BUNDLE_DIR/sounds. Imports land next to user data so they survive updates.
_USER_SOUNDS_DIR = _DATA_DIR / "sounds"
# User dropped Piper voices. Same split again. The bundled default voice ships
# read only in _BUNDLE_DIR/voices, which on a frozen build is _internal and gets
# deleted by every self update. So user voices live next to the exe, where user
# data survives. On a source checkout the two dirs coincide.
_USER_VOICES_DIR = _DATA_DIR / "voices"
# Per callout Japanese overlay, trigger id to translated tts_text. The committed
# copy ships bundled. The background refresh writes a SEPARATE writable cache under
# a distinct filename so a source checkout's download can't clobber the committed
# file, since _DATA_DIR == _BUNDLE_DIR there. Cache wins when present.
_CALLOUTS_JA_BUNDLE = _BUNDLE_DIR / "callouts_ja.json"
_CALLOUTS_JA_CACHE  = _DATA_DIR   / "callouts_ja.cache.json"
_CALLOUTS_JA_MAX_BYTES = 4_000_000
# Same idea for the other GitHub fetches. urlopen's timeout caps time, not
# size, so every read is bounded before json.loads or persist can amplify it.
_REPO_JSON_MAX_BYTES = 8_000_000     # triggers.json runs about 300 KB today
_TIMELINE_MAX_BYTES = 2_000_000      # cactbot .txt timelines run tens of KB
# Downloaded cactbot timelines carry no upstream validator, so the file mtime,
# stamped by the atomic replace on fetch, is the age stamp. Past the TTL the
# cached copy still serves while a re fetch runs in the background.
_CACTBOT_TIMELINE_TTL_S = 7 * 24 * 3600
# Cross source callout de duplication. An own trigger callout claims its text
# for this long. A guest cactbot callout arriving inside the window is silenced
# so the same mechanic is not called out twice. Kept short, only wide enough to
# cover the own vs guest race. A guest lands within a few hundred ms of the own
# trigger for the same mechanic. A long window here would collapse distinct
# mechanics that happen to share short callout text like Spread, Stack, or
# recurring autos, and drop legitimate callouts.
_CALLOUT_CLAIM_S = 0.5
# Guests wait this long before speaking so an own trigger firing for the same
# mechanic can claim the text first. A guest is supplementary, so a short wait
# beats double firing. Skipped when own triggers are off, pure guest mode.
_GUEST_CALLOUT_DEFER_MS = 200
# Guest callouts carry the shared info/alert/alarm vocabulary. When two guests
# with the same text collapse, the higher tier must win. cactbotSay is always
# info and usually lands before the popup, which carries the real tier, for the
# same cactbot trigger. A plain first wins drop would skip the alarm sound and
# push the downgraded tier to the overlay.
_GUEST_SEVERITY_RANK = {"info": 0, "alert": 1, "alarm": 2}
_VERSION            = "1.4.0"
# What the Settings version line and the sidebar brand version show. Frozen
# builds carry the full rolling stamp in _VERSION already. Git checkouts get
# the nearest rolling tag, something like 1.3.0.165+9. Source copies get
# -src. Logic like update checks and version stamps keeps using plain
# _VERSION.
_DISPLAY_VERSION    = updater.display_version(_VERSION)
# Local dev marker, gitignored so it never leaves this machine. With the
# file present the Settings version line shows a literal X instead of the
# number. Everyone else, git checkout or frozen release, keeps the real
# rolling version.
if (_DATA_DIR / ".nyaa-version-x").exists():
    _DISPLAY_VERSION = "X"


def _as_dict(value) -> dict:
    """Settings value coerced to a dict. {} on the wrong type so a corrupt file
    can't raise out of __init__ and block startup."""
    return value if isinstance(value, dict) else {}


def _as_strdict(value) -> dict:
    """Settings value coerced to a dict of str to str. Entries of any other
    shape are dropped so a hand edited file can't park junk in here that the
    callout text consumers then raise on."""
    return {k: v for k, v in _as_dict(value).items()
            if isinstance(k, str) and isinstance(v, str)}


def _as_text_overrides(value) -> dict:
    """Settings value coerced to the engine text override shape, a str key to
    a dict of str fields. Same hand edit guard as _as_strdict. The consumers
    .get and subscript the inner dicts."""
    return {k: v for k, v in _as_dict(value).items()
            if isinstance(k, str) and isinstance(v, dict)
            and all(isinstance(f, str) for f in v.values())}


def _as_strset(value) -> set:
    """Settings value coerced to a set of ids. isinstance check, never a bare
    set call, since set on a plain string would silently yield a char set.
    Non-string elements go too. A hand edited list can park ints in it, and
    ids are strings. A mixed-type set makes every sorted call on it raise
    TypeError, which would abort startup and checklist toggles alike."""
    if not isinstance(value, (list, set, tuple)):
        return set()
    return {x for x in value if isinstance(x, str)}


def _as_str(value) -> str:
    """Value coerced to a plain string. "" on the wrong type so a hand edited
    or poisoned payload can't park a truthy non-string where a sorted call or
    a .lower would raise on it later."""
    return value if isinstance(value, str) else ""


def _atomic_write_json(path: "Path", data, *, indent: "int | None" = None) -> None:
    """Write JSON atomically. Sibling .tmp then os.replace, so a crash mid write
    can't truncate the file. The tmp stays in the same directory since os.replace
    is only atomic within one filesystem. Raises OSError like write_text.

    The tmp name is pid and tid suffixed so two concurrent writers of the same
    path never share the same .tmp, which would interleave writes. Today every
    path has a single writer, but the suffix keeps that invariant unbreakable
    and mirrors main._download's part name scheme."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    payload = json.dumps(data, indent=indent, ensure_ascii=False)
    try:
        # Create the tmp owner only, 0600. nyaatriggers_settings.json stores the
        # FFLogs OAuth client_secret alongside other settings. A plain write_text
        # would inherit the umask, often 0644, and leave creds world readable on
        # shared hosts. 0600 survives a 022 umask.
        def _owner_only(p, flags):
            return os.open(p, flags, 0o600)
        with open(tmp, "w", encoding="utf-8", opener=_owner_only) as f:
            f.write(payload)
            # A rename can commit before the data hits disk, so without the
            # fsync a power loss right after this returns can leave the
            # destination at 0 bytes.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except (OSError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_file(path: "Path") -> None:
    """Flush one file's data to disk. A rename can commit before the data
    does, so the tmp plus rename writers fsync before the os.replace, or a
    power loss in between can leave the destination at 0 bytes."""
    with open(path, "r+b") as f:
        os.fsync(f.fileno())


def _next_bad_name(path: "Path", cap: int = 100) -> "Path":
    """Next free .bad backup path for a corrupt file. .bad, then .bad.1,
    .bad.2 and so on. Rotating keeps a second corruption from overwriting the
    first recoverable copy. The cap, like dps_store._new_log's bound, keeps a
    corrupt every launch loop from filling the dir. The last name gets reused."""
    candidate = path.with_name(path.name + ".bad")
    for n in range(1, cap):
        if not candidate.exists():
            break
        candidate = path.with_name(f"{path.name}.bad.{n}")
    return candidate


def _repo_download_version() -> "str | None":
    """The _VERSION that fetched the downloaded repo trigger set, or None when
    the stamp is missing or unreadable, like a partial or pre stamp download."""
    try:
        v = json.loads(_REPO_TRIGGERS_VERSION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, str) else None


def _sweep_stale_update_parts(tmpdir: "Path", older_than_s: float = 3600.0) -> None:
    """Unlink updater.download leftovers, the <asset>.<pid>.<tid>.part files in
    tmpdir older than older_than_s. download removes its .part on handled
    failures, but a SIGKILL or OOM mid download leaks the ~50 MB+ file per
    crashed attempt. The age guard keeps a concurrent second instance's in
    flight download untouched. Its .part is rewritten continuously, and any
    stall is bounded by the per read socket timeout plus a total 60 minute
    deadline. Best effort, like the dest cleanup in
    _start_install."""
    cutoff = time.time() - older_than_s
    try:
        for part in Path(tmpdir).glob("NyaaTriggers-*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _hex_id(value: str) -> int:
    """Hex field, zone id on an 01 line and friends, as an int. 0 when unparsable."""
    try:
        return int(str(value).strip(), 16)
    except (TypeError, ValueError):
        return 0


def _bare_fight_tag(tag: str) -> str:
    """The fight tag as a bare timeline file name, "" when separators
    or .. would walk the read out of TIMELINES_DIR. The loader blanks
    with this, so the re-detect comparison must use it too, or the two
    never agree and the tick reloads forever."""
    if "/" in tag or "\\" in tag or ".." in tag:
        return ""
    return tag


_zone_names_cache: "dict | None" = None


def canonical_zone_name(zone_id: int) -> str:
    """English name for a zone id, "" when the id is 0 or unknown.

    Loaded once, lazily. A missing or malformed file degrades to "". Zone
    matching then falls back to the name the feed reported, as before.
    """
    global _zone_names_cache
    if _zone_names_cache is None:
        try:
            data = json.loads(ZONE_NAMES_FILE.read_text(encoding="utf-8"))
            _zone_names_cache = {str(k): v for k, v in data.items()
                                 if isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError, AttributeError):
            _zone_names_cache = {}
    try:
        return _zone_names_cache.get(str(int(zone_id)), "")
    except (TypeError, ValueError):
        return ""


_cactbot_tl_cache: "dict | None" = None


def cactbot_timeline_for_zone(zone_id: int) -> "tuple[str, str]":
    """Tag and txt_relpath of the cactbot timeline for a zone id. Empty tuple
    when the id is 0 or unknown, or the zone has no cactbot timeline.

    Loaded once, lazily. A missing or malformed file degrades to the empty
    tuple. The loader then falls back to the name regex fight resolution, as
    before.
    """
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
        except (OSError, ValueError, AttributeError):
            _cactbot_tl_cache = {}
    try:
        return _cactbot_tl_cache.get(str(int(zone_id)), ())
    except (TypeError, ValueError):
        return ()


def _compile_phrase_patterns(phrases: dict) -> list:
    """Regex patterns for phrase keys with a COMPLEX {token}, Groovy style, e.g.
    {event.estimatedRemainingDuration}. Engine callouts arrive AFTER Groovy
    substitution, like "Go Behind Head 5.0s", so the raw template key can't
    exact match. Each compiles to ^escaped_literal.*?...$ and is tried on the
    post substitution text. The JA value must be token free. The engine TTS
    path never substitutes {source}/{target}/{count}, so a token in the result
    would be spoken literally. SIMPLE token keys, {source}/{target}/{count},
    are skipped. Those belong to the local trigger path, where _fire
    substitutes the tokens, and widening them into regexes would leak tokens
    into unrelated static matches. Keys with too little literal text, e.g.
    "{safe}" or "{safe} safe", are rejected. They compile to near universal
    ^.*?$ matchers that hijack unrelated callouts and return wrong generic
    translations. Require >=6 alphanumeric literal chars."""
    simple = re.compile(r"^\{\w+\}$")
    has_token = re.compile(r"\{[^}]*\}")
    out = []
    for en, ja in phrases.items():
        if not has_token.search(en):
            continue                       # static key, stays in the exact dict
        if simple.search(en) or has_token.search(ja):
            continue                       # simple token key, or JA still holds a token
        literal = re.sub(r"\{[^}]*\}", "", en)
        if len(re.sub(r"[\W_]+", "", literal)) < 6:
            continue                       # too generic, would match almost anything
        parts = re.split(r"(\{[^}]*\})", en)
        pat = [".*?" if (p.startswith("{") and p.endswith("}") and len(p) > 1) else re.escape(p)
               for p in parts]
        out.append((re.compile("^" + "".join(pat) + "$", re.DOTALL), ja))
    return out

MAX_ABILITY_LINES = 200
# Rolling capture of the complete raw WS feed for the Save log export.
# Covers a full pull, a few MB at worst.
MAX_RAW_CAPTURE = 20000

# Wall clock budget for one log line's trigger matching loop on the GUI thread.
# A pathological trigger set must not starve the UI. On exceed the rest of the
# triggers are skipped for that line and the drop is logged.
_DISPATCH_BUDGET_S = 1.0

# Fight tags to cactbot timeline .txt paths, relative to ui/raidboss/data/,
# derived from the converter's TARGETS so the two stay in sync.
try:
    from convert_cactbot import TARGETS as _CB_TARGETS
    FIGHT_TO_CACTBOT_TXT = {
        tag: (rel[:-3] + ".txt") if rel.endswith(".ts") else rel
        for rel, tag in _CB_TARGETS if tag
    }
except Exception as _cb_exc:  # noqa: BLE001 - converter is optional at runtime
    # Log it. Silently empty means "cactbot timelines just don't download" with
    # no trace, which reads as a broken install rather than a broken import.
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

# Extra UserRole slots for tree items
_ITEM_TYPE_ROLE   = Qt.ItemDataRole.UserRole + 1   # str, one of folder, custom_hdr, custom_group
_ITEM_ID_ROLE     = Qt.ItemDataRole.UserRole + 2   # str, folder UUID
_SECTION_ROLE     = Qt.ItemDataRole.UserRole + 4   # str, row's source group, general/dot/local/engine

_GITHUB_URL  = "https://github.com/CateDesu/NyaaTriggers"
_DISCORD_URL = "https://discord.com/invite/TQJrbZcgKF"

# More Piper voices. Managed by hand. Drop a model's .onnx and .onnx.json into
# the voices folder and they appear in the dropdown.
_PIPER_VOICES_URL = "https://rhasspy.github.io/piper-samples/"

# Default Telesto Dalamud plugin HTTP endpoint for automarkers. Served inside
# the game process, Wine or Proton on Linux, reachable via localhost like
# IINACT's WS server. Mirrors the engine's telesto-support.uri default.
DEFAULT_TELESTO_URI = "http://localhost:45678/"

# ── UMAD Dancing Mad Ultimate automark preset ────────────────────────────────
# Load UMAD preset syncs the rule list to these P3-P4 debuffs. Rules seed with no
# marker assigned and unassigned rules never fire. Scope "party" marks whoever
# gets the debuff. Compound entries like "A+B" fire only when one player holds
# both, and sit out while the black hole chains toggle owns those statuses.
# Per id evidence lives in docs/UMAD-DEBUFFS.md. Each entry is a status hex or
# compound plus a label.
_UMAD_FIGHT_TAG = "UMAD"
_UMAD_FIGHT_TAG_CF = _UMAD_FIGHT_TAG.casefold()   # hot path compare, folded once
_UMAD_AUTOMARK_PRESET: "list[tuple[str, str]]" = [
    # P3. The Accretion carriers, told apart by their real 1st/2nd in Line status.
    ("644+BBC", "Accretion (1st in Line) - cleansed first"),
    ("644+BBD", "Accretion (2nd in Line) - cleansed second"),
    # P4, the Neo Exdeath "Kefka Says" real vs fake phase. 15A5-15AA are the new
    # 7.51 status block for this phase. 566/1C6 are reused classic ids and their
    # names exist exactly once in the Status sheet. Wounds carry real 15A5/15A6
    # and fake 1317/1318 variants. The preset marks the real ones.
    ("15A7", "Cursed Shriek - gaze (real: look away / fake: look at)"),
    ("15A8", "Forked Lightning - real: spread / fake: stack"),
    ("15A9", "Compressed Water - stack marker"),
    ("15AA", "Acceleration Bomb - stop moving when it expires"),
    ("15A5", "White Wound - real: lethal in White Antilight / fake: Black"),
    ("15A6", "Black Wound - real: lethal in Black Antilight / fake: White"),
    ("566",  "Beyond Death - real: must take lethal / fake: avoid lethal"),
    ("1C6",  "Allagan Field - real: avoid lethal / fake: must take lethal"),
]

# Canonical status key to human label for the rules list. Keys are canon_status_key
# form, plain ids and compound "A+B". Look up with _canon_status, never _norm_hex.
_UMAD_STATUS_LABELS: "dict[str, str]" = {
    _canon_status(h): label for h, label in _UMAD_AUTOMARK_PRESET
}

# Curated fight tree, category then expansion then fight tag, rendered newest
# expansion first. Leaf tags must match the `fight` field in triggers.json.
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
        (N_("Shadowbringers"), ["E12S", "E8S", "E3S"]),
        (N_("Stormblood"),     ["O12S", "O11S"]),
    ]),
    (N_("Extreme Trials"), [
        (N_("Dawntrail"),      ["Zelenia EX", "Enuo EX", "Doomtrain EX",
                            "Queen EX", "Valigarmanda EX", "Zoraal Ja EX"]),
        (N_("Endwalker"),      ["Zeromus EX", "Golbez EX", "Rubicante EX"]),
        (N_("A Realm Reborn"), ["Ultima's Bane EX"]),
    ]),
    (N_("Deep Dungeons"), [
        (N_("Endwalker"),      ["Eureka Orthos"]),
    ]),
    (N_("Field Operations"), [
        (N_("Shadowbringers"), ["Delubrum Reginae"]),
    ]),
    (N_("Normal Raids"), [
        (N_("Dawntrail"),      ["M12N", "M11N", "M10N", "M9N", "M8N", "M7N",
                            "M6N", "M5N", "M4N", "M3N", "M2N", "M1N"]),
        (N_("Endwalker"),      ["P10N", "P9N", "P8N", "P7N", "P6N", "P5N",
                            "P4N", "P3N", "P2N", "P1N"]),
        (N_("Shadowbringers"), ["E4N"]),
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
        (N_("Endwalker"),      ["Aglaia"]),
        (N_("Shadowbringers"), ["The Puppets' Bunker", "The Copied Factory"]),
        (N_("Stormblood"),     ["The Orbonne Monastery", "The Ridorana Lighthouse"]),
        (N_("Heavensward"),    ["Dun Scaith", "The Weeping City of Mhach", "The Void Ark"]),
        (N_("A Realm Reborn"), ["The World of Darkness", "Syrcus Tower",
                            "The Labyrinth of the Ancients"]),
    ]),
]

# Fight tags with a slot in the curated tree. Any other official tag surfaces
# under the dynamic TBD node.
_TREE_FIGHTS = {fight for _cat, _exps in _FIGHT_TREE
                for _exp, _fights in _exps for fight in _fights}


# ACT/IINACT placeholder names like "unknown_A55B" plus bare hex ids that leak
# into the name field. The hex pattern requires a 0x prefix or a digit bearing
# token of 4+ chars so real all letter names that happen to be hex, like "Dead"
# or "Face", survive.
_UNKNOWN_NAME_RE = re.compile(r"^\s*unknown_[0-9a-f]+\s*$", re.IGNORECASE)
_BARE_HEX_RE     = re.compile(r"^\s*(0x[0-9a-f]+|(?=[0-9a-f]*[0-9])[0-9a-f]{4,})\s*$", re.IGNORECASE)


def _clean_ability_name(raw: str, fallback: str = "New Trigger") -> str:
    """Normalise a raw ability/effect name for prefill. Strips parser junk like
    "unknown_<hex>" and bare hex IDs, and collapses whitespace. Returns
    ``fallback`` when nothing usable is left."""
    name = (raw or "").strip()
    if not name:
        return fallback
    if _UNKNOWN_NAME_RE.match(name) or _BARE_HEX_RE.match(name):
        return fallback
    return " ".join(name.split())


def _prefill_name_tts(raw_name: str, source: str = "", target: str = "",
                      me: str = "") -> tuple[str, str]:
    """Build the name and tts_text for a prefilled trigger. Appends " on you"
    when the target is the local player, " on {target}" for a real distinct
    entity, and no qualifier when the target is empty, the source, or the name,
    meaning a self cast."""
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


# Sample substitutions so an engine callout's Triggevent/Groovy tokens read
# speakably in the Test TTS preview. Order matters, specific first.
_TV_PREVIEW_TOKENS = [
    (re.compile(r"\{event\.estimatedRemainingDuration[^{}]*\}", re.IGNORECASE), "5 seconds"),
    (re.compile(r"\{event\.target(?:\.[\w().]+)?\}", re.IGNORECASE), "you"),
    (re.compile(r"\{event\.source(?:\.[\w().]+)?\}", re.IGNORECASE), "the boss"),
    (re.compile(r"\{event\.ability(?:\.[\w().]+)?\}", re.IGNORECASE), "the ability"),
    (re.compile(r"\{event\.buff(?:\.[\w().]+)?\}", re.IGNORECASE), "the buff"),
    (re.compile(r"\{target\}", re.IGNORECASE), "you"),
    (re.compile(r"\{source\}", re.IGNORECASE), "the boss"),
]


# In app neural Japanese voices, Kokoro ids to display names. The model ships
# more voices. We surface one female and one male.
_JP_NEURAL_VOICES = (("jf_alpha", "(JPN) Alpha"), ("jm_kumo", "(JPN) Kumo"))

# ISO 639-1 to the 3 letter tag shown in the Model dropdown.
_VOICE_LANG_TAGS = {"en": "ENG", "ja": "JPN", "de": "GER", "fr": "FRE", "es": "SPA"}


def _voice_display(stem: str) -> str:
    """Friendly dropdown name for a Piper voice file. Turns
    'en_US-arctic-medium' into '(ENG) Arctic'. Unrecognized stems pass through."""
    m = re.match(r"([a-z]{2})_[A-Z]{2}-([A-Za-z0-9]+)", stem)
    if m:
        lang = _VOICE_LANG_TAGS.get(m.group(1), m.group(1).upper())
        return f"({lang}) {m.group(2).capitalize()}"
    return stem


def _engine_preview_text(s: str) -> str:
    """Render an engine callout's text as a speakable example. Swap common
    Triggevent tokens for sample values, drop any remaining {groovy} tokens."""
    s = s or ""
    for pat, val in _TV_PREVIEW_TOKENS:
        s = pat.sub(val, s)
    s = re.sub(r"\{[^{}]*\}", "", s)
    return re.sub(r"\s+", " ", s).strip()


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
