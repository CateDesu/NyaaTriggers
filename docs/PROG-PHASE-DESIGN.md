# Prog phase tracking and session comparisons

Design proposal for the next two additions to the Prog tab. This describes
planned behavior. The existing session and saved recap behavior is documented
in [Prog session overview](PROG-SESSION-DESIGN.md).

## Implementation status

The first implementation adds the Furthest phase column, confirmation details,
optional saved phase data, and a logical attempt coordinator. Synthetic rules
exercise all of these through the real feed dispatch and Prog page. The live
definition registry is empty. UMAD candidates remain inactive and the page
explains that verified combat recordings are still needed.

The coordinator supports transition evidence received before a combat drop,
followed by a new meter encounter and the expected phase confirmation. A
transition can also complete without a combat drop. Other event orders remain
uncertain. Do not enable a real definition whose captures require a different
order until that order is implemented and covered by replay tests.

Supported collection waits for a reset or an explicit initial-pull rule after
a new encounter starts. A later phase after an intermission cannot establish
a fresh full attempt. Deaths before collection is established remain in Recent
deaths and are not retrospectively assigned to a prog pull.

Session phase summaries and comparisons are still planned. The remaining
sections specify the intended complete feature, including the evidence needed
before UMAD activation.

## Scope and delivery order

1. Verify UMAD phase evidence and attempt boundaries against captured feeds.
2. Add phase observations, persistence, and pull details for UMAD.
3. Add comparisons between sessions in the same duty.

The first phase release covers P1 through P5. It requires verified rules for
all five phases and their intervening boundaries before enabling the UMAD
definition. The program can implement and test the model while evidence is
being collected. Unverified candidates must not become live detection rules.

Mechanic milestones, automatic clear detection, manual phase corrections,
exports, and comparisons across more than two sessions remain later work.

## Prog tab layout

Keep the session picker, capture controls, name, and existing summary at the
top. Add a compact **Phase progress** summary below it. For example:
**P3 confirmed on 6 of 20 eligible pulls · 3 interrupted pulls excluded**.
Use the furthest phase among eligible pulls for this summary. If none has
phase evidence, show No confirmations with the eligible pull count.

Add **Furthest phase** between Duration and Ending in the pull table. Keep
the duration chart above the table. Its bar heights continue to mean duration.
Add the furthest phase to its tooltip, with an interruption label when needed.
Selecting a bar continues to select the corresponding pull.

Below the table, add a compact **Phase confirmations** table for the selected
pull, followed by the existing bookmark, recap button, and notes. Example
values below illustrate the display only:

| Phase | Confirmed at | Observation |
| --- | --- | --- |
| P1 | 00:11 | Confirmed |
| P2 | — | Established by P3, confirmation time unavailable |
| P3 | 09:06 | Confirmed |
| P4 | — | No confirmation |
| P5 | — | No confirmation |

The label below this table explains that times are measured from pull start
to the first observed confirming event. They are not exact transition times.
Do not label missing confirmation as a wipe in that phase. Keep the ending
reason separate, including **Combat ended**, which does not establish a clear.

Use these distinct display states:

| Recording condition | Furthest phase and details |
| --- | --- |
| Supported pull with evidence | Highest confirmed phase, such as P3 |
| Supported pull without evidence | No confirmation |
| Active pull | Live observations with Recording status |
| Interrupted or uncertain pull | Retain its phase and times with an explanation |
| Older pull without phase data | Not recorded |
| Duty without rules | Not supported |
| Unreadable or unknown phase format | Phase data unavailable |

Tracking starts with session capture and works while another tab is selected.
Changing the viewed session never changes the session being recorded. Refresh
phase cells and summaries without moving the selected row, taking keyboard
focus, or overwriting notes being edited. Flush notes before navigation as the
existing page does. Use text labels as well as any color, and translate all
new user-facing strings through the existing catalog.

## Phase definitions and evidence

Use a small built-in UMAD definition with a stable definition ID, revision,
numeric territory ID, ordered phase IDs, and explicit evidence rules. The
current local timelines identify UMAD as territory 1363. Each rule specifies:

