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

Windows and Linux release builds bundle the Python dependencies and English Piper voice. IINACT is installed separately, and Linux engines also use the [system dependencies listed in the README](../README.md#linux-engine-dependencies). This table covers the base requirements for running from source on Linux.

| Requirement | Notes |
|---|---|
| [IINACT](https://github.com/marzent/IINACT) | Running and connected to the game |
| Python 3.11+ | System Python is fine |
| PyQt6 | `sudo pacman -S python-pyqt6` / `sudo apt install python3-pyqt6 python3-pyqt6.qtwebsockets` / `pip install PyQt6`. Debian splits the WebSockets binding into its own package and the program needs it for the game feed. |
| piper-tts | Installed automatically on first launch into `~/.venv/ffxiv` |
| Audio backend | `aplay` via `alsa-utils` |
| PyQt6-WebEngine | **Optional** on source installs, only for the cactbot source in **Settings - Cactbot**. `sudo pacman -S python-pyqt6-webengine` or `pip install PyQt6-WebEngine`. The rest of the program works without it. The packaged release builds bundle it. |

---

## Voice

Pick the backend in **Settings - Voice - Engine**:

- **System** - your OS voice, Windows SAPI or Linux `spd-say` / `espeak`. No download or extra dependency. **Default on Windows.**
- **Piper** - fully offline neural TTS via [Piper](https://github.com/OHF-Voice/piper1-gpl). **Default on Linux.** Release builds include the `en_US-arctic-medium` model. Source runs download it automatically on first launch into `voices/`.

**Adding Piper voices:** browse the [voice samples](https://rhasspy.github.io/piper-samples/), download a `.onnx` model and its matching `.onnx.json` config, and drop both in the `voices/` folder. **Settings - Voice - Open voices folder** opens it. Pick the voice under **Model**, and hit **Refresh list** or restart if you added it while open. Medium voices are ~65 MB, low ~30 MB.

**Japanese voices:** the **Model** dropdown also lists the neural Japanese voices Alpha and Kumo next to the English one. Pick one and callouts are spoken in Japanese. The program downloads the voice and sets it up on first pick, about 330 MB, and it reads kanji. Until it is ready Japanese falls back to espeak.

**Test TTS** speaks a sample with the current voice. On Linux source installs, **Piper venv** sets the path to the venv holding piper-tts, default `~/.venv/ffxiv`.

---

## Triggers tab

The trigger editor and your trigger set, bundled plus custom. The live combat log is a separate **Current Instance** tab.

**Callout modes.** With **Cactbot** off, the program runs your editable **Local**, **Triggevent**, and imported **Triggernometry** callouts according to their row checkboxes. Enabling **Settings - Cactbot** switches those callouts off and runs cactbot instead. Turning Cactbot off restores editable callout mode. The same Cactbot switch controls its timelines: on, and the current fight uses cactbot's `.txt` files alongside its callouts. Dungeon timelines ship with the program, and the others download and cache on demand. With Cactbot off, local timelines are used.

**Callout choices.** Engine callouts are enabled unless you mute them, including newly discovered callouts. Local rows keep their own saved on/off choices. Local triggers use their zone restrictions to decide where they can fire. Check the row and per-fight controls when choosing your setup.

**Sidebar tree**, grouped by content type, then expansion, then fight:

- **General** - fires in any zone, like tank invulns and personal mitigations
- **By content type** - Ultimates, Savage Raids, Extreme Trials, Deep Dungeons, Field Operations, Normal Raids, Normal Trials, Alliance Raids, each split by expansion then fight: Dawntrail, Endwalker, Shadowbringers, Stormblood, Heavensward, A Realm Reborn. Ultimates: FRU, UMAD, TOP, DSR, TEA, UwU, UCoB.
- **TBD** - bundled triggers whose fight has no curated slot yet
- **Unsorted** - your custom triggers with no zone lock. A custom trigger with a Zone Regex auto-files under its fight instead, or under TBD if that fight isn't in the tree.

Click a fight or folder to filter the table. Click a header to expand or collapse it.

**Source groups** in the table: **General**, **DoT** for reapply-warning timers, **Local**, **Triggevent**, **Triggernometry**. All start collapsed, and typing in the search box flattens them into one list.

**Toggles.** **Global - Local On/Off** and **Global - Triggevent On/Off** flip a whole source on or off across every fight. Selecting a single fight adds a bar with **Local** and **Triggevent** checkboxes. A box is ticked when that source is fully on for the fight, and toggling it changes only that source. A global toggle lights the matching boxes, and a single-row change re-derives them. Turning a box or a global button on expands the affected sections, and turning it off collapses them. You *can* run both at once, but they will double up on any fight they both cover, so usually pick one per fight.

**Zone column** shows a live dot per trigger: **green** means the trigger matches your current zone and can fire, **red** means locked out, **none** means not connected or no zone detected yet.

**Right-click a row** for Edit, Duplicate, Test Fire, Enable / Disable, Delete, Move to Folder, and Reset to Default. Move to Folder changes the fight tag, and Reset to Default restores a modified bundled trigger.

**Right-click a tree node** to manage Unsorted folders: New Folder, New Subfolder, **New folder for a fight...** with a searchable Savage / Ultimate / Extreme picker, Rename, and Delete Folder. Delete asks for the count first and removes the folder, its subfolders, and the triggers inside.

The **toolbar** mirrors the row actions plus the two **Global** toggles and **Reset to Default**, which unchecks every trigger everywhere. Reset only clears the on/off marks. It never removes a trigger or restores edited values.

---

## Engine triggers

Engine triggers appear right in the **Triggers** list under their fight, tinted and tagged in the Type column, and fire by default:

- **Uncheck a row** to silence that callout, and check it again to bring it back. New triggers fire the first time they're seen, nothing is muted just for being new. Cactbot suppresses disabled callouts at source via `DisabledTriggers`. Triggevent and Triggernometry drop the disabled ids. All apply live with no restart.
- **Double-click** a Triggevent callout to change its wording, or right-click and pick **Edit spoken text**. Tokens like `{event.target}` still substitute, and **Reset to default** restores it. Edits persist and re-apply each time the engine starts.
- **Triggernometry** callouts list as editable rows too. Edit the spoken text or toggle them per trigger, the same as Triggevent. See [Triggernometry engine](#triggernometry-engine-wip).
- **Test TTS** from the right-click menu or the ▶ button in the edit dialog speaks a callout with sample token values.
- **Cactbot** callouts can't be edited at the engine. Uncheck the row to silence one, and check it again to bring it back. Cactbot's on/off, per-trigger overrides, and the page **URL** live in **Settings - Cactbot**. The URL defaults to the hosted raidboss build, so point it at a local build only if you bundle one. Cactbot needs PyQt6-WebEngine. The packaged release builds bundle it, and source installs need the package from the Requirements table.

Simple cast-based engine triggers are bundled as editable Local triggers instead. The complex imperative and Groovy ones stay in the engine and show here.

---

## Triggevent Engine

Triggevent runs against the same IINACT feed and speaks its enabled callouts: built-in, EasyTriggers, and your Groovy scripts in `~/.triggevent`, including the code-based ones that cannot become Local triggers. Its callouts run with Cactbot off and follow the row, per-fight, and global Triggevent controls.

It runs Triggevent's engine ([`xpdota/event-trigger`](https://github.com/xpdota/event-trigger), GPL-3.0) as a side process. **Release builds bundle the engine and a Java 17 runtime.** A source checkout builds the sidecar once with **JDK 17 + Maven**:

| OS | JDK 17 + Maven | Build |
|----|----------------|-------|
| **Linux** (Arch/CachyOS) | `sudo pacman -S jdk17-openjdk maven` | `cd triggevent-core && ./build.sh` |
| **Bazzite / Fedora** | `brew install openjdk@17 maven` | `cd triggevent-core && ./build.sh` |
| **Windows** | `winget install EclipseAdoptium.Temurin.17.JDK Apache.Maven` | `cd triggevent-core && build.bat` |

That produces `triggevent-core/target/triggevent-core.jar`, which NyaaTriggers auto-detects, falling back to a bundled or system Java. Or skip the build entirely. **Settings - Program - Update Triggevent Engine** downloads the same prebuilt jar the packaged builds ship, no git or Maven needed. See `triggevent-core/README.md` for the full design.

Some engine components build a Swing overlay as they load, so the sidecar runs the engine against a display you never see. On Linux that is a throwaway **Xvfb** when installed, otherwise your session display. Install it with `sudo pacman -S xorg-server-xvfb` if you are on a headless or pure-Wayland box. Triggevent's own overlays render off-screen. You only get what NyaaTriggers speaks and what the optional companion overlay plugin draws.

---

## Triggernometry engine (WIP)

> **Note.** Triggernometry support is still being validated in-game.

Triggernometry runs its **real engine** to preserve conditions, shared variables, delayed actions, trigger chains and C# `ExecuteScript` actions. These need the original XML pack because the simple converter cannot represent their logic. The engine starts once a pack has been imported and Cactbot is off. Turning Cactbot on stops it.

**Importing a pack.** Point **Settings - Data - Import Triggernometry** at a Triggernometry `.xml` export. The whole pack runs through the engine and lists under its own **Triggernometry** section as editable rows. Edit the spoken text or toggle them per trigger, just like Triggevent. On a build without the engine the simple triggers, a literal ability ID plus a plain text-to-speech line, fall back to editable **Local** rows instead.

**Pack compatibility.** Network triggers receive raw FFXIV logs and ACT log triggers receive the corresponding formatted logs. Replay tests cover TOP's player markers and wipe reset, Zelenia's Bloom callout sequence from the [Paissa sharing-channel pack](https://github.com/paissaheavyindustries/Triggernometry-Triggers/tree/main/Repositories), and a C# calculation used by a delayed callout. This does not validate every trigger in those packs. Legacy Triggernometry auras and scripts that read game memory directly are still unsupported.

**Telesto callbacks and drawings.** With [Telesto](https://github.com/paissaheavyindustries/Telesto) running in the game, imported packs can subscribe to memory changes and draw lines, circles, beams and other Telesto doodles. The program uses **Telesto URL** in the Automarkers tab and sets up the callback address automatically. These features run with Triggernometry in editable callout mode. The Automarkers switch separately gates pack actions that send game commands or macros. No extra listener settings or administrator setup are needed for a local Telesto connection.

TOP's Party Synergy weapon calls and Pantokrator circles and beams are covered by replays through this integration. Subscriptions and drawings belong to one engine run and are cleaned up when it stops. Late notifications from retired subscriptions or replaced drawings are ignored. Telesto performs the memory reads and renders the drawings inside the game. Pack memory offsets still need to match the game version.

**How it runs.** A headless .NET sidecar (`triggernometry-core`) hosts the real engine and routes its callouts to NyaaTriggers' voice and the optional companion overlay plugin. It's cross-platform .NET Framework 4.6.2. **Windows runs it natively**, and **Linux runs it under Mono** with `sudo pacman -S mono`. Release builds bundle the prebuilt sidecar, so there's nothing to build to use it.

**Building from source.** The sidecar's prebuilt binaries are vendored in `triggernometry-core/bin/` and used as-is, so a normal source run needs no build step. Linux still needs the [engine dependencies](../README.md#linux-engine-dependencies). To rebuild it, only needed if you change the host or bump the pinned engine, run `triggernometry-core/build-all.sh` with **Mono 6.12+** installed. It clones the engine at a pinned commit, applies the shims, and rebuilds `bin/`. See `triggernometry-core/README.md` for the design.

---

## Current Instance tab

A live combat log for the current zone, with a **Filter** box.

- **Easy-to-Read Log** - human-readable ability feed colored by actor, **green** for player action and **orange** for enemies and NPCs, each line ending with the hex ability ID like `Boss begins casting Tankbuster [A55B]`. Zone changes show as purple banners. Use the checkboxes to show or hide players, enemies, casts, abilities, cancels, and statuses. Right-click a line to open a trigger dialog pre-filled with its ability ID, name, log type, fight tag, and zone regex. Ability IDs are the most reliable way to match a trigger since they survive patches and are language-independent.

---

## DPS tab

A live damage meter parsed by the program itself straight from the combat log. Per player it shows DPS, damage share, HPS, crit and direct hit rates, max hit and deaths, updating every second, with the encounter title, duration and party DPS on top. Pets and summons are merged into their owners like ACT does. A fight starts on the in-combat flag and ends on the flag dropping, a wipe, or a zone change. The meter is always on.

- The on-screen meter pauses after a stretch of no damage, **Reset display after** 15s to 10m with a 2m default, holds its numbers, and starts a fresh segment when damage resumes. The recorded log always keeps the whole pull, downtime included.
- A finished pull stays frozen on screen until the next one starts.
- **Recent pulls** lists this session's pulls newest first. Click one to review its numbers. The feed goes back to live on its own when the next pull starts, or hit **<- Back to live**.
- **Record encounters**, off by default, appends each finished pull to a JSONL log in `dps_logs/`, one line per pull, fights mixed like ACT's log files. A log rolls over at 25 pulls of one fight or 5 distinct fights, and once 5 full logs sit in the folder the oldest are culled.
- With the companion overlay plugin connected the live meter is also drawn in the game, up to 24 players plus party DPS, once a second while a fight runs. The plugin controls its appearance and whether the last encounter stays visible after combat ends.

---

## Death Recap tab

A separate page for reviewing the 15 seconds before a player's death. Select a
death on the left to see observed damage, healing, status gains and losses, and
the statuses still observed at death. Self-heals and reflected damage follow
their actual recipient. Instant-death effects have their own label.

The recent view keeps the latest 80 deaths in memory until the program closes. Zone changes
and disconnects clear the live observation buffers, while completed recaps remain
available. Wipe events keep the buffers until the next pull so deaths arriving
just after the wipe can still be reviewed. Buffs and healing observed between
pulls remain available for the next pull's recap.

Pulls collected by a Prog session also save their death recaps locally. In Prog,
select a pull and press **View death recaps** to open just that attempt's deaths,
including after a restart. The heading identifies the session, pull number, and
duty. **Back to Prog** returns to the selected pull and its notes. **Recent deaths**
returns to the live history. New deaths from another pull do not change a saved
pull being reviewed.

Saved recaps are independent of the recent view's 80-death limit. Older pulls
recorded before this feature have no saved recaps, and the page says so. A pull
with no recaps shows an empty view. Unreadable or missing records and save errors
are reported without replacing the files.

This is a record of the feed, not a reconstruction of exact HP. Healing includes
overheal, some damage and healing are reported as aggregate ticks, and statuses
that were already active before connection may be missing. Ability events may
precede their effects resolving. These limitations follow the
[combat log format](https://github.com/OverlayPlugin/cactbot/blob/main/docs/LogGuide.md).

## Prog tab

**Start session** begins a named session for the current duty. It becomes
available after connection, duty identification, and the first combat-state
message. If combat is already running, collection waits for a new full pull.

Each pull adds its start time, duration, ending, and recorded death count. The
summary shows complete and interrupted attempts separately, the longest complete
pull, total observed combat time, and session elapsed time. The duration chart
selects a pull when clicked. Add a bookmark or a note below the table, and edit
the session name above it.

**Furthest phase** and **Phase confirmations** show saved phase observations
when available. Confirmation times measure the first observed confirming event
after pull start. An earlier phase established by a later observation has no
invented confirmation time. Interrupted recordings retain their observations.

Automatic UMAD phase tracking is awaiting verified combat recordings. New UMAD
pulls currently show **Not recorded** with that explanation. Older pulls also
show **Not recorded**, and duties without phase support show **Not supported**.
If phase data cannot be read, notes and death recaps remain available.

**View death recaps** opens the selected pull's observed deaths in Death Recap.
Recaps save as deaths arrive, so they remain available for an interrupted pull
after a crash. Death messages received within two seconds after combat ends or
a wipe can still attach to that pull. A new pull, disconnect, duty change, or
session end closes that association. Starting a session during combat does not
save recaps from the partial attempt being skipped.

**End session** stops collection without resetting the live meter or callouts.
Leaving the duty ends the session too. Wipes and breaks stay in the same session.
A disconnect preserves the observed attempt as interrupted and waits for the
next full pull after reconnecting. Interrupted attempts stay visible but do not
count toward the longest complete pull. **Combat ended** does not mean a clear.

Sessions save to `prog_sessions/` in the program's writable data directory,
independently of the DPS recording switch. Notes save after a short typing pause
and flush on shutdown. Active sessions also save elapsed time and observed pull
progress every 15 seconds between other saves. Crash recovery retains progress
through the last successful save. The session picker opens previous sessions after restart.
An unfinished session from a crash is marked interrupted. Files that cannot be
read are preserved and the page shows the error. Session history does not follow
the DPS log rotation limits.

Each death recap has its own file under `prog_sessions/recaps/`, grouped by the
session and pull IDs. Recaps do not enlarge the session summary file or follow
DPS log rotation. Failed writes are retried when session changes are flushed.
The retry queue retains the latest 256 unsaved recaps and reports any losses if
storage remains unavailable beyond that limit. Storage must recover before those records can
survive closing the program. A death without observed damage remains reviewable
as an interrupted attempt if the meter reports an empty encounter, including a
death received within two seconds after combat ends or a wipe.

## Profiles

Open **Profiles** at the bottom of the Triggers tab to save and restore named
setups for jobs, groups, or strategies. **Default** starts selected and represents
your normal saved setup. Edits made while Default is active are kept through the
normal trigger editor. Apply a named profile to use a different setup, then
select **Default** and press **Apply** to return to your normal choices.

**Save new** captures the current local trigger
toggles and spoken text, engine callout toggles, and editable Triggevent and
Triggernometry wording. **Update** replaces the selected snapshot
with the current setup.

**Delete** removes the selected named profile after confirmation. Deleting the
active profile returns to Default, so it must wait until combat ends. Default
cannot be deleted or overwritten with **Update**. The last applied profile and
your separate Default setup survive restarting the program. Selecting a name
or saving a new profile does not change the active setup until you press **Apply**.

Select a profile and press **Apply** between pulls. Existing trigger
definitions, folders, and newly added triggers are preserved. Applying a profile
does not switch the Cactbot mode, voice, or automarkers. Deleted local triggers
are not recreated. Changes made afterward use the normal trigger editor and are
only copied back into the profile when you update it. Profiles are stored in
`trigger_profiles/` alongside the program's other writable data.

## Automarkers tab

Places FFXIV head-sign markers like attack, bind, ignore, and shapes through the [Telesto](https://github.com/paissaheavyindustries/Telesto) Dalamud plugin's local HTTP API. Rules are fight + debuff -> marker, marking either **me** or **whoever gets the debuff** by party slot. Marking another player needs a live party, and an unknown slot is skipped rather than guessed.

- **Connection** - the Telesto URL, default `http://localhost:45678/`, **Enable automarkers**, and **Test mark (on me)** to prove the pipeline before enabling anything.
- **Rules** - rules seed with **no marker assigned** and never fire that way. Pick a rule, choose a sign next to **Marker**, and it is armed. Pick *(unassigned)* again to disarm. **Load UMAD preset** seeds rules for every known Dancing Mad Ultimate player debuff, **Clear all party marks** sends a full clear, and **Remove the mark when the debuff falls off**, the auto-cleanse option on by default, drops each sign the moment its debuff is cleansed or expires.
- **UMAD** - two dedicated toggles that run their own sequencers rather than plain rules. The **black-hole chains** give one roaming sign per cleanse queue in P3, DPS, supports, and the Accretion pair, each with its own marker picker. The **Cursed Shriek gaze pairs** split P4's look-away and look-at signs by debuff timer. Both suspend the overlapping plain rules while on so two systems never fight over one sign. Debuff IDs and the reasoning behind each rule: [UMAD-DEBUFFS.md](UMAD-DEBUFFS.md).

---

## Alert sound

Under **Settings - Alert Sound**, play a sound when an alert fires. Three built-in sounds; Ding, Alert, and Coin, or **Import SFX** to add your own `.wav` as a named reusable sound. Imported files copy into a user sounds folder, so they work on frozen installs and survive updates. You can also point at any `.wav` directly. Each has its own **Volume**, a choice of every alert vs urgent only, and a **Test** button. The Volume slider is perceptual, so 100% is full, 50% is about -20 dB, 0% is silent, default 50%, then scaled by the master volume. It plays on its own channel alongside the spoken callout.

---

## In-game display

Timeline bars and callouts are drawn inside the game by a separate Dalamud plugin, [NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay). NyaaTriggers still does the thinking and speaking, and the plugin only draws what it is told. It is optional, and callouts are spoken with or without it.

**Setting it up:**

1. Install the plugin. Its README has the repository link you paste into Dalamud.
2. In the game, type `/nyaa` to place the boxes, then tick **Lock**.
3. In NyaaTriggers, **Settings - In-Game Overlay** should show *Connected*.

The two connect over a loopback socket and can start in either order, so it does not matter whether you launch the game or the program first. The link is always on. When the plugin is not there yet **Settings - In-Game Overlay** reads *Waiting for the game plugin*, and it connects on its own the moment the game comes up.

NyaaTriggers used to draw its own overlay window: a transparent Qt window composited by gamescope on Linux, an always-on-top window on Windows. Both were ways around the fact that a separate process cannot draw inside the game, and neither worked the same way twice. Dalamud can, on every platform, with no compositor setup.

---

## Trigger fields

| Field | Description |
|---|---|
| Log Type | ACT network event type: `20` = cast start, fires the moment the cast bar appears and gives the earliest warning. `21` = single-target ability, `22` = AoE ability, `23` = cancelled cast, `26` = status effect gained (GainsEffect), `30` = status effect lost (LosesEffect), `00` = chat/dialogue line, match it with Ability Regex. |
| Ability ID | Hex ID from ACT field [4]. Pipe-separate multiple: `9494\|9495`. Takes priority over regex. |
| Ability Regex | Pattern matched against the ability name when no ID is set. |
| TTS Text | Spoken callout. Use `{source}`, `{target}`, or `{count}` for the status stack count. The ▶ button speaks a preview. |
| Applies to | For status effects, types 26/30, whose effect fires the trigger. **You** = the debuff is on you, boss to you, the default. **Target - you applied it** = a debuff/DoT/buff *you* keep up on the enemy, e.g. Reaper's Death's Design, with `{target}` as the enemy. **Anyone** = no source or target filter. |
| Reapply warning | For **GainsEffect (26)** only: speak this many seconds *before* the effect runs out instead of when it lands, a reapply soon reminder. It reads the effect's own duration, re-arms each time you refresh it, and cancels if it drops early, you change zone, or the instance resets. `0` = speak immediately on apply. When set the **Cooldown** becomes the minimum gap between reminders, so an AoE DoT landing on a whole pack at once still calls out just once instead of once per enemy. |
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

Most features live in their own **Settings** sections.

- **Program** - **Check for Updates** and a startup auto-check, see [Updating](#updating). **Update Triggevent Engine** downloads the prebuilt engine jar on source installs. Links to **GitHub** and **Discord**. **Language** picks **Automatic** which follows your OS, English, or 日本語, and takes effect on restart. Under a Japanese UI the trigger editor, the Current Instance tab, the Automarkers tab, and the built-in callouts are translated, and the trigger list shows Japanese names and callouts with search matching either language. Translations are machine-assisted and a work in progress, and untranslated text falls back to English.
- **Connection** - **Auto-connect on startup**, and **My character**, used to scope status triggers. Those set to **You** fire only when the effect is on you, and **Target - you applied it** only for effects *you* cast like Death's Design. It auto-fills from the game as soon as you connect and again on login or zone-in. Edit it if it's wrong.

---

## Personal triggers

Your additions, edits, and deletions live in `triggers.local.json`, and the bundled `assets/triggers.json` stays read-only. Your local file is gitignored, so updates never remove yours and your local set merges back automatically on startup.

Under **Settings - Data**:

- **Update Triggers** - pull a fresh bundled `assets/triggers.json` from GitHub for new fight coverage, no effect on your local set
- **Restore from Repo** - re-download the bundled set if it gets out of shape. Your custom and edited triggers are kept
- **Save log…** - export the full captured combat feed to a text file for debugging
- **Export** / **Import** - save your local triggers and folders to a file, or load them back. Import replaces your current local set

---

## Updating

NyaaTriggers can update itself. It quietly checks GitHub for a newer release on startup, toggle in **Settings - Program**, or press **Check for Updates** any time. When one exists, a banner offers **Install**, **Release notes**, and dismiss. Nothing downloads until you click Install and confirm. The program restarts when done, and your personal triggers and settings are never overwritten.

Updates come from the main channel.

How the install is applied depends on how you run it:

- **Git clone** - runs `git pull --ff-only --tags` (tags too, so the version label follows the rolling tag the checkout sits on), then installs any pip requirements the pull brought in with `pip install -r requirements.txt` using the same Python that runs the program, then restarts. Clones are told about every rolling release, one per commit to main. A successful install or dismissing the banner snoozes that release until a newer one appears. Local edits to tracked files that block the pull are reported so you can update manually.
- **Source copy without git** - can't update itself in place, so the banner's button is **Download**, opening the releases page. These copies are told about every rolling release, one per commit to main, and dismissing the banner or clicking Download snoozes that release until a newer one appears.
- **Linux release**, the `.tar.gz` - downloads the new build and swaps the program files in place, preserving settings, local triggers, and timelines beside the program, then restarts.
- **Windows**, the `.zip` - downloads and installs the update, then closes and reopens to finish. Because Windows locks a running program's own files, a staged fresh copy completes the swap, with a full backup and an automatic rollback if the new build won't start.
