# UMAD (Dancing Mad Ultimate) player debuffs

Reference for **Load UMAD preset**, defined by `_UMAD_AUTOMARK_PRESET` in `nyaatriggers/app_common.py`. IDs use the ACT log's hexadecimal form. Party slots are `<1>` through `<8>`.

The original checks used cactbot `0.37.3`, its generated 7.51 status data, FFLogs zone 76 and encounter 1085, XIVAPI/datamining CSVs, and Icy Veins and Materia guides. Evidence details are below.

Preset rules start **unassigned** and do not mark until you choose a sign. One sign can mark only one player, even when several share a debuff. Reserve the P3 chain signs, attack1 through attack3 by default, for their cleanse queues.

## Seeded by the preset (verified, P3-P4)

Compound entries such as `A+B` require both statuses on one player, in either arrival order. Black-hole chains suspend overlapping rules while enabled.

| Hex  | Debuff                     | Phase | Notes |
|------|----------------------------|-------|-------|
| 644+BBC | Accretion (1st in Line) | P3    | Compound: the Accretion carrier who cleanses first |
| 644+BBD | Accretion (2nd in Line) | P3    | Compound: the Accretion carrier who cleanses second |
| 15A7 | Cursed Shriek              | P4    | 2 pairs, one per Grand Cross wave; real: look away / fake: look at, told by the wave's Inferno or Tsunami |
| 15A8 | Forked Lightning           | P4    | 1 Sup+1 DPS; real: spread / fake: stack |
| 15A9 | Compressed Water           | P4    | Stack marker |
| 15AA | Acceleration Bomb          | P4    | Stop everything when it expires |
| 15A5 | White Wound                | P4    | Real: lethal in White Antilight / fake: lethal in Black |
| 15A6 | Black Wound                | P4    | Real: lethal in Black Antilight / fake: lethal in White |
| 566  | Beyond Death               | P4    | 4 players; real: take lethal to cleanse / fake: avoid lethal |
| 1C6  | Allagan Field              | P4    | 4 players; real: avoid lethal or the party wipes / fake: take lethal |

## Other verified statuses

Rows marked "Trimmed" were dropped from the preset on 2026-07-03 as unnecessary in practice.

| Hex  | Debuff            | Phase | Why not seeded |
|------|-------------------|-------|----------------|
| 13DB | Spell's Trouble   | P2    | All 8 get 4 stacks; head icon = spread/stack/cone per soak |
| 154E | Primordial Crust  | P3    | All 8 to 1 HP; cleanse via lethal tether hit. Verified UMAD ID. TOP uses 645 |
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
| 642  | Headwind             | P3 | Trimmed. Cleanse knockback facing away |
| 643  | Tailwind             | P3 | Trimmed. Cleanse knockback facing toward |
| 1312 | Unbecoming           | P3 | Trimmed. Black-hole tether DoT, stacks |
| 640  | Entropy              | P3 | Trimmed. Point-blank AoE on expiry - spread |
| 641  | Dynamic Fluid        | P3 | Trimmed. Donut AoE on expiry |
| 15AB | Entropy              | P4 | Trimmed. P4 copy of the P3 spread (new 7.51 id) |
| 15AC | Dynamic Fluid        | P4 | Trimmed. P4 copy of the P3 donut (new 7.51 id) |
| 644  | Accretion            | P3 | Single-status rules cannot order the pair. Use the compound entries above |

### How the P4 block was verified (2026-07-03)

The original P4 status identification combined game data and guides. The later gaze check against raw logs is recorded below.

- **7.51 data:** cactbot `0.37.3`, built 2026-06-23, contains the contiguous `15A5` through `15AC` block for White Wound, Black Wound, Cursed Shriek, Forked Lightning, Compressed Water, Acceleration Bomb, Entropy, and Dynamic Fluid. These names match the P4 debuffs described by the guides.
- **Guide coverage:** Icy Veins and Materia describe White or Black Wound on all players, Allagan Field on four, Beyond Death on four, and the other statuses above. Cactbot identifies Kefka Says with `C2DC`. The mechanics use positioning and gaze direction; Forced March inversion IDs `50D` through `510` are not UMAD rules.
- **Reused IDs:** Beyond Death `566` and Allagan Field `1C6` each have one name match in that status sheet. The same identification method gives Accretion `644`, Unbecoming `1312`, Epic Hero `1060`, Fated Hero `1062`, and Primordial Crust `154E`.
- **P1 through P3:** cactbot's fight triggers directly match `13D6`, `130C` through `130F`, `13D7` through `13DA`, `13DB`, `1060`, `1062`, `642`, and `643`. Its comments identify Confused `503` from Indulgent Will `BAB5` and Sleep `131E` from Idyllic Will `BAB6`.

