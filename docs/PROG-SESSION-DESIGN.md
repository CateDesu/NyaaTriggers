# Prog session overview

Implementation notes for the first version.

## First version

The separate **Prog** tab reviews a raid session. The player starts a
session, the program collects its pulls automatically, and the player ends it
when finished. Store the history locally so it remains available after a restart.

Death Recap has its own tab. **View death recaps** opens the selected prog pull's
saved observations there and keeps that view separate from recent live deaths.

The page contains:

- A session picker with the current session and saved sessions by date and fight.
- **Start session** and **End session** controls, with an editable session name.
- A summary showing pull count, longest pull, total combat time, and session elapsed time.
- A pull table with pull number, start time, duration, ending, recorded deaths, bookmark, and note.
- A simple duration chart by pull number. Clicking a bar selects its table row.

Use **Longest pull**, not **Best pull**. A longer attempt does not necessarily
mean more progress. Bookmarks let the player mark the attempts they care about
without the program guessing which were good.

## Session boundaries

- **Start session** arms collection for the next full pull in the current duty.
  Starting during combat waits for the next pull so a partial capture does not
  become a misleading first attempt.
- Use the numeric territory ID for the fight identity and keep the readable zone
  name for display. A session belongs to one duty.
- Wipes and short breaks stay in the same session. Do not split sessions after
  an arbitrary inactivity timeout.
- Leaving the duty ends the session. Re-entering starts a new session only when
  the player chooses **Start session** again.
- Ending a session during a pull stops collection and keeps the observed portion
  as an interrupted attempt. It does not stop or reset the DPS meter or triggers.
- A lost feed marks the open attempt as interrupted and pauses collection. After
  reconnecting, wait for a fresh pull boundary. Do not count the remainder of the
  same fight as another complete pull.
- Closing the program ends the session. If the program crashes, show the saved
  session as interrupted on next launch and retain its completed pulls and notes.

Keep interrupted attempts visible with their recorded durations, but exclude
them from longest-pull and completed-pull statistics. Show their count separately
so the summary agrees with the table.

## Pull data and timing

The existing meter already maintains a full encounter separately from its live
display. `DpsMeter.finalize` emits the full snapshot through
`on_encounter_end`. That snapshot contains the pull start time, duration, zone,
and total deaths. The display's idle timeout can reset its visible segment
without changing that full encounter.

Use those finalized records for the first version. Session tracking must not
depend on the DPS **Record encounters** checkbox, which controls a different
log. It must also work with callouts disabled.

The meter keeps its existing DPS callback and adds isolated pull-start and
pull-finish observers. Their snapshots provide lifecycle information:

- A stable pull ID and a start notification for each new full encounter.
- The session stores the numeric territory ID and readable zone supplied by the connection.
- An end reason such as combat ended, wipe signal, duty left, feed lost, or program closed.
- The session marks completeness from the observed ending and waits for a fresh
  pull after collection begins during combat or the feed reconnects.

A combat flag dropping is not proof of a clear. The page shows **Combat ended**
when that is all the feed establishes. Clear detection is not part of this
version. A wipe received within two seconds after combat ends updates the same
pull ID instead of inserting another pull.

Keep the meter's current duration convention explicit: finalized duration ends
at the last observed combat action and includes downtime within the pull. Use
that full duration for pull lengths and total combat time. Use a monotonic clock
for session elapsed time and wall-clock timestamps for dates and start times.

## Local storage

Store one versioned JSON file per session in `prog_sessions/` under the writable
program data directory. Use UUIDs for session and pull IDs, with no character
names in filenames. The session summary only needs aggregate pull data, bookmarks,
notes, and an observed recap count, not another copy of the raw combat log.

Each session stores its name, duty ID and name, start and end timestamps, elapsed
seconds, state, and pulls. Each pull stores its ID, number, start timestamp,
duration, ending, completeness, death count, bookmark, and note. Calculate the
summary from the pulls so edits or interrupted attempts cannot leave stale totals.

Save on session start, pull start, pull end, and session end. Debounce note edits
and flush them on shutdown. A pull-start record leaves evidence of an interrupted
attempt if the program crashes before its final snapshot arrives. Write through
a temporary sibling and atomic replace, following the existing data helpers.
Writes run in order at session boundaries and after debounced edits so an older
save cannot overwrite a newer note.

Keep session files separate from DPS log rotation. Do not silently delete a
player's bookmarked sessions when the DPS logs reach their retention limit.
Show load or save failures in the Prog page and preserve unreadable files.

Save each observed death as a separate versioned record under
`prog_sessions/recaps/<session ID>/<pull ID>/<recap ID>.json`. Save on each death,
then update the pull's recap count. Use the existing pull ID as the association
so recap files remain discoverable if the later summary write fails. Read just
the selected pull's recaps when opened and preserve unreadable files. Failed
recap writes stay queued across pull and session changes until a flush succeeds.
Keep the latest 256 pending records during persistent storage failure and report
any dropped records. This bound does not limit recaps already saved on disk.
Saved status observations contain names and sources, with no live clock expiry.

The active collected pull accepts recaps until it ends. Combat end and wipe allow
two seconds for late death messages. Repeated endings do not extend that period.
A new pull or an interruption clears the association. Midcombat session starts
skip the current attempt's recaps as well as its summary. An empty encounter
that already has recaps stays visible as interrupted so its deaths remain
reviewable. Old summaries without a recap count load unchanged.

## Code layout

- `prog_session.py`: session and pull records, boundary handling, and summary calculations.
- `record_store.py`: bounded JSON reads and atomic writes for sessions and profiles.
- `ui/prog_tab.py`: session controls, pull table, notes, and duration chart.
- `dps_meter.py`: additive encounter lifecycle metadata while preserving the live meter's behavior.
- `ui/session_tracking.py`: route lifecycle events to the session tracker and handle feed and duty changes.

Keep the session state out of `ui/dps_tab.py`. Both pages can consume the same
encounter events without making either page own the other. The session tracker
never resets combat tracking when its own controls are used.

## Later additions

The next phase is specified in [Prog phase tracking and session
comparisons](PROG-PHASE-DESIGN.md). That proposal covers UMAD first, including
logical pull boundaries across verified transitions and comparison eligibility.
Phase details and the storage and boundary infrastructure are implemented.
UMAD detection remains inactive while its combat evidence is unverified.

- Fight-specific phase and mechanic milestones based on verified combat events.
- Furthest confirmed milestone and the percentage of attempts reaching it.
- Comparison with previous sessions for the same fight.

Phase progress needs its own event rules. Do not infer phases from elapsed time
alone because downtime and checkpoints change the relationship. This tracking
does not need to load cactbot timelines or change the existing Cactbot switch.

## Validation

Exercise repeated wipes, duplicate end signals, idle display resets during a
long pull, empty encounters, and zone transitions. Verify mid-pull start and end,
feed loss followed by reconnect during the same pull, and a later fresh pull.
Confirm notes and bookmarks survive restarts, interrupted saves preserve prior
data, and changing session controls leaves the live meter and callouts running.
