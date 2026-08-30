# NyaaTriggers Guide

The full reference for NyaaTriggers. Install steps are in the [README](../README.md).

- [Requirements](#requirements)
- [Voice](#voice)
- [Triggers tab](#triggers-tab)
- [Engine triggers](#engine-triggers)
- [Triggevent Engine](#triggevent-engine)
- [Triggernometry engine](#triggernometry-engine)
- [Current Instance tab](#current-instance-tab)
- [DPS tab](#dps-tab)
- [Automarkers tab](#automarkers-tab)
- [Alert sound](#alert-sound)
- [In-game display](#in-game-display)
- [Trigger fields](#trigger-fields)
- [Settings](#settings)
- [Personal triggers](#personal-triggers)
- [Updating](#updating)

---

## Requirements

The Windows and Linux release builds bundle everything below. This table applies only when running from source on Linux.

| Requirement | Notes |
|---|---|
| [IINACT](https://github.com/marzent/IINACT) | Running and connected to the game |
| Python 3.11+ | System Python is fine |
| PyQt6 | `sudo pacman -S python-pyqt6` / `sudo apt install python3-pyqt6` / `pip install PyQt6` |
| piper-tts | Installed automatically on first launch into `~/.venv/ffxiv` |
| Audio backend | `aplay` via `alsa-utils` |
| PyQt6-WebEngine | **Optional** on source installs, only for the cactbot source in **Settings - Cactbot**. `sudo pacman -S python-pyqt6-webengine` or `pip install PyQt6-WebEngine`. The rest of the program works without it. The packaged release builds bundle it. |

---

## Voice

Pick the backend in **Settings - Voice - Engine**:

- **System** - your OS voice, Windows SAPI or Linux `spd-say` / `espeak`. No download or extra dependency. **Default on Windows.**
- **Piper** - fully offline neural TTS via [Piper](https://github.com/OHF-Voice/piper1-gpl). **Default on Linux.** The `en_US-arctic-medium` model downloads automatically on first launch into `voices/`.

**Adding Piper voices:** browse the [voice samples](https://rhasspy.github.io/piper-samples/), download a `.onnx` model and its matching `.onnx.json` config, and drop both in the `voices/` folder. **Settings - Voice - Open voices folder** opens it. Pick the voice under **Model**, and hit **Refresh list** or restart if you added it while open. Medium voices are ~65 MB, low ~30 MB.

**Japanese voices:** the **Model** dropdown also lists the neural Japanese voices Alpha and Kumo next to the English one. Pick one and callouts are spoken in Japanese. The program downloads the voice and sets it up on first pick, about 330 MB, and it reads kanji. Until it is ready Japanese falls back to espeak.

**Test TTS** speaks a sample with the current voice. On Linux source installs, **Piper venv** sets the path to the venv holding piper-tts, default `~/.venv/ffxiv`.

---

## Triggers tab

The trigger editor and your trigger set, bundled plus custom. The live combat log is a separate **Current Instance** tab.

**Master switch.** **Triggers: ON/OFF** runs your editable triggers: **Local**, the **Triggevent** Engine, and the **Triggernometry** engine once you've imported a pack. **Cactbot** is separate and **mutually exclusive** - enabling it (in **Settings - Cactbot**) turns this switch off and vice versa, since cactbot isn't editable and would just double up. That one Cactbot switch is also all there is to timelines: on, and the current fight's timeline bars come from cactbot's own `.txt` files (dungeon timelines ship with the program, the rest are downloaded and cached on demand) alongside its callouts; off, and your Local triggers and timelines stand alone.

**Everything ships off.** Bundled Local triggers and engine callouts all start disabled, including each new patch's additions, so a fresh install stays silent until you opt in. Local triggers are zone-locked. A trigger tagged M4S only fires inside that zone.

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
- **Triggernometry** callouts list as editable rows too. Edit the spoken text or toggle them per trigger, the same as Triggevent. See [Triggernometry engine](#triggernometry-engine).
- **Test TTS** from the right-click menu or the ▶ button in the edit dialog speaks a callout with sample token values.
- **Cactbot** callouts can't be edited at the engine. Uncheck the row to silence one, and check it again to bring it back. Cactbot's on/off, per-trigger overrides, and the page **URL** live in **Settings - Cactbot**. The URL defaults to the hosted raidboss build, so point it at a local build only if you bundle one. Cactbot needs PyQt6-WebEngine. The packaged release builds bundle it, and source installs need the package from the Requirements table.

Simple cast-based engine triggers are bundled as editable Local triggers instead. The complex imperative and Groovy ones stay in the engine and show here.

---

## Triggevent Engine

Triggevent runs headless against the same IINACT feed and speaks **every** callout it produces: built-in, EasyTriggers, and your Groovy scripts in `~/.triggevent`, including the code-based ones that can't become Local triggers. There's no separate on/off. It follows the master **Triggers** switch.

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

Triggernometry runs its **real engine** headless so its complex *scripted* triggers fire 1:1, the imperative C# and `ExecuteScript` ones that can't reduce to Local triggers, the way the [Triggevent Engine](#triggevent-engine) does for Groovy. There's no separate on/off. An imported pack follows the master **Triggers** switch, and the engine only starts once you've actually imported a pack.

**Importing a pack.** Point **Settings - Data - Import Triggernometry** at a Triggernometry `.xml` export. The whole pack runs through the engine and lists under its own **Triggernometry** section as editable rows. Edit the spoken text or toggle them per trigger, just like Triggevent. On a build without the engine the simple triggers, a literal ability ID plus a plain text-to-speech line, fall back to editable **Local** rows instead.

**How it runs.** A headless .NET sidecar (`triggernometry-core`) hosts the real engine and routes its callouts to NyaaTriggers' voice and the optional companion overlay plugin. It's cross-platform .NET Framework 4.6.2. **Windows runs it natively**, and **Linux runs it under Mono** with `sudo pacman -S mono`. Release builds bundle the prebuilt sidecar, so there's nothing to build to use it.

**Building from source.** The sidecar's prebuilt binaries are vendored in `triggernometry-core/bin/` and used as-is, so a normal source run needs nothing extra. To rebuild it, only needed if you change the host or bump the pinned engine, run `triggernometry-core/build-all.sh` with **Mono 6.12+** installed. It clones the engine at a pinned commit, applies the shims, and rebuilds `bin/`. See `triggernometry-core/README.md` for the design.

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
- With the companion overlay plugin connected the live meter is also drawn in the game, top 8 players plus party DPS, once a second while a fight runs. Needs a plugin build that understands the `dps` frame.

---

## Automarkers tab

Places FFXIV head-sign markers like attack, bind, ignore, and shapes through the [Telesto](https://github.com/paissaheavyindustries/Telesto) Dalamud plugin's local HTTP API. Rules are fight + debuff -> marker, marking either **me** or **whoever gets the debuff** by party slot. Marking another player needs a live party, and an unknown slot is skipped rather than guessed.

- **Connection** - the Telesto URL, default `http://localhost:45678/`, **Enable automarkers**, and **Test mark (on me)** to prove the pipeline before enabling anything.
- **Rules** - rules seed with **no marker assigned** and never fire that way. Pick a rule, choose a sign next to **Marker**, and it is armed. Pick *(unassigned)* again to disarm. **Load UMAD preset** seeds rules for every known Dancing Mad Ultimate player debuff, **Clear all party marks** sends a full clear, and **Remove the mark when the debuff falls off**, the auto-cleanse option on by default, drops each sign the moment its debuff is cleansed or expires.
- **UMAD** - two dedicated toggles that run their own sequencers rather than plain rules. The **black-hole chains** give one roaming sign per cleanse queue in P3, DPS, supports, and the Accretion pair, each with its own marker picker. The **Cursed Shriek gaze pairs** split P4's look-away and look-at signs by debuff timer. Both suspend the overlapping plain rules while on so two systems never fight over one sign. Debuff IDs and the reasoning behind each rule: [UMAD-DEBUFFS.md](UMAD-DEBUFFS.md).

---

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

Your additions, edits, and deletions live in `triggers.local.json`, and the bundled `triggers.json` stays read-only. That file is gitignored, so updates never remove yours and your local set merges back automatically on startup.

Under **Settings - Data**:

- **Update Triggers** - pull a fresh bundled `triggers.json` from GitHub for new fight coverage, no effect on your local set
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
