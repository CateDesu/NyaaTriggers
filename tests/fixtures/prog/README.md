# UMAD phase replay

`umad-2026-09-13.jsonl` contains selected events from an IINACT recording between
21:37:31 and 22:17:33 on September 13, 2026. Names and actor IDs are replaced.
Times are seconds from the start of that excerpt.

The five pulls reach P1, P4, P1, P4 and P1. The fixture retains combat flags,
phase casts, repeated middle jumps, P4 Ultima Upsurge, wipe signals, and the
first damaging boss action in each pull. Other damage is omitted, so this
fixture checks phase observations and boundaries, not full encounter totals.

Wipe signals arrive about three seconds after combat ends. Both P4 pulls have
no intervening combat drops. Ultima Upsurge must not confirm P5.

P5 tests use synthetic Ultima Repeater events based on the
[upstream UMAD triggers](https://github.com/OverlayPlugin/cactbot/blob/main/ui/raidboss/data/07-dt/ultimate/dancing_mad.ts)
and [timeline](https://github.com/OverlayPlugin/cactbot/blob/main/ui/raidboss/data/07-dt/ultimate/dancing_mad.txt).
The local recordings do not yet contain P5 or a successful duty ending.
