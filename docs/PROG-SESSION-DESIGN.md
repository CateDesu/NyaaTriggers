# Prog session overview

Implementation notes for the first version.

## First version

The **Prog** tab records named duty sessions and saves them locally. The player starts collection, pulls are added automatically, and **End session** stops it. **View death recaps** opens the selected pull's saved observations in Death Recap, separately from recent live deaths.

The page contains:

- A picker for current and saved sessions by date and fight.
- Start and end controls and an editable session name.
- Pull count, longest pull, total combat time, and session elapsed time.
- Pull number, start, duration, ending, deaths, bookmark, and note.
- A duration chart whose bars select the corresponding pulls.

Label the duration statistic **Longest pull**. Duration alone does not establish progress; bookmarks let players identify meaningful attempts.

## Session boundaries

- **Start session** collects the next full pull. Starting during combat skips the partial attempt.
- Each session belongs to one numeric territory ID, with the readable zone name for display.
- Wipes and breaks stay in the session without an inactivity timeout.
- Leaving the duty ends collection. Re-entry requires another explicit start.
- Ending during a pull preserves the observed portion as interrupted without resetting the meter or triggers.
- Feed loss interrupts the pull and pauses collection until a fresh pull after reconnecting.
- Closing ends the session. After a crash, retain saved pulls and notes and mark the unfinished session interrupted.

Show interrupted attempts and their count separately. Exclude them from completed-pull and longest-pull statistics.

## Pull data and timing

`DpsMeter.finalize` emits the full encounter through `on_encounter_end`, including start, duration, zone, and deaths. Use that record independently of live display resets, the DPS **Record encounters** switch, and callout settings.

Keep the existing DPS callback and add isolated pull-start and pull-finish observers. Snapshots carry a stable pull ID and an ending reason such as combat ended, wipe, duty left, feed lost, or program closed. The connection supplies numeric territory ID and zone name. Completeness follows the observed ending, with a fresh pull required after midcombat starts or reconnects.

**Combat ended** does not establish a clear. A wipe received within two seconds after combat ends updates the same pull instead of adding another.

Finalized duration ends at the last observed combat action and includes downtime within the pull. Use it for pull lengths and total combat time. Session elapsed time uses a monotonic clock; dates and start labels use wall-clock timestamps.

## Local storage

Store one versioned JSON file per session in `prog_sessions/` under writable program data. Use UUIDs for session and pull IDs without character names in filenames. Store aggregate pull data, bookmarks, notes, and recap counts.

Each session stores its name, duty ID and name, start and end timestamps, elapsed
seconds, state, and pulls. Each pull stores its ID, number, start timestamp,
duration, ending, completeness, death count, bookmark, and note. Calculate the
summary from the pulls so edits or interrupted attempts cannot leave stale totals.

Save at session and pull boundaries. Debounce notes and flush on shutdown. A pull-start record preserves the attempt if its final snapshot never arrives. Use ordered writes through a temporary sibling and atomic replace so older saves cannot overwrite newer notes.

Session files are independent of DPS log rotation. Report load and save failures in Prog and preserve unreadable files.

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

- `nyaatriggers/prog_session.py`: session and pull records, boundary handling, and summary calculations.
- `nyaatriggers/record_store.py`: bounded JSON reads and atomic writes for sessions and profiles.
- `nyaatriggers/ui/prog_tab.py`: session controls, pull table, notes, and duration chart.
- `nyaatriggers/dps_meter.py`: additive encounter lifecycle metadata while preserving the live meter's behavior.
- `nyaatriggers/ui/session_tracking.py`: route lifecycle events to the session tracker and handle feed and duty changes.

Keep session state outside `nyaatriggers/ui/dps_tab.py`. Both pages consume encounter events independently; session controls never reset combat tracking.

## Later additions

[Phase tracking and comparisons](PROG-PHASE-DESIGN.md) specifies verified milestones, confirmed reach rates, logical pull boundaries, and comparisons within a duty. Phase details, storage, and boundary handling are implemented. UMAD detection awaits verified recordings.

Phase rules require combat evidence. Elapsed time alone is insufficient, and tracking must not load cactbot timelines or alter the Cactbot switch.

## Validation

Exercise repeated wipes, duplicate end signals, idle display resets during a
long pull, empty encounters, and zone transitions. Verify mid-pull start and end,
feed loss followed by reconnect during the same pull, and a later fresh pull.
Confirm notes and bookmarks survive restarts, interrupted saves preserve prior
data, and changing session controls leaves the live meter and callouts running.
