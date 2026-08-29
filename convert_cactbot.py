#!/usr/bin/env python3
"""
Convert cactbot .ts trigger files to NyaaTriggers JSON format.

Usage.
    python3 convert_cactbot.py /path/to/cactbot              # prints JSON to stdout
    python3 convert_cactbot.py /path/to/cactbot out.json     # writes to a file
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path


# Fixed uuid5 namespace, shared by all three converters. Reruns give the same
# id for the same trigger key, so references to converted triggers survive a
# re-merge.
_ID_NS = uuid.UUID('c6a2b8e4-9d31-4f75-a0b8-5e2c7d94f1a6')


# cactbot type → our log_type. cactbot's 'Ability' matcher covers both
# NetworkAbility, line 21, and NetworkAOEAbility, line 22. Raidwides usually
# land as 22, so it maps to the pipe form Trigger.matches understands.
TYPE_MAP = {
    'StartsUsing': '20',
    'Ability':     '21|22',
    'GainsEffect': '26',
    'LosesEffect': '30',
}

# Responses.XXX calls → English callout. Keys are the function names in
# cactbot's resources/responses.ts, checked against main. Target-sensitive
# responses map to their generic text, same as tankBuster always did. Names
# that exist only as Outputs strings live in OUTPUTS below, not here.
RESPONSES = {
    'aoe':             'Raidwide',
    'bigAoe':          'Large Raidwide',
    'bleedAoe':        'AoE + Bleed',
    'hpTo1Aoe':        'HP to 1',
    'tankBuster':      'Tank Buster',
    'tankBusterSwap':  'Tank Swap!',
    'tankCleave':      'Tank Cleave',
    'miniBuster':      'Mini Buster',
    'sharedTankBuster':'Shared Tank Buster',
    'spread':          'Spread',
    'stackMarker':     'Stack',
    'stackMarkerOn':   'Stack',
    'getTogether':     'Stack',
    'stackPartner':    'Stack With Partner',
    'stackMiddle':     'Stack in Middle',
    'stackInTower':    'Stack in Tower',
    'doritoStack':     'Dorito Stack',
    'healerGroups':    'Healer Groups',
    'rolePositions':   'Role Positions',
    'protean':         'Protean',
    'spreadThenStack': 'Spread => Stack',
    'stackThenSpread': 'Stack => Spread',
    'knockback':       'Knockback',
    'knockbackOn':     'Knockback',
    'drawIn':          'Draw In',
    'getOut':          'Out',
    'getIn':           'In',
    'getUnder':        'Get Under',
    'outOfMelee':      'Out of Melee',
    'getBehind':       'Get Behind',
    'goFront':         'Go Front',
    'goFrontOrSides':  'Go Front / Sides',
    'goMiddle':        'Get Middle',
    'goLeft':          'Left',
    'goRight':         'Right',
    'goWest':          'Get Left/West',
    'goEast':          'Get Right/East',
    'goLeftThenRight': 'Left => Right',
    'goRightThenLeft': 'Right => Left',
    'goFrontBack':     'Go Front/Back',
    'goSides':         'Sides',
    'getInThenOut':    'In => Out',
    'getOutThenIn':    'Out => In',
    'getBackThenFront':     'Back => Front',
    'getFrontThenBack':     'Front => Back',
    'getSidesThenFrontBack':'Sides => Front/Back',
    'getFrontBackThenSides':'Front/Back => Sides',
    'getIntercards':   'Intercards',
    'getTowers':       'Get Towers',
    'lookAway':        'Look Away',
    'lookTowards':     'Look Towards Boss',
    'lookAwayFromTarget': 'Look Away',
    'lookAwayFromSource': 'Look Away',
    'preyOn':          'Prey on you',
    'awayFrom':        'Away',
    'awayFromFront':   'Away From Front',
    'meteorOnYou':     'Meteor on you',
    'stopMoving':      'Stop Moving!',
    'stopEverything':  'Stop Everything!',
    'moveAway':        'Move!',
    'moveAround':      'Move!',
    'breakChains':     'Break Chains',
    'moveChainsTogether': 'Move Chains Together',
    'earthshaker':     'Earth Shaker',
    'wakeUp':          'Wake Up!',
    'killAdds':        'Kill Adds',
    'killExtraAdd':    'Kill Extra Add',
    'sleep':           'Sleep',
    'stun':            'Stun',
    'stunIfPossible':  'Stun',
    'interrupt':       'Interrupt',
    'interruptIfPossible': 'Interrupt',
    'stunOrInterruptIfPossible': 'Stun or Interrupt',
}

# Outputs.XXX key → English text, also used for bare output.KEY! calls. Keys
# that exist only as Outputs strings, never as Responses calls, live here.
# Spellings follow cactbot's resources/outputs.ts, note sharedTankbuster's
# lowercase b.
OUTPUTS = {**RESPONSES, **{
    'sharedTankbuster': 'Shared Tank Buster',
    'preyOnYou':       'Prey on you',
    'lookTowardsBoss': 'Look Towards Boss',
    'inThenOut':       'In => Out',
    'outThenIn':       'Out => In',
    'stacks':          'Stacks',
    'baitPuddles':     'Bait Puddles',
    'avoidTankCleave': 'Avoid Tank Cleave',
    'tankbuster':      'Tank Buster',
    'out':        'Out',
    'in':         'In',
    'unknown':    '???',
    'text':       None,   # dynamic, skip it
}}


# ── Files to process, relative path inside data/ plus fight tag ───────────────
TARGETS = [
    ('00-misc/general.ts',                               ''),
    # DT raids, 7.0 through 7.2
    *[(f'07-dt/raid/r{i}s.ts',  f'M{i}S')  for i in range(1, 13)],
    *[(f'07-dt/raid/r{i}n.ts',  f'M{i}N')  for i in range(1, 13)],
    ('07-dt/ultimate/futures_rewritten.ts',              'FRU'),
    ('07-dt/ultimate/dancing_mad.ts',                    'UMAD'),
    # EW ultimates
    ('06-ew/ultimate/dragonsongs_reprise_ultimate.ts',   'DSR'),
    ('06-ew/ultimate/the_omega_protocol.ts',             'TOP'),
    # SHB ultimate
    ('05-shb/ultimate/the_epic_of_alexander.ts',         'TEA'),
    # DT extreme trials
    ('07-dt/trial/zoraal-ja-ex.ts',    'Zoraal Ja EX'),
    ('07-dt/trial/queen-eternal-ex.ts','Queen EX'),
    ('07-dt/trial/valigarmanda-ex.ts', 'Valigarmanda EX'),
    ('07-dt/trial/doomtrain-ex.ts',    'Doomtrain EX'),
    ('07-dt/trial/enuo-ex.ts',         'Enuo EX'),
    ('07-dt/trial/arkveld-ex.ts',      'Arkveld EX'),
    ('07-dt/trial/zelenia-ex.ts',      'Zelenia EX'),
]
# ─────────────────────────────────────────────────────────────────────────────


# A `/` can only open a regex literal after one of these, or a keyword, or at
# the start of input. After a value it is division. Standard JS lexer look-behind.
_REGEX_PRECEDERS = set('=([{,;:!&|?+-*%~^<>')
_REGEX_KEYWORDS = {'return', 'typeof', 'case', 'in', 'of', 'new', 'delete',
                   'void', 'instanceof', 'do', 'else', 'yield', 'await', 'throw'}


def _regex_can_start(out: str | list, i: int | None = None) -> bool:
    """True if a `/` at position `i` of `out` opens a regex literal rather than
    division, judged from the text before it. `i` defaults to the end of `out`,
    the strip_js_comments case where `out` is the text scanned so far. Passing
    the index lets the block scanners avoid a text[:i] copy per `/`, quadratic
    on several-hundred-KB files."""
    k = (len(out) if i is None else i) - 1
    while k >= 0 and out[k] in ' \t\r\n':
        k -= 1
    if k < 0 or out[k] in _REGEX_PRECEDERS:
        return True
    j = k
    while j >= 0 and (out[j].isalnum() or out[j] == '_'):
        j -= 1
    return ''.join(out[j + 1:k + 1]) in _REGEX_KEYWORDS


def _regex_end(text: str, i: int) -> int:
    """Index just past the regex literal whose opening `/` sits at text[i].
    Ends at the first unescaped `/` outside a [...] class. A newline or the
    end of text first means it was not a regex after all."""
    i += 1
    in_class = False
    while i < len(text):
        rc = text[i]
        if rc == '\\':
            i += 2
            continue
        if rc == '[':
            in_class = True
        elif rc == ']':
            in_class = False
        elif rc == '\n':
            return i
        elif rc == '/' and not in_class:
            return i + 1
        i += 1
    return i


def strip_js_comments(text: str) -> str:
    """Remove // and /* */ comments, respecting string AND regex literals.
    Without the string tracking, an apostrophe in a comment like "it's" reads as
    an open string and the brace tracker merges adjacent triggers, attaching the
    wrong callout, or drops them. Without the regex tracking, a literal like
    /\\/\\// reads as a line comment and silently eats the rest of the line.
    Comment bodies are replaced with spaces so offsets stay printable."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str, sc = False, ''
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == sc:
                in_str = False
            i += 1
            continue
        if c in ('"', "'", '`'):
            in_str, sc = True, c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            j = n if end < 0 else end + 2
            out.append(' ')
            # keep newlines so any line-oriented regexes still see the structure
            out.extend(ch for ch in text[i:j] if ch == '\n')
            i = j
            continue
        if c == '/' and _regex_can_start(out):
            # Regex literal. Copy verbatim so `//`, `/*`, quotes or braces
            # inside it can't be misread. Ends at the first unescaped `/`
            # outside a [...] class. Hitting a newline first means it wasn't a
            # regex after all, and everything consumed was copied through as-is.
            out.append(c)
            i += 1
            in_class = False
            while i < n:
                rc = text[i]
                out.append(rc)
                i += 1
                if rc == '\\' and i < n:
                    out.append(text[i])
                    i += 1
                elif rc == '[':
                    in_class = True
                elif rc == ']':
                    in_class = False
                elif rc == '\n' or (rc == '/' and not in_class):
                    break
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def extract_top_blocks(text: str) -> list[str]:
    """Extract top-level {...} blocks, the individual triggers, from a JS/TS array body."""
    blocks: list[str] = []
    depth, start, i = 0, -1, 0
    in_str, sc = False, ''
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '\\':
                i += 2
                continue
            if c == sc:
                in_str = False
        else:
            if c in ('"', "'", '`'):
                in_str, sc = True, c
            elif c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}':
                # Clamp at zero. One stray closer in a malformed file would
                # push depth negative, and the next trigger's outer brace
                # then fails the depth == 0 check, so its nested netRegex
                # block is recorded as a top level trigger and the real one
                # is silently dropped.
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        blocks.append(text[start: i + 1])
                        start = -1
            elif c == '/' and _regex_can_start(text, i):
                # Regex literal. Skip over it so a brace or bracket inside,
                # say /hit \{ now/, can't corrupt the depth count.
                i = _regex_end(text, i)
                continue
        i += 1
    return blocks


