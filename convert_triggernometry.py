#!/usr/bin/env python3
"""
Convert Triggernometry XML trigger files to NyaaTriggers JSON format.

Merges every XML under SOURCE_DIRS into triggers.json, deduped on
log_type + ability_id. Both network-log regex dialects are handled.
  - pipe, OverlayPlugin / IINACT   ^20\\|f\\|f\\|f\\|HEXID\\|
  - colon, cactbot / ACT hex types \\A.{N}1[56]:src:name:HEXID:  15/16 map to ability 21/22
Ids may be literals, char classes like [7A], alternations like (642|643),
or named groups. Each expands to one trigger per concrete hex id. Only
triggers that pin a literal id and carry a static non-token UseTTS line
get converted. Complex, wildcard and Lua triggers are dropped. See
extract_ids.

Usage
    python3 convert_triggernometry.py                  # merge into triggers.json in place
    python3 convert_triggernometry.py out.json         # write to separate file
"""

import collections
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from pathlib import Path


HOME = Path.home()

SOURCE_DIRS = [
    HOME / 'Aho-Triggers/Triggernometry',
    HOME / 'FFXIV-Triggernometry-TriggerCollection',
    HOME / 'Triggernometry-Triggers/Repositories',
    HOME / 'ffxiv-triggers/xml',
    # Colon/cactbot-format repos, Dawntrail 7.x. Missing dirs are skipped with a warning.
    HOME / 'xiv_triggernometry',          # lexxiesia, M1-M12 and FRU in cactbot colon format
    HOME / 'TriggernometryArchive/dist',  # decorwdyun mirror, S7 Arcadion, U7 FRU, Ex7
    HOME / 'Downloads',                   # loose XMLs pulled from the Discord sharing channel
]

EXISTING_JSON = Path(__file__).parent / 'triggers.json'

# Fixed uuid5 namespace, shared by all three converters. Reruns emit the same
# id for the same trigger key, so references to converted triggers survive a
# re-merge.
_ID_NS = uuid.UUID('c6a2b8e4-9d31-4f75-a0b8-5e2c7d94f1a6')

HEX = set('0123456789ABCDEF')


# ── Fight name derivation ──────────────────────────────────────────────────────

ULTIMATE_MAP = {
    'The Epic of Alexander':        'TEA',
    'The Unending Coil of Bahamut': 'UCoB',
    "The Weapon's Refrain":         'UwU',
    "Dragonsong's Reprise":         'DSR',
    "Dragonson's Reprise":          'DSR',
    'Top Triggers':                 'TOP',
    'Ffxiv Ultimate Top':           'TOP',
}

RAID_ABBREV = {
    # Alexander, Heavensward
    'Gordias - The Fist Of The Father':    'A1',
    'Gordias - The Cuff Of The Father':    'A2',
    'Gordias - The Arm Of The Father':     'A3',
    'Gordias - The Burden Of The Father':  'A4',
    'Midas - The Fist Of The Son':         'A5',
    'Midas - The Cuff Of The Son':         'A6',
    'Midas - The Arm Of The Son':          'A7',
    'Midas - The Burden Of The Son':       'A8',
    'Alexander - The Eyes Of The Creator': 'A9',
    'Alexander - The Breath Of The Creator':'A10',
    'Alexander - The Heart Of The Creator':'A11',
    'Alexander - The Soul Of The Creator': 'A12',
    # Omega, Stormblood
    'Deltascape V1.0':   'O1', 'Deltascape V2.0':   'O2',
    'Deltascape V3.0':   'O3', 'Deltascape V4.0':   'O4',
    'Sigmascape V1.0':   'O5', 'Sigmascape V2.0':   'O6',
    'Sigmascape V3.0':   'O7', 'Sigmascape V4.0':   'O8',
    'Alphascape V1.0':   'O9', 'Alphascape V2.0':   'O10',
    'Alphascape V3.0':   'O11','Alphascape V4.0':   'O12',
    # Eden, Shadowbringers
    "Eden's Gate: Resurrection":  'E1', "Eden's Gate: Descent":     'E2',
    "Eden's Gate: Inundation":    'E3', "Eden's Gate: Sepulture":   'E4',
    "Eden's Verse: Fulmination":  'E5', "Eden's Verse: Furor":      'E6',
    "Eden's Verse: Iconociasm":   'E7', "Eden's Verse: Refulgence": 'E8',
    "Eden's Promise: Umbra":      'E9', "Eden's Promise: Litany":   'E10',
    "Eden's Promise: Anamorphosis":'E11',"Eden's Promise: Eternity":'E12',
    # Asphodelos, Endwalker. The "Asphodeols" typo appears in Aho-Triggers
    'Asphodelos: The First Circle':   'P1', 'Asphodeols: The First Circle':   'P1',
    'Asphodelos: The Second Circle':  'P2', 'Asphodeols: The Second Circle':  'P2',
    'Asphodelos: The Third Circle':   'P3', 'Asphodeols: The Third Circle':   'P3',
    'Asphodelos: The Fourth Circle':  'P4', 'Asphodeols: The Fourth Circle':  'P4',
    # Abyssos. Also misfiled under the "Asphodeols" circle names in Aho-Triggers
    'Abyssos: The Fifth Circle':   'P5', 'Asphodeols: The Fifth Circle':   'P5',
    'Abyssos: The Sixth Circle':   'P6', 'Asphodeols: The Sixth Circle':   'P6',
    'Abyssos: The Seventh Circle': 'P7', 'Asphodeols: The Seventh Circle': 'P7',
    'Abyssos: The Eighth Circle':  'P8', 'Asphodeols: The Eighth Circle':  'P8',
    # Anabaseios
    'Anabaseios: The Ninth Circle':   'P9',  'Asphodeols: The Ninth Circle':   'P9',
    'Anabaseios: The Tenth Circle':   'P10', 'Asphodeols: The Tenth Circle':   'P10',
    'Anabaseios: The Eleventh Circle':'P11', 'Asphodeols: The Eleventh Circle':'P11',
    'Anabaseios: The Twelfth Circle': 'P12', 'Asphodeols: The Twelfth Circle': 'P12',
    # Dawntrail
    'AAC Light-heavyweight M1': 'M1', 'AAC Light-heavyweight M2': 'M2',
    'AAC Light-heavyweight M3': 'M3', 'AAC Light-heavyweight M4': 'M4',
    'AAC Cruiserweight M1':     'M5', 'AAC Cruiserweight M2':     'M6',
    'AAC Cruiserweight M3':     'M7', 'AAC Cruiserweight M4':     'M8',
}

