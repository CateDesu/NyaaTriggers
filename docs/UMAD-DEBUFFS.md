# UMAD (Dancing Mad Ultimate) player debuffs

Reference for the UMAD mechanic tabs and native Triggevent P4 controls. IDs use the ACT log's hexadecimal form. Party slots are `<1>` through `<8>`.

The original checks used cactbot `0.37.3`, its generated 7.51 status data, FFLogs zone 76 and encounter 1085, XIVAPI/datamining CSVs, and Icy Veins and Materia guides. Evidence details are below.

Bomb and Lightning assignments start **unassigned**. One sign can mark only one player. P3 black-hole chains use attack1 and attack2 by default; the separate Accretion controller uses ignore1 and ignore2. Saved debuff rules from older versions are inactive.

## Encounter statuses (verified, P3-P4)

The Accretion controller identifies carriers by Accretion and their line order, in either arrival order. Native Triggevent handles P4 statuses without a dedicated local tab.

| Hex  | Debuff                     | Phase | Notes |
|------|----------------------------|-------|-------|
| 644+BBC | Accretion (1st in Line) | P3    | First carrier, ignore1 until third tether hit |
| 644+BBD | Accretion (2nd in Line) | P3    | Second carrier, ignore2 after first completes |
| 15A7 | Cursed Shriek              | P4    | 2 pairs, one per Grand Cross wave; real: look away / fake: look at, identified by Neo Exdeath's status VFX |
| 15A8 | Forked Lightning           | P4    | 1 Sup+1 DPS; real: spread / fake: stack |
| 15A9 | Compressed Water           | P4    | Stack marker |
| 15AA | Acceleration Bomb          | P4    | Real: stay still / fake: keep moving when it expires |
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
| 154C | Unbecoming           | P3 | First tether hit |
| 154D | Meanest Existence    | P3 | Second tether hit |
| 640  | Entropy              | P3 | Trimmed. Point-blank AoE on expiry - spread |
| 641  | Dynamic Fluid        | P3 | Trimmed. Donut AoE on expiry |
| 15AB | Entropy              | P4 | Trimmed. P4 copy of the P3 spread (new 7.51 id) |
| 15AC | Dynamic Fluid        | P4 | Trimmed. P4 copy of the P3 donut (new 7.51 id) |
| 644  | Accretion            | P3 | Healed off before tether hits. Carrier identity remains until Crust loss |

### How the P4 block was verified (2026-07-03)

The original P4 status identification combined game data and guides. The later gaze check against raw logs is recorded below.

- **7.51 data:** cactbot `0.37.3`, built 2026-06-23, contains the contiguous `15A5` through `15AC` block for White Wound, Black Wound, Cursed Shriek, Forked Lightning, Compressed Water, Acceleration Bomb, Entropy, and Dynamic Fluid. These names match the P4 debuffs described by the guides.
- **Guide coverage:** Icy Veins and Materia describe White or Black Wound on all players, Allagan Field on four, Beyond Death on four, and the other statuses above. Cactbot identifies Kefka Says with `C2DC`. The mechanics use positioning and gaze direction; Forced March inversion IDs `50D` through `510` are not UMAD rules.
- **Reused IDs:** Beyond Death `566` and Allagan Field `1C6` each have one name match in that status sheet. The same identification method gives Accretion `644`, Epic Hero `1060`, Fated Hero `1062`, and Primordial Crust `154E`. The earlier name-only Unbecoming match `1312` is not the P3 tether status; the encounter uses `154C`.
- **P1 through P3:** cactbot's fight triggers directly match `13D6`, `130C` through `130F`, `13D7` through `13DA`, `13DB`, `1060`, `1062`, `642`, and `643`. Its comments identify Confused `503` from Indulgent Will `BAB5` and Sleep `131E` from Idyllic Will `BAB6`.

## Unconfirmed status IDs

- **P5 Celestriad resistance-downs:** Fire, Ice, and Lightning Resistance Down each affect two players for 20 seconds during three sets of nine towers. Their names have multiple status IDs, so names alone cannot establish the hex. Cactbot handles towers through actor IDs. Capture the status IDs from Current Instance before adding rules.
- **Other unpinned statuses:** Magic Vulnerability Up, Damage Down, Weakness, Brink of Death, Earth Resistance Down, Wind Resistance Down II.

## P3 black-hole cleanse order (First/Second/Third in Line)

First, Second, and Third in Line use `BBC`, `BBD`, and `BBE`. During the black hole, every player receives Primordial Crust `154E` and an order status. One DPS and one healer also receive Accretion `644`. Tether hits cleanse Crust in line order.

**Black-hole chains** marks DPS without Accretion with attack1 and supports without Accretion with attack2. Each sign marks the earliest player in its queue who still has Crust. It advances on Crust loss and clears after the last cleanse. Roles come from job data and combatant snapshots. Both Accretion carriers must be known so they are excluded from these queues. Missing jobs, membership or line order leave the affected queue unmarked.