def find_sub_block(text: str, keyword: str) -> str:
    """Return the {...} block that immediately follows a `keyword` entry in text."""
    m = re.search(re.escape(keyword) + r'\s*:\s*\{', text)
    if not m:
        return ''
    return _block_at(text, m.end() - 1)


def _block_at(text: str, open_idx: int) -> str:
    """Return the balanced {...} block whose opening brace sits at open_idx,
    strings, escapes and regex literals honored. '' when the brace never
    closes."""
    depth, i, in_str, sc = 0, open_idx, False, ''
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '\\':
                i += 2
                continue
            if c == sc:
                in_str = False
        else:
            if c in ('"', "'", '`'):
                in_str, sc = True, c
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[open_idx: i + 1]
            elif c == '/' and _regex_can_start(text, i):
                i = _regex_end(text, i)   # regex literal, skip it
                continue
        i += 1
    return ''


def _array_at(text: str, open_idx: int) -> str:
    """Return the balanced [...] array whose opening bracket sits at open_idx,
    strings, escapes and regex literals honored. '' when the bracket never
    closes."""
    depth, i, in_str, sc = 0, open_idx, False, ''
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '\\':
                i += 2
                continue
            if c == sc:
                in_str = False
        else:
            if c in ('"', "'", '`'):
                in_str, sc = True, c
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return text[open_idx: i + 1]
            elif c == '/' and _regex_can_start(text, i):
                i = _regex_end(text, i)   # regex literal, skip it
                continue
        i += 1
    return ''


