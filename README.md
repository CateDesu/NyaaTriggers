# NyaaTriggers

An FFXIV trigger manager. It connects to [IINACT](https://github.com/marzent/IINACT) and speaks callouts when configured abilities appear in the combat log.

There is a live DPS meter parsed by the program itself, which includes DPS logs, party automarkers via the Telesto plugin, death recap and prog log. If you'd like other languages, feel free to create an issue. Currently only ENG and JP are supported.

![NyaaTriggers showing the Triggers tab and collapsed Profiles section](docs/images/triggers-tab.png)

**Platform:** Linux · Windows  ·  **[Full guide](docs/GUIDE.md)**  ·  [Changelog](CHANGELOG.md)  ·  [Discord](https://discord.com/invite/TQJrbZcgKF)

[Installation](#installation) · [Connection](#connecting-to-iinact) · [Callouts](#choosing-callouts) · [Triggernometry](#triggernometry-packs) · [DPS and prog](#dps-death-recaps-and-prog) · [Profiles](#profiles) · [Updating](#updating-and-saved-data)

---

## Installation

### Windows

Download `NyaaTriggers-windows.zip` from the [latest release](https://github.com/CateDesu/NyaaTriggers/releases/latest), extract the whole folder somewhere writable, and run `NyaaTriggers.exe` inside it. Keep the `_internal` folder beside the executable.

Release builds include Python, the English Piper voice, cactbot's browser runtime, and the Triggevent and Triggernometry engines. Java is bundled for Triggevent. Triggernometry uses Windows' .NET Framework, so it needs no Mono installation.

### Linux packaged build

Download `NyaaTriggers-linux.tar.gz` from the [latest release](https://github.com/CateDesu/NyaaTriggers/releases/latest) and extract it somewhere writable:

```bash
tar -xzf NyaaTriggers-linux.tar.gz
cd NyaaTriggers
./NyaaTriggers.sh
```

Use `NyaaTriggers.sh` for shortcuts too. The launcher can recover a missing runtime folder after an interrupted update. Python, the English Piper voice, cactbot's browser runtime, and Triggevent's Java runtime are bundled.

### Linux engine dependencies

Triggernometry needs a system Mono installation, including Windows Forms, for both packaged and source runs. Xvfb lets both engines run their hidden interfaces on a virtual display. Without Xvfb they use your session's X display.

Arch / CachyOS, using the distribution's [Mono package](https://archlinux.org/packages/extra/x86_64/mono/):

```bash
sudo pacman -S --needed mono xorg-server-xvfb xorg-xauth
```

Debian / Ubuntu, using [Mono development packages](https://packages.ubuntu.com/noble/mono-devel):

```bash
sudo apt update
sudo apt install mono-devel libmono-system-windows-forms4.0-cil libgdiplus xvfb xauth
```

Fedora, using [mono-complete](https://packages.fedoraproject.org/pkgs/mono/mono-complete/) to include Windows Forms:

```bash
sudo dnf install mono-complete xorg-x11-server-Xvfb xorg-x11-xauth
```

On Bazzite, install those Fedora packages inside a `distrobox` or `toolbox` and launch NyaaTriggers there, or layer them with `rpm-ostree install` and reboot.

### Linux from source

Use Python 3.11 or newer. On Arch, CachyOS, Debian, or Ubuntu:

```bash
git clone https://github.com/CateDesu/NyaaTriggers
cd NyaaTriggers
bash setup.sh
python3 main.py
```

`setup.sh` installs PyQt6, WebSocket support, regex, and `aplay` through pacman or apt. First launch downloads the English voice and installs Piper into `~/.venv/ffxiv`.

Optional engines need their own dependencies:

| Engine | Source setup |
|---|---|
| Triggevent | Install Java 17, then use **Settings - Program - Update Triggevent Engine** to download the prebuilt engine on a fresh checkout. Building it yourself needs JDK 17 and Maven. See the [engine instructions](triggevent-core/README.md#build-from-source-developers-only). |
| Triggernometry | Install the [Linux engine dependencies](#linux-engine-dependencies). The prebuilt engine is already included in the checkout. |
| Cactbot | Install PyQt6-WebEngine and restart. On Arch / CachyOS: `sudo pacman -S python-pyqt6-webengine`. For a pip environment: `python3 -m pip install -r requirements.txt`. |

The normal source setup does not require cloning or rebuilding either engine's repository. Program code lives in `nyaatriggers/`, interface code in `nyaatriggers/ui/`, and `main.py` remains the entry point.

## Connecting to IINACT

1. Install and enable IINACT in Dalamud using its [installation guide](https://www.iinact.com/installation/), then log into your character.
2. In NyaaTriggers, set **WebSocket** to `ws://127.0.0.1:10501/ws`, or your configured IINACT address, and press **Connect**.
3. Open **Current Instance** to check that the zone and combat log are arriving. **My character** in **Settings - Connection** fills automatically and can be corrected there.
4. Enable **Auto-connect on startup** in that same section if wanted. After connecting, the program retries a lost connection automatically.

The IINACT connection supplies the combat feed. Telesto and the companion overlay have their own connections, described below.

## Choosing callouts

The **Triggers** tab groups fights by content type and expansion. Search spans all fights. Expand a source group to inspect its rows, and use the checkboxes to choose which callouts can fire.

| Source | What it runs | Controls |
|---|---|---|
| Local | Bundled and custom triggers, status reminders, and follow-up sequences | Edit the full trigger, including its ability, zone, spoken text, sound, and timing. |
| Triggevent | The bundled engine's callouts, EasyTriggers, and user Groovy scripts | Toggle individual callouts and edit their spoken wording. |
| Triggernometry | Imported XML packs, including conditions and scripts | Toggle individual triggers and edit their spoken wording. See [pack support](#triggernometry-packs). |
| Cactbot | The raidboss engine's callouts and timelines | Enable under **Settings - Cactbot**. Individual callouts can be muted, but their engine logic is not editable here. |

Engine callouts are enabled unless you mute them, including newly discovered callouts. Local rows keep their own saved on/off choices. **Global - Local On/Off** and **Global - Triggevent On/Off** affect that source across all fights. The **Local** and **Triggevent** checkboxes above the table affect only the selected fight. Enabling overlapping sources can produce duplicate calls.

Cactbot is an alternative to Local, Triggevent, and Triggernometry callouts. Turning it on switches those off; turning it off restores editable callout mode. Its one switch also selects cactbot timelines. Dungeon timelines ship with the program, while other cactbot timelines download and cache as needed. With Cactbot off, local timelines are used.

Right-click a line in **Current Instance** to create a trigger from its ability ID and zone. **Test Fire** previews a selected row. The toolbar's **Reset to Default** clears trigger checkmarks while keeping definitions and edited wording.

## Triggernometry packs

Download a Triggernometry XML export, such as a pack from the [Paissa repositories](https://github.com/paissaheavyindustries/Triggernometry-Triggers/tree/main/Repositories), then choose **Settings - Data - Import Triggernometry**. The program copies the XML into its pack folder and loads it with the real Triggernometry engine. Imported triggers appear under **Triggernometry** in the fight list.

The engine runs conditions, shared variables, delayed actions, trigger chains, and C# scripts. It supplies formatted ACT logs to Log triggers and raw network logs to FFXIVNetwork triggers. When the engine is unavailable, the importer can only convert simple ability matches with plain speech into Local rows.

Packs can also receive Telesto memory notifications and draw Telesto lines, circles, and beams. Set **Telesto URL** in the **Automarkers** tab to the running plugin's address. Callback addresses are configured automatically. Memory subscriptions and drawings run with Triggernometry in editable callout mode; pack commands and macros also require **Enable automarkers**. Stopping the engine cleans up its subscriptions and drawings.

Replay checks cover TOP headmarkers, Party Synergy weapon calls, Pantokrator drawings, and Zelenia's Bloom sequence. Live fight validation is still pending. Legacy Triggernometry auras, direct game-memory access from C# scripts, and ACT combat-state or encounter-duration hooks are not supported. Memory offsets used through Telesto must match the game version. See the [engine notes](triggernometry-core/README.md) for details.

## DPS, death recaps, and prog

| Tab | What it records |
|---|---|
| **DPS** | Live DPS, damage share, HPS, crit and direct hit rates, max hit, and deaths. Pets merge into their owners. **Recent pulls** lets you review attempts from the current run. |
| **Death Recap** | The 15 seconds of observed damage, healing, and status changes before a death. The recent view keeps the latest 80 deaths until the program closes. |
| **Prog** | Named duty sessions with pull durations, endings, death counts, a duration chart, bookmarks, notes, and saved death recaps. Previous sessions can be reopened after restart. |

The live meter works whenever the combat feed is connected. **Record encounters**, off by default in DPS, saves complete pull summaries to `dps_logs/`. **Reset display after** changes how the live meter handles downtime; recorded summaries keep the whole pull. DPS logs rotate automatically, with up to five completed logs plus the active log.

In Prog, choose **Start session** once the duty and combat state are known. Starting during combat waits for the next full pull. Select a recorded pull and choose **View death recaps** to inspect that attempt's deaths, including after a restart. Prog sessions and their recaps save independently of **Record encounters** and are not removed by DPS log rotation. Interrupted attempts remain identified separately, and **Combat ended** does not mean a clear.

Phase details are available for saved observations, but automatic UMAD phase detection is still waiting for verified combat recordings. New UMAD pulls show **Not recorded**. Death recaps describe events observed in the feed and cannot reconstruct exact HP or effects missed before connection.

**Settings - FFLogs** can show your best recorded rDPS after a fight when you supply your personal API client details, server, and region. **IINACT Logs** opens the raw log folder for an FFLogs uploader. The program's DPS summaries are separate from those uploadable logs.

## Profiles

Expand **Profiles** at the bottom of the Triggers tab to save setups for different jobs, groups, or strategies. **Save new** captures trigger checkmarks and callout wording. Select a profile and press **Apply** between pulls to activate it, or **Update** to replace its saved snapshot with your current choices.

**Default** preserves your normal setup separately. Select it and press **Apply** to return to those choices. The active profile and Default both survive a restart. Deleting the active named profile restores Default and must be done between pulls. Selecting a name or saving a new profile alone does not activate it.

Profiles preserve trigger definitions and newly added triggers. They do not change the voice, Cactbot mode, or automarkers.

## Voice and language

Choose **System** or **Piper** in **Settings - Voice** and use **Test TTS** to check playback. Windows defaults to the system voice; Linux defaults to offline Piper. The **Model** list includes the Japanese neural voices Alpha and Kumo, which download on first selection. Additional Piper voices can be added through **Open voices folder** using a model and its matching `.onnx.json` file.

**Settings - Program - Language** offers Automatic, English, and 日本語, with a restart to apply an interface change. Japanese callout translations are available separately, with English fallback for untranslated text. **Settings - Alert Sound** provides built-in sounds, volume controls, and **Import SFX** for your own `.wav` files.

## In-game display and automarkers

The optional [NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay) Dalamud plugin draws timeline bars, callouts, and the DPS meter inside the game. Install it using its README, then use `/nyaa` to position and lock the windows. **Settings - In-Game Overlay** shows the connection status. The link connects automatically, and its port must match the plugin's port. Speech works without the overlay.

Party automarkers use the separate [Telesto](https://github.com/paissaheavyindustries/Telesto) plugin. In **Automarkers**, set **Telesto URL**, use **Test mark (on me)**, then enable marking when ready. Rules can mark you or the party member who receives a debuff. Unassigned rules do not place a mark.

**Load UMAD preset** adds the known debuff rules for Dancing Mad Ultimate. Assign signs for your strategy. The tab also offers the P3 black-hole cleanse queues, P4 Cursed Shriek gaze pairs, automatic removal when a debuff falls off, and **Clear all party marks**. See the [automarker guide](docs/GUIDE.md#automarkers-tab) for the controls.

## Updating and saved data

Use **Settings - Program - Check for Updates**, or leave the startup check enabled. The update banner offers installation when a new release is available. Packaged builds replace the program files and restart while preserving saved data. Git checkouts pull main with fast-forward updates and install changed Python requirements. A source copy without Git opens the download page instead. See [Updating](docs/GUIDE.md#updating) for recovery and install details.

**Settings - Data - Update Triggers** refreshes the bundled trigger set without replacing your custom triggers. **Export Triggers** and **Import Triggers** transfer local definitions and folders; importing replaces the current local set. Profiles are saved separately.

Most writable data lives beside `main.py` for source runs or beside the executable for packaged builds:

| File or folder | Contents |
|---|---|
| `nyaatriggers_settings.json` | Connection, voice, language, engine callout choices, and other settings |
| `triggers.local.json` | Custom triggers, edited bundled triggers, folders, and saved overrides |
| `trigger_profiles/` | Named profiles and the separate Default setup |
| `dps_logs/` | Recorded DPS pull summaries |
| `prog_sessions/` | Saved sessions, notes, and per-pull deaths under `recaps/` |
| `pull_logs/` | Optional raw captures for engine replay |
| `voices/`, `sounds/`, `timelines/` | User voices, imported sounds, and local timelines |

Imported Triggernometry packs and engine diagnostics use the per-user configuration folder instead: `~/.config/nyaatriggers/` on Linux, or `%APPDATA%\nyaatriggers\` on Windows. Linux respects `XDG_CONFIG_HOME`. Packs live under `triggernometry-packs/`; logs are `triggevent.log` and `triggernometry-core/triggernometry.log`. Include this folder as well as the program's writable data when moving your setup.

If you use existing Triggevent EasyTriggers or Groovy scripts, keep your Triggevent user folder too, normally `~/.triggevent/`.

---

## Documentation

The **[full guide](docs/GUIDE.md)** covers the individual controls:

- [Triggers tab](docs/GUIDE.md#triggers-tab) - the editor, the fight tree, enabling callouts
- [Engine triggers](docs/GUIDE.md#engine-triggers) - cactbot, Triggevent, and Triggernometry rows, editing spoken text
- [Triggevent Engine](docs/GUIDE.md#triggevent-engine) - what it runs, building from source
- [Triggernometry engine](docs/GUIDE.md#triggernometry-engine-wip) - XML imports, scripts, Telesto callbacks, drawings, and current limitations
- [Voice](docs/GUIDE.md#voice) - System vs offline Piper, Japanese voices, adding voices
- [Alert sound](docs/GUIDE.md#alert-sound) - built-in chimes, importing your own, and volume slider
- [DPS tab](docs/GUIDE.md#dps-tab) - live meter parsed by the program, encounter recording
- [Death Recap tab](docs/GUIDE.md#death-recap-tab) - recent deaths and saved recaps from prog pulls
- [Prog tab](docs/GUIDE.md#prog-tab) - saved raid sessions, pull durations, death recaps, bookmarks, and notes
- [Profiles](docs/GUIDE.md#profiles) - named setups, Default, applying, updating, and deleting
- [Automarkers tab](docs/GUIDE.md#automarkers-tab) - party marks via the Telesto plugin, the UMAD preset
- [In-game display](docs/GUIDE.md#in-game-display) - drawing bars and callouts in the game via the plugin
- [Current Instance tab](docs/GUIDE.md#current-instance-tab) - live log, making triggers from it
- [Trigger fields](docs/GUIDE.md#trigger-fields) - every field in the editor
- [Settings](docs/GUIDE.md#settings) - language, callout translation, and connection
- [Personal triggers](docs/GUIDE.md#personal-triggers) · [Updating](docs/GUIDE.md#updating) - your data, kept across updates
- [Requirements](docs/GUIDE.md#requirements) - Python, voice, and optional browser dependencies
- [Tests](tests/README.md) - running the full suite or selected checks
- [Planned work](docs/TODO.md) - completed features and next steps

For a callout problem, **Settings - Data - Save log…** exports the captured combat feed. **Settings - Connection - Record pulls to pull_logs for engine replay** can capture future attempts for replay. Include the fight, missing callout, approximate time, and relevant log when opening an [issue](https://github.com/CateDesu/NyaaTriggers/issues) or asking in [Discord](https://discord.com/invite/TQJrbZcgKF).

---

## License

MIT. Bundled engines and dependencies retain their own licenses.
