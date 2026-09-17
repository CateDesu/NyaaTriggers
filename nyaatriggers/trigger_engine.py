"""Match triggers against log lines and expand captured fields without Qt dependencies.
"""

import functools
import math
import re
import time
import uuid
from dataclasses import dataclass, field

from nyaatriggers.drop_log import log_drop

# User patterns require the timeout capable regex engine. The standard library has no
# bounded fallback.
try:
    import regex as _regex_mod
    _HAVE_REGEX = True
except ImportError:  # pragma: no cover - optional dependency
    _regex_mod = None
    _HAVE_REGEX = False

# Maximum time for one regex operation.
_MATCH_TIMEOUT = 0.5

# Ability and status lines store IDs and actors in different columns.
_ABILITY_IDX: dict[str, int] = {
    "20": 5,   # NetworkStartsCasting  - ability name
    "21": 5,   # NetworkAbility
    "22": 5,   # NetworkAOEAbility
    "23": 5,   # NetworkCancelAbility
    "26": 3,   # GainsEffect           - effect name
    "30": 3,   # LosesEffect           - effect name
}

_ID_IDX: dict[str, int] = {
    "20": 4, "21": 4, "22": 4, "23": 4,
    "26": 2, "30": 2,
}

_SOURCE_IDX: dict[str, int] = {
    "20": 3, "21": 3, "22": 3, "23": 3,
    "26": 6, "30": 6,
}
_TARGET_IDX: dict[str, int] = {
    "20": 7, "21": 7, "22": 7, "23": 7,
    "26": 8, "30": 8,
}

# Status lines support self, by_me and any scope filters.
_STATUS_TYPES: frozenset[str] = frozenset({"26", "30"})

# Only gain line 26 carries duration. Loss line 30 uses a placeholder.
_DURATION_IDX: dict[str, int] = {"26": 4}

# Status counts are hexadecimal in field 9.
_COUNT_IDX: dict[str, int] = {"26": 9, "30": 9}


# Reject oversized or deeply nested patterns before compilation.
_MAX_PATTERN_LEN = 512
_MAX_REPEAT_COST = 8192


def _regex_resource_limit(pattern: str) -> bool:
    """Bound numeric repeats before compiling a pattern."""
    if len(pattern) > _MAX_PATTERN_LEN:
        return True
    # Count repeats individually so a literal # cannot hide them.
    cost = max(1, len(pattern))
    for index, char in enumerate(pattern):
        if char != "{":
            continue
        source = pattern[index:]
        compact = re.sub(r"\s+|#[^\n]*(?:\n|$)", "", source)
        bounds = [1]
        for spelling in (source, compact):
            match = re.match(r"\{([0-9]*)(?:,([0-9]*))?\}", spelling)
            if match:
                bounds.extend(int(value or 0) for value in match.groups())
        cost *= max(bounds)
        if cost > _MAX_REPEAT_COST:
            return True
    return False


def _looks_catastrophic(pattern: str) -> bool:
    """Reject likely excessive nested repetition before compilation. Matching also has a
    timeout.
    """
    if re.search(r"\([^()]*[*+][^()]*\)\s*[*+{]", pattern):
        return True
    return _has_nested_unbounded(pattern)


def _has_nested_unbounded(pattern: str) -> bool:
    """Track nested quantifiers while respecting escapes and character classes."""

    def _outer_quant_unbounded(i: int) -> "tuple[bool, int]":
        if i < len(pattern) and pattern[i] in "*+":
            return True, i + 1
        if i < len(pattern) and pattern[i] == "{":
            m = re.match(r"\{(\d+)(,(\d*))?\}", pattern[i:])
            if m:
                return (m.group(2) is not None and m.group(3) == ""), i + m.end()
        return False, i

    # Track quantifiers inside each group.
    contains = [False]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
        elif c == "[":
            # A leading ] can be a literal inside a character class.
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
                return False        # Leave syntax errors to the compiler.
            inner = contains.pop()
            unbounded, i = _outer_quant_unbounded(i + 1)
            if unbounded and inner:
                return True
            # Include a group quantifier in its parent count.
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
    if _regex_resource_limit(pattern) or _looks_catastrophic(pattern):
        return None
    if not _HAVE_REGEX:
        # Refuse user patterns when bounded matching is unavailable.
        return None
    try:
        return _regex_mod.compile(pattern, flags)
    except Exception:
        return None


def _is_regex_mod_pattern(rx) -> bool:
    """Select timeout arguments for the compiled pattern type."""
    return _HAVE_REGEX and isinstance(rx, _regex_mod.Pattern)


def _safe_search(rx, text):
    """Search with a timeout when supported. A timeout returns no match."""
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
    """Substitute with a timeout, preserving the input on failure."""
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
        # Leave invalid backreferences unchanged.
        log_drop("redos", "callout replacement regex failed; left unchanged")
        return text


@functools.lru_cache(maxsize=2048)
def _id_set(ability_id: str) -> frozenset:
    """Match IDs as literal alternatives rather than regexes."""
    return frozenset(p.strip().upper() for p in ability_id.split("|") if p.strip())