# A single- or double-quoted JS string literal, honoring backslash escapes so
# an apostrophe inside a single-quoted string, en 'Boss\'s Right', doesn't
# terminate the match early and leave a trailing backslash in the callout.
# The two branches are disjoint. A backslash is consumed ONLY by `\\.`, never
# by the `[^\\]` branch, so the quantifier can't backtrack exponentially on a
# long run of backslashes. A linear match, no ReDoS on malformed input.
_QSTR = r"(?P<q>['\"])(?P<v>(?:\\.|(?!(?P=q))[^\\])*)(?P=q)"


def _unescape_js(s: str) -> str:
    r"""Resolve JS string escapes in an extracted callout. \uXXXX and \xXX
    become their char, \' \" \\ become the bare char, and \n \t \r become a
    space. cactbot en strings occasionally embed them. One left to right pass,
    so \\ wins before the u and x forms get a look and an escaped backslash
    keeps the text behind it literal."""

    def _sub(m):
        esc = m.group(1)
        if len(esc) > 1:     # the uXXXX and xXX forms
            return chr(int(esc[1:], 16))
        return ' ' if esc in 'ntr' else esc

    s = re.sub(r'\\(u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|.)', _sub, s)
    # An escaped astral char lands as two surrogate code points. Recombine the
    # well-formed pairs and drop malformed leftovers, a leftover surrogate
    # raises on any later UTF-8 encode.
    s = re.sub(r'[\ud800-\udbff][\udc00-\udfff]',
               lambda m: chr(0x10000 + ((ord(m.group(0)[0]) - 0xd800) << 10)
                             + (ord(m.group(0)[1]) - 0xdc00)), s)
    return re.sub(r'[\ud800-\udfff]', '', s)


