# UMAD (Dancing Mad Ultimate) player debuffs

Reference for the automarker "Load UMAD preset" button (`_UMAD_AUTOMARK_PRESET` in
`app_common.py`). UMAD = Dancing Mad (Ultimate) / DMU - Party slots: `<1>`..`<8>`.

Status IDs are the ACT/log hex form (what you type in the Debuff field). They were
cross-checked across cactbot `ui/raidboss/data/07-dt/ultimate/dancing_mad.ts` (authoritative
for the hex - the npm release `cactbot@0.37.3`, built 2026-06-23, ships both the DMU
triggers and the regenerated 7.51 Status sheet in `resources/effect_id`), FFLogs (zone 76,
encounter 1085), XIVAPI/datamining status CSVs, and Icy Veins / Materia guides. Preset
rules seed with **no marker assigned** (an unassigned rule never fires). Assign each
sign in the Automarkers tab to fit your strat. Many UMAD debuffs hit several players at
once, so a single sign can only mark one of them. Avoid assigning attack1-3 to anything
that fires during the P3 black hole: the chains use those as their roaming signs.

## Seeded by the preset (verified, P3-P4)

Compound entries (`A+B`) fire only when **one player holds both statuses at once**
(arrival order doesn't matter). They sit out while the black-hole chains toggle is on,
since the chain sequencer owns those statuses.

| Hex  | Debuff                     | Phase | Notes |
|------|----------------------------|-------|-------|
| 644+BBC | Accretion (1st in Line) | P3    | Compound: the Accretion carrier who cleanses first |
| 644+BBD | Accretion (2nd in Line) | P3    | Compound: the Accretion carrier who cleanses second |
| 15A7 | Cursed Shriek              | P4    | 2 pairs, one per Grand Cross wave; real: look away / fake: look at, told by the wave's Inferno or Tsunami |
| 15A8 | Forked Lightning           | P4    | 1 Sup+1 DPS; real: spread / fake: stack |
| 15A9 | Compressed Water           | P4    | Stack marker |
| 15AA | Acceleration Bomb          | P4    | Stop everything when it expires |
| 566  | Beyond Death               | P4    | 4 players; real: MUST take lethal to cleanse / fake: avoid lethal |
| 1C6  | Allagan Field              | P4    | 4 players; real: avoid lethal (explode = wipe) / fake: MUST take lethal |

## Verified but NOT seeded

Rows marked "Trimmed" were dropped from the preset on 2026-07-03 as unnecessary in practice.

| Hex  | Debuff            | Phase | Why not seeded |
|------|-------------------|-------|----------------|
| 13DB | Spell's Trouble   | P2    | All 8 get 4 stacks; head icon = spread/stack/cone per soak |
| 154E | Primordial Crust  | P3    | All 8 to 1 HP; cleanse via lethal tether hit. DMU 7.5x id (verified live), NOT TOP's 645 |
| 15A5 | White Wound       | P4    | All 8 get White or Black; real: lethal in White Antilight / fake: lethal in Black |
| 15A6 | Black Wound       | P4    | All 8 get White or Black; real: lethal in Black Antilight / fake: lethal in White |
| 130C | Tele-portent (Up)    | P1 | Trimmed. Arrow set 1 (~7s, resolves first) |
| 130D | Tele-portent (Down)  | P1 | Trimmed. Arrow set 1 |
| 130E | Tele-portent (Right) | P1 | Trimmed. Arrow set 1 |
| 130F | Tele-portent (Left)  | P1 | Trimmed. Arrow set 1 |
| 13D7 | Tele-portent (Up)    | P1 | Trimmed. Arrow set 2 (~10s, resolves second) |
| 13D8 | Tele-portent (Down)  | P1 | Trimmed. Arrow set 2 |
| 13D9 | Tele-portent (Right) | P1 | Trimmed. Arrow set 2 |
| 13DA | Tele-portent (Left)  | P1 | Trimmed. Arrow set 2 |
| 503  | Confused             | P1 | Trimmed. Yellow/left statue tether or mismatched KB - isolate |
| 131E | Sleep                | P1 | Trimmed. Purple/right statue tether or matched KB |
| 1060 | Epic Hero            | P3 | Trimmed. Damage Chaos only (4 nearest Chaos) |
| 1062 | Fated Hero           | P3 | Trimmed. Damage Exdeath only (4 nearest Exdeath) |
| 13D6 | Double-trouble Trap  | P1 | Trimmed. Knockback carrier (1 DPS+1 Sup), jumps to a fresh player x3 |
| 642  | Headwind             | P3 | Trimmed. Cleanse knockback facing AWAY |
| 643  | Tailwind             | P3 | Trimmed. Cleanse knockback facing TOWARD |
| 1312 | Unbecoming           | P3 | Trimmed. Black-hole tether DoT, stacks |
| 640  | Entropy              | P3 | Trimmed. Point-blank AoE on expiry - spread |
| 641  | Dynamic Fluid        | P3 | Trimmed. Donut AoE on expiry |
| 15AB | Entropy              | P4 | Trimmed. P4 copy of the P3 spread (new 7.51 id) |
| 15AC | Dynamic Fluid        | P4 | Trimmed. P4 copy of the P3 donut (new 7.51 id) |
| 644  | Accretion            | P3 | As a PLAIN rule: a single-status match can't order the pair (1st vs 2nd in Line). Superseded by the compound `644+BBC` / `644+BBD` entries above |

### How the P4 block was verified (2026-07-03)

The P4 hexes rest on three corroborating sources. Only a raw DMU fight log would be
stronger:

- **7.51 game data**: cactbot's generated Status sheet (`resources/effect_id` in
  `cactbot@0.37.3`, npm, built 2026-06-23 from post-7.51 data) contains the contiguous
  new block `WhiteWound 15A5`, `BlackWound 15A6`, `CursedShriek 15A7`, `ForkedLightning
  15A8`, `CompressedWater 15A9`, `AccelerationBomb 15AA`, `Entropy 15AB`, `DynamicFluid
  15AC` - eight newly minted statuses whose names are exactly the eight P4 player debuffs
  the guides describe, sitting in one block the way the P1 Tele-portent set (13D7-13DA)
  does. Nothing else in 7.51 uses these names.