def _as_float(value, default: float) -> float:
    """Read a finite number or return the fallback."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _as_int(value, default: int) -> int:
    """Read an integer from a numeric value or string."""
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
    """Convert scalar configuration values to text."""
    if isinstance(value, str):
        return value or default
    if isinstance(value, (int, float, bool)):
        return str(value) if value else default
    return default


def _as_bool(value, default: bool) -> bool:
    """Read booleans without treating the string false as true."""
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
    ability_id: str = ""      # Hex ID alternatives for the selected log type.
    ability_regex: str = ""   # Used when the ID filter is empty.
    tts_text: str = ""
    cooldown_s: float = 5.0
    enabled: bool = True
    zone_regex: str = ""
    fight: str = ""
    sequence: list = field(default_factory=list)
    speed: float = 1.0
    interrupt: bool = False
    # Zero leaves the duration bound open.
    duration_min: float = 0.0
    duration_max: float = 0.0
    count_min: int = 0
    count_max: int = 0
    status_scope: str = "self"
    # StatusTimerRunner schedules expiry warnings and handles refreshes and losses.
    expiry_warn_s: float = 0.0
    sound_file: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Runtime state is not persisted.
    _last_fired: dict = field(default_factory=dict, init=False, repr=False, compare=False)

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
        # Coerce edited configuration values without failing startup.
        seq = d.get("sequence")
        if not isinstance(seq, list):
            seq = []
        # Normalize log type spacing and default to 20 for stable round trips.
        log_type = _str_or(d.get("log_type"), "20").strip() or "20"
        parts = {p.strip() for p in log_type.split("|") if p.strip()}
        ability_id = _str_or(d.get("ability_id"), "")
        if ability_id and not all(p in _ID_IDX for p in parts):
            # Ignore ID filters unless every selected log type has an ID field.
            ability_id = ""
            log_drop("trigger-load",
                     f"ability_id dropped, log type {log_type!r} has no ID field; "
                     f"{_str_or(d.get('name'), '?')!r} now matches on type alone")
        warn = _as_float(d.get("expiry_warn_s"), 0.0)
        if "26" not in parts or not parts <= _STATUS_TYPES:
            # Expiry needs only status types and at least one gain type. Mixed ability
            # types must keep their normal match behavior.
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
            # Use self scope if the saved value is invalid.
            status_scope=(d.get("status_scope")
                          if d.get("status_scope") in ("self", "by_me", "any")
                          else "self"),
            expiry_warn_s=warn,
            sound_file=_str_or(d.get("sound_file"), ""),
        )

    def matches(self, fields: list[str], me: str = "") -> dict | None:
        """Match a log line using the current player name for self scope."""
        if not self.enabled or not fields:
            return None
        # Select fields using the actual incoming type when the trigger has
        # alternatives.
        lt = self.log_type
        if "|" in lt:
            if fields[0] not in (p.strip() for p in lt.split("|")):
                return None
            lt = fields[0]
        elif fields[0] != lt:
            return None

        if self.ability_id:
            # Match literal hexadecimal alternatives without case sensitivity.
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

        # Check scope before consuming the cooldown.
        if lt in _STATUS_TYPES:
            scope = self.status_scope or "self"
            if scope in ("self", "by_me"):
                if not me:
                    return None  # Self scope cannot match until the player is known.
                idx_map = _TARGET_IDX if scope == "self" else _SOURCE_IDX
                idx = idx_map.get(lt, 8)
                if len(fields) <= idx or fields[idx].casefold() != me.casefold():
                    return None

        # Check duration before consuming the cooldown.
        dur_idx = _DURATION_IDX.get(lt)
        if dur_idx is not None and (self.duration_min > 0 or self.duration_max > 0):
            if len(fields) <= dur_idx:
                return None
            try:
                dur = float(fields[dur_idx])
            except ValueError:
                return None
            # Reject nonfinite durations.
            if not math.isfinite(dur):
                return None
            if dur < self.duration_min:
                return None
            if self.duration_max > 0 and dur > self.duration_max:
                return None

        cnt_idx = _COUNT_IDX.get(lt)
        if cnt_idx is not None and (self.count_min > 0 or self.count_max > 0):
            if len(fields) <= cnt_idx:
                return None
            try:
                cnt = int(fields[cnt_idx], 16)
            except ValueError:
                return None
            if cnt < self.count_min:
                return None
            if self.count_max > 0 and cnt > self.count_max:
                return None

        # Cooldown keys use field 2, which is the source for abilities and the effect
        # for statuses. Expiry gains and losses bypass this check so they can reset
        # timers. Timer firing applies the cooldown.
        if not (self.expiry_warn_s > 0 and lt in _STATUS_TYPES):
            source_id = fields[2].upper() if len(fields) > 2 else ""
            now = time.monotonic()
            last_fired = self._last_fired.get(source_id)
            if last_fired is not None and now - last_fired < self.cooldown_s:
                log_drop("cooldown", f"{self.name!r} suppressed ({self.cooldown_s:g}s cooldown, src {source_id})")
                return None
            self._last_fired[source_id] = now
            # Discard expired cooldown entries.
            if len(self._last_fired) > 256:
                cutoff = now - max(self.cooldown_s, 1.0)
                self._last_fired = {k: v for k, v in self._last_fired.items()
                                    if v >= cutoff}
        src_idx = _SOURCE_IDX.get(lt, 3)
        tgt_idx = _TARGET_IDX.get(lt, 7)
        # Convert hexadecimal counts to decimal for substitutions.
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
