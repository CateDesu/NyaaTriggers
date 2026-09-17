#!/usr/bin/env python3
"""Convert cactbot TypeScript triggers to local JSON. Run python3 -m
nyaatriggers.convert_cactbot with the cactbot checkout path and an optional output file.
Omit the file to write to stdout.
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path

from nyaatriggers.paths import bundle_root


# Share a fixed UUID namespace across converters so repeated imports preserve trigger
# IDs.
_ID_NS = uuid.UUID('c6a2b8e4-9d31-4f75-a0b8-5e2c7d94f1a6')


# Cactbot Ability covers both single target line 21 and AoE line 22.
TYPE_MAP = {
    'StartsUsing': '20',
    'Ability':     '21|22',
    'GainsEffect': '26',
    'LosesEffect': '30',
}

# English text for Responses calls. Target specific responses use generic wording.
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

# English text for Outputs keys and output.KEY lookups.
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


# A slash after a value means division. These tokens allow a regex literal instead.
_REGEX_PRECEDERS = set('=([{,;:!&|?+-*%~^<>')
_REGEX_KEYWORDS = {'return', 'typeof', 'case', 'in', 'of', 'new', 'delete',
                   'void', 'instanceof', 'do', 'else', 'yield', 'await', 'throw'}


def _regex_can_start(out: str | list, i: int | None = None) -> bool:
    """Decide whether a slash opens a regex using the preceding text. Passing an index
    avoids copying the prefix during block scans.
    """
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
    """Return the end of a regex literal, respecting escapes and character classes. Stop at
    a newline if no closing slash is found.
    """
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
    """Replace JavaScript comments with spaces while preserving newlines, string literals
    and regex literals.
    """
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
            # Keep newlines so later scans retain the line structure.
            out.extend(ch for ch in text[i:j] if ch == '\n')
            i = j
            continue
        if c == '/' and _regex_can_start(out):
            # Copy regex literals unchanged, including comment markers inside them. An
            # unescaped slash outside a character class ends the literal.
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
    """Extract trigger objects from a JavaScript array body."""
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
                # Clamp depth at zero so a stray closing brace cannot corrupt the next
                # trigger block.
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        blocks.append(text[start: i + 1])
                        start = -1
            elif c == '/' and _regex_can_start(text, i):
                # Skip regex literals so their braces do not affect depth.
                i = _regex_end(text, i)
                continue
        i += 1
    return blocks


def find_sub_block(text: str, keyword: str) -> str:
    """Return the object block immediately following a keyword."""
    m = re.search(re.escape(keyword) + r'\s*:\s*\{', text)
    if not m:
        return ''
    return _block_at(text, m.end() - 1)


def _block_at(text: str, open_idx: int) -> str:
    """Return a balanced object block, respecting strings and regexes. Return an empty
    string if it is unclosed.
    """
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
                i = _regex_end(text, i)
                continue
        i += 1
    return ''


def _array_at(text: str, open_idx: int) -> str:
    """Return a balanced array, respecting strings and regexes. Return an empty string if
    it is unclosed.
    """
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
                i = _regex_end(text, i)
                continue
        i += 1
    return ''


# The string branches consume backslash escapes separately from other characters.
# Keeping them disjoint avoids excessive backtracking on malformed input.
_QSTR = r"(?P<q>['\"])(?P<v>(?:\\.|(?!(?P=q))[^\\])*)(?P=q)"


def _unescape_js(s: str) -> str:
    """Decode JavaScript escapes in one pass and replace escaped whitespace with spaces.
    Escaped backslashes keep the following text literal.
    """

    def _sub(m):
        esc = m.group(1)
        if len(esc) > 1:
            return chr(int(esc[1:], 16))
        return ' ' if esc in 'ntr' else esc

    s = re.sub(r'\\(u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|.)', _sub, s)
    # Combine valid surrogate pairs and drop lone surrogates that cannot encode as
    # UTF-8.
    s = re.sub(r'[\ud800-\udbff][\udc00-\udfff]',
               lambda m: chr(0x10000 + ((ord(m.group(0)[0]) - 0xd800) << 10)
                             + (ord(m.group(0)[1]) - 0xdc00)), s)
    return re.sub(r'[\ud800-\udfff]', '', s)


def _clean_callout(s: str | None) -> str | None:
    """Decode an extracted string and reject values with no speakable content."""
    if not s:
        return None
    s = _unescape_js(s).strip()
    if not re.search(r'\w', s, re.UNICODE):
        return None
    return s


def resolve_output_key(key: str, os_block: str) -> str | None:
    """Look up the English string for `key` inside an outputStrings block."""
    # Require a key boundary so text cannot match context.
    m = re.search(
        r'(?<![\w$])[\'"]?' + re.escape(key) + r'[\'"]?\s*:\s*\{[^}]*en\s*:\s*' + _QSTR,
        os_block, re.DOTALL,
    )
    if m:
        return m.group('v')
    m = re.search(r'(?<![\w$])[\'"]?' + re.escape(key) + r'[\'"]?\s*:\s*Outputs\.(\w+)', os_block)
    if m:
        return OUTPUTS.get(m.group(1))
    return None


def get_callout(block: str) -> str | None:
    """Extract a simple English callout string from a trigger block."""
    m = re.search(r'\bresponse\s*:\s*Responses\.(\w+)\(', block)
    if m:
        callout = _clean_callout(RESPONSES.get(m.group(1)))
        if callout:
            return callout

    os_block = find_sub_block(block, 'outputStrings')

    # Try later text fields if an earlier one has no usable callout.
    for fld in ('alarmText', 'alertText', 'infoText'):
        m = re.search(fld + r'\s*:\s*' + _QSTR, block)
        if m:
            callout = _clean_callout(m.group('v'))
        else:
            # Keep output lookups inside this field so they cannot take a later field's
            # key.
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
    """Extract uppercase hex IDs from scalar or array netRegex fields."""
    nr = find_sub_block(block, 'netRegex')
    if not nr:
        # NetRegex calls wrap the ID object in their argument list.
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


EXISTING_JSON = bundle_root() / 'assets' / 'triggers.json'


def _dedup_keys(log_type: str, ability_id: str) -> set[tuple[str, str]]:
    """Expand pipe alternatives into individual log type and ID pairs for deduplication.
    """
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
    # Limit scanning to the triggers array so helper objects cannot become triggers.
    blocks = extract_top_blocks(_array_at(text, m.end() - 1))

    results: list[dict] = []
    seen: set[tuple] = set()
    skipped = 0

    for block in blocks:
        # Skip delayed or conditional triggers whose behavior cannot be preserved
        # locally. Only an explicit disabled: true suppresses conversion.
        if (re.search(r'\b(?:delaySeconds|condition|promise|preRun|suppressSeconds)\s*:',
                      block)
                or re.search(r'\bdisabled\s*:\s*true\b', block)):
            skipped += 1
            continue

        # Use only the first field as the trigger name. Nested ID fields belong to other
        # objects.
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
            # Report unmapped responses so upstream changes are visible.
            resp = re.search(r'\bresponse\s*:\s*Responses\.(\w+)\(', block)
            if resp and resp.group(1) not in RESPONSES:
                print(f'  WARN: {ts_path.name}: dropping {trig_name!r}, '
                      f'Responses.{resp.group(1)} is not in the RESPONSES map',
                      file=sys.stderr)
            continue

        ability_id = '|'.join(ids)
        dedup = (ability_id, fight_tag, log_type)
        # Compare individual type and ID pairs so overlapping alternatives cannot
        # duplicate callouts.
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
        print('Usage: python3 -m nyaatriggers.convert_cactbot /path/to/cactbot [output.json]',
              file=sys.stderr)
        sys.exit(1)

    data_dir = Path(sys.argv[1]) / 'ui' / 'raidboss' / 'data'
    out_path  = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not data_dir.is_dir():
        print(f'ERROR: cactbot data dir not found: {data_dir}', file=sys.stderr)
        sys.exit(1)

    if out_path is not None and out_path.resolve() == EXISTING_JSON.resolve():
        # Writing here would replace the trigger database with this converter's batch.
        print(f'ERROR: {out_path} is the live trigger database; pick another output',
              file=sys.stderr)
        sys.exit(1)

    # Report overlaps with the shipped database before merging.
    existing_keys: set[tuple] = set()
    try:
        existing = json.loads(EXISTING_JSON.read_text(encoding='utf-8'))
        if not isinstance(existing, list):
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
            # Report missing files so upstream moves cannot silently shrink the output.
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
            # Keep the previous output when extraction produces no triggers.
            print(f'ERROR: 0 triggers extracted, refusing to overwrite {out_path}',
                  file=sys.stderr)
            sys.exit(1)
        # Replace through a sibling temporary file to preserve the previous output if
        # interrupted.
        tmp_path = out_path.with_name(out_path.name + '.tmp')
        tmp_path.write_text(out, encoding='utf-8')
        os.replace(tmp_path, out_path)
        print(f'Wrote to {out_path}', file=sys.stderr)
    else:
        print(out)


if __name__ == '__main__':
    main()