- **Guides** (Icy Veins P4, Materia): P4 (Neo Exdeath, the "Kefka Says" real-vs-fake
  phase - cactbot tags the phase off ability `C2DC` "Kefka Says") applies exactly these
  debuffs: everyone gets White or Black Wound, 4 players get Allagan Field, 4 get Beyond
  Death, 1 Sup+1 DPS each get Cursed Shriek and Forked Lightning, plus Compressed Water,
  Acceleration Bomb, Entropy and Dynamic Fluid. Despite the "Kefka Says" name the phase
  resolves by positioning/aim (spread/stack/gaze real-vs-fake), NOT control inversion:
  the Forced-March-style invert statuses `50D`-`510` do not exist in DMU - don't build
  rules on them.
- **Beyond Death `566` / Allagan Field `1C6`**: each name exists exactly ONCE in the whole
  Status sheet (classic ids, no 7.51 copy), so if the debuff with that name lands - and
  the guides say it does - its hex can only be this. Same single-name argument pins
  Accretion `644`, Unbecoming `1312`, Epic Hero `1060`, Fated Hero `1062`, Primordial
  Crust `154E`.
- **P1-P3 remain fight-confirmed**: `cactbot@0.37.3` triggers match on 13D6,
  130C-130F/13D7-13DA, 13DB, 1060/1062, 642/643 directly, and its comments pin `503`
  Confused (from BAB5 Indulgent Will) and `131E` Sleep (from BAB6 Idyllic Will).

## UNCONFIRMED - capture the real hex from the in-game Current Instance log before use

- **P5 Celestriad tower resistance-downs**: Fire / Ice / Lightning Resistance Down,
  2 players each, 20s (three sets of nine towers). The names are generic reused statuses
  with MANY same-name ids in the sheet (e.g. Fire Resistance Down `26D`/`29E` + seven
  "II" variants) and 7.51 minted no new copies, so the hex cannot be pinned by name.
  Cactbot handles the towers via actor ids, not player statuses. Capture the hex from
  the Current Instance log if you want tower rules.
- **Generic / unpinned**: Magic Vulnerability Up, Damage Down, Weakness/Brink of Death,
  Earth Resistance Down, Wind Resistance Down II, Meanest Existence (RSV-masked).

## P3 black-hole cleanse order (First/Second/Third in Line)

**First/Second/Third in Line are real player statuses** in DMU - the classic
single-name ids reused from TOP: First in Line
`BBC`, Second in Line `BBD`, Third in Line `BBE` (each name exists exactly once in the
Status sheet, so the hex is certain). During the P3 black hole every player gets
Primordial Crust `154E` plus one of these order debuffs; one DPS + one healer also get
Accretion `644` (tanks never do), and Crust is cleansed in In-Line order by tether hits.