- The event type and numeric ability, status, or actor base ID it requires.
- Any prerequisite evidence needed to distinguish a reused event.
- The phase it confirms and a stable rule ID for the saved observation.
- A captured example, a negative example, and the source of verification.

Runtime actor instance IDs can associate events with known actors, but must
not be hardcoded across attempts. Resolve actor base IDs from the feed when
needed. Localized actor names, timeline positions, and trigger names must not
decide progress. Actor presence alone needs evidence that it establishes the
phase, since an actor can exist before becoming active.

The following are investigation candidates from [the local UMAD
timeline](../timelines/UMAD.txt), not approved detection rules:

| Phase | Candidate evidence | Verification needed |
| --- | --- | --- |
| P1 | StartsUsing C403 | Establish phase context and repetition behavior |
| P2 | Ability C24C | Check earliest reliable confirmation and alternate routes |
| P3 | Abilities C2E2 or C2E3 | Check both actor orders and when the phase is established |
| P4 | StartsUsing C2DC | Check phase entry and all supported preceding outcomes |
| P5 | Ability C24A or BB40 | Check earliest reliable confirmation and repeated use |

The timeline reuses C554 and C555 in several phases and contains unresolved
route and network-log notes. Its timing and labels alone cannot establish a
safe detector. In particular, verify the apparent duty-ending routes around
P2 before treating an ending message as an attempt boundary.

Each phase can have several verified confirming events. Deduplicate by phase
within the logical pull and retain the first accepted confirmation. A missing
opener can then be confirmed by a later event in that same phase. The reported
time remains the time of that later observation.

Furthest phase only advances. A unique later-phase event may advance it even
if an earlier event was absent. Earlier phase reach can be established from
that later observation only after verification that the route necessarily
passes through those phases. Store only direct observations and derive that
reach relationship from the pinned definition. Never invent earlier times.
Ignore evidence for an earlier phase after a later phase has been confirmed,
so delayed or reused events cannot supply a misleading earlier-phase time.
If route verification does not support a linear P1 to P5 order, revise this
definition and the reach calculation before enabling it.

## One logical pull across phase transitions

The meter currently finalizes an encounter when either combat flag falls.
Its idle display reset already preserves the full encounter. UMAD evidence
must establish whether phase transitions produce additional combat edges.

Prog owns the logical attempt boundary for supported phase tracking. Its
first meter encounter ID remains the pull ID, so recaps, notes, and bookmarks
have one stable destination. If a verified transition spans meter encounters,
associate the later encounter IDs with that same logical pull. Route their
updates through this association. Do not rewrite the live meter's lifecycle.

| Signal | Prog behavior |
| --- | --- |
| Fresh full-pull start | Create one pull and reset its phase observations |
| Verified transition evidence | Remember the specific continuation that is expected |
| Combat drop covered by that evidence | Keep the pull active and show Phase transition |
| Verified continuation | Attach the new meter encounter to the existing pull |
| Ordinary ending outside a transition | Finish once with the observed ending reason |
| Wipe or verified reset | Finish once and clear transition state |
| Unexpected continuation or conflicting evidence | Preserve the partial attempt as uncertain and wait for a verified fresh start |
| Feed loss, duty change, session end, or shutdown | Preserve observations as interrupted and stop attribution |

A combat rise or a matching phase number alone is insufficient to join
encounters. Each join requires the verified transition sequence and no
intervening reset. Never merge already finished pulls retroactively. If the
feed orders a transition marker after a combat drop, its verified rule must
define a bounded pending decision before finalization. A timeout can mark
the boundary uncertain, but cannot establish a phase, clear, or successful
continuation. The evidence review must supply any such bound and its basis.

On uncertain boundaries, preserve later observations as interrupted fragments
when useful, but exclude them from rates until a fresh full attempt begins.
Midcombat session starts and reconnects still wait for a fresh attempt. For
UMAD, an intermission combat drop must not satisfy that wait. An explicit
reset or verified initial-pull sequence must establish the fresh attempt.