def _clean_callout(s: str | None) -> str | None:
    """Unescape a raw extracted string and drop it if it carries no speakable
    content, say a structural helper key like 'separator' whose en value is
    ' => ', grabbed by the output.KEY! fallback from a dynamic infoText."""
    if not s:
        return None
    s = _unescape_js(s).strip()
    if not re.search(r'\w', s, re.UNICODE):     # needs at least one letter or digit
        return None
    return s


def resolve_output_key(key: str, os_block: str) -> str | None:
    """Look up the English string for `key` inside an outputStrings block."""
    # The left boundary keeps a short key like text from matching inside a
    # longer one like context. Quoted and bare keys both still match.
    # key holds an object whose en field is the text
    m = re.search(
        r'(?<![\w$])[\'"]?' + re.escape(key) + r'[\'"]?\s*:\s*\{[^}]*en\s*:\s*' + _QSTR,
        os_block, re.DOTALL,
    )
    if m:
        return m.group('v')
    # key points at Outputs.xxx
    m = re.search(r'(?<![\w$])[\'"]?' + re.escape(key) + r'[\'"]?\s*:\s*Outputs\.(\w+)', os_block)
    if m:
        return OUTPUTS.get(m.group(1))
    return None


def get_callout(block: str) -> str | None:
    """Extract a simple English callout string from a trigger block."""
    # 1. Responses.xxx call. A name missing from RESPONSES cleans to nothing
    # and falls through, same rule as the text fields below.
    m = re.search(r'\bresponse\s*:\s*Responses\.(\w+)\(', block)
    if m:
        callout = _clean_callout(RESPONSES.get(m.group(1)))
        if callout:
            return callout

    os_block = find_sub_block(block, 'outputStrings')

    # 2. alarmText / alertText / infoText. A field that cleans to nothing, an
    # unknown output key or a structural string, must not sink the whole block
    # when a later field carries a usable callout.
    for fld in ('alarmText', 'alertText', 'infoText'):
        m = re.search(fld + r'\s*:\s*' + _QSTR, block)
        if m:
            callout = _clean_callout(m.group('v'))
        else:
            # Arrow form, an output.KEY! lookup. The scan must stay inside the
            # field's own text. An unbounded scan crosses into a lower field
            # and steals its output key.
            m = re.search(fld + r'(?:(?!(?:alarmText|alertText|infoText|outputStrings)\s*:).)*?'
                          r'output\.(\w+)!\(\)', block, re.DOTALL)
            if not m:
                continue
            key = m.group(1)
            val = resolve_output_key(key, os_block) if os_block else None
            callout = _clean_callout(val if val else OUTPUTS.get(key))
        if callout:
            return callout

    return None


