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
| PyQt6 | `sudo pacman -S python-pyqt6` / `sudo apt install python3-pyqt6 python3-pyqt6.qtwebsockets` / `pip install PyQt6`. Debian splits the WebSockets binding into its own package and the program needs it for the game feed. |
| piper-tts | Installed automatically on first launch into `~/.venv/ffxiv` |
| Audio backend | `aplay` via `alsa-utils` |
| PyQt6-WebEngine | Optional for cactbot in source runs. Use `sudo pacman -S python-pyqt6-webengine` or `pip install PyQt6-WebEngine`. Releases bundle it. |

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

## Triggernometry engine (WIP)

Triggernometry runs conditions, shared variables, delayed actions, trigger chains, and C# `ExecuteScript` actions from imported XML packs. It starts when a pack is available and Cactbot is off. Live fight validation is still pending.

**Import a pack** through **Settings - Data - Import Triggernometry**. Its triggers appear as editable Triggernometry rows. Without the engine, the importer converts only simple ability matches with plain speech to Local rows.

**Update or remove a pack** while the program is closed by replacing or moving its XML in the [pack folder](../README.md#updating-and-saved-data), then restarting. Importing the same filename adds another copy.

**Compatibility.** FFXIVNetwork triggers receive raw network logs; Log triggers receive formatted ACT logs. Replays cover TOP player markers and wipe resets, Zelenia's Bloom sequence from the [Paissa packs](https://github.com/paissaheavyindustries/Triggernometry-Triggers/tree/main/Repositories), and a delayed callout's C# calculation. These checks cover selected mechanics. Legacy auras, direct game-memory reads from scripts, and ACT combat-state or encounter-duration hooks are unsupported.

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

Select a death to review the previous 15 seconds of observed damage, healing, status changes, and statuses remaining at death. Self-heals and reflected damage follow their actual recipient. Instant-death effects have a separate label.

The recent view retains 80 deaths until the program closes. Zone changes and disconnects clear live observation buffers but preserve completed recaps. Wipes retain buffers until the next pull to capture late deaths. Observed buffs and healing between pulls also carry into the next recap.

From Prog, **View death recaps** opens only the selected pull's saved deaths, identified by session, pull, and duty. **Back to Prog** returns to that pull and its notes; **Recent deaths** returns to live history. New deaths do not replace a saved view. Saved recaps survive restarts and are independent of the recent view's limit. Missing, older, or unreadable records show an explanation, and unreadable files are preserved.

Recaps describe the feed and cannot reconstruct exact HP. Healing includes overheal, some ticks are aggregated, and statuses active before connection may be missing. Ability events may arrive before their effects resolve. See the [combat log format](https://github.com/OverlayPlugin/cactbot/blob/main/docs/LogGuide.md).

## Prog tab

**Start session** begins a named duty session after connection, duty identification, and the first combat-state message. Starting during combat waits for the next full pull.

Each pull records its start, duration, ending, and deaths. The summary separates complete and interrupted attempts and shows the longest complete pull, total observed combat time, and session elapsed time. Click the duration chart to select a pull. Edit the session name above the table and add bookmarks or notes below it.

**Furthest phase** and **Phase confirmations** display saved observations. Times identify the first confirming event after pull start. Earlier phases established by later evidence have no invented time. Interrupted recordings retain their observations. Automatic UMAD tracking awaits verified recordings, so new UMAD pulls show **Not recorded** with an explanation. Older pulls also show **Not recorded**; unsupported duties show **Not supported**. Unreadable phase data does not hide notes or recaps.

**View death recaps** opens the selected attempt in Death Recap. Deaths save as they arrive, including during interrupted pulls. Late deaths can attach for two seconds after combat ends or a wipe. A new pull, disconnect, duty change, or session end closes that window. Starting midcombat skips the partial attempt and its recaps. An empty meter encounter with observed deaths remains reviewable as interrupted.

**End session** or leaving the duty ends collection. Wipes and breaks stay in the session. A disconnect preserves the observed pull as interrupted and waits for a fresh pull after reconnecting. Interrupted attempts do not count toward longest complete pull. **Combat ended** does not establish a clear. Session controls leave the live meter and callouts running.

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

- **Connection:** set **Telesto URL**, default `http://localhost:45678/`, use **Test mark (on me)**, then select **Enable automarkers**.
- **Rules:** unassigned rules do not fire. Select a rule and choose its **Marker**, or choose *(unassigned)* to disable it. **Load UMAD preset** adds the selected Dancing Mad Ultimate rules. **Remove the mark when the debuff falls off** is on by default. **Clear all party marks** clears every sign.
- **UMAD sequences:** **black-hole chains** assign roaming signs to the P3 DPS, support, and Accretion cleanse queues, each with its own picker. **Cursed Shriek gaze pairs** assign P4 look-at and look-away signs using the wave's follow-up cast. Both suspend overlapping plain rules. See [UMAD debuff rules and evidence](UMAD-DEBUFFS.md).

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
| Git clone | Run `git pull --ff-only --tags`, install requirements with the program's Python, then restart. Tags keep the version label current. Blocking local edits are reported for manual resolution. |
| Source copy without Git | **Download** opens the releases page for manual installation. |
| Linux `.tar.gz` | Replace program files in place and restart, preserving settings, local triggers, and timelines. |
| Windows `.zip` | A staged copy replaces files after exit, with a backup and automatic rollback if startup fails. |

Git clones and source copies are notified of each rolling release. Installing, choosing Download, or dismissing a banner snoozes that release until a newer one appears.
