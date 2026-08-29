"""Parse cactbot-compatible timeline .txt files.

Supported syntax per line
    time "label" [EventType { key: "val", ... }]
                 [window N | window before,after]
                 [jump time | jump "label" | forcejump time | forcejump "label"]
    time label "name"          silent jump target

Clauses after the label may appear in any order, the event block included.
An unrecognized clause is skipped without discarding the clauses that follow
it. duration D is one of those, cactbot uses it for timer bar length and we
keep nothing but the label.

hideall "label" hides every entry with that label. It still syncs and still
works as a jump target, it just never speaks or shows on the bars. Same as
cactbot's ignores set.

Directives we ignore
    sync /regex/             old-style sync, flagged at load, never syncs
    # comment
"""

import math
import re
from dataclasses import dataclass, field as dc_field

_LINE_RE = re.compile(
    r'^(?P<time>-?[\d.]+)\s+(?P<labelkw>label\s+)?"(?P<label>[^"]*)"\s*(?P<rest>.*)$'
)
_EVENT_RE = re.compile(r"(?P<event>[A-Za-z]\w*)\s*\{(?P<fields>(?:[^{}\"']|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')*)\}")
# Fallback for event blocks whose fields hold nested braces, say
# pair: [{ key: ..., value: ... }], which _EVENT_RE cannot consume. Lifts
# only the leading keyword, marked unsupported by the parser since an
# empty-fields entry under a supported keyword would sync on every line
# of that type.
_EVENT_KW_RE = re.compile(r"\b([A-Za-z]\w*)\s*\{")
# cactbot splits a single "window X" evenly, X/2 on each side, so
# window 5000 means window 2500,2500 rather than window 5000,5000.
_WINDOW_RE = re.compile(r'\bwindow\s+(?P<before>[\d.]+)(?:\s*,\s*(?P<after>[\d.]+))?')
_JUMP_RE = re.compile(r'\b(?P<force>force)?jump\s+(?:"(?P<jlabel>[^"]*)"|(?P<jtime>-?[\d.]+))')
# cactbot writes both single- and double-quoted values, and its JSON5 also
# allows bare scalars like effectId: 644. findall leaves the unmatched quote
# groups empty, so callers take whichever group matched. Quoted values keep
# their backslashes, an escaped quote must not close the value early, and the
# fields are matched as regexes downstream where \" still reads as a quote.
_KV_RE = re.compile(r"(\w+)\s*:\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|([^\s,\[\]{},\"']+))")
# cactbot also writes list-valued sync fields, like id set to ["9D00", "9D01"],
# which _KV_RE, scalar only, would silently drop. That leaves the entry with no
# id, so it then syncs on any ability of its type. Capture the array and its
# quoted items.
_KV_ARRAY_RE = re.compile(r"(\w+)\s*:\s*\[((?:[^\[\]\"']|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')*)\]")
_ARRAY_ITEM_RE = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")
# Old-style sync /regex/ clause. Unsupported, but lifted out before the event
# search since its body can hold brace quantifiers that would fake one.
_LEGACY_SYNC_RE = re.compile(r'\bsync\s*/(?:[^/\\]|\\.)*/')
# hideall "name" directive, cactbot's ignore list. Entries it names keep
# their sync but never speak or show.
_HIDEALL_RE = re.compile(r'^hideall\s+"([^"]+)"')


def _array_fields(fields_text: str) -> list[tuple[str, str]]:
    """key, body pairs for each real key: [...] field. The scan moves left to
    right and steps over quoted strings whole, so array syntax inside a quoted
    scalar value, say name set to "id: ['9D00']", can't fabricate a key."""
    pairs: list[tuple[str, str]] = []
    i, n = 0, len(fields_text)
    while i < n:
        c = fields_text[i]
        if c in '"\'':
            # quoted value, skip it whole so its contents can't read as fields
            q = c
            i += 1
            while i < n and fields_text[i] != q:
                # an escaped char can't close the string, step over both
                i += 2 if fields_text[i] == "\\" else 1
            i += 1
            continue
        m = _KV_ARRAY_RE.match(fields_text, i)
        if m:
            pairs.append((m.group(1), m.group(2)))
            i = m.end()
            continue
        i += 1
    return pairs


def _strip_comment(line: str) -> str:
    """Drop a trailing # comment, but ignore # inside a quoted string,
    even after a \\" escape, which must not flip the string state. An
    apostrophe opens a string only in value position, right after one of
    : , [ or {. Anywhere else it is plain text, say the one in an old
    sync /Boss's Move/ body, and must not keep a trailing comment alive."""
    quote = ''
    esc = False
    prev = ''
    for i, ch in enumerate(line):
        if esc:
            esc = False
        elif quote and ch == "\\":
            esc = True
        elif quote:
            if ch == quote:
                quote = ''
        elif ch == '"' or (ch == "'" and prev in ":,[{"):
            quote = ch
        elif ch == '#':
            return line[:i]
        if not ch.isspace():
            prev = ch
    return line


def _find_jump(rest: str) -> re.Match[str] | None:
    """Leftmost jump clause in rest, matched at quote depth zero. The jump
    lift runs before the sync and event lifts, so a plain search would also
    arm on jump text inside a quoted event value, say line set to
    ".*jump 5.*", or inside a sync /regex/ body. Quoted strings and sync
    bodies are skipped whole, with the same quote rules as _strip_comment."""
    quote = ''
    esc = False
    prev = ''
    i = 0
    while i < len(rest):
        ch = rest[i]
        if esc:
            esc = False
        elif quote:
            if ch == "\\":
                esc = True
            elif ch == quote:
                quote = ''
        elif ch == '"' or (ch == "'" and prev in ":,[{"):
            quote = ch
        else:
            jm = _JUMP_RE.match(rest, i)
            if jm:
                return jm
            sm = _LEGACY_SYNC_RE.match(rest, i)
            if sm:
                i = sm.end()
                prev = '/'
                continue
        if not ch.isspace():
            prev = ch
        i += 1
    return None