def parse_netregex_ids(block: str) -> list[str]:
    """Return list of uppercase hex ability/effect IDs from the netRegex block.
    Both keys support the scalar, 'XXXX', and array, ['XXXX', 'YYYY'], forms."""
    nr = find_sub_block(block, 'netRegex')
    if not nr:
        # Call form, netRegex comes as NetRegex.ability wrapping a { ... } object.
        # The id object is the argument of that call, not the value itself.
        m = re.search(r'\bnetRegex\s*:\s*NetRegex\.\w+\s*\(\s*\{', block)
        if m:
            nr = _block_at(block, m.end() - 1)
    if not nr:
        return []
    for key in ('id', 'effectId'):
        m = re.search(r'\b' + key + r'\s*:\s*[\'"]([A-Fa-f0-9]+)[\'"]', nr)
        if m:
            return [m.group(1).upper()]
        m = re.search(r'\b' + key + r'\s*:\s*\[([^\]]+)\]', nr)
        if m:
            ids = [x.strip().strip('\'"').upper()
                   for x in m.group(1).split(',')
                   if re.match(r"^\s*['\"]?[A-Fa-f0-9]+['\"]?\s*$", x)]
            if ids:
                return ids
    return []


EXISTING_JSON = Path(__file__).parent / 'triggers.json'


def _dedup_keys(log_type: str, ability_id: str) -> set[tuple[str, str]]:
    """One key per log type / id pair. Both fields may be pipe-joined,
    "21|22" or "A55D|A55E". Comparing them whole would let a file that pins
    A55D alone slip past an existing "A55D|A55E" trigger and double-call
    the same cast."""
    lts = [p.strip() for p in str(log_type).split('|') if p.strip()]
    ids = [p.strip().upper() for p in str(ability_id).split('|') if p.strip()]
    return {(lt, aid) for lt in lts for aid in ids}


