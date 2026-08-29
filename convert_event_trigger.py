#!/usr/bin/env python3
"""
Convert event-trigger Java trigger files to NyaaTriggers JSON format.

Pulls the @NpcCastCallout hex ability ids paired with each
ModifiableCallout.durationBasedCall name and TTS string. Only static TTS
text is kept, no {token} substitution.

Usage
    python3 convert_event_trigger.py /path/to/event-trigger           # stdout
    python3 convert_event_trigger.py /path/to/event-trigger out.json  # write file
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path

# Fixed uuid5 namespace, shared by all three converters. Reruns emit the same
# id for the same trigger key, so references to converted triggers survive a
# re-merge.
_ID_NS = uuid.UUID('c6a2b8e4-9d31-4f75-a0b8-5e2c7d94f1a6')

# @CalloutRepo name → our fight tag. Empty string means skip, and stays
# silent. A name absent from the map warns in convert_file, upstream adding
# a repo must never shrink the output quietly.
REPO_TO_FIGHT: dict[str, str] = {
    # DT ultimate
    "DMU Triggers":          "UMAD",
    # DT savage
    "M1S": "M1S",   "M2S": "M2S",   "M3S": "M3S",   "M4S": "M4S",
    "M5S": "M5S",   "M6S": "M6S",   "M7S": "M7S",   "M8S": "M8S",
    "M9S": "M9S",   "M10S": "M10S", "M11S": "M11S", "M12S": "M12S",
    # DT normal
    "M1N": "M1N",   "M2N": "M2N",   "M3N": "M3N",   "M4N": "M4N",
    # DT ultimate + extremes
    "FRU Triggers":          "FRU",
    "EX1":                   "Zoraal Ja EX",
    "EX2":                   "Queen EX",
    # EW ultimates
    "TOP Triggers":          "TOP",
    "Dragonsong's Reprise":  "DSR",
    # SHB / SB ultimates
    "TEA":                                  "TEA",
    "The Unending Coil of Bahamut":         "UCoB",
    "The Weapon's Refrain":                 "UwU",
    # EW savage, Pandaemonium
    "P1S": "P1S",   "P2S": "P2S",   "P3S": "P3S",   "P4S": "P4S",
    "P5S": "P5S",   "P6S": "P6S",   "P7S": "P7S",
    "P8S Door Boss":   "P8S",
    "P8S Final Boss":  "P8S",
    "P8S Final Boss Dominion Priority": "P8S",
    "P9S": "P9S",   "P10S": "P10S", "P11S": "P11S",
    "P12S Doorboss":   "P12S",
    "P12S Final Boss": "P12S",
    # EW normal
    "P1N": "P1N",   "P2N": "P2N",   "P3N": "P3N",   "P4N": "P4N",
    "P5N": "P5N",   "P6N": "P6N",   "P7N": "P7N",   "P8N": "P8N",
    "P9N": "P9N",   "P10N": "P10N", "P11N": "P11N", "P12N": "P12N",
    # EW extremes
    "Endsinger Extreme": "Endsinger EX",
    "EX4": "Barbariccia EX",
    "EX5": "Rubicante EX",
    "EX6": "Golbez EX",
    "EX7": "Zeromus EX",
    # Upstream repos that are not fights, skipped on purpose. The jail solver
    # is a mechanics helper and the dummy is a self test.
    "Titan Gaols":           "",
    "Dummy (/e c:testcall)": "",
}


def parse_hex_ids(annotation_body: str) -> list[str]:
    """Return uppercase hex IDs from @NpcCastCallout body. Real ability ids run
    3-6 hex digits, the same window convert_triggernometry.expand_id_expr uses.
    Shorter runs are inert 0x0 placeholders, longer ones junk. Named params like
    suppressMs are stripped before the hex scan, a hex value there is a tuning
    knob, not an id. A few annotations carry bare decimal values instead of 0x
    hex, so fall back to decimal when no hex matched. The fallback only accepts
    a bare value list as the whole body, which keeps suppressMs numbers from
    minting bogus ids."""
    # Every named param but value. The decimal fallback below still sees the
    # raw body, its fullmatch rejects named params on its own.
    hex_scan = re.sub(r'\b(?!value\b)\w+\s*=\s*[^,}]+', '', annotation_body)
    ids = [h.upper() for h in re.findall(r'0x([0-9A-Fa-f]{1,8})(?![0-9A-Fa-f])', hex_scan)]
    if not ids and re.fullmatch(r'\s*(?:value\s*=\s*)?\{?[\d,\s_]*\}?\s*', annotation_body):
        # Cap the digit run: int() refuses past 4300 digits, and anything over
        # 16 decimal digits hex-converts past the 6 digit id window anyway.
        ids = [format(int(d.replace('_', '')), 'X')
               for d in re.findall(r'\d[\d_]*', annotation_body)
               if len(d) <= 16]
    return [h for h in ids if 3 <= len(h) <= 6]


def map_event_tokens(s: str) -> str:
    """Translate Triggevent's {event.source}/{event.target} to the {source}/{target}
    tokens NyaaTriggers substitutes at runtime. Case-insensitive on the field
    name, upstream has both {event.target} and {event.Target}."""
    s = re.sub(r'\{event\.(?i:target)(?:\.[\w.]+)?\}', '{target}', s)
    s = re.sub(r'\{event\.(?i:source)(?:\.[\w.]+)?\}', '{source}', s)
    return s


def normalize_callout(s: str) -> str:
    """Make a Triggevent callout string speakable in NyaaTriggers.

    '=>' / '==>' -> ', then'. Stray '=' removed. Dynamic {tokens} dropped,
    we can't evaluate them locally, except {source}/{target}.
    e.g. 'Stack on {target} => out' -> 'Stack on {target}, then out'
    """
    s = re.sub(r'\\[ntr]', ' ', s)                # \n \t \r escapes -> space, not 'n'
    s = s.replace('\\', '')                       # drop remaining escapes like \"
    s = re.sub(r'\s*=+>\s*', ', then ', s)
    s = re.sub(r'\{(?!source\}|target\})[^{}]*\}', '', s)
    s = s.replace('=', ' ')
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s+([,.;:!?])', r'\1', s)
    s = re.sub(r'(,\s*then\s*)+', ', then ', s)   # collapse 'then' runs from empty tokens
    s = re.sub(r',\s*then\s*,', ', ', s)          # ', then,' from an empty token -> ', '
    s = re.sub(r'(,\s*){2,}', ', ', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' ,')


def convert_file(java_path: Path) -> list[dict]:
    try:
        if java_path.stat().st_size > 2 * 1024 * 1024:  # real trigger sources top out ~150 KB
            return []
        text = java_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []

    m = re.search(r'@CalloutRepo\s*\(\s*name\s*=\s*"([^"]+)"', text)
    if not m:
        return []
    repo_name = m.group(1)
    if repo_name not in REPO_TO_FIGHT:
        # Upstream added or renamed a repo. Say so, or the converted set
        # quietly shrinks. Mapped-but-empty names skip silently on purpose.
        print(f'  WARN: {java_path.name}: @CalloutRepo {repo_name!r} is not in '
              f'REPO_TO_FIGHT, skipped', file=sys.stderr)
    fight_tag = REPO_TO_FIGHT.get(repo_name)
    if not fight_tag:
        return []

    results: list[dict] = []
    seen: set[tuple] = set()

    # The match is  @NpcCastCallout(...), then any mix of whitespace, line
    # comments and other annotations, then
    #         private final ModifiableCallout<...> fieldName =
    #             ModifiableCallout.METHOD("Label", "TTS", ...)
    # The field modifiers are any mix of private/public/protected/static/final,
    # including none at all, and the generic may nest one level. A second
    # annotation or a same-line declaration used to drop the trigger silently.
    # The RHS also comes as ModifiableCallout.<Type>METHOD( with an explicit
    # type witness, and as the constructor form new ModifiableCallout<>(...).
    # String captures allow Java escapes, \" \\ and so on, so a label like
    # "Say \"Go\" now" isn't truncated at the backslash.
    pattern = re.compile(
        # Line anchored. A commented out annotation must not mint a live
        # trigger, the vendored tree carries a few of those.
        r'(?m)^[ \t]*@NpcCastCallout\(([^)]+)\)'
        # Single whitespace chars, not \s+, so a long run has exactly one
        # partition and the gap scan stays linear.
        r'(?:\s|//[^\n]*|@(?!NpcCastCallout)\w+(?:\([^()]*\))?)*'
        r'(?:(?:private|public|protected|static|final)\s+)*'
        r'ModifiableCallout<(?:[^<>]|<[^<>]*>)+>\s+\w+\s*=\s*'
        r'(?:ModifiableCallout\.(?:<[^>]+>)?\w+|new\s+ModifiableCallout<[^>]*>)'
        r'\(\s*"((?:[^"\\]|\\.)+)"\s*,\s*"((?:[^"\\]|\\.)+)"',
    )

    def _unescape(s: str) -> str:
        # Only the escaped quote and backslash. A bare `\"` in the source would
        # otherwise be captured literally. Leave \n \t \r intact because
        # normalize_callout turns those into spaces. Stripping the backslash
        # here first would leave 'Out\nthen' as 'Outnthen in' instead of
        # 'Out then in'.
        return re.sub(r'\\(["\\])', r'\1', s)

    for match in pattern.finditer(text):
        annotation_body = match.group(1)
        # Map {event.*} tokens first. Callouts still carrying dynamic tokens
        # are the sidecar's job.
        raw_tts = map_event_tokens(_unescape(match.group(3)))
        if re.search(r'\{(?!source\}|target\})[^{}]*\}', raw_tts):
            continue
        label           = normalize_callout(_unescape(match.group(2)))
        tts_text        = normalize_callout(raw_tts)
        if not tts_text:
            continue

        hex_ids = parse_hex_ids(annotation_body)
        if not hex_ids:
            continue

        ability_id = '|'.join(hex_ids)
        # Per id keys, the same rule _dedup_keys applies cross file. Joined
        # whole, 8C01 and 8C01|8C02 both survive and speak twice on the
        # 8C01 cast.
        keys = _dedup_keys("20", ability_id)
        if seen & keys:
            # A silent drop is how a re-run quietly loses a callout. Say it,
            # same policy as the sibling converters.
            print(f'  WARN: {java_path.name}: duplicate callout for '
                  f'{fight_tag} ability {ability_id}, dropped', file=sys.stderr)
            continue
        seen |= keys

        results.append({
            # repo_name disambiguates repos sharing a fight tag, the P8S/P12S splits.
            "id":            str(uuid.uuid5(_ID_NS, '\n'.join((repo_name, ability_id, fight_tag)))),
            "name":          f"{fight_tag} - {label}",
            "fight":         fight_tag,
            "log_type":      "20",
            "ability_id":    ability_id,
            "ability_regex": "",
            "tts_text":      tts_text,
            "cooldown_s":    5.0,
            "enabled":       True,
        })

    return results


EXISTING_JSON = Path(__file__).parent / 'triggers.json'


def _dedup_keys(log_type: str, ability_id: str) -> set[tuple[str, str]]:
    """One key per id in a pipe-joined ability_id. Both this converter and
    the shipped database join multi-id casts with pipes, "8C01|8C02", so
    comparing the joined strings whole would let a converted "8C01|8C02"
    slip past a shipped "8C01" and double-call the cast. log_type stays
    whole, the shipped pipe-joined ids all sit on one log type."""
    ids = [p.strip().upper() for p in str(ability_id).split('|') if p.strip()]
    return {(str(log_type), aid) for aid in ids}


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: convert_event_trigger.py /path/to/event-trigger [output.json]',
              file=sys.stderr)
        sys.exit(1)

    root     = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not root.is_dir():
        print(f'ERROR: input root not found: {root}', file=sys.stderr)
        sys.exit(1)

    if out_path is not None and out_path.resolve() == EXISTING_JSON.resolve():
        # This tool writes only its own batch, no merge, so that command line
        # would replace the whole trigger database with just this set.
        print(f'ERROR: {out_path} is the live trigger database; pick another output',
              file=sys.stderr)
        sys.exit(1)

    all_triggers: list[dict] = []
    seen_keys: set[tuple] = set()
    for java_file in sorted(root.rglob('*.java')):
        if '/test/' in java_file.as_posix():
            continue
        batch = convert_file(java_file)
        if not batch:
            continue
        kept = []
        for t in batch:
            keys = _dedup_keys(t['log_type'], t['ability_id'])
            if seen_keys & keys:
                # Two files can share one @CalloutRepo name. Same warn and
                # drop policy as the within-file check in convert_file.
                print(f'  WARN: {java_file.name}: duplicate callout for '
                      f"{t['fight']} ability {t['ability_id']}, dropped",
                      file=sys.stderr)
                continue
            seen_keys |= keys
            kept.append(t)
        if kept:
            fight = kept[0]['fight']
            print(f'  {java_file.name:50s}  {fight:20s}  {len(kept)} triggers',
                  file=sys.stderr)
            all_triggers.extend(kept)

    print(f'\nTotal: {len(all_triggers)} triggers', file=sys.stderr)

    # The shipped database may already claim some of these casts, and a blind
    # merge would call those twice. One summary count, the same heads-up
    # convert_cactbot gives per file.
    shipped = EXISTING_JSON
    if shipped.is_file():
        existing_keys: set[tuple] = set()
        try:
            existing = json.loads(shipped.read_text(encoding='utf-8'))
            if not isinstance(existing, list):
                # A scalar or object top level parses fine but is not a trigger
                # list. Route it to the same WARN as any other unreadable file.
                raise ValueError('top level is not a JSON array of triggers')
            for t in existing:
                if not isinstance(t, dict):
                    continue
                existing_keys |= _dedup_keys(t.get('log_type', ''),
                                             t.get('ability_id', ''))
        except (OSError, ValueError) as e:
            print(f'  WARN: cannot check against {shipped}: {e}', file=sys.stderr)
        else:
            claimed = sum(1 for t in all_triggers
                          if _dedup_keys(t['log_type'], t['ability_id']) & existing_keys)
            if claimed:
                print(f'  WARN: {claimed} of {len(all_triggers)} converted keys '
                      f'already claimed in {shipped.name}, merging would call '
                      f'those casts twice', file=sys.stderr)

    out = json.dumps(all_triggers, indent=2)
    if out_path:
        if not all_triggers:
            # Zero triggers means the source tree moved or the extraction
            # broke. Never atomically overwrite a previous good output with it.
            print(f'ERROR: 0 triggers extracted, refusing to overwrite {out_path}',
                  file=sys.stderr)
            sys.exit(1)
        # Sibling tmp + rename, so an interrupted run can't leave a truncated
        # file where the previous good output was.
        tmp_path = out_path.with_name(out_path.name + '.tmp')
        tmp_path.write_text(out, encoding='utf-8')
        os.replace(tmp_path, out_path)
        print(f'Wrote to {out_path}', file=sys.stderr)
    else:
        print(out)


if __name__ == '__main__':
    main()