Death recaps follow the logical pull through a verified transition. Reset the
live observation buffer on a logical pull start, not on each meter segment.
Retain the existing two-second late-death allowance after a final ending.
It does not extend on duplicate endings or permit phase events to modify a
finished attempt. New attempts and interruptions clear late attribution.
Keep phase-only attempts visible even if the meter would discard them as
empty. Without a verified ending they remain interrupted or uncertain.

## Event order and clocks

Use one ordered passive feed path for combat flags, log events, lifecycle
notifications, duty changes, and connection changes. Capture a monotonic
receipt time once per incoming event. Pass that time to the detector and
session coordinator so callback order cannot move a marker into another pull.

The current dispatcher invokes meter callbacks before the passive log tap.
The implementation must collect the incoming event's meter notifications,
then resolve the logical boundary and phase observation together before
publishing a Prog update. Opening evidence on that event belongs to the new
pull. Reset evidence closes and clears the old attempt before later events.

Measure confirmation times from the same monotonic origin used for the
logical attempt. Expose additive timing metadata from the meter if needed.
Do not reconstruct elapsed time by subtracting wall-clock timestamps, and
do not mix log timestamps with receipt times. Replay tests inject the clock.
Wall-clock times remain for session dates and pull start labels.

For UMAD, finalized duration reaches the last accepted combat activity or
phase confirmation relative to that origin, including intervening downtime.
It never ends before a saved confirmation. A pending intermission alone does
not extend finalized duration through an unobserved idle tail. Other duties
retain the current meter duration convention. State this observation-based
convention in the UI help and keep the duration basis with the rule revision.

Accumulate each mapped meter segment's latest death total once. Finalizing a
segment twice must replace its contribution, not add it twice. Preserve the
existing recap-count floor for the logical pull's death total.

## Saved data and compatibility

Keep the existing version 1 session envelope. Add an optional, separately
versioned `phase_tracking` object to new supported pulls with these fields:

The initial implementation writes null for new pulls without active rules.
An absent field identifies older pulls. This distinguishes unrecorded history
from currently unavailable tracking without changing the session envelope.

| Field | Meaning |
| --- | --- |
| version | Phase data format version, initially 1 |
| definition_id | Stable UMAD phase definition ID |
| definition_revision | Exact rules and duration convention used |
| coverage | Recording, complete, interrupted, or uncertain |
| reason | Stable reason code for interrupted or uncertain tracking |
| transition | Expected transition ID while awaiting continuation, otherwise null |
| observations | Direct phase ID, elapsed seconds, and rule ID entries |

Pin the definition for a recording session. Never reinterpret saved
observations under changed rules. Compute furthest phase and statistics from
the observations rather than storing totals that can become stale. The
session's numeric duty ID supplies fight identity.

Save the new observation, current duration, and coverage atomically in the
same session write. Persist logical transition state as well, so a crash
cannot make an open transition appear finished. Reuse the existing ordered
save and retry path. A write failure leaves the observation visible with the
existing save warning, and subsequent retries preserve newer notes and data.
After a restart, an open attempt is interrupted and never resumed.

Validate finite nonnegative elapsed times, unique phase entries, known field
types, and bounded observation counts and strings. Boolean values are not
numeric times. A bad optional phase block must not hide otherwise readable
notes or recaps: report it, omit it from calculations, and preserve the
original block when saving unrelated edits. Unknown phase versions receive
the same treatment. Retain readable definitions for historical revisions.
Older pulls without the block remain Not recorded and are never backfilled
from duration or death recaps.

## Statistics and comparisons

Use **Confirmed reach** for phase percentages. For a phase, the numerator is
the number of eligible pulls with direct evidence or a verified later-phase
observation that establishes its reach. The denominator is all eligible pulls
under that same complete definition, including quick wipes with no phase
confirmation. Such wipes count as zero confirmations, not proof that the
party never entered the phase.

A pull is eligible only when its full start and ending were observed, phase
tracking was available throughout, and its logical boundaries are resolved.
Exclude active, interrupted, uncertain, unreadable, and unrecorded pulls.
Continuous feed coverage cannot prove every network event arrived: missing
evidence stays unknown, and percentages describe confirmed observations.
Show excluded counts by reason. Zero eligible pulls displays No eligible
pulls rather than 0%. Bookmarks and notes do not affect eligibility.

