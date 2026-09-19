"""Parse cactbot timeline entries, sync windows and jump targets. Unsupported sync clauses
remain visible for diagnostics and cannot match log lines.
"""

import math
import re
from dataclasses import dataclass, field as dc_field

_LINE_RE = re.compile(
    r'^(?P<time>-?[\d.]+)\s+(?P<labelkw>label\s+)?"(?P<label>[^"]*)"\s*(?P<rest>.*)$'
)
_EVENT_RE = re.compile(r"\b(?P<event>[A-Za-z]\w*)\s*\{(?P<fields>(?:[^{}\"']|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')*)\}")
# Keep unsupported nested fields for diagnostics so an empty constraint set cannot match
# every line.
_EVENT_KW_RE = re.compile(r"\b([A-Za-z]\w*)\s*\{")
# A single window value applies on both sides of the entry.
_WINDOW_RE = re.compile(r'\bwindow\s+(?P<before>[\d.]+)(?:\s*,\s*(?P<after>[\d.]+))?')
_JUMP_RE = re.compile(r'\b(?P<force>force)?jump\s+(?:"(?P<jlabel>[^"]*)"|(?P<jtime>-?[\d.]+))')
# Accept quoted or bare values and preserve regex escapes.
_KV_RE = re.compile(r"\b(\w+)\s*:\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|([^\s,\[\]{},\"']+))")
# Parse arrays explicitly so ID alternatives are not lost.
_KV_ARRAY_START_RE = re.compile(r"\b(\w+)\s*:\s*\[")
_ARRAY_ITEM_RE = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")
# Remove legacy regex sync bodies before searching for event clauses.
_LEGACY_SYNC_RE = re.compile(r'\bsync\s*/(?:[^/\\]|\\.)*/')
# Hidden entries can still sync the clock.
_HIDEALL_RE = re.compile(r'^hideall\s+"([^"]+)"')


def _array_fields(fields_text: str) -> list[tuple[str, str | None, int, int]]:
    """Scan array fields while skipping quoted text."""
    pairs = []
    i, n = 0, len(fields_text)
    while i < n:
        c = fields_text[i]
        if c in '"\'':
            # Skip quoted text as one value.
            q = c
            i += 1
            while i < n and fields_text[i] != q:
                i += 2 if fields_text[i] == "\\" else 1
            i += 1
            continue
        m = _KV_ARRAY_START_RE.match(fields_text, i)
        if m:
            j, depth, quote = m.end(), 1, ''
            while j < n and depth:
                c = fields_text[j]
                if quote:
                    if c == '\\':
                        j += 2
                        continue
                    if c == quote:
                        quote = ''
                elif c in '\"\'':
                    quote = c
                elif c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                j += 1
            j = min(j, n)
            body = fields_text[m.end():j - 1] if depth == 0 else None
            pairs.append((m.group(1), body, i, j))
            i = j
            continue
        i += 1
    return pairs


def _strip_comment(line: str) -> str:
    """Strip comments outside quoted values. Apostrophes inside bare regexes do not start
    quoted strings.
    """
    quote = ''
    esc = False
    prev = ''
    i = 0
    while i < len(line):
        ch = line[i]
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
        else:
            sync = _LEGACY_SYNC_RE.match(line, i)
            if sync:
                i = sync.end()
                prev = '/'
                continue
        if not ch.isspace():
            prev = ch
        i += 1
    return line


def _find_jump(rest: str) -> re.Match[str] | None:
    """Find jump clauses outside quoted values and legacy regex sync bodies."""
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
        # Reject nonfinite times.
        if not math.isfinite(time):
            continue

        rest = m.group('rest') or ''
        # Parse jump targets first so quoted names cannot be read as clauses.
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
                if jump is not None and not math.isfinite(jump):
                    continue
            rest = rest[:jm.start()] + ' ' + rest[jm.end():]

        # Exclude legacy regex bodies from event clause parsing.
        legacy_sync = False
        lm = _LEGACY_SYNC_RE.search(rest)
        if lm:
            legacy_sync = True
            rest = rest[:lm.start()] + ' ' + rest[lm.end():]

        event_type = ''
        event_fields: dict[str, str] = {}
        # Clauses can appear in any order.
        em = _EVENT_RE.search(rest)
        if em:
            event_type = em.group('event')
            fields_text = em.group('fields') or ''
            # Parse arrays before scanning scalar fields.
            arrays = _array_fields(fields_text)
            scalar_chars = list(fields_text)
            for key, body, start, end in arrays:
                scalar_chars[start:end] = ' ' * (end - start)
            scalar_text = ''.join(scalar_chars)
            event_fields = {key: dq or sq or bq
                            for key, dq, sq, bq in _KV_RE.findall(scalar_text)}
            # Join array values as regex alternatives. An explicit scalar for the same
            # key takes precedence.
            for key, body, start, end in arrays:
                if key in event_fields:
                    continue
                if body is None:
                    event_fields[key] = '(?!)'
                    continue
                items = [dq or sq for dq, sq in _ARRAY_ITEM_RE.findall(body)]
                shape = _ARRAY_ITEM_RE.sub("X", body)
                if items and re.fullmatch(r'\s*X(?:\s*,\s*X)*\s*,?\s*', shape):
                    event_fields[key] = '(?:' + '|'.join(items) + ')'
                else:
                    event_fields[key] = '(?!)'
            rest = rest[:em.start()] + ' ' + rest[em.end():]
        else:
            # Mark unsupported nested fields so the entry cannot match without checking
            # them.
            kw = _EVENT_KW_RE.search(rest)
            if kw:
                event_type = kw.group(1) + " nested fields"

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

    # Prefer explicit silent labels when resolving named jumps.
    by_label: dict[str, float] = {}
    for e in entries:
        if e.label and e.label not in by_label:
            by_label[e.label] = e.time
    for e in entries:
        if e.silent and e.label:
            by_label[e.label] = e.time
    for e in entries:
        if e.jump_label:
            e.jump = by_label.get(e.jump_label)
            if e.jump is None:
                e.force_jump = False

    # Apply hideall after parsing because it can appear anywhere in the file.
    for e in entries:
        if e.label in hidden:
            e.hidden = True

    return sorted(entries, key=lambda e: e.time)
