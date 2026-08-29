"""Trigger dataclass plus the per-line matching core. Pure data, no Qt.

Trigger.matches tests one already-split ACT/OverlayPlugin log line and
returns the captured {source, target, count} dict, or None. The
module-level _*_IDX tables map a log-type code to its field index for the
two line layouts, ability 20-23 vs status 26/30.
"""

import functools
import math
import re
import time
import uuid
from dataclasses import dataclass, field

from drop_log import log_drop

# Optional backing engine. The `regex` module supports a per-call timeout, the
# only runtime backstop against a catastrophic-backtracking pattern that slips
# past _looks_catastrophic, say an overlapping alternation like (a|aa)+ that the
# heuristic cannot model. When it is absent, compile_user_regex refuses user
# patterns, returning None which callers treat as never-matching, rather than
# compile them with stdlib re, whose matches the _safe_* helpers cannot bound.
try:
    import regex as _regex_mod
    _HAVE_REGEX = True
except ImportError:  # pragma: no cover - optional dependency
    _regex_mod = None
    _HAVE_REGEX = False

# Wall-clock cap on a single user-regex match. Real callouts match in well under
# a millisecond. This only ever fires on a crafted or buggy catastrophic pattern.
_MATCH_TIMEOUT = 0.5

# Ability and cast lines, types 20-23, share the layout
#   type|ts|sourceId|sourceName|abilityId|abilityName|targetId|targetName|...
# Status lines, 26 GainsEffect and 30 LosesEffect, use a different one
#   type|ts|effectId|effectName|duration|sourceId|sourceName|targetId|targetName|...
# Verified against FFXIV_ACT_Plugin 3.0.x, IINACT 2.10.x and the cactbot log defs.
_ABILITY_IDX: dict[str, int] = {
    "20": 5,   # NetworkStartsCasting  - ability name
    "21": 5,   # NetworkAbility
    "22": 5,   # NetworkAOEAbility
    "23": 5,   # NetworkCancelAbility
    "26": 3,   # GainsEffect           - effect name
    "30": 3,   # LosesEffect           - effect name
}

# Field index of the matchable hex ID, per log type.
# Ability lines 20-23 have it at field 4, status lines 26/30 at field 2.
_ID_IDX: dict[str, int] = {
    "20": 4, "21": 4, "22": 4, "23": 4,
    "26": 2, "30": 2,
}

# Field indices of the source/target entity names, per log type.
_SOURCE_IDX: dict[str, int] = {
    "20": 3, "21": 3, "22": 3, "23": 3,
    "26": 6, "30": 6,
}
_TARGET_IDX: dict[str, int] = {
    "20": 7, "21": 7, "22": 7, "23": 7,
    "26": 8, "30": 8,
}

# Status-effect log types, 26 GainsEffect and 30 LosesEffect. These carry a
# source/target pair. Trigger.status_scope picks who the effect must involve.
_STATUS_TYPES: frozenset[str] = frozenset({"26", "30"})

# Effect duration in seconds at field 4, 26 GainsEffect only. Ability lines put
# the ability ID there, and a 30 LosesEffect line carries a hardcoded 0.00
# placeholder, so a duration window on a 30 trigger could never match.
_DURATION_IDX: dict[str, int] = {"26": 4}

# Status stack count at field 9, right after targetId at 7 and targetName at 8.
# Ability lines have no stack count.
_COUNT_IDX: dict[str, int] = {"26": 9, "30": 9}


# User-supplied patterns, trigger files can come from third-party packs, are
# compiled once through here. Length cap plus a heuristic rejection of the
# nested-quantifier shapes whose catastrophic backtracking would freeze the
# GUI thread mid-combat. Returns None for anything unusable. Callers treat
# that as never-matching.
_MAX_PATTERN_LEN = 512


def _looks_catastrophic(pattern: str) -> bool:
    """Heuristic for exponential-backtracking shapes.

    Level one is the flat regex pass. It catches a quantifier directly inside a
    group that is itself quantified, the (x+)+ or (x*)* or (x+){2,} shape. Level
    two is the scanner. It catches an unbounded quantifier nested at ANY depth
    inside an unbounded-quantified group, like (?:a(?:b+)c)+ or ((a)+b)+, which
    the flat regex cannot see across parens. Python's re has no match timeout,
    so rejection at compile time is the only real guard against a crafted
    pattern freezing the GUI thread mid-combat."""
    if re.search(r"\([^()]*[*+][^()]*\)\s*[*+{]", pattern):
        return True
    return _has_nested_unbounded(pattern)