These are NOT seeded as plain automark rules (a static "mark whoever gains First in
Line" would thrash across the three players who share it). Instead the **UMAD
black-hole chains** toggle in the Automarkers tab runs a dedicated sequencer
(`umad_chains.py`): one roaming sign per cleanse queue - DPS without Accretion
(default attack1), supports without Accretion (attack2), the Accretion pair (attack3).
The sign sits on the queue's earliest in-Line player who still has Crust, hops to the
next when a Crust is cleansed (30 line), and is cleared off the queue's last player.

Behavior notes (all fail-closed - a wrong sign in a lethal-order mechanic is worse
than none):

- While the toggle is on, plain automark rules for the chain statuses (644, 154E,
  BBC/BBD/BBE - including the preset's Accretion -> circle rule) are suspended in
  UMAD so two systems never fight over the same sign. The preset's P3 Entropy/
  Dynamic Fluid rules use attack5/attack6 for the same reason.
- Roles come from 03/AddedCombatant job data, the PartyChanged roster, and a
  getCombatants snapshot (so an app restart mid-instance still resolves them). A
  queue whose membership can't be resolved - unknown job (including any job newer
  than 7.5x), or an Accretion 644 line that never arrived - stays unmarked rather
  than guess.
- A queue is never started while a still-crusted member's in-Line order is unknown
  (a guessed head would walk the sign one player behind all mechanic), and a 644
  that arrives late re-seats the Accretion sign onto the true earliest-in-line.
- Marks that can't be sent yet (party slot unknown) are retried on the next party
  refresh instead of silently dropped, and superseded if the queue advances first.

## P4 Cursed Shriek gaze pairing (look away vs look at)

Fight confirmed 2026-08-25 against raw IINACT logs (2026-08-20, 08-23, 08-25).
During the Kefka Says phase each of the first two Grand Cross waves deals one
pair of Cursed Shriek `15A7`, 15s apart. Both gains of a pair share one
timestamp, set 1 reads a 60.00s timer and set 2 reads 69.00s. Both carriers of
a pair are the same kind, two real look-away gazes or two fake look-at ones,
and which kind rides which wave swaps per pull. The status duration therefore
carries no real or fake meaning, it only numbers the sets.

The log tell is each wave's followup cast. About 4s before the gains land,
the wave resolves its fire or water element as Inferno (`BB1E`/`BB20`) or
Tsunami (`BB1F`/`BB21`), and on the labeled pull (2026-08-25 23:05, set 1 fake
plus set 2 real, read off the icon letters) Inferno rode the fake set and
Tsunami the real one. The **UMAD Cursed Shriek gaze pairs** toggle runs a
dedicated engine (`umad_chains.py`, `CursedShriekPairs`) that arms the wave's
kind from that cast and marks the pair the moment its two gains land. The
fake pair gets the look-at signs (default bind1/bind2, the chain icon), the
real pair the look-away signs (default ignore1/ignore2). Each pair is
numbered 1/2 by party slot, falling back to actor id, so a strat can pin who
stands where.

Behaviour notes, fail-closed throughout since a mis-aimed gaze in an Ultimate
is worse than an unmarked one:

- A wave whose followup cast never reached the log marks nothing. The gains
  wait for nothing, an unarmed set is discarded.
- An incomplete pair (a partner 26 that never came, a carrier who died before
  application) marks nothing and is discarded after the 5s burst gap.
- A third carrier inside one burst discards the set.
- While the toggle is on, the plain 15A7 preset rule is suspended in UMAD so
  the two systems never fight over the same sign.
- A player losing the gaze (30 line) clears their sign. A wipe or toggle-off
  clears every sign still up.
- Marks that can't be sent yet (party slot unknown) are retried on the next
  party refresh instead of being dropped.
- The Inferno-fake / Tsunami-real mapping rests on one labeled pull so far.
  The constants `FAKE_FOLLOWUP_IDS` / `REAL_FOLLOWUP_IDS` in `umad_chains.py`
  swap in one line if a future labeled pull reads the other way. When unsure,
  leave the toggle off, it never guesses.

## Hex-copy note

Entropy / Dynamic Fluid appear twice: **P3 = 640/641**, **P4 = 15AB/15AC** (identical
names, distinguished by phase). The preset seeds both pairs - P3 as attack5/attack6
(attack1-3 are reserved for the black-hole chain queues in that phase), P4 as
attack1/attack2. Because rules match on the hex id, the P3 rules will not fire in P4
or vice versa. If you loaded the preset before the P3 pair moved to attack5/attack6,
the loader keeps your existing rules - edit or re-add the 640/641 rules if you also
run the chains.