# RAID_ABBREV plus the two ultimates, for the unsorted-category fallback scan.
_RAID_ABBREV_FULL = {**RAID_ABBREV, 'FRU': 'FRU', 'TOP': 'TOP'}

JOB_MAP = {
    '1 - WHM': 'WHM', '2 - SCH': 'SCH', '3 - AST': 'AST', '4 - SGE': 'SGE',
    '1 - BLM': 'BLM', '2 - SMN': 'SMN', '3 - RDM': 'RDM', '4 - PCT': 'PCT',
    '1 - MNK':  'MNK', '2 - DRG': 'DRG', '3 - NIN': 'NIN', '4 - SAM': 'SAM',
    '5 - RPR': 'RPR', '6 - VPR': 'VPR',
    '1 - BRD': 'BRD', '2 - MCH': 'MCH', '3 - DNC': 'DNC',
    '1 - PLD': 'PLD', '2 - WAR': 'WAR', '3 - DRK': 'DRK', '4 - GNB': 'GNB',
    '00 - Role Actions': 'Role', '99 - BLU': 'BLU',
}


def _strip_num_prefix(s: str) -> str:
    # Strip leading "NN - " style number prefixes, dotted or dashed. Several may be stacked
    while True:
        stripped = re.sub(r'^\d+[\d.]* ?[-–:] ?', '', s).strip()
        if stripped == s:
            break
        s = stripped
    return s


def _strip_attribution(s: str) -> str:
    # Trailing credits only. If the whole string matches, it is a name that
    # merely looks like a credit, "Made By Heaven" for instance. Keep it.
    # The word boundary keeps a mid-word "by" from stripping, "Kirby
    # Triggers" is a name, not a credit.
    out = re.sub(r'\s*[\(\[]?\b(from|by|credit|made by|originally made by)\s+[^\)\]]+[\)\]]?\s*$', '', s, flags=re.I).strip()
    return out or s.strip()


def _normalize_fight_name(name: str) -> str:
    """Map a raw folder name to a canonical fight tag where possible."""
    # p9s, m4, m10s, etc.
    m = re.match(r'^([pm])(\d+)(s|n)?$', name.lower())
    if m:
        prefix = m.group(1).upper()
        num    = m.group(2)
        suffix = (m.group(3) or '').upper()
        return f'{prefix}{num}{suffix}'
    return name


_KNOWN_CATS = {'sharing channel', 'ffxiv battle jobs', 'eureka-like', 'eureka', 'hunts',
               'pull', 'trials', 'raids', 'dungeons', 'duty_partyfinder',
               'ultimate', 'alliance', 'unreal', 'criterion', 'field operations',
               'variant & criterion'}


def _word(pattern: str, text: str):
    # \b counts the underscore as a word char, so a snake_case folder like
    # disciples_of_war or BA_Raid falls out of the word-bounded checks and
    # the file misfiles into the fallback branch. Bound on alphanumerics
    # instead, which treats the underscore as a separator.
    return re.search(r'(?<![A-Za-z0-9])' + pattern + r'(?![A-Za-z0-9])', text)