## Unconfirmed status IDs

- **P5 Celestriad resistance-downs:** Fire, Ice, and Lightning Resistance Down each affect two players for 20 seconds during three sets of nine towers. Their names have multiple status IDs, so names alone cannot establish the hex. Cactbot handles towers through actor IDs. Capture the status IDs from Current Instance before adding rules.
- **Other unpinned statuses:** Magic Vulnerability Up, Damage Down, Weakness, Brink of Death, Earth Resistance Down, Wind Resistance Down II, and the RSV-masked Meanest Existence.

## P3 black-hole cleanse order (First/Second/Third in Line)

First, Second, and Third in Line use `BBC`, `BBD`, and `BBE`. During the black hole, every player receives Primordial Crust `154E` and an order status. One DPS and one healer also receive Accretion `644`. Tether hits cleanse Crust in line order.

Plain order rules would move a sign between players sharing the status. **UMAD black-hole chains** instead runs `BlackHoleChains` in `nyaatriggers/umad_chains.py`, with one sign per queue:

| Queue | Default sign |
|---|---|
| DPS without Accretion | attack1 |
| Supports without Accretion | attack2 |
| Accretion pair | attack3 |

Each sign marks the earliest player in its queue who still has Crust. It advances on Crust loss and clears after the last cleanse.

- Enabling chains suspends overlapping `644`, `154E`, and `BBC/BBD/BBE` rules in UMAD.
- Roles come from AddedCombatant job data, PartyChanged, and combatant snapshots. Unknown jobs, missing membership, or incomplete order data leave the affected queue unmarked.
- Late Accretion data moves its sign to the correct queue head.
- Unknown party slots delay marking until a roster refresh. If the queue advances first, the newer mark replaces the pending request.

## P4 Cursed Shriek gaze pairing (look away vs look at)

Checked on 2026-08-25 against IINACT logs from August 20, 23, and 25. The first two Grand Cross waves each apply a Cursed Shriek `15A7` pair, 15 seconds apart. Pair members share a timestamp. The first pair has a 60-second timer and the second 69 seconds. Either wave can be real or fake, so duration identifies the set but not gaze direction.

A follow-up cast about four seconds before the gains identifies the wave. On the labeled 2026-08-25 23:05 pull, Inferno `BB1E/BB20` preceded the fake pair and Tsunami `BB1F/BB21` the real pair. **UMAD Cursed Shriek gaze pairs** uses that mapping in `CursedShriekPairs`:

| Cast | Gaze | Default signs |
|---|---|---|
| Inferno | Fake, look at | bind1 and bind2 |
| Tsunami | Real, look away | ignore1 and ignore2 |

The pair is numbered by party slot, with actor ID as fallback. Marking begins when both gains arrive after the identifying cast.

- Missing cast evidence leaves the set unmarked.
- An incomplete pair expires after the five-second burst gap. A third carrier discards the set.
- The plain `15A7` preset rule is suspended while pairing is enabled.
- Status loss clears that player's sign. A wipe or disabling the toggle clears all remaining signs.
- Unknown party slots delay marks until a roster refresh.
- The Inferno/fake and Tsunami/real mapping rests on one labeled pull. Leave the toggle off if uncertain. `FAKE_FOLLOWUP_IDS` and `REAL_FOLLOWUP_IDS` hold the mapping for future verification.

## IDs shared by name

Entropy and Dynamic Fluid use `640/641` in P3 and `15AB/15AC` in P4. Matching by hex keeps the phases separate. These rules were removed from the preset, but older saved rules remain. If you still use them with P3 chains, keep their signs separate from the chain queues. Earlier presets used attack5/attack6 for the P3 pair and attack1/attack2 for P4.