def _has_nested_unbounded(pattern: str) -> bool:
    """True if an unbounded-quantified group transitively contains an unbounded
    quantifier. Tracks escapes and character classes, so class contents can
    neither fake a group nor hide one. Malformed patterns return False and are
    left for re.compile to reject."""

    def _outer_quant_unbounded(i: int) -> "tuple[bool, int]":
        """Classify the quantifier at pattern[i], just past a closing paren.
        Returns the unbounded flag and the index past the quantifier. {m} and
        {m,n} are bounded, `*` `+` and {m,} are not. A malformed {...} is a
        literal brace, not a quantifier."""
        if i < len(pattern) and pattern[i] in "*+":
            return True, i + 1
        if i < len(pattern) and pattern[i] == "{":
            m = re.match(r"\{(\d+)(,(\d*))?\}", pattern[i:])
            if m:
                return (m.group(2) is not None and m.group(3) == ""), i + m.end()
        return False, i

    # One flag per open group, tracking whether it transitively contains an
    # unbounded quantifier. contains[0] is the top level.
    contains = [False]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
        elif c == "[":
            # Skip the class. A ']' right at the start, after an optional '^', is a literal.
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
        elif c == "(":
            contains.append(False)
            i += 1
        elif c == ")":
            if len(contains) < 2:
                return False        # unbalanced, not our problem
            inner = contains.pop()
            unbounded, i = _outer_quant_unbounded(i + 1)
            if unbounded and inner:
                return True
            # A quantified group is itself a quantifier occurrence for its parent.
            contains[-1] |= inner or unbounded
        elif c in "*+":
            contains[-1] = True
            i += 1
        elif c == "{":
            m = re.match(r"\{(\d+)(,(\d*))?\}", pattern[i:])
            if m:
                if m.group(2) is not None and m.group(3) == "":
                    contains[-1] = True    # {m,} is unbounded
                i += m.end()
            else:
                i += 1
        else:
            i += 1
    return False


@functools.lru_cache(maxsize=4096)
def compile_user_regex(pattern: str, flags: int = 0):
    if len(pattern) > _MAX_PATTERN_LEN or _looks_catastrophic(pattern):
        return None
    if not _HAVE_REGEX:
        # No bounded engine, no user regex. stdlib re has no match timeout, so
        # a catastrophic pattern that slipped past _looks_catastrophic would
        # run unguarded in the _safe_* helpers. Refuse instead of compiling.
        return None
    try:
        return _regex_mod.compile(pattern, flags)
    except Exception:
        # A bad pattern degrades to never-matching rather than raising.
        return None


def _is_regex_mod_pattern(rx) -> bool:
    """True for patterns compiled by the optional regex module, which is what
    compile_user_regex returns when it is installed. The timeout kwarg exists
    only on that engine's match methods, so dispatch on the pattern's actual
    type, not on module availability. Callers may hand us a stdlib pattern."""
    return _HAVE_REGEX and isinstance(rx, _regex_mod.Pattern)


def _safe_search(rx, text):
    """rx.search with a wall-clock timeout when the pattern's engine supports
    it, so a catastrophic pattern that slipped past _looks_catastrophic cannot
    freeze the GUI thread. On timeout the match is treated as a no-match and the
    drop is logged. Stdlib re has no timeout, so it is unguarded there."""
    if _is_regex_mod_pattern(rx):
        try:
            return rx.search(text, timeout=_MATCH_TIMEOUT)
        except TimeoutError:
            log_drop("redos", "user regex timed out; treated as no-match")
            return None
        except Exception:
            log_drop("redos", "user regex failed; treated as no-match")
            return None
    return rx.search(text)


def _safe_fullmatch(rx, text):
    """rx.fullmatch with the same timeout guard as _safe_search."""
    if _is_regex_mod_pattern(rx):
        try:
            return rx.fullmatch(text, timeout=_MATCH_TIMEOUT)
        except TimeoutError:
            log_drop("redos", "timeline regex timed out; treated as no-match")
            return None
        except Exception:
            log_drop("redos", "timeline regex failed; treated as no-match")
            return None
    return rx.fullmatch(text)


def _safe_sub(rx, repl, text):
    """rx.sub with the same timeout guard. Used for user find/replace rules on
    callout text. On timeout the original text is returned unchanged. A bad
    backreference in the replacement, on either engine, also leaves the text
    unchanged rather than dropping the whole callout from the Qt slot."""
    if _is_regex_mod_pattern(rx):
        try:
            return rx.sub(repl, text, timeout=_MATCH_TIMEOUT)
        except TimeoutError:
            log_drop("redos", "callout replacement regex timed out; left unchanged")
            return text
        except Exception:
            log_drop("redos", "callout replacement regex failed; left unchanged")
            return text
    try:
        return rx.sub(repl, text)
    except re.error:
        # Stdlib fallback. Same bad-backreference guard, since the regex
        # module is not there to catch it above.
        log_drop("redos", "callout replacement regex failed; left unchanged")
        return text