def path_to_fight(path: str) -> str:
    parts = [p.strip() for p in path.split('/') if p.strip()]

    # Strip versioned wrapper roots, e.g. "Github v7.51.260608" in colon-format
    # repos, so the real category leads.
    while len(parts) > 1 and parts[0].lower() not in _KNOWN_CATS \
            and 'misc' not in parts[0].lower() and 'positional' not in parts[0].lower() \
            and not parts[0].startswith('FFXIV Battle Jobs') \
            and any(p.lower() in _KNOWN_CATS for p in parts[1:]):
        parts = parts[1:]

    top = parts[0] if parts else ''

    # Sharing channel, normalize then re-classify
    if top.lower() == 'sharing channel' and len(parts) > 1:
        category = parts[1].lower()
        rest = '/'.join(parts[2:])

        if 'ultimate' in category:
            for key, abbrev in ULTIMATE_MAP.items():
                if key.lower() in path.lower():
                    return abbrev
            if 'fru' in path.lower() or 'futures rewritten' in path.lower():
                return 'FRU'
            # Word-bounded, same as the unsorted scan below, so STOP or
            # DESKTOP can't mistag as TOP.
            if re.search(r'\bTOP\b', rest) or re.search(r'\bOmega Protocol\b', rest):
                return 'TOP'

        elif 'savage' in category:
            # e.g. "Savages/6.4 - Anabaseios/P9s/..."
            for p in parts[2:]:
                n = _normalize_fight_name(_strip_attribution(_strip_num_prefix(p)))
                if re.match(r'^[PM]\d+[SN]?$', n):
                    return n if n[-1] in ('S','N') else n + 'S'
            for p in parts[2:]:
                clean = _strip_num_prefix(p)
                for key, abbrev in RAID_ABBREV.items():
                    if key.lower() in clean.lower():
                        return abbrev + 'S'

        elif 'trial' in category:
            # Same scan as the dedicated Trials branch below, one folder
            # deeper, the Sharing Channel root and category shift every
            # component, so parts[2] is the era and the fight name starts
            # at parts[3].
            for p in parts[3:]:
                name = _strip_num_prefix(p)
                name = _strip_attribution(name)
                name = re.sub(r'\s*\(.*?\)', '', name).strip()
                if name and name.lower() not in ('ex', 'savage', 'hard', 'network triggers',
                                                 'extreme', 'attempt at complex mechanics',
                                                 'weaponcounter'):
                    return name

        # Word-bounded so "Edwards" can't land in the job branch. The plural
        # matters, Disciples of War is the usual folder name.
        elif _word(r'disciples?', category) or _word(r'war', category):
            for p in reversed(parts):
                m = re.search(r'\b(WHM|SCH|AST|SGE|BLM|SMN|RDM|PCT|MNK|DRG|NIN|SAM|RPR|VPR|BRD|MCH|DNC|PLD|WAR|DRK|GNB|BLU)\b', p)
                if m:
                    return m.group(1)
            if 'hunt' in path.lower():
                return 'Hunts'
            return 'Job'

        elif 'variant' in category or 'criterion' in category:
            for p in reversed(parts[2:]):
                name = _strip_attribution(_strip_num_prefix(p))
                name = re.sub(r'\s*[\(\[].*?[\)\]]', '', name).strip()
                if name and name.lower() not in ('unsorted', 'variant & criterion'):
                    return name

        else:
            # Unsorted, try to find a fight tag from the folder names
            for p in parts[2:]:
                name = _strip_attribution(p)
                name = re.sub(r'\s*[\(\[].*?[\)\]]', '', name).strip()
                n = _normalize_fight_name(_strip_num_prefix(name))
                if re.match(r'^[PM]\d+[SN]?$', n):
                    # No difficulty defaults to Savage, same as the savage
                    # branch above and the final fallback below.
                    return n if n[-1] in ('S','N') else n + 'S'
                for key, abbrev in _RAID_ABBREV_FULL.items():
                    # Word-bounded so the three-letter tags, FRU and TOP,
                    # can't substring-match inside words like "Stop" or
                    # "Desktop".
                    if re.search(r'\b' + re.escape(key) + r'\b', name, re.I):
                        return abbrev
            for p in reversed(parts[2:]):
                name = _strip_attribution(_strip_num_prefix(p))
                name = re.sub(r'\s*[\(\[].*?[\)\]]', '', name).strip()
                if name:
                    return name
        return ''

    # Jobs
    if top.startswith('FFXIV Battle Jobs'):
        for p in reversed(parts):
            if p in JOB_MAP:
                return JOB_MAP[p]
            m = re.match(r'\d+ - ([A-Z]{2,3})$', p)
            if m:
                return m.group(1)
        return 'Job'

    # Misc / positionals
    if 'Misc' in top or 'Positional' in path:
        return 'Misc'

    # Eureka / Bozja
    if top == 'Eureka-Like':
        if _word(r'BA', path) or _word(r'Baldesion', path):
            return 'BA'
        if 'Delubrum' in path:
            return 'Delubrum Reginae'
        if 'Bozja' in path or 'Bozjan' in path:
            return 'Bozja'
        for p in parts[1:]:
            clean = _strip_num_prefix(p)
            if clean and clean not in ('Eureka-Like', 'Eureka', 'Eureka Callouts'):
                return clean
        return 'Eureka'

    # Eureka, the FFXIV-Triggernometry-TriggerCollection layout
    if top == 'Eureka' or ('Eureka' in path and 'Anemos' in path):
        if 'Anemos' in path:
            return 'Anemos'
        if 'Pagos' in path:
            return 'Pagos'
        if 'Pyros' in path:
            return 'Pyros'
        if 'Hydatos' in path:
            return 'Hydatos'
        return 'Eureka'
    if 'Pagos' in path:
        return 'Pagos'

    # Hunts
    # Word-bounded and plural, a trial folder like "The Hunt Line" does not
    # name a hunts category and falls through to Trials.
    if top == 'Hunts' or re.search(r'\bHunts\b', path):
        for p in parts:
            m = re.search(r'(\d+\.\d+)', p)
            if m:
                return f'Hunts {m.group(1)}'
        return 'Hunts'

    # Duty/Party Finder
    if _word(r'Duty_PartyFinder', path) or _word(r'Party', path):
        return 'Party Finder'

    # Pull timers
    if top == 'Pull':
        return 'Pull'

    # Trials
    if top == 'Trials':
        # Grab the specific trial name, 3rd component onward, skipping the era
        for p in parts[2:]:
            name = _strip_num_prefix(p)
            name = _strip_attribution(name)
            name = re.sub(r'\s*\(.*?\)', '', name).strip()
            if name and name.lower() not in ('ex', 'savage', 'hard', 'network triggers',
                                              'extreme', 'attempt at complex mechanics',
                                              'weaponcounter'):
                return name
        return 'Trial'

    # Dungeons
    if top == 'Dungeons':
        if 'Deep Dungeons' in path:
            if 'Eureka Orthos' in path:
                return 'Eureka Orthos'
            return 'Deep Dungeon'
        if 'BLU' in path or 'Masked Carnival' in path:
            return 'BLU Masked Carnival'
        for p in reversed(parts):
            name = _strip_num_prefix(p)
            if name and not re.match(r'^\d+\.\d+', name) and \
               name not in ('Dungeons', '! - TTS Callouts', '# - Settings'):
                return name
        return 'Dungeon'

    # Ultimate, colon/lexxie layout. Ultimate/<expansion>/<fight>/...
    if top == 'Ultimate':
        low = path.lower()
        if 'futures rewritten' in low or 'fru' in low:
            return 'FRU'
        for key, abbrev in ULTIMATE_MAP.items():
            if key.lower() in low:
                return abbrev
        if 'omega protocol' in low:
            return 'TOP'
        for p in parts[2:]:
            name = _strip_num_prefix(p)
            if name and 'network' not in name.lower():
                return re.sub(r'\s*\(Ultimate\)', '', name).strip()
        return ''

    # Alliance raids, colon/lexxie layout. Alliance/<expansion>/NN - <RaidName>/<boss>
    if top == 'Alliance':
        for p in parts[2:]:
            name = re.sub(r'\s*[\(\[].*?[\)\]]', '', _strip_num_prefix(p)).strip()
            if name:
                return name
        return ''

    # Field Operations, Bozja / Eureka / Occult Crescent
    if top == 'Field Operations':
        low = path.lower()
        if 'delubrum' in low:
            return 'Delubrum Reginae'
        if 'bozjan southern front' in low:
            return 'Bozja'
        for zone in ('Zadnor', 'Anemos', 'Pagos', 'Pyros', 'Hydatos'):
            if zone.lower() in low:
                return zone
        if 'occult' in low or 'forked tower' in low or 'crescent' in low:
            return 'Occult Crescent'
        for p in parts[2:]:
            name = _strip_num_prefix(p)
            if name:
                return name
        return ''

    # Unreal trials, Unreal/<TrialName>/...
    if top == 'Unreal':
        _roles = {'trackers', 'weapon trackers', 'tanks', 'healers', 'ranged', 'melee', 'caster'}
        for p in parts[1:]:
            name = re.sub(r'\s*[\(\[].*?[\)\]]', '', _strip_num_prefix(p)).strip()
            if name and name.lower() not in _roles:
                return f'{name} (Unreal)'
        return ''

    # Criterion dungeons, Criterion/<DungeonName>/<boss>/...
    if top == 'Criterion':
        for p in parts[1:]:
            name = re.sub(r'\s*[\(\[].*?[\)\]]', '', _strip_num_prefix(p)).strip()
            if name and name.lower() not in ('trash mobs', 'trackers'):
                return name
        return ''

    # Raids
    if top == 'Raids':
        # Ultimates
        if any('Ultimate' in p or 'Ultimates' in p for p in parts):
            for p in parts:
                for key, abbrev in ULTIMATE_MAP.items():
                    if key.lower() in p.lower():
                        return abbrev
            # fallback, grab the folder name
            for p in parts:
                if 'Ultimate' in p:
                    name = _strip_num_prefix(p)
                    return re.sub(r'\s*\(Ultimate\)', '', name).strip()

        # Savage-capable tiers default to Savage unless the folder marks normal/story.
        # Tiers with no savage variant fall through unsuffixed.
        is_savage = any(p.lower() == 'savage' or p.lower().endswith('(savage)') for p in parts)
        is_normal = any(p.lower() in ('normal', 'story') or p.lower().endswith('(normal)') for p in parts)

        for p in parts:
            clean = _strip_num_prefix(p)
            clean_nosav = re.sub(r'\s*\((?:savage|normal)\)', '', clean, flags=re.I).strip()
            for key, abbrev in RAID_ABBREV.items():
                if key.lower() in clean_nosav.lower():
                    return abbrev + ('N' if (is_normal and not is_savage) else 'S')

        # Generic fallback, last substantive folder, no S/N suffix
        skip = {'savage', 'normal', '! - tts callouts', '# - settings',
                'raids', '8-man raids', 'alliance raids', 'ultimates'}
        for p in reversed(parts):
            if p.lower() in skip:
                continue
            name = _strip_num_prefix(p)
            name = _strip_attribution(name)
            name = re.sub(r'\s*[\(\[].*?[\)\]]', '', name).strip()
            if name:
                return name

    # Final fallback. A bare fight-abbrev folder, m10, p4s, M8S, anywhere in the
    # path, for loose single-fight exports. No difficulty defaults to Savage.
    for p in parts:
        n = _normalize_fight_name(p)
        if re.match(r'^[PM]\d+[NS]$', n):
            return n
        if re.match(r'^[PM]\d+$', n):
            return n + 'S'

    return ''