**Accretion** is a separate tab and toggle. First in Line receives ignore1. Healing removes Accretion before the tether sequence, so its loss leaves that mark in place. The first tether hit applies Unbecoming `154C`, the second applies Meanest Existence `154D`, and the third removes Primordial Crust `154E`. That Crust loss clears the first mark and gives Second in Line ignore2. The second carrier's Crust loss clears ignore2. This follows status events rather than a fixed timer. Beyond Death belongs to P4 and does not advance this queue.

The third-hit completion signal and status IDs come from [cactbot's encounter implementation](https://github.com/OverlayPlugin/cactbot/blob/main/ui/raidboss/data/07-dt/ultimate/dancing_mad.ts#L5975). The native [Triggevent DMU trigger](https://github.com/CateDesu/event-trigger/blob/main/triggers/triggers-dt/src/main/java/gg/xp/xivsupport/triggers/ultimate/DMU.java#L1573) also waits for Crust loss. The local Accretion fixture is a synthetic event sequence, not a recorded P3 pull.

Incomplete or conflicting Accretion ranks leave the pair unmarked. Unknown party slots delay marking until a roster refresh. If the queue advances first, the obsolete pending mark is cancelled before the next mark is sent. Disabling either P3 toggle clears its own signs.

## P4 Cursed Shriek gaze pairing (look away vs look at)

The first two Grand Cross waves each apply a Cursed Shriek `15A7` pair, 15 seconds apart. Pair members share a timestamp. The first pair has a 60-second timer and the second 69 seconds. Either wave can be real or fake, including two real waves or two fake waves. Duration identifies the set but not gaze direction.

Neo Exdeath receives status `808` just before Grand Cross. Its hexadecimal extra field identifies the debuffs from that wave. **UMAD Cursed Shriek gaze pairs** reads this value in `CursedShriekPairs`:

| Status 808 extra field | Gaze | Default signs |
|---|---|---|
| 461 | Fake, look at | bind1 and bind2 |
| 462 | Real, look away | ignore1 and ignore2 |

Chaos's Inferno and Tsunami casts do not identify gaze direction. The old element mapping was incorrect. The VFX mapping matches the bundled Triggevent trigger and the 44 recorded pulls in [the replay fixture](../tests/fixtures/umad_gazes.json).

The pair is numbered by party slot, with actor ID as fallback. Marking begins when both gains arrive within 12 seconds of the identifying VFX.

- Missing VFX evidence leaves the set unmarked. A complete pair can wait for a delayed VFX for the five-second burst window.
- An incomplete pair expires after the five-second burst gap.
- If both waves need the same signs, the earlier pair keeps them until its gaze ends. The later pair then receives those signs.
- Status loss or expiry clears that player's sign. A wipe, a new Kefka Says, or disabling the toggle clears all remaining signs and waiting assignments.
- Unknown party slots delay marks until a roster refresh.

## P4 Acceleration Bomb and Forked Lightning

The **Acceleration Bomb** and **Forked Lightning** tabs provide separate real and fake marker assignments. They read the same Neo Exdeath status `808` tell as Cursed Shriek: `461` is fake and `462` is real. A missing tell or incomplete wave leaves players unmarked.

| Debuff | Real | Fake | Carriers per wave |
|---|---|---|---|
| Acceleration Bomb `15AA` | Stay still on expiry | Keep moving on expiry | Four, two short and two long |
| Forked Lightning `15A8` | Spread | Stack | Two |

Bomb wave one has two 51-second and two 76-second timers. Wave two has two 36-second and two 61-second timers. Bomb signs are numbered within the short and long pairs by party slot, with actor ID as fallback. Lightning signs use party order. The real or fake tell applies to the entire wave and does not depend on duration. These mechanics and the tell mapping are recorded in [cactbot's encounter implementation](https://github.com/OverlayPlugin/cactbot/blob/main/ui/raidboss/data/07-dt/ultimate/dancing_mad.ts).

New Bomb and Lightning assignments start unassigned. Choose distinct signs for the players you want marked and enable the mechanic. Unassigned players receive no sign. When both waves use the same signs, the earlier carriers retain them until loss or expiry. Later carriers then receive the available signs.

Local chain queues, Accretion and Grand Cross controllers retain ownership of their signs. An older mechanic's loss or expiry cannot clear a newer local mark on the same actor. Grand Cross pending marks expire with their debuff before any roster retry. Accretion pending marks are cancelled on Crust completion. Wipes clear owned local signs and cancel queued marks. Zone changes and feed loss discard actors and pending actions because party slots may have changed.

Local gaze, Bomb or Lightning controls suppress native Triggevent Kefka marking. P3 black-hole chains and Accretion leave native P4 marking available.

Telesto commands are queued with a thirty-second age limit. Cleanup follows the existing encounter and party boundaries. Diagnostics distinguish queueing, a POST attempt and an accepted HTTP response. Telesto returns no confirmation that the game applied the command, so an accepted response cannot prove marker delivery. Requests with ambiguous timeout outcomes are not retried.

## IDs shared by name

Entropy and Dynamic Fluid use `640/641` in P3 and `15AB/15AC` in P4. Matching by hex keeps the phases separate. Older saved rules remain inactive. Native P4 controls handle the encounter debuffs without the removed assignment list.