@functools.lru_cache(maxsize=2048)
def _id_set(ability_id: str) -> frozenset:
    """Pipe-separated hex ID list -> uppercase set. IDs are matched as literal
    strings, never as regex, so no pack can smuggle a pattern through them."""
    return frozenset(p.strip().upper() for p in ability_id.split("|") if p.strip())


def _as_float(value, default: float) -> float:
    """Coerce a JSON numeric to float. Missing/null/blank/non-numeric values
    from hand-edited triggers degrade to default rather than raising out of
    from_dict and bricking startup."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    """Coerce a JSON numeric to int, tolerating "5.0". Same fallback as
    _as_float, non-finite floats included. int of inf raises OverflowError out
    of from_dict and bricks the trigger load."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = float(value)
            return int(parsed) if math.isfinite(parsed) else default
        except (TypeError, ValueError, OverflowError):
            return default


def _str_or(value, default: str) -> str:
    """`value or default` for text fields, with non-string scalars coerced,
    an "ability_id" of 123 becomes "123", so they can't raise downstream in
    the .split and regex consumers. Lists and dicts are junk. They
    degrade to default like null instead of becoming a repr."""
    if isinstance(value, str):
        return value or default
    if isinstance(value, (int, float, bool)):
        return str(value) if value else default
    return default


