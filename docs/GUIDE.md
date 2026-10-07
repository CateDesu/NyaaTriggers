# NyaaTriggers Guide

The full reference for NyaaTriggers. Install steps are in the [README](../README.md).

- [Requirements](#requirements)
- [Voice](#voice)
- [Triggers tab](#triggers-tab)
- [Engine triggers](#engine-triggers)
- [Triggevent Engine](#triggevent-engine)
- [Triggernometry engine](#triggernometry-engine-wip)
- [Current Instance tab](#current-instance-tab)
- [DPS tab](#dps-tab)
- [Death Recap tab](#death-recap-tab)
- [Prog tab](#prog-tab)
- [Profiles](#profiles)
- [Automarkers tab](#automarkers-tab)
- [Alert sound](#alert-sound)
- [In-game display](#in-game-display)
- [Trigger fields](#trigger-fields)
- [Settings](#settings)
- [Personal triggers](#personal-triggers)
- [Updating](#updating)

---

## Requirements

Windows and Linux release builds bundle the Python dependencies and English Piper voice. IINACT is installed separately, and Linux audio and engines also use the [system dependencies listed in the README](../README.md#linux-system-dependencies). This table covers the base requirements for running from source on Linux.

| Requirement | Notes |
|---|---|
| [IINACT](https://github.com/marzent/IINACT) | Running and connected to the game |
| Python 3.11+ | System Python is fine |
| PyQt6 with Qt WebSockets | `sudo pacman -S python-pyqt6 qt6-websockets` / `sudo apt install python3-pyqt6 python3-pyqt6.qtwebsockets` / `pip install PyQt6`. The program needs Qt WebSockets for the game feed. |
| piper-tts | Installed automatically on first launch into `~/.venv/ffxiv` |
| Audio backend | `aplay` via `alsa-utils` |
| PyQt6-WebEngine | Optional for cactbot in source runs. Use `sudo pacman -S python-pyqt6-webengine` or `pip install PyQt6-WebEngine`. Releases bundle it. |
| packaging | Checks dependency versions during source updates. Installed by `setup.sh`; manually use `sudo pacman -S python-packaging` / `sudo apt install python3-packaging` / `pip install packaging`. |

---

## Voice

Choose an engine in **Settings - Voice**:

- **System** uses Windows SAPI or Linux's `spd-say` / `espeak`. It is the Windows default. On Linux, English falls back to Piper if no system backend is available.
- **Piper** provides [offline neural speech](https://github.com/OHF-Voice/piper1-gpl) and is the Linux default. Releases bundle `en_US-arctic-medium`; source runs download it to `voices/` on first launch.

To add a Piper voice, choose one from the [samples](https://rhasspy.github.io/piper-samples/) and put its `.onnx` model and matching `.onnx.json` config in **Open voices folder**. Use **Refresh list** or restart, then select it under **Model**. Medium voices are about 65 MB; low voices are about 30 MB.

The Japanese voices **Alpha** and **Kumo** also appear under **Model**. First selection downloads about 330 MB. They read kanji, with espeak as the fallback while setup finishes.

**Test TTS** previews the selected voice. On Linux source installs, **Piper venv** points to the piper-tts environment, normally `~/.venv/ffxiv`.

The sidebar volume slider controls audio from 0% to 200%. Click the speaker to mute or unmute. Its right-click menu offers **Mute for 5 minutes**, **Mute for 15 minutes**, **Mute until next zone**, and **Unmute**. The combat feed, recordings, and visual callouts continue while muted.

---

## Triggers tab

Edit bundled and custom triggers here. The live log is in **Current Instance**.

**Callout modes.** With **Cactbot** off, Local, Triggevent, and Triggernometry callouts follow their checkboxes. **Settings - Cactbot** switches those off and enables cactbot callouts and timelines. Dungeon timelines are bundled; others download and cache on demand. Turning Cactbot off restores editable callouts and local timelines.

Engine callouts start enabled, including newly discovered ones. Local rows keep their saved choices and zone restrictions.

**Sidebar tree.** Fights are grouped by content type, expansion, and fight. Special folders are:

- **General** for callouts that work in any zone, such as personal mitigations.
- **TBD** for fights without a curated tree entry.
- **Unsorted** for custom triggers without a zone restriction. A Zone Regex files a custom trigger under its fight, or TBD if the fight has no entry.

Select a fight or folder to filter the table. Headers expand or collapse groups. The table's **General**, **DoT**, **Local**, **Triggevent**, and **Triggernometry** groups start collapsed. DoT contains reapply reminders. Search shows matching rows across all fights in one list.

**Toggles.** **Global - Local On/Off** and **Global - Triggevent On/Off** affect every fight. Global Local also controls local timelines. Selecting one fight shows **Local** and **Triggevent** checkboxes for its rows. A box is checked when all its rows are enabled. Enabling a group expands it; disabling it collapses it. Overlapping sources can produce duplicate calls.

**Zone column.** A green dot means the trigger matches your current zone, red means it is excluded, and no dot means the zone is unknown or disconnected.

**Row menu.** Right-click for Edit, Duplicate, Test Fire, Enable / Disable, Delete, Move to Folder, and Reset to Default. Moving changes the fight tag. Reset restores a modified bundled trigger's values.

**Callout delay.** The editor can wait before speaking, starting from the matching event or the final follow-up step. Wipes, zone changes, disconnects, and disabling the trigger or local callouts cancel pending delays. Reapply warnings use their own expiry timer instead.

**Add.** Choose **Local trigger** for the existing editor, or **Triggevent callout** for the [sequence builder](#custom-triggevent-callouts).

**Tree menu.** Right-click to create, rename, or delete Unsorted folders and subfolders. **New folder for a fight...** opens a searchable Savage, Ultimate, and Extreme picker. Deletion confirms how many triggers it will remove, including subfolders.

The toolbar has row actions, global toggles, and **Reset to Default**. This reset only clears all trigger checkmarks; definitions and edited values are preserved.

---

## Engine triggers

Engine rows appear under their fight, tinted and tagged by source. Changes apply immediately and persist across restarts.

- Uncheck a row to mute it, or check it to restore the callout.
- Double-click Triggevent or Triggernometry rows, or choose **Edit spoken text**, to change their wording. Triggevent tokens such as `{event.target}` and Triggernometry tokens such as `${_me.x}` still resolve. **Reset to default** restores the original text.
- **Test TTS** in the row menu or the editor's ▶ button previews speech with sample token values.
- Cactbot rows support muting only. Its switch, overrides, and page **URL** are in **Settings - Cactbot**. The default URL uses hosted raidboss; a local build can replace it. Source installs need PyQt6-WebEngine from [Requirements](#requirements).

Simple cast-based engine triggers are bundled as editable Local rows. Complex imperative and Groovy triggers remain in the engine.

Callouts created with the Triggevent builder open their sequence editor when double-clicked. Their row menu also offers Duplicate, Test speech, and Delete.

---

## Triggevent Engine

Triggevent runs built-in callouts, EasyTriggers, and Groovy scripts from `~/.triggevent` against the IINACT feed. With Cactbot off, it follows the row, fight, and global Triggevent controls.

Release builds bundle the engine and Java 17. For source runs, install Java 17 and use **Settings - Program - Update Triggevent Engine** to download the prebuilt jar. Developers rebuilding the engine need JDK 17 and Maven:

| OS | JDK 17 and Maven | Build |
|---|---|---|
| Arch / CachyOS | `sudo pacman -S jdk17-openjdk maven` | `cd triggevent-core && ./build.sh` |
| Bazzite / Fedora | `brew install openjdk@17 maven` | `cd triggevent-core && ./build.sh` |
| Windows | `winget install EclipseAdoptium.Temurin.17.JDK Apache.Maven` | `cd triggevent-core && build.bat` |

NyaaTriggers detects `triggevent-core/target/triggevent-core.jar` and uses bundled or system Java. See the [engine README](../triggevent-core/README.md) for build details.

Some engine components construct Swing windows. On Linux, Xvfb supplies a hidden display; otherwise the engine uses your session's X display. Install the [Linux dependencies](../README.md#linux-system-dependencies) for headless or pure Wayland sessions. The companion overlay handles the in-game display.

---

## Custom Triggevent callouts

Choose **Triggers - Add - Triggevent callout** to build a sequence without writing a script. Enter a callout name, then type a fight name or abbreviation in **Fight**. Selecting a search result fills the fight folder and its Zone ID. Fights with several zones offer a separate result for each zone. Search uses the bundled fight and zone data and works offline.

New callouts start without a fight. Leave the field blank, or clear it, to save under **Unsorted**. Unselected search text does not assign a fight. The Zone ID restricts where the callout runs, with `0` allowing any zone; you can still enter it manually.

**Start when** matches a cast start, resolved ability, status gain or loss, absolute headmarker ID, or tether ID. IDs are hexadecimal and accept alternatives separated by `|`. Choose Anyone or Me for the event's target. Headmarker IDs are the raw IDs from the log and can vary between pulls in fights that use offsets.

Add and reorder these steps:

- **Event wait:** wait for another matching event. Its target can also be the starting event's target or source.
- **Delay:** wait a number of seconds.
- **Callout:** speak unconditionally, or choose between two texts using the starting event's ID, the latest matched event's ID, or statuses currently on you. A blank conditional branch stays silent. Status alternatives mean any listed status for the positive condition and none for the negative condition.

For example, start on either of two debuffs on Me, wait for a boss cast, then choose “Go left” or “Go right” according to the starting debuff's ID. The starting event stays available even after that debuff disappears. Current-status conditions instead check the buffs present when the callout step runs.

Speech accepts `{player}`, `{source}`, `{target}`, `{id}`, `{start.source}`, `{start.target}`, and `{start.id}`. The unprefixed event fields refer to the latest matched event. **Test speech** previews one text with sample values and does not test matching.

The total sequence timeout includes all waits and delays. Each definition runs one sequence at a time and ignores new starting events while it is active. The builder supports up to 32 steps and a timeout up to 600 seconds. It provides conditional speech, not arbitrary calculations or nested script logic; existing Groovy scripts remain available for those cases.

Save applies the definition to the running Triggevent engine. Edits, deletion, disabling the row, wipes, pull changes and zone changes cancel its pending sequence. Repeated announcements of the same zone preserve it. Connection recovery can reconstruct a sequence from the recorded pull without repeating historical speech. The existing Triggevent and Cactbot controls apply.

Definitions are saved separately in `triggevent.custom.json` beside the program's other writable data. Include this file in backups; Local trigger import and export do not include it. Profiles save these rows' enabled choices, while their sequences and speech stay in the definition file. An unreadable or unsupported file blocks edits rather than replacing it. Older engine jars need **Settings - Program - Update Triggevent Engine** and a restart before builder callouts can run.

---

## Triggernometry engine (WIP)

Triggernometry runs conditions, shared variables, delayed actions, trigger chains, and C# `ExecuteScript` actions from imported XML packs. It starts when a pack is available and Cactbot is off. Live fight validation is still pending.

**Import a pack** through **Settings - Data - Import Triggernometry**. Its triggers appear as editable Triggernometry rows. Without the engine, the importer converts only simple ability matches with plain speech to Local rows.

**Create or edit a pack.** Choose **Triggers - Add - Triggernometry trigger** to create a native XML pack. **Add - Edit Triggernometry pack** opens an existing pack, including triggers without speech actions. A Triggernometry row's context menu also offers **Edit trigger and actions**. Double-clicking a row still edits only its spoken text.

The editor has pages for the event source and regular expression, nested conditions, and actions. Actions include speech, scalar variables, delays, running or cancelling another trigger, and C# scripts. Delay expressions use milliseconds. **Run actions in order** makes each action follow the previous one. Choose **Called by another trigger** for a trigger that should only run through a chain. Named regex captures use `${name}`, shared variables use `${var:name}`, and your character is available through `${_me.name}`.

Search **Pack fight** and select a result to fill the outer folder's zone restriction. Leaving a new pack unassigned puts its speech rows under Unsorted. Triggers in nested folders keep their own restrictions. Imported settings and unsupported actions remain intact; **Edit trigger XML** exposes settings outside the forms.

Saving checks the pack with the bundled engine's XML types and .NET regular expressions before writing. The previous file is kept as `.xml.bak`. An external edit blocks saving until you reopen the pack. Saving reloads Triggernometry and clears its running sequences and variables; during combat, the reload waits until combat ends. Changed speech clears text overrides for the affected trigger. If any of its speech was muted, its updated speech stays muted. Cactbot mode keeps Triggernometry off.

**Silent live check.** From a source checkout, run `python3 tools/validate_triggernometry_live.py` while the game and IINACT are connected. This starts a temporary engine with its own probe pack and checks live raw and formatted logs, player identity, HP, position, and zone. It produces a report without speaking or changing installed packs. This checks the feed and engine integration; encounter mechanics still require their own validation.

**Update a pack** by importing its updated export. When the engine is available, matching packs ask for confirmation before replacement and save the previous XML with a `.bak` suffix. Identical imports reuse the existing copy. Separate packs can share a filename and receive separate files, but overlapping trigger IDs are rejected. **Remove a pack** by closing the program, moving its XML out of the [pack folder](../README.md#updating-and-saved-data), then restarting.

**Compatibility.** FFXIVNetwork triggers receive raw network logs; Log triggers receive formatted ACT logs. Combat-state and encounter-duration hooks use the IINACT feed. Replays cover TOP player markers and wipe resets, Zelenia's Bloom sequence from the [Paissa packs](https://github.com/paissaheavyindustries/Triggernometry-Triggers/tree/main/Repositories), and a delayed callout's C# calculation. These checks cover selected mechanics. Legacy auras and direct game-memory reads from scripts are unsupported.

**Telesto.** Set **Telesto URL** in Automarkers to use pack memory subscriptions and drawings. Callback setup is automatic for local connections. These features run with Triggernometry; game commands and macros additionally require **Enable automarkers**. Stopping the engine removes its subscriptions and drawings, and stale callbacks are ignored. TOP Party Synergy and Pantokrator are covered by replays. Telesto performs the memory reads and drawing, so pack offsets must match the game version.

**Runtime and builds.** The prebuilt .NET Framework 4.6.2 host is included in releases and source checkouts. Windows runs it natively; Linux needs [Mono and its system dependencies](../README.md#linux-system-dependencies). Rebuild only after changing the host or engine pin: run `triggernometry-core/build-all.sh` with Mono 6.12+. See the [engine README](../triggernometry-core/README.md).

---

## Current Instance tab

The live zone log has a text filter and checkboxes for players, enemies, casts, abilities, cancels, and statuses. Player actions are green, enemies and NPCs orange, and zone changes purple.

Each line ends with a hex ability ID, such as `Boss begins casting Tankbuster [A55B]`. Right-click to create a trigger with its ID, name, log type, fight tag, and zone regex filled in. IDs avoid language-dependent ability names.

---

## DPS tab

The meter updates every second from the combat log. It shows per-player DPS, damage share, HPS, crit and direct hit rates, max hit, and deaths, plus encounter duration and party DPS. Pets merge into their owners. Combat flags start and end pulls; wipes and zone changes also end them.

- **Reset display after** pauses the display after no damage and starts a new segment when damage resumes. Options range from 15 seconds to 10 minutes, defaulting to 2 minutes. Recorded pulls include all downtime.
- Finished pulls stay visible until the next starts. **Recent pulls** lists this run's attempts newest first. Select one to review it, then use **<- Back to live** or wait for the next pull.
- **Record encounters**, off by default, saves one JSONL record per pull in `dps_logs/`. Logs roll over after 25 pulls of one fight or 5 distinct fights. The newest five completed logs are retained.
- The companion overlay receives live DPS once a second for up to 24 players. Its settings control appearance and whether the last encounter remains visible.

---

## Death Recap tab

Choose a player and death to review up to 60 seconds of observed damage, incoming healing, HP, shields, and the buffs and debuffs active at each event. Self-heals and reflected damage follow their actual recipient. Instant-death effects have a separate label.

Deaths and their events appear newest first. Times such as `-2.6s` are relative to death. Damage is blue, healing is green, and `!` marks a critical hit or heal. HP bars show health before the event, with confirmed healing in light green and shields in yellow. Standalone HP and shield updates show their current values. Hover a bar for its values or a status icon for its name, source, remaining duration, and stacks.

**Targeted mitigation** shows single-target player defenses such as Intervention, Oblation, Blackest Night, Nascent Glint, Heart of Corundum, Aquaveil, Taurochole, and Exaltation, including their shields and follow-up effects. It also shows Addle, Feint, Reprisal, Dismantled, Malodorous, Conked, and Candy Cane observed on the attacker, following [Death Recap's capture behavior](https://github.com/Kouzukii/ffxiv-deathrecap/blob/658ec3a19614f225e354b207ebba87aaf64943c7/Events/CombatEventCapture.cs). Other buffs and debuffs remain in **Status effects**. Tooltips distinguish effects on the player from effects on the attacker and identify who applied them. These are the statuses observed at each event, not estimates of prevented damage. Saved recaps and imported logs use the same columns and filters.

Status tooltips include the game's description. Hover an amount to see damage type, critical hits, direct hits, blocks, or parries when recorded. `!!` marks a critical direct hit.

**Filter buffs** uses checked boxes to hide individual statuses throughout the recap without deleting recorded data. Offensive buffs such as two-minute raid buffs and damage procs are hidden by default. Mitigation, shields, healing effects, Weakness, Damage Down, and other debuffs stay visible. Mixed offensive and defensive statuses and unknown encounter effects also stay visible. Search for a status, check it to hide it, or uncheck it to show it. **Show all statuses** clears the selections; **Restore defaults** selects the offensive buff filter. Both buttons affect the full list while searching. The count shows how many statuses are selected to hide. Click **Apply** to save and refresh the recap while keeping the dialog open, or **OK** to save and close. **Cancel** discards changes made since the last Apply. Existing saved choices are preserved.

**Damage**, **Healing**, **Buff changes**, **Debuff changes**, and **HP and shield updates** select which event rows appear. The change switches control gain and loss rows, leaving status icons on damage and healing rows visible. Gain rows show duration and the correct stacked icon. Buff and debuff categories come from game data. Unknown categories remain visible when either change switch is on. Internal statuses without a game icon are omitted. Separate change and health rows are off by default, and existing status-change preferences are preserved. These choices are saved. Drag the dividers or column edges to resize the view. Long text and status icons wrap.

The program includes the game icons for the full action and status catalog, including status stack variants. They work offline. Icons for new entries can be fetched from [XIVAPI](https://v2.xivapi.com/docs/guides/assets/) and cached locally. See [recap icon data](RECAP-ICONS.md) for sources and refresh instructions.

The recent view retains 80 deaths until the program closes. Zone changes and disconnects clear live observation buffers but preserve completed recaps. Wipes retain buffers until the next pull to capture late deaths. Observed buffs and healing between pulls also carry into the next recap.

From Prog, **View death recaps** opens only the selected pull's saved deaths, identified by session, pull, and duty. **Back to Prog** returns to that pull and its notes; **Recent deaths** returns to live history. New deaths do not replace a saved view. Saved recaps survive restarts and are independent of the recent view's limit. Missing, older, or unreadable records show an explanation, and unreadable files are preserved.

**Saved pulls…** opens saved sessions and pulls directly from Death Recap. Use **Previous pull** and **Next pull** to browse adjacent attempts, then select a player and death in the list.

**Open log…** reads an IINACT network `.log` file in the background with progress and cancellation. Browse all its deaths or select an inferred pull and player. Imported recaps use the original log timestamps, statuses, HP, and healing. Pull boundaries are inferred from combat activity and wipe signals, so their numbers can differ from Prog. Importing does not write to Prog or replace live recording. Cancelled or failed imports keep the current view. Imported details use temporary storage and are loaded one death at a time, beyond the live view's 80-death limit, up to 50,000 deaths per file. Return to recent deaths or open another source to release that storage. Reopen the source log after restarting the program. A growing log is read only up to its size when opened.

Recaps describe the observed feed. Incoming healing amounts include overheal. The light green segment uses the observed HP rise at resolution, capped at the reported heal. Confirmed HP updates take precedence over ability snapshots that may be stale. Shields are rounded percentages of maximum HP. Some ticks are aggregated, and events missed before connection cannot be recovered. Status list updates recover buffs already active at connection. A recap retains at most 256 observations. Older saved recaps remain readable and show **Not recorded** for missing HP and per-event statuses. Ability events may arrive before their effects resolve. See the [combat log format](https://github.com/OverlayPlugin/cactbot/blob/main/docs/LogGuide.md).

Unlike the in-game Death Recap plugin, this program reconstructs events from IINACT logs rather than reading the game directly. Reopen an original log to regenerate historical recaps with current parsing and attacker mitigation. Previously saved recaps cannot acquire details that were not recorded.

## Prog tab

**Start session** begins a named duty session after connection, duty identification, and the first combat-state message. Starting during combat waits for the next full pull.

**Find sessions by name or duty** searches the saved list using all the words you enter, ignoring case. Names and duty names can match different words. Clearing a search with no results restores the previous session and pull. Searching saves pending notes and leaves collection running even when the active session is hidden. Starting a session clears the search so the new session is visible.

**Archive session** hides a finished session from the usual picker and comparison choices. Enable **Show archived** to review its notes and recaps, copy its details, or compare it again. Archived entries have a label next to their names. **Restore session** returns an archived session to the usual list. Archiving retains the saved files and recaps. Active sessions must end first. A failed archive write keeps the previous visibility and reports the save error.

Each pull records its start, duration, ending, and deaths. The pull table lists the newest first and keeps the original pull numbers. The summary separates complete and interrupted attempts and shows the longest complete pull, total observed combat time, and session elapsed time. Click the duration chart to select a pull. Edit the session name above the table and add bookmarks or notes below it.

The star arrow buttons move to the previous or next bookmarked pull in its original order. They stop at the first or last bookmark, and notes save before moving. **Copy pull summary** puts the selected session, duty, pull number, observed duration, ending, deaths, phase confirmations, and notes on the clipboard. Earlier phases established by later evidence keep their confirmation time unavailable in the copied text.

**Furthest phase** and **Phase confirmations** record UMAD progress from boss casts and ability events. Times identify the first confirming event after pull start. P5 requires Ultima Repeater because Ultima Upsurge also occurs in P4. Earlier phases established by later evidence have no invented time. Interrupted recordings retain their observations. Pulls without confirming events show **No confirmation**. Older pulls without phase data remain **Not recorded**; unsupported duties show **Not supported**. Unreadable phase data does not hide notes or recaps.

**Phase progress** summarizes the furthest confirmed phase among eligible pulls, with its count and sample size. Only complete attempts with readable phase data and matching verified rules contribute. Quick wipes without confirmations remain in the denominator. Active, interrupted, uncertain, unreadable, and unrecorded pulls are excluded, with counts by reason. Zero eligible pulls shows **No eligible pulls**. Rates describe received confirmations, so missing feed events can hide a phase that the party reached.

**Compare with…** opens a comparison area for another saved, finished session in the same duty. An interrupted session can still contain eligible complete pulls. The first opening chooses the most recent earlier compatible session when available. The table shows finished pull counts, eligible phase pull counts, furthest confirmed phases, longest finished pulls, and each phase's confirmed reach. Rates include sample sizes, and changes use percentage points calculated before rounding. For example, 6/20 and 3/15 show 30% and 20%, with a change of +10 percentage points.

Phase comparisons require identical tracking definitions and revisions. Mixed or unknown revisions cannot silently contribute a subset of their pulls. Longest pulls require matching duration conventions. Older sessions can compare ordinary counts and compatible durations without phase data. Comparison choices stay selected as sessions arrive, and changing duties clears an incompatible choice. Closing the comparison keeps the selected pull and notes. The Prog details scroll when they need more room, while the search, picker, and session controls stay visible.

**Copy comparison** puts the session names and dates, counts, rates, percentage-point changes, and exclusion notes on the clipboard as a text table.

**View death recaps** opens the selected attempt in Death Recap. Deaths save as they arrive, including during interrupted pulls. Late deaths can attach for two seconds after combat ends or a wipe. A new pull, disconnect, duty change, or session end closes that window. Starting midcombat skips the partial attempt and its recaps. An empty meter encounter with observed deaths remains reviewable as interrupted.

**End session** or leaving the duty ends collection. Wipes and breaks stay in the session. A disconnect preserves the observed pull as interrupted and waits for a fresh pull after reconnecting. Interrupted attempts do not count toward longest complete pull. **Combat ended** does not establish a clear. Session controls leave the live meter and callouts running.

A wipe signal received within five seconds of combat ending updates that pull's ending. This does not extend the two-second allowance for late deaths.

Sessions save in `prog_sessions/`, independently of DPS recording and log rotation. Notes save after a typing pause and flush on shutdown. Active progress and elapsed time also save every 15 seconds. The session picker opens previous sessions after restart. Crash recovery retains the last successful save and marks unfinished sessions interrupted. Unreadable files are preserved and errors are shown.

Recaps save separately under `prog_sessions/recaps/`, grouped by session and pull IDs. Failed writes retry when session changes flush. The queue holds the latest 256 unsaved recaps and reports any dropped records. Storage must recover before queued records can survive closing the program.

## Profiles

Expand **Profiles** below the trigger list to save setups for jobs, groups, or strategies.

| Control | Effect |
|---|---|
| **Save new** | Capture Local toggles and speech, engine toggles, and editable Triggevent and Triggernometry wording. |
| **Apply** | Activate the selected setup between pulls. Selecting or saving alone does not activate it. |
| **Update** | Replace the selected named snapshot with current choices. Later edits reach that snapshot only through Update. |
| **Delete** | Remove a named profile after confirmation. Deleting the active profile restores Default and requires combat to end. |
| **Default** | Your normal saved setup. Apply it to restore those choices. It cannot be deleted or overwritten with Update. |

The active profile and separate Default setup survive restarts. Applying preserves definitions, folders, and newly added triggers, but does not recreate deleted triggers or change Cactbot mode, voice, or automarkers. Profiles are stored in `trigger_profiles/` beside other writable program data.

## Automarkers tab

Place party signs through [Telesto](https://github.com/paissaheavyindustries/Telesto). Each rule maps a fight and debuff to a marker for **me** or **whoever gets the debuff**. Marking another player requires a known party slot.

**Enable automarkers** and **Telesto URL** also configure Triggevent's native
markers after an engine restart. **Triggevent encounter automarkers** exposes all
13 bundled output sets across UWU, DSR, TOP and UMAD, using their native encounter
logic. Turning off Triggevent callouts leaves markers running. Nyaa supports
Telesto for marking.

The native controls include independent mechanic switches, shared and overridden
job priorities, eight marker maps with per-slot enable and presets, and encounter
and command delays. Job priority chooses mechanic assignments; it does not change
the game's party slots. A disabled priority override retains its own order and
shows the effective shared order. UWU's clear delay uses milliseconds; Sigma and
Omega delays use seconds. Native Next and Clear signs retain Triggevent's behavior.

Rendering controls leaves your existing Triggevent settings intact. Explicit edits
are saved in Nyaa and applied before native marking is enabled after restart or
recovery. UWU and native UMAD keep their enabled defaults under the disabled
master switch; DSR and TOP keep their disabled defaults. Invalid settings show a
native settings error and leave the last valid configuration intact. Disabling
the master and claiming local P4 ownership still takes effect if a saved native
setting is rejected.

Use a control's **Reset** button to replace an invalid saved preference with its
native default. This also repairs values whose displayed fallback already looks
correct, allowing later edits to other controls.

Local UMAD gaze pairing or an assigned local P4 debuff rule scoped to UMAD or any
fight keeps P4 marker ownership in Nyaa. Native UMAD P4 marks are then suppressed,
even when **Enable native P4 debuff markers** is selected.
P1 and P3 rules leave native P4 marking available.
Other native encounters keep their existing settings.

Native P4 pairs players by debuff resolution across two waves. Nyaa's local gaze
pairing uses wave and party order and distinguishes real and fake gaze signs.
Choose the producer that matches your strategy; the controls preserve one owner.

The connection indicator confirms that Telesto responds. Diagnostics distinguish
queued actions, attempted requests, failures and endpoint acceptance. Telesto
does not confirm that a marker appeared in the game. A timed-out command may
already have been submitted, so it is not retried automatically.

- **Connection:** set **Telesto URL**, default `http://localhost:45678/`, use **Test mark (on me)**, then select **Enable automarkers**.
- **Rules:** unassigned rules do not fire. Select a rule and choose its **Marker**, or choose *(unassigned)* to disable it. **Load UMAD preset** adds the selected Dancing Mad Ultimate rules. **Remove the mark when the debuff falls off** is on by default. **Clear all party marks** clears every sign.
- **UMAD sequences:** **black-hole chains** assign roaming signs to the P3 DPS, support, and Accretion cleanse queues, each with its own picker. **Cursed Shriek gaze pairs** assign P4 look-at and look-away signs using Neo Exdeath's status VFX. When both pairs need the same signs, the later pair receives them after the first gaze ends. Both suspend overlapping plain rules. See [UMAD debuff rules and evidence](UMAD-DEBUFFS.md).

---

## Alert sound

In **Settings - Alert Sound**, choose Ding, Alert, Coin, or **Import SFX** to copy a `.wav` into the user sounds folder. Imported sounds survive updates. **Custom file...** selects a file elsewhere.

Choose every alert or urgent only, set **Volume**, and use **Test**. Volume defaults to 50%, about -20 dB; 100% is full volume and 0% is silent. Master volume applies afterward. Sounds play alongside speech on a separate channel.

---

## In-game display

The optional [NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay) Dalamud plugin draws timeline bars, callouts, and DPS inside the game. NyaaTriggers handles the trigger logic and speech.

1. Follow the plugin's README to install its Dalamud repository.
2. Type `/nyaa` in game to position the windows, then select **Lock**.
3. Check for **Connected** in **Settings - In-Game Overlay**.

The local connection is automatic and ports must match. Start the game and program in either order. **Waiting for the game plugin** means the plugin has not connected yet. Speech works without the plugin.

---

## Trigger fields

| Field | Description |
|---|---|
| Log Type | `20`: cast start. `21`: single-target ability. `22`: AoE ability. `23`: cancelled cast. `26`: status gained. `30`: status lost. `00`: chat or dialogue, matched with Ability Regex. |
| Ability ID | Hex ability or status ID. Separate alternatives with a pipe: `9494\|9495`. Takes priority over regex. |
| Ability Regex | Pattern matched against the ability name when no ID is set. |
| TTS Text | Spoken callout. Use `{source}`, `{target}`, or `{count}` for the status stack count. The ▶ button speaks a preview. |
| Applies to | For types 26 and 30. **You**, the default, matches effects on you. **Target - you applied it** matches effects you applied, such as Death's Design, with `{target}` naming the recipient. **Anyone** removes the source and target filter. |
| Reapply warning | For **GainsEffect (26)**, seconds before expiry to speak. Refreshing the effect re-arms the reminder. Early loss, zone changes, and instance resets cancel it. `0` speaks on application. **Cooldown** sets the minimum gap between reminders across targets, preventing repeated calls for an AoE DoT. |
| Stacks | For status effects, types 26/30: fire only when the stack count is within a min/max window. Pairs with `{count}`. |
| Alert Sound | Path to a `.wav` file played on trigger. Can be used alongside or instead of TTS. Place sounds in the `sounds/` folder. |
| Cooldown | Minimum seconds between firings per source entity. |
| Fight Tag | Links the trigger to a sidebar entry, e.g. `M4S`. Use the **Pick…** button to choose from the catalog. Leave blank to show under General. |
| Zone Regex | Restricts the trigger to zones whose name matches this pattern. A live dot shows whether your current zone matches as you type. |
| Speed | Speech rate multiplier, 1.0 = normal and 2.0 = twice as fast. |
| Interrupt | If checked, cuts off any currently playing TTS and speaks this trigger immediately. |
| Follow-up Steps | Sequential steps that must match in order after the initial trigger fires. Each step has its own log type, ability, and timeout. |

---

## Settings

- **Program:** check for updates, enable the startup check, download the prebuilt Triggevent engine for source installs, and open GitHub or Discord. **Language** offers Automatic, English, and 日本語 and applies after restart. Automatic follows the OS. Japanese translates the interface, trigger names, and built-in callouts; search accepts either language. Translations are machine-assisted and incomplete, with English fallback.
- **Connection:** **Auto-connect on startup** and **My character**. The character name fills from the game on connection, login, and zone entry. Correct it if needed. Status triggers use it to match effects on **You** or effects you applied with **Target - you applied it**.

---

## Personal triggers

Additions, edits, and deletions save to the gitignored `triggers.local.json` and merge on startup. Bundled definitions remain in `assets/triggers.json`.

Under **Settings - Data**:

- **Update Triggers** refreshes bundled definitions from GitHub while preserving local changes.
- **Restore from Repo** downloads the bundled set again for repair.
- **Save log…** exports the captured combat feed for debugging.
- **Export** / **Import** transfers local triggers and folders. Import replaces the current local set.

---

## Updating

The optional startup check and **Settings - Program - Check for Updates** look for releases from main. The banner offers installation and release notes. Downloads begin after **Install** and confirmation. Updates preserve saved data and restart the program.

| Installation | Update behavior |
|---|---|
| Git clone | Run `git pull --ff-only --tags`, check Python requirements, then restart. Writable Python environments install requirements. Distribution-managed Python checks installed packages and reports missing ones for manual installation. Tags keep the version label current. If published commits were combined, a clean main checkout can follow the rewritten history when Git records its current commit in the upstream reflog. The previous checkout is saved under `refs/nyaa-update-backups/`. Local commits, tracked edits, and conflicting untracked files are preserved for manual resolution. |
| Source copy without Git | **Download** opens the releases page for manual installation. |
| Linux `.tar.gz` | Replace program files in place and restart, preserving settings, local triggers, and timelines. |
| Windows `.zip` | A staged copy replaces files after exit, with a backup and automatic rollback if startup fails. |

Git clones and source copies are notified of each rolling release. Installing, choosing Download, or dismissing a banner snoozes that release until a newer one appears.