# ── Regex parsing ──────────────────────────────────────────────────────────────
# extract_ids turns a Triggernometry RegularExpression into the concrete
# log_type + ability_id pairs it pins, in both pipe and colon dialects.

def _split_top(s: str, sep: str) -> list[str]:
    """Split s on `sep` chars that sit at paren/bracket depth 0."""
    out, depth, cur = [], 0, ''
    for c in s:
        if c in '([':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        if c == sep and depth == 0:
            out.append(cur); cur = ''
        else:
            cur += c
    out.append(cur)
    return out


def _expand_class(body: str) -> list[str] | None:
    """Expand the inside of a [...] hex char class. None if not pure hex or negated."""
    if body.startswith('^'):
        return None
    chars, i = set(), 0
    while i < len(body):
        if i + 2 < len(body) and body[i + 1] == '-':
            lo, hi = body[i].upper(), body[i + 2].upper()
            if lo not in HEX or hi not in HEX or int(lo, 16) > int(hi, 16):
                return None
            for v in range(int(lo, 16), int(hi, 16) + 1):
                chars.add(format(v, 'X'))
            i += 3
            continue
        c = body[i].upper()
        if c not in HEX:
            return None
        chars.add(c)
        i += 1
    return sorted(chars)


def expand_id_expr(expr: str, cap: int = 32, level: int = 0) -> list[str] | None:
    """Concrete hex-id set a restricted regex fragment denotes, or None if not finite.
    The length/hex filter runs only on the top-level result. Recursive calls
    return the raw expansion so a factored-out shared prefix like 27 with a
    5C-or-7[346] alternation survives concatenation. The floor stays 3, so a
    bare [0-9A-F] field still bails, it expands to 1-char strings, rather than
    emitting one trigger per hex digit."""
    expr = expr.strip()
    if not expr or level > 20:      # degenerate nesting, bail instead of RecursionError
        return None

    def _finish(ids: Iterable[str]) -> list[str] | None:
        if level > 0:
            out = sorted(set(ids))
        else:
            out = sorted({r for r in ids if 3 <= len(r) <= 6 and all(ch in HEX for ch in r)})
        return out or None
    # Strip one fully-enclosing group, repeatedly. Handles both named forms
    # plus the .NET single quote one, non-capturing and plain. Bound the
    # passes. Each costs a full string scan and a crafted export nests deep
    # enough to hang the import dialog.
    for _ in range(64):
        m = re.match(r"^\((?:\?P?<[^>]+>|\?:|\?'[^']+')?(.*)\)$", expr, re.S)
        if not m:
            break
        inner = m.group(1)
        depth, outermost = 0, True
        for c in inner:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth < 0:
                    outermost = False; break
        if not outermost or depth != 0:
            break
        expr = inner.strip()
    else:
        return None   # still stripping after 64 passes, degenerate input
    # top-level alternation
    alts = _split_top(expr, '|')
    if len(alts) > 1:
        out: set[str] = set()
        for a in alts:
            r = expand_id_expr(a, cap, level + 1)
            if r is None:
                return None
            out.update(r)
            if len(out) > cap:
                return None
        return _finish(out)
    # single alternative, concatenation of atoms into a cartesian product
    results = ['']
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == '(':
            depth, j = 0, i
            while j < len(expr):
                if expr[j] == '(':
                    depth += 1
                elif expr[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(expr):
                return None
            sub = expand_id_expr(expr[i:j + 1], cap, level + 1)
            if sub is None:
                return None
            results = [p + s for p in results for s in sub]
            i = j + 1
        elif c == '[':
            # First UNescaped ']' ends the class. A plain find of ']' would
            # truncate a class containing '\]' and mis-split everything after it.
            j = i + 1
            while j < len(expr) and expr[j] != ']':
                j += 2 if expr[j] == '\\' else 1
            if j >= len(expr):
                return None
            sub = _expand_class(expr[i + 1:j])
            if sub is None:
                return None
            results = [p + s for p in results for s in sub]
            i = j + 1
        elif c.upper() in HEX:
            results = [p + c.upper() for p in results]
            i += 1
        else:
            return None  # '.', '*', '+', '{', '\', anchors, non-hex literal -> bail
        if len(results) > cap:
            return None
    return _finish(results)


def _normalize_repeats(rx: str) -> str:
    """Expand compact field repeats, (?:[^|]*\\|){3} becomes [^|]*\\|[^|]*\\|[^|]*\\|."""
    def rep(m):
        inner = m.group(1)
        try:
            n = int(m.group(2))
        except ValueError:
            # Python 3.11 refuses int strings past 4300 digits. Keep a count
            # that absurd as literal text, same as any count too big to expand.
            return m.group(0)
        return inner * n if n <= 10 else m.group(0)
    return re.sub(r'\(\?:([^()]*?(?:\\\||:))\)\{(\d+)\}', rep, rx)


def _fields_pipe(body: str) -> list[str]:
    out, depth, cur, i = [], 0, '', 0
    while i < len(body):
        c = body[i]
        if depth == 0 and body[i:i + 2] == r'\|':
            out.append(cur); cur = ''; i += 2; continue
        if c in '([':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        cur += c; i += 1
    out.append(cur)
    return out


def _fields_colon(body: str) -> list[str]:
    out, depth, cur = [], 0, ''
    for c in body:
        if c in '([':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        if c == ':' and depth == 0:
            out.append(cur); cur = ''
        else:
            cur += c
    out.append(cur)
    return out


PIPE_CAST = {'20', '21', '22', '23'}    # ability id at field index 3, after time, srcId, srcName
PIPE_STATUS = {'26', '30'}              # effect id at field index 1, after time
# colon hex type -> decimal log_type plus id field index after the type prefix
# 1[56] is ability-or-aoe. Keep both via the pipe form Trigger.matches
# understands, same as the cactbot converter's 'Ability', so a raidwide that
# lands as 22 isn't misread as single-target 21.
COLON_TYPES = {'14': ('20', 2), '15': ('21', 2), '16': ('22', 2),
               '1A': ('26', 0), '1E': ('30', 0), '1[56]': ('21|22', 2)}


def extract_ids(regex: str) -> list[tuple[str, str]]:
    """Return the log_type and ability_id pairs the regex pins to literal hex ids, empty list if none."""
    if not regex:
        return []
    # No html.unescape here. ET.parse already unescapes attribute values, so a
    # second pass would double-decode a literal &amp; in the source.
    rx = _normalize_repeats(regex)

    # PIPE form, ^<dec>\|...
    m = re.match(r'^\^(\d+)\\\|(.*)$', rx, re.S)
    if m:
        lt, body = m.group(1), m.group(2)
        fields = _fields_pipe(body)
        if lt in PIPE_CAST and len(fields) > 3:
            ids = expand_id_expr(fields[3])
            return [(lt, i) for i in ids] if ids else []
        if lt in PIPE_STATUS and len(fields) > 1:
            ids = expand_id_expr(fields[1])
            return [(lt, i) for i in ids] if ids else []
        return []

    # COLON form, \A.{N} or ^.{N}, optional skip group or ACT type word like
    # StatusAdd, then the hex type and colon
    m = re.match(r'^(?:\\A|\^)(?:\.\{\d+(?:,\d+)?\})?(?:\(\?:\[\^:\]\*\)|\S+ )?(1[456AE]|1\[56\]):(.*)$', rx, re.S)
    if m and m.group(1) in COLON_TYPES:
        lt, idx = COLON_TYPES[m.group(1)]
        fields = _fields_colon(m.group(2))
        if len(fields) > idx:
            ids = expand_id_expr(fields[idx])
            return [(lt, i) for i in ids] if ids else []
    return []


# ── XML walking ───────────────────────────────────────────────────────────────

def walk_xml(elem: ET.Element, path: str = '', depth: int = 0) -> Iterator[tuple[str, ET.Element]]:
    if depth > 128:     # degenerate nesting. Real folder trees are shallow
        return
    name = elem.attrib.get('Name', '')
    fullpath = (path + '/' + name).strip('/') if name else path
    for child in elem:
        if child.tag == 'Trigger':
            yield fullpath, child
        else:
            yield from walk_xml(child, fullpath, depth + 1)


def extract_tts(trigger_elem) -> str | None:
    for action in trigger_elem.findall('.//Action[@ActionType="UseTTS"]'):
        if action.attrib.get('Enabled', '').lower() == 'false':
            continue    # a disabled action never speaks
        text = action.attrib.get('UseTTSTextExpression', '').strip()
        if text and '{' not in text and not text.startswith('_'):
            return text
    return None


# Folder and trigger-name fragments that mark debug or test scaffolding, skipped.
_SKIP_PATH_RE = re.compile(r'(?i)(?:^|[/ ])(debug|regression|log ?lines?|hello ?world|sandbox|settings)(?:$|[/ ])')

# Zone regexes for fights missing from the existing triggers.json map.
_EXTRA_ZONES = {
    'FRU': 'Futures Rewritten',
}


def _fallback_zone(fight: str, path: str) -> str:
    """Zone regex for a fight absent from the canonical map. Field Operations,
    Alliance raids, and Criterion dungeons use the zone name AS the fight tag, so the
    tag doubles as a substring zone regex. Trials are skipped, boss name != zone name."""
    if not fight or re.match(r'^[A-Z]{1,3}\d+[NS]?$', fight) or fight.endswith('(Unreal)'):
        return ''
    if 'Field Operations' in path or 'Alliance' in path or 'Criterion' in path:
        return fight
    return ''


def load_zone_map(existing: list[dict]) -> dict[str, str]:
    """Build a fight tag to zone regex map from the existing triggers, most common zone per fight."""
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t in existing:
        if not isinstance(t, dict):
            continue
        fight, zone = t.get('fight', ''), t.get('zone_regex', '')
        if not isinstance(fight, str) or not isinstance(zone, str):
            # A hand edited non scalar value, a list for a fight tag, would
            # raise TypeError as a dict key and kill the whole merge. The
            # row is junk, skip it.
            continue
        if fight and zone:
            counts[fight][zone] += 1
    return {fight: c.most_common(1)[0][0] for fight, c in counts.items()}


def convert_xml(xml_path: Path, zone_map: dict[str, str]) -> list[dict]:
    try:
        size = xml_path.stat().st_size
        # Real Triggernometry exports are well under 1 MB. 16 MB is a generous
        # ceiling that also bounds the in-memory amplification libexpat still
        # allows, sub-100x per-entity expansion, if a crafted file slips past
        # the DOCTYPE check below.
        if size > 16 * 1024 * 1024:
            print(f"  SKIP (too large): {xml_path.name}", file=sys.stderr)
            return []
        # Scan the WHOLE file, not just the first 64 KB. A DOCTYPE hidden behind
        # a large comment prolog would slip past a fixed-window check. Drop NULs
        # so a UTF-16 prolog matches.
        head = xml_path.read_bytes().replace(b'\x00', b'')
        if b'<!DOCTYPE' in head or b'<!ENTITY' in head:
            # ElementTree expands DTD entities. Trigger XMLs never carry a DTD,
            # so refuse any file that declares one.
            print(f"  SKIP (DOCTYPE/ENTITY declaration): {xml_path.name}", file=sys.stderr)
            return []
        tree = ET.parse(xml_path)
    # LookupError covers an unknown encoding name, ValueError a declared
    # multi byte encoding like utf-7 or utf-32 without a BOM.
    except (OSError, ET.ParseError, LookupError, ValueError) as e:
        print(f"  SKIP (parse error): {xml_path.name}: {e}", file=sys.stderr)
        return []

    results = []
    key_counts: dict[str, int] = {}
    suffixed = 0
    for folder_path, trigger in walk_xml(tree.getroot()):
        if _SKIP_PATH_RE.search(folder_path):
            continue
        pairs = extract_ids(trigger.attrib.get('RegularExpression', ''))
        if not pairs:
            continue

        tts = extract_tts(trigger)
        if not tts:
            continue

        fight = path_to_fight(folder_path)
        zone = (zone_map.get(fight, '') or _EXTRA_ZONES.get(fight, '')
                or _fallback_zone(fight, folder_path))
        base_name = trigger.attrib.get('Name', '') or pairs[0][1]
        multi = len(pairs) > 1
        for log_type, ability_id in pairs:
            # The fight goes into the key. Different fights legitimately reuse
            # the same ability id, and identical row ids break id-keyed UI
            # actions, select and delete hit the first match. A key repeated
            # within one file, per-role variants of one cast, gets an
            # occurrence suffix so every row id stays unique. The walk order
            # is fixed, so ids stay deterministic across runs of one input.
            key = f'{fight}\n{log_type}\n{ability_id}'
            n = key_counts.get(key, 0)
            key_counts[key] = n + 1
            if n:
                key = f'{key}\n{n}'
                suffixed += 1
            results.append({
                'id':            str(uuid.uuid5(_ID_NS, key)),
                'name':          f'{base_name} [{ability_id}]' if multi else base_name,
                'fight':         fight,
                'log_type':      log_type,
                'ability_id':    ability_id,
                'ability_regex': '',
                'zone_regex':    zone,
                'tts_text':      tts,
                'cooldown_s':    5.0,
                'enabled':       False,
            })

    if suffixed:
        print(f"  suffixed {suffixed} repeated-key row id(s): {xml_path.name}", file=sys.stderr)

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else EXISTING_JSON

    try:
        existing: list[dict] = json.loads(EXISTING_JSON.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(f"  ERROR: cannot read {EXISTING_JSON}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(existing, list):
        print(f"  ERROR: {EXISTING_JSON} must hold a JSON array of triggers", file=sys.stderr)
        sys.exit(1)
    def _dedup_keys(log_type: str, ability_id: str) -> set[tuple[str, str]]:
        """One key per log type and id pair. Both fields may be pipe-joined,
        "21|22", "A55D|A55E". Comparing them whole would let an XML that pins
        A55D alone slip past an existing "A55D|A55E" trigger and double-call
        the same cast."""
        lts = [p.strip() for p in str(log_type).split('|') if p.strip()]
        ids = [p.strip().upper() for p in str(ability_id).split('|') if p.strip()]
        return {(lt, aid) for lt in lts for aid in ids}

    existing_keys: set[tuple] = set()
    for t in existing:
        if not isinstance(t, dict):
            continue
        # The runtime coerces ability_id to str on load, so a numeric id in
        # the file must not crash us here.
        aid = str(t.get('ability_id', '')).upper()
        lt  = t.get('log_type', '')
        if aid:
            existing_keys |= _dedup_keys(lt, aid)

    zone_map = load_zone_map(existing)

    print(f"Existing triggers: {len(existing)} ({len(existing_keys)} unique ability keys, "
          f"{len(zone_map)} fight->zone mappings)", file=sys.stderr)

    xml_files: list[Path] = []
    for src in SOURCE_DIRS:
        if src.exists():
            # Match the suffix lowercased so an uppercase .XML still converts
            # on Linux.
            xml_files.extend(p for p in src.rglob('*') if p.suffix.lower() == '.xml')
        else:
            print(f"  WARN: source dir not found: {src}", file=sys.stderr)

    # Skip hidden dirs, .idea, .git and friends, plus non-trigger XMLs
    xml_files = [
        f for f in xml_files
        if not any(part.startswith('.') for part in f.parts) and 'pom.xml' not in f.name
        and f.stem not in ('Settings', 'examples', 'RDM_Gauge_Example',
                            'BLM_Gauge_Example', 'Auto-Attack_Example',
                            'ffxiv_ahos_debug', 'ffxiv_ahos_experimental_triggers',
                            'Honk', 'Paisley Park')
    ]

    print(f"Processing {len(xml_files)} XML files...", file=sys.stderr)

    new_triggers: list[dict] = []
    seen_keys: set[tuple] = set(existing_keys)
    counts = {'total': 0, 'dup_existing': 0, 'dup_new': 0, 'added': 0}

    for xml_path in sorted(xml_files):
        converted = convert_xml(xml_path, zone_map)
        file_added = 0
        for t in converted:
            counts['total'] += 1
            keys = _dedup_keys(t['log_type'], t['ability_id'])
            if keys & seen_keys:
                if keys & existing_keys:
                    counts['dup_existing'] += 1
                else:
                    counts['dup_new'] += 1
                continue
            seen_keys |= keys
            new_triggers.append(t)
            file_added += 1
            counts['added'] += 1
        if file_added:
            print(f"  +{file_added:3d}  {xml_path.name}", file=sys.stderr)

    merged = existing + new_triggers
    tmp_path = out_path.with_name(out_path.name + '.tmp')
    tmp_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp_path, out_path)

    print("\nDone.", file=sys.stderr)
    print(f"  Scanned:           {counts['total']}", file=sys.stderr)
    print(f"  Dup (existing):    {counts['dup_existing']}", file=sys.stderr)
    print(f"  Dup (within new):  {counts['dup_new']}", file=sys.stderr)
    print(f"  Added:             {counts['added']}", file=sys.stderr)
    print(f"  Total in output:   {len(merged)}", file=sys.stderr)
    print(f"  Written to:        {out_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