def _as_bool(value, default: bool) -> bool:
    """Coerce a JSON boolean. Plain truthiness reads a hand-edited "enabled"
    set to the string "false" as True, since any non-empty string is truthy,
    silently re-arming a trigger the user meant to turn off. Strings read as
    true unless they spell out a false value, numbers go by v != 0, anything
    else degrades to default."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return default


@dataclass
class Trigger:
    name: str = "New Trigger"
    log_type: str = "20"
    ability_id: str = ""      # Hex IDs from ACT field[4], e.g. "A55B" or "A55D|A55E"
    ability_regex: str = ""   # Regex on ability name, field[5], used when ability_id is blank
    tts_text: str = ""
    cooldown_s: float = 5.0
    enabled: bool = True
    zone_regex: str = ""
    fight: str = ""
    sequence: list = field(default_factory=list)
    speed: float = 1.0
    interrupt: bool = False
    # Status duration window in seconds, for ordered-timer mechanics where the
    # same debuff carries a different duration per assignment. Both at 0 means
    # no filter. duration_max at 0 means no upper bound. 26 lines only, a 30
    # line carries no real duration to window on.
    duration_min: float = 0.0
    duration_max: float = 0.0
    # Stack-count window for stacking debuffs. Same zero semantics. 26/30 only.
    count_min: int = 0
    count_max: int = 0
    # Status-effect scope, 26/30 only. Whose effect this trigger matches.
    #   "self"  - the effect is ON you, target == me. The default.
    #   "by_me" - YOU applied it, source == me, e.g. Reaper's Death's Design.
    #   "any"   - no source/target filter.
    status_scope: str = "self"
    # Pre-expiry warning, GainsEffect 26 only. When above 0, the trigger does not
    # speak on the gain. It fires this many seconds before the effect's duration
    # runs out. Re-arms on refresh, cancelled on early removal. Scheduling lives
    # in StatusTimerRunner. matches only suppresses the immediate callout.
    expiry_warn_s: float = 0.0
    sound_file: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Runtime state. Never persisted.
    _last_fired: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "log_type": self.log_type,
            "tts_text": self.tts_text,
            "cooldown_s": self.cooldown_s,
            "enabled": self.enabled,
        }
        if self.ability_id:
            d["ability_id"] = self.ability_id
        if self.ability_regex:
            d["ability_regex"] = self.ability_regex
        if self.zone_regex:
            d["zone_regex"] = self.zone_regex
        if self.fight:
            d["fight"] = self.fight
        if self.sequence:
            d["sequence"] = self.sequence
        if self.speed != 1.0:
            d["speed"] = self.speed
        if self.interrupt:
            d["interrupt"] = self.interrupt
        if self.duration_min:
            d["duration_min"] = self.duration_min
        if self.duration_max:
            d["duration_max"] = self.duration_max
        if self.count_min:
            d["count_min"] = self.count_min
        if self.count_max:
            d["count_max"] = self.count_max
        if self.status_scope and self.status_scope != "self":
            d["status_scope"] = self.status_scope
        if self.expiry_warn_s:
            d["expiry_warn_s"] = self.expiry_warn_s
        if self.sound_file:
            d["sound_file"] = self.sound_file
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Trigger":
        # `_str_or` turns an explicit JSON null into "", None would crash the
        # .casefold and regex consumers, and coerces non-string scalars. Numerics
        # go through _as_float/_as_int. A raised ValueError would brick the
        # load and _load_triggers' guards don't catch it.
        seq = d.get("sequence")
        if not isinstance(seq, list):
            # A truthy non-list, say 42 or True, is not iterable of steps and
            # would raise TypeError in the comprehension below. Skip the sequence,
            # keep the trigger.
            seq = []
        # Stripped like the piped parts below. A hand edited " 21" loads
        # fine but never equals a line's type field, a dead trigger. A
        # whitespace only edit strips to "", which the falsy fallback in
        # _str_or would read as missing on the NEXT load and revive as "20",
        # so take the default now and keep the round trip stable.
        log_type = _str_or(d.get("log_type"), "20").strip() or "20"
        # Empty parts from a stray pipe match no real line, so they must not
        # disqualify the ID and warn gates below.
        parts = {p.strip() for p in log_type.split("|") if p.strip()}
        ability_id = _str_or(d.get("ability_id"), "")
        if ability_id and not all(p in _ID_IDX for p in parts):
            # A log type with no _ID_IDX entry, a 00 chat line for one, has
            # no ID field to match. A configured ability_id there can never
            # fire, so drop it and let the regex do the matching. On a mixed
            # pipe like 00|21 the 00 half would look the ID up at the ability
            # layout's field 4 and read chat text as a hex ID, so every part
            # must carry an ID field for the ID to survive. Files saved by
            # older builds can still carry the shape, so say so when it goes.
            ability_id = ""
            log_drop("trigger-load",
                     f"ability_id dropped, log type {log_type!r} has no ID field; "
                     f"{_str_or(d.get('name'), '?')!r} now matches on type alone")
        warn = _as_float(d.get("expiry_warn_s"), 0.0)
        if "26" not in parts or not parts <= _STATUS_TYPES:
            # The expiry warning arms off a GainsEffect line, so with no 26
            # type it can never fire. Worse, every match with a warn set
            # lands in the host's swallow branch and the trigger stays
            # silent for good. A mixed pipe like 26|21 would swallow its
            # ability lines the same way, so the warn survives only when
            # every part is a status type. Same rule the dialog saves with.
            warn = 0.0
        return cls(
            id=_str_or(d.get("id"), str(uuid.uuid4())),
            name=_str_or(d.get("name"), "Unnamed"),
            log_type=log_type,
            ability_id=ability_id,
            ability_regex=_str_or(d.get("ability_regex"), ""),
            tts_text=_str_or(d.get("tts_text"), ""),
            cooldown_s=max(0.0, _as_float(d.get("cooldown_s"), 5.0)),
            enabled=_as_bool(d.get("enabled"), True),
            zone_regex=_str_or(d.get("zone_regex"), ""),
            fight=_str_or(d.get("fight"), ""),
            sequence=[s for s in seq if isinstance(s, dict)],
            speed=_as_float(d.get("speed"), 1.0),
            interrupt=_as_bool(d.get("interrupt"), False),
            duration_min=_as_float(d.get("duration_min"), 0.0),
            duration_max=_as_float(d.get("duration_max"), 0.0),
            count_min=_as_int(d.get("count_min"), 0),
            count_max=_as_int(d.get("count_max"), 0),
            # Unknown values fall back to "self". An unrecognised scope must
            # narrow the filter, not silently widen it to everyone.
            status_scope=(d.get("status_scope")
                          if d.get("status_scope") in ("self", "by_me", "any")
                          else "self"),
            expiry_warn_s=warn,
            sound_file=_str_or(d.get("sound_file"), ""),
        )

    # ------------------------------------------------------------------
    def matches(self, fields: list[str], me: str = "") -> dict | None:
        """Return captured fields dict if this trigger fires, None otherwise.

        ``me`` is the local player's name. Required for self-scoped 26/30 triggers.
        """
        if not self.enabled or not fields:
            return None
        # log_type may be pipe-separated, e.g. "21|22" since cactbot's 'Ability'
        # covers both NetworkAbility and NetworkAOEAbility. `lt` becomes the
        # concrete type of THIS line so the field-index lookups below are right.
        lt = self.log_type
        if "|" in lt:
            if fields[0] not in (p.strip() for p in lt.split("|")):
                return None
            lt = fields[0]
        elif fields[0] != lt:
            return None

        if self.ability_id:
            # Case-insensitive hex ID match. May be pipe-separated, "A55D|A55E",
            # with whitespace around IDs tolerated. Literal comparison, not regex.
            id_idx = _ID_IDX.get(lt, 4)
            if len(fields) <= id_idx:
                return None
            if fields[id_idx].upper() not in _id_set(self.ability_id):
                return None
        elif self.ability_regex:
            idx = _ABILITY_IDX.get(lt)
            search_text = (
                fields[idx] if idx is not None and idx < len(fields)
                else "|".join(fields)
            )
            rx = compile_user_regex(self.ability_regex, re.IGNORECASE)
            if rx is None or not _safe_search(rx, search_text):
                return None

        # Status scope filter, 26/30, see the status_scope field. Checked
        # before the cooldown so an out-of-scope line for the same effect
        # can't trip the cooldown and swallow a real one.
        if lt in _STATUS_TYPES:
            scope = self.status_scope or "self"
            if scope in ("self", "by_me"):
                if not me:
                    return None  # can't confirm it's us without knowing who we are
                idx_map = _TARGET_IDX if scope == "self" else _SOURCE_IDX
                idx = idx_map.get(lt, 8)
                if len(fields) <= idx or fields[idx].casefold() != me.casefold():
                    return None
            # "any" means no source/target filter

        # Duration window [duration_min, duration_max]. Checked before the
        # cooldown so a wrong-duration line can't trip it. Ignored on log types
        # with no duration field so a stray value can't kill an ability trigger.
        dur_idx = _DURATION_IDX.get(lt)
        if dur_idx is not None and (self.duration_min > 0 or self.duration_max > 0):
            if len(fields) <= dur_idx:
                return None
            try:
                dur = float(fields[dur_idx])
            except ValueError:
                return None
            # Reject inf/nan. A crafted 26-line duration would otherwise pass the
            # window comparison below and feed inf into status_timer, which crashes.
            if not math.isfinite(dur):
                return None
            if dur < self.duration_min:
                return None
            if self.duration_max > 0 and dur > self.duration_max:
                return None

        # Stack-count window [count_min, count_max]. Same before-cooldown and
        # no-count-field rules as the duration window.
        cnt_idx = _COUNT_IDX.get(lt)
        if cnt_idx is not None and (self.count_min > 0 or self.count_max > 0):
            if len(fields) <= cnt_idx:
                return None
            try:
                cnt = int(fields[cnt_idx], 16)   # status stack counts are hex
            except ValueError:
                return None
            if cnt < self.count_min:
                return None
            if self.count_max > 0 and cnt > self.count_max:
                return None

        # Cooldown, keyed on fields[2]. That's the source ID for ability lines
        # but the EFFECT ID for status lines, 26/30 put source at fields[5],
        # so a status trigger's cooldown is per-effect, not per-source. Skipped
        # for expiry-warning triggers. Every refresh must re-match so the timer
        # re-arms. The 30 half of a piped 26|30 speaks nothing, the host
        # swallows it, so it must skip too. Its write would debounce the next
        # 26's warning after a quick dispel and re-apply. The firing path
        # debounces on effect id in _on_status_timer, which keys this same map
        # upper-cased, so do the same here.
        if not (self.expiry_warn_s > 0 and lt in _STATUS_TYPES):
            source_id = fields[2].upper() if len(fields) > 2 else ""
            now = time.monotonic()
            if now - self._last_fired.get(source_id, 0.0) < self.cooldown_s:
                log_drop("cooldown", f"{self.name!r} suppressed ({self.cooldown_s:g}s cooldown, src {source_id})")
                return None
            self._last_fired[source_id] = now
            # Entity IDs churn every pull. Drop expired entries, they no longer
            # suppress anything, so the map can't grow for a whole session.
            if len(self._last_fired) > 256:
                cutoff = now - max(self.cooldown_s, 1.0)
                self._last_fired = {k: v for k, v in self._last_fired.items()
                                    if v >= cutoff}
        src_idx = _SOURCE_IDX.get(lt, 3)
        tgt_idx = _TARGET_IDX.get(lt, 7)
        # Stack count is hex on the wire. Expose decimal so {count} speaks
        # "10", not "A".
        count_str = ""
        if cnt_idx is not None and len(fields) > cnt_idx:
            try:
                count_str = str(int(fields[cnt_idx], 16))
            except ValueError:
                count_str = ""
        return {
            "source": fields[src_idx] if len(fields) > src_idx else "",
            "target": fields[tgt_idx] if len(fields) > tgt_idx else "",
            "count": count_str,
        }
