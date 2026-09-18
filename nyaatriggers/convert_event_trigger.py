#!/usr/bin/env python3
"""Convert static Java callouts to local JSON, translating supported source and target
tokens. Run python3 -m nyaatriggers.convert_event_trigger with the engine checkout path
and an optional output file.
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

# Map CalloutRepo names to fight tags. Empty mappings are deliberately skipped. Unknown
# names produce a warning.
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
    "EX1":                   "Valigarmanda EX",
    "EX2":                   "Zoraal Ja EX",
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
    # These repositories contain helpers or tests rather than fight callouts.
    "Titan Gaols":           "",
    "Dummy (/e c:testcall)": "",
}


def parse_hex_ids(annotation_body: str) -> list[str]:
    """Extract uppercase hex IDs from annotation values, accepting decimal and hexadecimal
    tokens. Ignore named tuning parameters and IDs outside the supported three to six
    digit range.
    """
    # Parse each value separately to preserve mixed decimal and hexadecimal IDs.
    value = re.search(r'\bvalue\s*=\s*(\{[^}]*\}|[^,]+)', annotation_body)
    body = value.group(1) if value else annotation_body
    body = body.strip()
    if body.startswith('{') and body.endswith('}'):
        body = body[1:-1]
    number = r'(?:0[xX][0-9A-Fa-f][0-9A-Fa-f_]*|[0-9][0-9_]*)'
    if re.fullmatch(rf'\s*{number}(?:\s*,\s*{number})*\s*,?\s*', body):
        ids = []
        for token in body.split(','):
            token = token.strip().replace('_', '')
            if not token or len(token) > 16:
                continue
            ids.append(token[2:].upper() if token.lower().startswith('0x')
                       else format(int(token, 10), 'X'))
        return [h for h in ids if 3 <= len(h) <= 6]
    # Annotation expressions retain their existing hexadecimal ID support.
    hex_scan = re.sub(r'\b(?!value\b)\w+\s*=\s*[^,}]+', '', annotation_body)
    ids = [h.upper() for h in re.findall(r'0x([0-9A-Fa-f]{1,8})(?![0-9A-Fa-f])', hex_scan)]
    return [h for h in ids if 3 <= len(h) <= 6]


def map_event_tokens(s: str) -> str:
    """Translate event source and target tokens to local substitutions without case
    sensitivity.
    """
    s = re.sub(r'\{event\.(?i:target)(?:\.[\w.]+)?\}', '{target}', s)
    s = re.sub(r'\{event\.(?i:source)(?:\.[\w.]+)?\}', '{source}', s)
    return s


def normalize_callout(s: str) -> str:
    """Convert sequence arrows to spoken pauses and remove unsupported dynamic tokens.
    Preserve source and target substitutions.
    """
    s = re.sub(r'\\[ntr]', ' ', s)
    s = s.replace('\\', '')
    s = re.sub(r'\s*=+>\s*', ', then ', s)
    s = re.sub(r'\{(?!source\}|target\})[^{}]*\}', '', s)
    s = s.replace('=', ' ')
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s+([,.;:!?])', r'\1', s)
    s = re.sub(r'(,\s*then\s*)+', ', then ', s)
    s = re.sub(r',\s*then\s*,', ', ', s)
    s = re.sub(r'(,\s*){2,}', ', ', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' ,')


def convert_file(java_path: Path) -> list[dict]:
    try:
        if java_path.stat().st_size > 2 * 1024 * 1024:  # Trigger source files are normally under 150 KB.
            return []
        text = java_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []

    m = re.search(r'@CalloutRepo\s*\(\s*name\s*=\s*"([^"]+)"', text)
    if not m:
        return []
    repo_name = m.group(1)
    if repo_name not in REPO_TO_FIGHT:
        # Warn about unknown repositories so upstream additions are visible.
        print(f'  WARN: {java_path.name}: @CalloutRepo {repo_name!r} is not in '
              f'REPO_TO_FIGHT, skipped', file=sys.stderr)
    fight_tag = REPO_TO_FIGHT.get(repo_name)
    if not fight_tag:
        return []

    results: list[dict] = []
    seen: set[tuple] = set()

    # Match the callout annotation and its field declaration, allowing intervening
    # comments and annotations. Accept factory calls, explicit type arguments and
    # constructors. String captures preserve Java escapes.
    pattern = re.compile(
        # Anchor annotations to the line so commented declarations are skipped.
        r'(?m)^[ \t]*@NpcCastCallout\(([^)]+)\)'
        # Consume comments whole to avoid overlapping matches on slash runs.
        r'(?:\s|//[^\n]*+|@(?!NpcCastCallout)\w+(?:\([^()]*\))?)*'
        r'(?:(?:private|public|protected|static|final)\s+)*'
        r'ModifiableCallout<(?:[^<>]|<[^<>]*>)+>\s+\w+\s*=\s*'
        r'(?:ModifiableCallout\.(?:<[^>]+>)?\w+|new\s+ModifiableCallout<[^>]*>)'
        r'\(\s*"((?:[^"\\]|\\.)+)"\s*,\s*"((?:[^"\\]|\\.)+)"',
    )

    def _unescape(s: str) -> str:
        # Decode quotes and backslashes here. Leave whitespace escapes for
        # normalize_callout.
        return re.sub(r'\\(["\\])', r'\1', s)

    for match in pattern.finditer(text):
        annotation_body = match.group(1)
        # Translate supported event tokens first. Remaining dynamic tokens require the
        # sidecar.
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
        # Compare individual IDs so overlapping alternatives cannot duplicate callouts.
        keys = _dedup_keys("20", ability_id)
        if seen & keys:
            # Report duplicate callouts dropped during conversion.
            print(f'  WARN: {java_path.name}: duplicate callout for '
                  f'{fight_tag} ability {ability_id}, dropped', file=sys.stderr)
            continue
        seen |= keys

        results.append({
            # Include the repository name because multiple repositories can share a
            # fight tag.
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


EXISTING_JSON = bundle_root() / 'assets' / 'triggers.json'


def _dedup_keys(log_type: str, ability_id: str) -> set[tuple[str, str]]:
    """Expand ability ID alternatives for deduplication. Keep log_type whole because
    shipped alternatives use a single type.
    """
    ids = [p.strip().upper() for p in str(ability_id).split('|') if p.strip()]
    return {(str(log_type), aid) for aid in ids}


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python3 -m nyaatriggers.convert_event_trigger /path/to/event-trigger [output.json]',
              file=sys.stderr)
        sys.exit(1)

    root     = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not root.is_dir():
        print(f'ERROR: input root not found: {root}', file=sys.stderr)
        sys.exit(1)

    if out_path is not None and out_path.resolve() == EXISTING_JSON.resolve():
        # Writing here would replace the trigger database with this converter's batch.
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

    # Report overlaps with the shipped database before merging.
    shipped = EXISTING_JSON
    if shipped.is_file():
        existing_keys: set[tuple] = set()
        try:
            existing = json.loads(shipped.read_text(encoding='utf-8'))
            if not isinstance(existing, list):
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
