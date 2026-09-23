# Prog session overview

Controls and recording behavior are covered by the [Prog guide](GUIDE.md#prog-tab).
The model lives in `nyaatriggers/prog_session.py`, with feed integration in
`nyaatriggers/ui/session_tracking.py`.

## Boundaries and timing

- Sessions consume full encounters independently of display resets, DPS recording,
  and callout settings. Session controls must not reset the meter or triggers.
- Midcombat starts and reconnects wait for a fresh pull. Duty changes end collection.
  Wipes and breaks have no inactivity timeout.
- **Combat ended** does not establish a clear. Duration does not establish progress.
  Interrupted attempts are excluded from completed-pull and longest-pull statistics.
- A wipe within five seconds of combat end updates the same pull. Late deaths have
  a separate two-second window. Duplicate endings cannot extend either window.
  New pulls and interruptions clear late attribution. Empty encounters with recaps remain reviewable.
- Pull duration ends at the last observed combat action, including internal downtime.
  Elapsed times use monotonic clocks. Dates and start labels use wall-clock time.

## Persistence

Session files and recaps survive DPS log rotation. Save pull starts so a crash cannot
erase an unfinished attempt, and order writes so retries cannot overwrite newer notes.
Unreadable files are preserved and reported in Prog.

Recaps use `prog_sessions/recaps/<session ID>/<pull ID>/<recap ID>.json`. Stable pull
IDs keep them discoverable if the later summary write fails. Failed recap writes
remain queued across session changes, with the latest 256 retained during persistent
failure. This cap does not limit saved recaps. Stored statuses have no live expiry.

[Phase tracking and comparisons](PROG-PHASE-DESIGN.md) covers UMAD detection and
planned comparisons. Elapsed time and cactbot timelines cannot establish phase progress.