Add **Compare with…** alongside the session picker. It opens a collapsible
comparison area within Prog. Offer other saved, finished sessions with the
same numeric duty ID, including interrupted sessions that contain eligible
finished pulls. On first opening, prefer the most recent earlier compatible
session. Otherwise leave the choice empty. Never silently change an existing
comparison selection when new sessions arrive.
Changing the selected duty clears an incompatible comparison choice. Show
No other sessions for this duty when there is no candidate.

The selected session may still be recording. Its comparison uses finished
pulls only and updates as pulls finish. Identify both sessions by name and
date. Closing the comparison leaves the selected pull and its notes intact.

The comparison table includes finished pull count, eligible phase pull count,
furthest phase among eligible pulls, longest finished pull, and one row per
phase. Phase rows show counts, percentages, and the selected session's change
from the comparison session in percentage points. For example:

| Phase | Selected session | Compared session | Change |
| --- | --- | --- | --- |
| P3 | 6/20 · 30% | 3/15 · 20% | +10 percentage points |

Calculate changes from unrounded rates. Show sample sizes next to each rate
and do not describe a small sample as a trend or attach performance grades.
Use the same eligibility calculation in the session summary and comparison.
An interrupted pull can appear as P4 in its details while the eligible
session summary reaches only P3: the exclusion explanation must make that
difference visible.

Initially, phase comparisons require identical definition IDs and revisions.
Historical sessions with different rules remain selectable for ordinary pull
counts, with phase comparisons labeled Unavailable because tracking differs.
Duration comparisons also require matching duration conventions. Older
sessions retain their ordinary summaries and show Not recorded for phases.
Do not silently compare a subset of revisions in a mixed imported session.

Confirmation times stay in pull details for this release. Comparing their
averages would mix early and fallback confirmation events and could imply
that one group transitioned faster without evidence for that conclusion.

## Implementation boundaries

- `nyaatriggers/prog_phases.py`: built-in definitions and a passive detector with no Qt or
  callout dependencies. It emits phase and transition evidence.
- `nyaatriggers/prog_session.py`: logical pull associations, coverage, saved observations,
  and pure summary and comparison calculations.
- `nyaatriggers/ui/session_tracking.py`: ordered feed integration and lifecycle routing.
- `nyaatriggers/ui/prog_tab.py`: the new column, details, progress summary, and comparison.
- `nyaatriggers/dps_meter.py`: additive timing metadata if required by the shared origin.

Phase tracking must work with callouts disabled and without DPS log recording.
It does not load cactbot timelines. The existing Cactbot switch remains the
single gate for those timelines. Keep detector failures isolated from the
meter and triggers, mark affected phase coverage uncertain, and expose the
tracking problem in Prog.

## Acceptance evidence

Before enabling the UMAD rules, retain sanitized replay fixtures covering
every phase entry, alternate supported route, intermission combat edge, and
fresh reset. Include negative evidence for reused IDs and the apparent P2
ending routes. The current pull recorder also splits at combat drops, so
inspect the consecutive capture files or a continuous feed when verifying
cross-phase boundaries.

Model tests must exercise duplicate and missing markers, a later unique phase
without earlier markers, quick wipes, unexpected event order, joined meter
segments, duplicate endings, phase-only pulls, midcombat starts, reconnects,
wall-clock changes, write retries, and restart during transitions. Verify
recap attribution and death totals across joined segments.

UI tests must cover selection and note preservation during live updates,
recap navigation, older and malformed phase records, incompatible rules,
zero eligible pulls, and the 6/20 versus 3/15 comparison above. Verify that
both views use identical denominators and show exclusion reasons. Check the
layout at the supported minimum window size and with Japanese labels.

Finish with a live UMAD validation against the replay expectations and run
the translation catalog check after adding UI strings. Synthetic fixtures
alone do not complete verification of the fight rules.