def convert_file(ts_path: Path, fight_tag: str) -> list[dict]:
    try:
        text = ts_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    text = strip_js_comments(text)

    m = re.search(r'\btriggers\s*:\s*\[', text)
    if not m:
        return []
    # Bound the scan to the triggers array. Running to EOF lets post-array
    # literals, timelineReplace blocks or helpers, leak in as candidates, and
    # any trigger-shaped one would convert as if it were a real trigger.
    blocks = extract_top_blocks(_array_at(text, m.end() - 1))

    results: list[dict] = []
    seen: set[tuple] = set()
    skipped = 0

    for block in blocks:
        # delaySeconds/condition/promise/preRun/suppressSeconds are never
        # translated. Converting one would turn a delayed or state-gated
        # trigger into an immediate unconditional callout. Skip, do not
        # mistranslate. disabled: true means cactbot ignores the trigger
        # completely, so it gets the same treatment. Match true explicitly,
        # skipping on the bare key would also drop disabled: false triggers.
        if (re.search(r'\b(?:delaySeconds|condition|promise|preRun|suppressSeconds)\s*:',
                      block)
                or re.search(r'\bdisabled\s*:\s*true\b', block)):
            skipped += 1
            continue

        # cactbot's `id` doubles as the display name. Only the block's first
        # field may name it. Blocks from extract_top_blocks begin with `{`,
        # and a line-start id deeper in, say inside netRegex, belongs to a
        # nested block. No first-field id means skip, never misname.
        nm = re.match(r"\{\s*id\s*:\s*" + _QSTR, block)
        if not nm:
            continue
        trig_name = _unescape_js(nm.group('v')).strip()

        tm = re.search(r"\btype\s*:\s*['\"](\w+)['\"]", block)
        if not tm:
            continue
        log_type = TYPE_MAP.get(tm.group(1))
        if not log_type:
            continue

        ids = parse_netregex_ids(block)
        if not ids:
            continue

        callout = get_callout(block)
        if not callout or '${' in callout:
            # A response-only trigger whose Responses name is missing from the
            # map drops here with no other trace. Name it, silent drops are
            # how a mapped fight goes quietly stale when cactbot renames or
            # adds a response.
            resp = re.search(r'\bresponse\s*:\s*Responses\.(\w+)\(', block)
            if resp and resp.group(1) not in RESPONSES:
                print(f'  WARN: {ts_path.name}: dropping {trig_name!r}, '
                      f'Responses.{resp.group(1)} is not in the RESPONSES map',
                      file=sys.stderr)
            continue

        ability_id = '|'.join(ids)
        dedup = (ability_id, fight_tag, log_type)
        # Dedup per concrete log type / id key, not on the joined string.
        # A trigger pinning A55D alone must not slip past one pinning A55D|A55E.
        keys = _dedup_keys(log_type, ability_id)
        if keys & seen:
            continue
        seen |= keys

        results.append({
            'id':            str(uuid.uuid5(_ID_NS, '\n'.join(dedup))),
            'name':          trig_name,
            'fight':         fight_tag,
            'log_type':      log_type,
            'ability_id':    ability_id,
            'ability_regex': '',
            'tts_text':      callout,
            'cooldown_s':    5.0,
            'enabled':       bool(fight_tag),
        })

    if skipped:
        print(f'  {ts_path.name}: skipped {skipped} timed/conditional trigger(s)',
              file=sys.stderr)

    return results


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: convert_cactbot.py /path/to/cactbot [output.json]', file=sys.stderr)
        sys.exit(1)

    data_dir = Path(sys.argv[1]) / 'ui' / 'raidboss' / 'data'
    out_path  = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not data_dir.is_dir():
        print(f'ERROR: cactbot data dir not found: {data_dir}', file=sys.stderr)
        sys.exit(1)

    if out_path is not None and out_path.resolve() == EXISTING_JSON.resolve():
        # This tool writes only its own batch, no merge, so that command line
        # would replace the whole trigger database with just the cactbot set.
        print(f'ERROR: {out_path} is the live trigger database; pick another output',
              file=sys.stderr)
        sys.exit(1)

    # Keys the shipped triggers.json already claims. Re-conversion must not
    # reintroduce them. Flag any overlap for the maintainer reviewing a merge.
    existing_keys: set[tuple] = set()
    try:
        existing = json.loads(EXISTING_JSON.read_text(encoding='utf-8'))
        if not isinstance(existing, list):
            # A scalar or object top level parses fine but is not a trigger
            # list. Route it to the same WARN as any other unreadable file.
            raise ValueError('top level is not a JSON array of triggers')
        for t in existing:
            if not isinstance(t, dict):
                continue
            existing_keys |= _dedup_keys(t.get('log_type', ''), t.get('ability_id', ''))
    except (OSError, ValueError) as e:
        print(f'  WARN: cannot check against {EXISTING_JSON}: {e}', file=sys.stderr)

    all_triggers: list[dict] = []
    missing = 0
    for rel, tag in TARGETS:
        ts = data_dir / rel
        if not ts.exists():
            # Upstream moves or retires files. A silent skip converts a moved
            # tree into a quietly incomplete trigger set, so say every miss.
            print(f'  WARN: target not found: {rel}', file=sys.stderr)
            missing += 1
            continue
        batch = convert_file(ts, tag)
        all_triggers.extend(batch)
        print(f'  {ts.stem:40s}  {tag or "(General)":20s}  {len(batch)} triggers',
              file=sys.stderr)
        claimed = sum(1 for t in batch
                      if _dedup_keys(t['log_type'], t['ability_id']) & existing_keys)
        if claimed:
            print(f'  WARN: {claimed} {ts.stem} key(s) already claimed in '
                  f'{EXISTING_JSON.name}', file=sys.stderr)

    print(f'\nTotal: {len(all_triggers)} triggers', file=sys.stderr)
    if missing:
        print(f'  ({missing} of {len(TARGETS)} targets missing, see WARNs above)',
              file=sys.stderr)

    out = json.dumps(all_triggers, indent=2)
    if out_path:
        if not all_triggers:
            # Zero triggers means the source tree moved or the extraction
            # broke. Never atomically overwrite a previous good output with it.
            print(f'ERROR: 0 triggers extracted, refusing to overwrite {out_path}',
                  file=sys.stderr)
            sys.exit(1)
        # Sibling tmp + rename, so an interrupted run can't leave a truncated
        # file in place of the previous good output.
        tmp_path = out_path.with_name(out_path.name + '.tmp')
        tmp_path.write_text(out, encoding='utf-8')
        os.replace(tmp_path, out_path)
        print(f'Wrote to {out_path}', file=sys.stderr)
    else:
        print(out)


if __name__ == '__main__':
    main()