@dataclass
class TimelineEntry:
    time: float
    label: str
    event_type: str = ""
    event_fields: dict[str, str] = dc_field(default_factory=dict)
    window_before: float = 2.5
    window_after: float = 2.5
    jump: float | None = None
    force_jump: bool = False
    jump_label: str = ""     # unresolved 'jump "name"' target, resolved in parse
    silent: bool = False     # 'label "name"' line, a jump target, never spoken
    legacy_sync: bool = False  # had a 'sync /regex/' clause we do not support
    hidden: bool = False     # named by a hideall directive, syncs but never shows

    @property
    def is_internal(self) -> bool:
        return (self.silent or self.hidden
                or (self.label.startswith("--") and self.label.endswith("--")))


def parse(text: str) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    hidden: set[str] = set()
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line or line.startswith('define'):
            continue
        if line.startswith('hideall'):
            hm = _HIDEALL_RE.match(line)
            if hm:
                hidden.add(hm.group(1))
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        try:
            time = float(m.group('time'))
        except ValueError:
            continue
        # A long enough digit string parses to inf, which would poison the
        # sort order and every clock comparison downstream.
        if not math.isfinite(time):
            continue

        rest = m.group('rest') or ''
        # Lift the jump clause out first. Its quoted target is free text, so
        # words inside it, say a window 5 or a brace block, must not reach
        # the searches below as real clauses. The leftmost match wins there,
        # a window inside a jump label would even shadow an explicit one.
        jump: float | None = None
        jump_label = ''
        force = False
        jm = _find_jump(rest)
        if jm:
            force = jm.group('force') is not None
            if jm.group('jlabel') is not None:
                jump_label = jm.group('jlabel')
            else:
                try:
                    jump = float(jm.group('jtime'))
                except ValueError:
                    jump = None
                # An overflowing jump target would snap the clock to inf.
                if jump is not None and not math.isfinite(jump):
                    continue
            rest = rest[:jm.start()] + ' ' + rest[jm.end():]

        # Lift an old-style sync /regex/ clause out next. We do not support
        # it, and its body can hold brace quantifiers the event search below
        # would otherwise read as an event block.
        legacy_sync = False
        lm = _LEGACY_SYNC_RE.search(rest)
        if lm:
            legacy_sync = True
            rest = rest[:lm.start()] + ' ' + rest[lm.end():]

        event_type = ''
        event_fields: dict[str, str] = {}
        # The event block need not lead the line. cactbot lets duration and
        # friends come first, so search for it and cut it out wherever it sits.
        em = _EVENT_RE.search(rest)
        if em:
            event_type = em.group('event')
            fields_text = em.group('fields') or ''
            event_fields = {key: dq or sq or bq
                            for key, dq, sq, bq in _KV_RE.findall(fields_text)}
            # Fold list-valued fields in as a regex alternation so they match
            # like the scalar form. Values are regex sources, matched by
            # re.fullmatch in the engine. A scalar of the same key wins.
            for key, body in _array_fields(fields_text):
                if key in event_fields:
                    continue
                items = [dq or sq for dq, sq in _ARRAY_ITEM_RE.findall(body)]
                if items:
                    event_fields[key] = '(?:' + '|'.join(items) + ')'
            rest = rest[:em.start()] + ' ' + rest[em.end():]
        else:
            # Nested-brace block, _EVENT_RE failed on it and the fields are
            # unusable. Record the leading keyword marked as unsupported, so
            # the engine's unsupported-type warning names it and the entry
            # never syncs. A bare supported keyword with empty fields would
            # match every line of its type and snap the clock to garbage.
            kw = _EVENT_KW_RE.search(rest)
            if kw:
                event_type = kw.group(1) + " nested fields"

        # The remaining clauses, window, duration and friends, may appear in
        # any order and may include ones we don't understand, so search rather
        # than consume sequentially. A variant token must not eat the clauses
        # after it.
        wbefore = wafter = 2.5
        wm = _WINDOW_RE.search(rest)
        if wm:
            try:
                wbefore = float(wm.group('before'))
                if wm.group('after'):
                    wafter = float(wm.group('after'))
                else:
                    wbefore = wafter = wbefore / 2
            except ValueError:
                wbefore = wafter = 2.5
            # An overflowing window would sync the entry on every earlier
            # fight time. Drop it like a bad time value.
            if not (math.isfinite(wbefore) and math.isfinite(wafter)):
                continue

        entries.append(TimelineEntry(
            time=time,
            label=m.group('label'),
            event_type=event_type,
            event_fields=event_fields,
            window_before=wbefore,
            window_after=wafter,
            jump=jump,
            force_jump=force,
            jump_label=jump_label,
            silent=m.group('labelkw') is not None,
            legacy_sync=legacy_sync,
        ))

    # Resolve 'jump "name"' targets. Prefer a 'label "name"' line, else any
    # entry whose label text matches. Unresolvable label jumps are dropped.
    by_label: dict[str, float] = {}
    for e in entries:
        if e.label and e.label not in by_label:
            by_label[e.label] = e.time
    for e in entries:                    # explicit 'label' definitions win
        if e.silent and e.label:
            by_label[e.label] = e.time
    for e in entries:
        if e.jump_label:
            e.jump = by_label.get(e.jump_label)
            if e.jump is None:
                e.force_jump = False

    # Mark the hideall names. Done after the whole file is read, the
    # directive may sit above or below the entries it names.
    for e in entries:
        if e.label in hidden:
            e.hidden = True

    return sorted(entries, key=lambda e: e.time)
