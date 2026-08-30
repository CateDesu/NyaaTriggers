# Changelog

## Unreleased

## v1.4.0 - 2026-08-30

### Added
- **A pull never runs with an empty timeline anymore.** After a mid-instance app restart the zone's timeline could stay unloaded - the zone replay resolves and loads it, but if that load missed, nothing retried (the 30 s re-detect only reacts to a fight *change*), so timeline callouts and overlay bars silently went missing for the rest of the session. The app now re-arms the timeline from the current zone the moment a pull starts with an empty schedule.
- **Automark signs clear themselves when the debuff falls off.** With the new **Remove the mark when the debuff falls off (auto-cleanse)** checkbox in the Automarkers tab (on by default), a sign placed by an automark rule is removed the moment the status that placed it is cleansed or expires - the UMAD Accretion pair's marks drop the instant they are healed to full, no manual clear. A loss only ever clears the sign its own rule placed, and signs owned by the black-hole chains or the gaze pairing are untouched.
- **Live DPS meter, parsed by the app itself.** The DPS tab is a single live meter: per-player DPS, damage share, HPS, crit, direct hit and crit+DH rates, max hit and deaths, updating every second, with the encounter title, duration and party DPS on top. Numbers come from a new built-in parser (`dps_meter.py`) that mirrors ACT's encounter/combatant aggregation straight off the combat log lines (abilities, DoT/HoT ticks, deaths, wipes), merges pets into their owners like ACT, and starts on the pull even if you connect late. Fights begin and end with ACT's own in-combat flag, or on a wipe or zone change. The on-screen meter pauses after a damage gap (dropdown on the tab: 15s to 10m, default 2m), holds those numbers, and resets to a fresh segment when damage resumes - the recorded log always captures the whole pull, downtime included. A finished pull stays frozen on screen until the next pull starts. With **Record encounters** on, each finished pull is appended to the active log in `dps_logs/` (JSONL, one line per pull, fights mixed like ACT's log files). A log is full after 25 pulls of one fight or 5 distinct fights and rolls over, and once 5 full logs sit in the folder the oldest are culled. The meter is always on.
- **DPS on the in-game overlay.** The companion Dalamud plugin now also receives the live meter (up to 24 players plus party DPS, once a second while a fight runs, hidden when it ends) via a new `dps` frame, so alliance raids show everyone. Sent automatically whenever the game plugin is connected. Needs a plugin build that understands the new frame.
- **FFLogs section in Settings.** An **IINACT Logs** button opens the folder IINACT writes its raw `Network_<build>_<date>.log` day files into, the ones an FFLogs uploader takes. The location is read from IINACT's own plugin config and mapped into the wine prefix on Linux. A note says so when no IINACT install is found.
- **The FFLogs best-parse comparison can be turned on in Settings.** The DPS tab's FFLogs line and refresh button read API credentials (`fflogs_client_id`, `fflogs_client_secret`, `fflogs_server`) that no screen ever wrote, so the comparison stayed hidden unless the settings file was hand edited. Settings - FFLogs now asks for the Client ID, Client secret, Server and Region from a personal API client on your fflogs.com profile page, saves them on focus-out, and shows the comparison as soon as id, secret and server are filled. The compared character falls back to the **My character** field.
- **Timeline bars for every fight cactbot covers, not just 37.** With **Cactbot** on, entering a fight with no local trigger file (P1S was the test case) sent an empty timeline to the plugin, because only 37 hand-mapped fights could resolve a cactbot timeline. The app now ships a generated index (`cactbot_timelines.json`, rebuilt with `tools/gen_cactbot_timelines.py`) of every zone id cactbot has a timeline for - 310 fights - keyed on the numeric zone id, so it works whatever language your client speaks. Every dungeon timeline ships inside the build, so those bars work on a fresh install with no download at all, and the shipped copies come along with every update too. Raids and trials still download on demand and cache for a week. Downloads land in a separate cache file next to the program, never over the shipped copies, so a source checkout's tracked files are never rewritten in place. Your own `<Fight>.txt` files are never touched, and still serve while a download is in flight or for fights cactbot does not cover.
- **Japanese localization (draft).** Set **Settings - Language** to 日本語 (or leave it on Automatic to follow your OS). The interface, the trigger editor, the Current Instance tab, the Automarkers tab, and the built-in trigger callouts are translated, including callouts from the Triggevent / cactbot / Triggernometry engines. The trigger list itself is translated too: under a Japanese UI the **Name and TTS columns** show the Japanese trigger names and callouts (search matches either language, and engine rows translate where their text is known). Callouts show natural kanji on-screen but are **spoken from a kana reading**, so offline voices (like espeak) that can't pronounce kanji speak them correctly instead of reading "Chinese letter". Japanese text auto-routes to a Japanese voice where one is installed. For much better quality than espeak, the **Model** dropdown in Settings - Voice now lists in-app neural Japanese voices next to the English one - pick a Japanese voice and callouts speak in Japanese (the app downloads and sets it up on first pick, no manual pip). It reads kanji. Until it is set up, Japanese falls back to espeak. Translations are **machine-assisted and a work in progress**. Untranslated text falls back to English, and corrections are welcome via the repo. Callout translations live in `callouts_ja.json` (built from `tools/callout_phrases_ja.json`); UI strings in `lang/ja.json`.
- **Japanese voices on Windows.** The neural Japanese voices Alpha and Kumo now work on the Windows and Linux builds, not just source installs. Pick one in the Model dropdown in the Voice settings. The app downloads the voice on first pick and keeps it after updates.
- **Callouts are drawn in the game again, by a companion plugin.** [NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay) is a Dalamud plugin that draws the timeline bars and callouts through the game's own UI, on every platform, with none of the compositor setup the old overlay needed. Install it separately, then turn it on under **Settings - In-Game Display**. The two connect over a loopback socket and start in either order, so it does not matter whether the game or the app comes up first, and closing the game just reconnects later. Optional: callouts are spoken with or without it.
- **DPS tab.** Between Current Instance and Automarkers. Turn on **Record encounters** and the app writes one snapshot per fight to `dps_logs/` (per-combatant DPS, HPS and damage share, finalised when the fight ends); pick any recorded fight on the left to see its numbers on the right, with a live auto-refresh while a fight is running.

### Changed
- **One Cactbot switch, no separate timeline setting.** The **Use cactbot timelines with my own triggers** checkbox is gone. The Cactbot on/off in **Settings - Cactbot** is the single switch: on, and cactbot's own `.txt` timeline files drive the current fight's bars alongside its callouts; off, and your Local triggers and timelines stand alone. Flipping the switch re-resolves the current zone's timeline on the spot, so the bars follow immediately. An old saved `local_cactbot_timelines` setting is simply ignored.
- **Git installs now also update their Python dependencies with the app.** A `git pull --ff-only` brings the new code but never the pinned packages that code needs, so an environment created before a requirements.txt addition (websockets, for the plugin link, was the one that bit) stayed broken no matter how many updates it installed. After any pull that moves HEAD, the updater runs `pip install -r requirements.txt` with the interpreter running the app before the restart, and says so in the result message. A pip failure never fails the update: the message shows pip's error and asks for a manual `pip install -r requirements.txt`.
- **The in-game overlay link is always on and auto-detected.** The Settings section is now just the connection status plus a link to the plugin repo. The enable toggle and the two DPS switches are gone. Everything is on by default and simply works when the game plugin is there.
- **Settings tidy-up.** Check for Updates and Update Triggevent Engine sit under the version text instead of the top right.
- **The old per-boss DPS logger is superseded.** Encounter history is now written by the built-in meter from its own parse instead of OverlayPlugin's CombatData events. The dormant CombatData consumer (`dps_logger.py`) is no longer wired into the app.
- **The top tab bar is now a sidebar.** Triggers, Current Instance, DPS, Automarkers and Settings move into pill navigation on the left, per the Ink design in UI-REDESIGN.md, with drawn line icons (the active pill's icon goes coral); the brand block sits above the nav (a small んて "NT" mark over the NyaaTriggers wordmark and version), the master-volume control moves to the sidebar footer, and the Ink aurora gives way to sakura scenery: a procedurally grown cherry tree cut in half by each side of the window. A straight full-height trunk, branches near the top, one cloud of light-pink blossoms in the top corner, with matching light-pink petals drifting slowly down the window while the window has focus (they lock in place the instant focus leaves and pick up from there when it comes back). The active nav pill is a ghost. The scenery bleeds through a soft coral neon glow ring, its label glowing to match, and every other nav label is drawn with a dark halo stroke. A soft dark "frost band" gradient dims the blossoms right where the labels sit. The whole sidebar (wordmark, んて mark, version, nav labels) is set in the bundled **Kosugi Maru** font (Apache-2.0, license included under `fonts/`), and the kana and version marks moved from near-invisible dim gray to the same cool gray-blue as the inactive tabs. Ctrl+1 through Ctrl+5 jump between pages.
- **"Save log…" moved to Settings - Data.** It still exports the full captured combat feed to a text file. It used to sit on the Current Instance tab next to the raw panel.
- **The Automarkers tab is regrouped into Connection, Rules, and UMAD sections.**

### Removed
- **The in-game overlay is gone, and is being replaced by a companion Dalamud plugin.** Drawing callouts over the game from outside it never worked the same way twice: on Linux it needed FFXIV to be launched inside gamescope and silently did nothing otherwise, and on Windows it was an always-on-top window that only worked in borderless. Both were working around the fact that an external process cannot draw inside the game. A Dalamud plugin can, natively and on every platform, so that is where timeline bars and alert pop-ups are moving: **[NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay)**. Removed with it: the **In-Game Overlay** and **Overlay Appearance** settings sections (enable, per-window show/see-through/lock, geometry, colours, text size), the gamescope integration and its diagnostics log, and the `python-xlib` dependency. **Alert Sound** is unaffected and is now its own Settings section. Callouts are still spoken. Only the on-screen drawing is on hold until the plugin lands.
- **The Changes tab.** Release notes stay linked from the update banner.
- **The "Raw Log (Enemy)" panel on Current Instance.** The Easy-to-Read log (with its right-click trigger pre-fill) stays. The full raw feed is still exported via **Save log…**.
- **The "Save DPS logs (per boss)" checkbox.** The per-boss DPS logger is now dormant: still wired up in the code, but with no toggle it never writes.
- **The "Probably shouldn't have both on :3" label.**
- **Dead code from the fourth review.** The superseded per-boss DPS logger (`dps_logger.py`), unwired since the built-in meter took over encounter history, is deleted for real, along with the 261 entity-add pair parser nothing dispatched to anymore and the engine callout override writer whose UI the cactbot row menu cleanup already removed. Overrides saved by older builds still load, still apply, and can still be cleared from the row menu.

### Fixed
- **Git checkouts fetch tags with every update pull.** The updater's pull ran plain `git pull --ff-only`, and git only auto-follows tags for commits the pull actually downloads, so a maintainer's own checkout, which downloads none of its own commits, never received the rolling tags the pushed releases are cut from. The version label, derived from `git describe`, sat at the last fetched tag plus a commit count no matter how often the app updated. The pull now passes `--tags`, and the label lands on the real rolling tag after the next update.
- **The master trigger switch now gates the timeline too.** Turning triggers off left the timeline running: bars kept flowing to the overlay plugin and timeline entries kept speaking, since both timeline feeds and the speech slot were gated only on the local triggers setting. The switch now resets the schedule and clears the plugin bars on disable, re-pushes on enable, and every bar push and timeline speech checks both switches.
- **Cactbot timelines no longer speak twice.** With Cactbot on, its .txt timelines drive the bars through the local timeline engine, which also spoke every labeled entry on its own, doubling the callouts the cactbot reader was already speaking. Cactbot sourced schedules are bars only now and the reader speaks. Local timeline files still speak as before.
- **A git checkout no longer offers an update it already has.** Every push to main builds a rolling release whose tag always sorts above the checkout's base version, so the update banner showed right after your own push with nothing new to pull. The update check now asks git whether HEAD already contains the upstream branch tip and stays quiet when it does. Any doubt, offline or an unfetched tip, falls back to the old version comparison.
- **The sakura drift no longer pins a CPU core while the window has focus.** The petal timer repainted the entire window every frame, which restyled every panel at 30fps and ate a whole core. Each tick now repaints only the small strips the petals actually move through, and the drift itself runs at 20fps. Focused cost drops to about what Discord sits at idle, and it still parks instantly on focus loss.
- **The Triggevent Engine caps its Java heap at 512 MB.** Default JVM settings let it reserve a quarter of system RAM, which showed up as roughly 800 MB held by a small log parser. The first cut of the cap, 256 MB, was too tight on long Ultimate sessions: GC pressure stalled the event pump, sequential trigger chains timed out mid mechanic, and callouts silently dropped (a lost Tele-trouncing direction call was the giveaway). 512 MB keeps the cap while leaving the chains headroom.
- **Triggers work on a non-English game client.** The game reports zone names in your client's language, but every shipped trigger matches an English zone name, so on a French, German or Japanese client no zone-locked callout could ever fire. The engine sidecars were unaffected because they match on the numeric zone id, which is exactly why Local went quiet while Triggevent kept talking. The app now resolves the zone id to its English name (`zone_names.json`, regenerated with `tools/gen_zone_names.py`) and matches against both that and the name your client reports. The Current Instance banner shows both when they differ.
- **Six fights' triggers could never fire.** Their zone patterns matched no zone in the game, so 69 shipped triggers were dead on arrival: Queen EX, Enuo EX, Zelenia EX, Doomtrain EX (which pointed at Valigarmanda's arena), Zeromus EX and The Ridorana Lighthouse. All six now match their real duty. A test (`test_zone_patterns.py`) fails the build if a shipped zone pattern ever again matches nothing.
- **Callouts no longer go silent when you connect during a duty.** The zone is only announced when you enter it, so starting NyaaTriggers after zoning in left the app with no zone, and every zone-locked trigger was skipped for the whole fight without a word about it. Zone locking now applies only when the zone is actually known, and the Current Instance banner says so while it is not.
- **Every build ships the Triggevent Engine.** Packaging used to treat the engine jar and the bundled Java runtime as optional, so a build could silently come out with an empty Triggevent section and nothing on screen saying why. Packaging now refuses to produce a build without them, and CI checks both landed in the finished package. A source checkout is the one case that cannot bundle them, so it offers to download the engine on first launch instead.
- **The Triggevent Engine can be installed on a source checkout.** A `git clone` ships no engine jar, and the only way to get one was a full git plus Maven rebuild of upstream event-trigger, so testers got "Triggevent auto-update skipped: 'mvn' not on PATH" or "event-trigger clone / build script missing" and simply never had any Triggevent triggers. **Update Triggevent Engine** now downloads the same prebuilt, checksum-verified jar the packaged builds ship. The startup message points at that button instead of naming a tool you do not need.
- **An empty Triggevent section now explains itself.** With no engine jar, or a jar and no Java, the section was just empty. The top bar now says which of the two is missing.
- **Triggevent and Triggernometry engines start on Linux.** The packaged Linux build failed to launch the engine sidecars because a bundled library shadowed the system one, so `/bin/sh` crashed. The sidecars now run with the system libraries.
- **Character and instance now detected when connecting mid-session** (reported on Windows). The app learned your character name and current zone only from the `02`/`01` log lines, which the game feed emits at login / zone-in. Starting NyaaTriggers *after* logging in (the usual Windows order: game and ACT/IINACT first, NyaaTriggers last) left **My character** empty and **Current Instance** on "No instance" until the next zone change. Both ACT's OverlayPlugin and IINACT replay the cached `ChangePrimaryPlayer` / `ChangeZone` events (name and zone included) the moment a client subscribes. NyaaTriggers now consumes them, so the character and the current instance fill in immediately on connect. This also un-breaks self-scoped status triggers (**Applies to: You**) and zone-gated triggers/timelines for that first duty. Affected every platform; the Linux workflow (connect before logging in) just rarely hit it.
- **Clipped button labels.** Disconnect, Open voices folder, and Restore from Repo no longer cut off their text.
- **The Windows updater refuses to act as a file-copy proxy.** The `--apply-update` hand-off trusted its command line completely: any process able to launch the exe could point it at an arbitrary destination folder and have it copy and launch files there. The hand-off now only proceeds for a real staged update - a live old process, and a staging folder with the update prefix next to the install - and refuses anything else.
- **Catastrophic zone regexes can no longer hang the app.** Two call sites matched the per-trigger zone pattern with a raw regular-expression search, bypassing the compile guard and match timeout every other user pattern goes through: the fight tagger (runs on every zone change and on the 30 s re-detect timer) and the trigger editor's live zone check (runs on every keystroke). A catastrophic backtracking pattern, including one typed partway through, could freeze the window. Both now use the same guarded compile and timeout as everything else, and the editor refuses to save a zone regex the guard rejects.
- **Update robustness.** The free-space preflight checks both drives an update touches (staging unpacks next to the install, the swap copies land inside it) instead of only one. A rollback that cannot restore the previous files now logs the failure and drops a RECOVER.txt naming the backup folder to rename back, instead of silently leaving a broken install. The launch-time cleanup only deletes folders that actually look like updater staging, so a folder you named `.nyaa-update-*` yourself next to the install survives. The wait for the old process to exit no longer reads a `tasklist` error as "exited" and swaps files it still holds open. Release downloads have a hard size ceiling, so a missing or lying Content-Length can no longer fill the disk.
- **piper-tts is pinned.** Both setup paths (the install script and the in-app first-run setup) installed the latest piper-tts unpinned and unhashed; they now install the tested 1.4.2, matching how requirements.txt pins everything else.
- **The app log is owner-only.** `nyaatriggers.log` (crash traces and raw game log lines, player names included) was created world-readable under a typical umask; it is now created 0600 like the DPS logs, and an existing loose log is tightened on the next write.
- **Importing triggers keeps a backup.** Replacing your local triggers from a file now first copies the previous file to `triggers.local.json.bak`, and the success message says so.
- **A damaged local trigger file no longer cascades.** A `"deleted": null` or mixed-type entry in the tombstone list quarantined the whole local file, or made every later save fail without a word. The list is filtered on load, and a save can no longer be killed by bad in-memory data. A corrupted downloaded trigger override now falls back to the bundled copy instead of loading an empty list, and both corruption paths rotate their `.bad` backups with a cap instead of one fixed name or an unbounded loop.
- **Sequence steps understand combined log types.** A follow-up step authored with a pipe-separated type like `21|22` (the form the converters produce) could never match, and its callout was silently dropped on timeout; steps now match any of the listed types like top-level triggers do.
- **Smaller fixes.** The cactbot converter reads the `NetRegex.ability({...})` call form; the DPS meter's actor id parsing matches the Telesto copy (negatives and booleans rejected); the kokoro model download and the install script's voice download enforce the same size ceiling, and the install script skips voice files already on disk; the last unstopped single-shot timers are stopped on close; dialogs opened with exec() are deleted after use; sidecar inventory parse failures are logged instead of silent; stale `.part` files from interrupted update downloads are swept.
- **The In-Game Overlay link works in packaged builds.** The build pipeline never installed the websockets package, so every packaged build shipped without it and Settings just read "the websockets package is not installed" no matter what you installed - the companion plugin could never connect. The package is now bundled like every other dependency, the build fails loudly if it ever goes missing again, and source runs that genuinely lack it now point at `pip install -r requirements.txt`.
- **Callouts can no longer be killed by a bad sound file or a wedged audio device.** A trigger's custom `sound_file` was played with no checks: pointing it at a FIFO, `/dev/zero` or a huge file hung the single speech worker in an unbounded file copy, silently ending every callout for the rest of the session. Sound files are now verified to be regular files and copied with a 32 MiB ceiling. Notification chimes give up after 60 seconds if the audio device wedges, instead of leaking a process per chime, and the callout queue is bounded (the oldest pending callout drops when it fills) so a stalled backend can no longer grow memory without limit.
- **Second audit hardening pass.** A revoked FFLogs login is dropped and refreshed with one retry instead of failing silently until its nominal expiry. A hand-edited `"enabled": "false"` string in a trigger file now actually turns the trigger off (it used to read as on). Incoming websocket messages are capped at 4 MiB like the other network reads, and engine stderr lines get the same 1 MiB cap stdout already had. User regexes are refused outright when the `regex` module is missing, because stdlib matching has no timeout. One log line's trigger matching is capped at one second of wall clock, so a pathological trigger set can no longer starve the interface. And the app warns when the cactbot URL points at a remote host, since that page runs with insecure content allowed.
- **Timeline bars and timeline callouts were silently off until a bulk button was clicked once.** The stored direction of the **Global - Local On/Off** bulk toggle defaulted to off for fresh installs and upgraders, and everything timeline reads it: the bars pushed to the overlay plugin, the spoken timeline entries, and the combat feed that starts and syncs the clock. Regular triggers don't read it, so callouts worked while the timeline looked broken. It now defaults on, matching the every-trigger-enabled state it ships with, and a stored click is still remembered.
- **Cactbot timeline bars advance.** With Cactbot on, its .txt schedule reached the overlay plugin but the timeline engine's clock never started: the log feed into the engine was gated on the Local switch, which Cactbot turns off, and the plugin only interpolates between clock ticks, so the bars sat frozen for the whole pull. The feed now reaches the engine in cactbot mode, and turning Cactbot on no longer clears the schedule it just loaded.
- **A stale automark retry can no longer knock off a live sign.** A rule whose mark failed on a cold party-slot map queued a retry on the 10-second tick. If another rule marked that player first and the first rule's debuff then fell off, the queued retry survived and could fire later, replacing the live sign - the game keeps one sign per player, last writer wins. A debuff's loss line now kills its pending retries no matter who has marked the player since, and the tick drops a retry whose target carries another rule's sign.
- **UMAD chains and the gaze pairing clear their signs on a stale-instance reset.** A missed log line followed by an internal reset (a long quiet gap, a new instance, a fifth gaze carrier) wiped the tracking state without clearing the signs it had placed, stranding a marker on a player for the rest of the pull, since the wipe cleanup reads that same state. The resets now emit the clears first, the way the Accretion re-seat already did.
- **Japanese callouts handle Linux speech failures properly.** A spawn failure in the system speech path reported "not handled" even for Japanese, so the text fell through to the English Piper voice and came out garbled. It now reports handled like every other Japanese failure path. A failed spd-say, speech-dispatcher down, also falls through to espeak instead of going silent.
- **Disabling the neural Japanese voice no longer stalls the interface during its first load.** The off switch took the same lock the multi-second model build was holding on the worker. The build now runs under its own lock, the way the English voice's build already did.
- **Hand-edited trigger JSON gets the editor's guards.** A non-status log type with an expiry warning set has the warning stripped on load instead of matching and swallowing the callout forever, an Ability ID on a log type with no ID field (00 chat lines, for one) is dropped so the regex can do the matching, and a sequence step timeout of 0 takes the 10-second fallback instead of expiring the step on the spot. The editor also greys the Ability ID field for those types, and its no-matcher refusal now shows before the clamp warning instead of after.
- **Smaller fixes.** M7N joins the curated Normal Raids list, so custom triggers tagged for it sort there instead of TBD. An unzoned custom trigger whose tag the curated tree covers no longer lists under Unsorted as well, where deleting one listing removed both and read as data loss. The cactbot row menu drops its two permanently disabled entries, Rewrite and Silence, which never had callout text to work on. A timeline entry exactly at a backward sync point no longer speaks at the sync, matching cactbot. The event-trigger converter warns on partial overlaps of pipe-joined ability ids like its siblings, and the Triggernometry converter's credit stripper no longer strips a mid-word "by" ("Kirby Triggers" came out as "Kir"). A corrupt or float WAV picked as an alert sound now plays at native volume instead of being dropped. The DPS log write is serialized across encounter-end threads, and its retention culls a full log left newest-named by a backward clock step. The meter's combatant map rests at its documented 1024 cap, a one-message combat-flag crossover finalizes the old pull before opening the new one, and a whiffed pull's live clock pauses on the idle timeout like a damaging one. A Telesto command that never reached the queue now reads as failure so automark retries it. The overlay plugin's send watchdog no longer kills a healthy socket in the gap between a send finishing and its all-clear. The FFLogs client is reused across fetches, so its OAuth token cache survives instead of minting a token per pull. The recovery script restores the newest interrupted-update backup rather than the first in glob order and tries the next one when a move fails. The staged Windows updater's clean refusal is no longer reported as antivirus quarantine, a process-open failure during the exit wait falls through to the tasklist check, and the release-info fetch raises its own size-cap error instead of a misleading parse failure. A `vv`-prefixed tag keeps one v instead of being stripped bare. Restarting into an update waits for the sidecars like the Windows handoff does and stops the background timers close stops. The frozen-build notes now say when the files on disk are already the new version and that a manual engine update is replaced by the next app update. The setup script's sudo expansion no longer errors on bash older than 4.4 when run as root.
- **A callout whose overlay send fails mid-write is no longer lost.** The plugin link pops each frame off the queue before sending it, and a failed send, the game closing or the Dalamud plugin reloading mid-write, tore the connection down with the frame already gone. Fire-once alerts are now re-queued on a failed send, the same protection the offline sweep already gave them, and the reconnect delivers them.
- **A missed cleanse line no longer silences the UMAD automarkers for the rest of the pull.** The black-hole chains only cleared a player's Crust flag when the debuff's loss line arrived, so one missed line (a death, log lag, a reconnect) blocked every reset path and each later black hole produced no marks at all until a wipe or zone change. An instance whose only remaining Crust sits on the queue heads, with the mechanic gone quiet past its burst gap, now counts as dead and resets on the next event, clearing the stranded signs first. The gaze pairing gets the same treatment: an unassigned burst that goes quiet can no longer leak a leftover entry into the next instance and complete a stale split that marks a player with no gaze.
- **Auto-reconnect survives a stalled websocket handshake.** A host that accepts the TCP connection then goes silent mid-handshake never raises an error, and the socket has no handshake timeout, so the reconnect timer kept re-arming against the stuck attempt and the feed never came back on its own. A stalled attempt is now cut loose and re-opened when the timer fires, and every fresh attempt arms the watchdog behind it.
- **A stalled engine sidecar can no longer pin tens of GB of memory.** The Triggevent and Triggernometry stdin queues were bounded by message count, but one relay message can be up to 4 MiB, so a sidecar wedged on stdin while the relay kept talking could hold tens of thousands of giant frames. Both queues now also budget 64 MiB of queued payload and drop oldest when either cap engages, the same policy the count cap already had.
- **A failed exe restore during a Windows update rollback leaves a trace.** The rollback's exe branch swallowed a restore failure silently, so with the live exe already parked aside the install could end up with no exe at all and zero diagnostics, the exact failure the `_internal` branch was made loud for. It now logs the failure and drops the same RECOVER.txt naming the backup to rename back by hand.
- **Imported fight tags with path separators no longer reload the timeline every 30 seconds.** The timeline loader blanks a tag containing `/`, `\` or `..` before building the file path and stamped the blanked value, but the 30 s re-detect compared against the raw tag, so the two never agreed and every tick re-ran the full load, engine reset and plugin re-push forever. Both sides now blank through the same helper. A corrupt or unreadable local timeline also leaves a drop-log line instead of presenting as "this zone has no timeline" with no trace.
- **The DPS meter learns the party from the relay roster, not just the log.** Jobs and your own id only reached the meter via the 03/02 log lines, so connecting mid-instance read the whole party as enemies until those lines happened to stream. The meter now also takes the roster jobs and primary-player id the relay replays on subscribe. Its combatant maps also evict who was seen longest ago instead of who arrived first, so a city or hunt session full of passers-by can no longer evict your own party mid-session.
- **A queued automark retry dies with its debuff even with auto-cleanse off.** The loss-line handler only ran when the auto-cleanse toggle was on, so a mark retry queued on a cold party-slot map could outlive the debuff that spawned it and place a sign up to 30 s later for a mechanic that had already resolved. The retry purge now runs on every loss line; with the toggle off, signs stay up exactly as before.
- **The translation string extractor fails loudly on a file it cannot parse.** A tracked source file that momentarily fails to parse (mid-rebase, bad merge) used to count as zero strings, so every one of its keys looked stale and the documented `--prune` recovery would delete their live translations. The run now stops and names the file, `--check` included, leaving the catalog untouched.
- **The Japanese callout build is stricter about its inputs.** Padded keys in `trigger_names_ja.json` are stripped the way the lookups already expected (one shipped entry was dead text), the substitution-token gate compares counts instead of presence so a translation cannot silently drop a repeated `{target}`, and a duplicate trigger id now warns instead of silently keeping the last callout.
- **A build env without PyQt6-WebEngine fails the build.** The packaging spec pinned the WebEngine modules but never checked them, so a local build without the package got only a warning and shipped a frozen app whose cactbot page could never load. It now hard-fails with the install command, like the websockets guard already did.
- **Smaller fixes.** An escaped backslash in front of a `\uXXXX` or `\xXX` sequence in a converted cactbot callout now stays literal text instead of decoding into a stray character. The neural Japanese voice's import no longer races a venv reconfigure into a permanently failed engine, and a wedged voice-model load gives up under the same 60 s ceiling synthesis already had instead of wedging every later callout. A system-voice spawn failure leaves a log line, Japanese callouts included, and espeak and spd-say now honor master volume above 100% like the other engines. cactbot timeline entries named by a hideall directive on a plain label now stay silent like cactbot's own, instead of speaking and pushing bars. An apostrophe inside an old-style `sync /Boss's Move/` body no longer keeps a trailing `#` comment alive to be scanned as timeline clauses. And an unknown marker token in a hand-edited automark rule now logs that it fell back to next-attack instead of silently placing one.

## v1.3.0 - 2026-08-04

Merges the two self-update channels into a single Stable channel, and drops the cancelled Rust/Slint port.

### Changed
- **The update channels are merged into a single Stable channel.** Every install now follows full releases only, and a stored Master (testing) choice is treated as Stable, so an install that had opted into testing builds simply follows the stable stream. The version was bumped from 1.2.7 to 1.3.0 to put this release ahead of every earlier Stable and Master build, so both sides converge on this and later releases.

### Removed
- **The Master (testing) channel and the "Revert to Stable" button.** With a single channel there is nothing to switch between and nothing to revert to, so the Update channel selector (Settings - App, added in v1.0.9) and the Master-only force-reinstall button (added in v1.1.0) are both gone.
- **The Rust/Slint port.** The experimental rewrite of the app and its release channel are discontinued and removed.

## v1.1.4 - 2026-07-03

Master/testing release. Extends automarkers from self-only to the whole 8-player party.

### Added
- **Party-wide automarkers.** A rule can now mark **whoever gets the debuff**, not just you. Each rule has an **on** choice - **me** (marks you via `/mk <marker> <me>`) or **whoever gets it** (marks the affected party member via `/mk <marker> <N>`). NyaaTriggers resolves the player's party slot from Telesto's live party list (`GetPartyMembers`), mirroring how Triggevent does it. Marking another player only works in-game with a live party; if a slot isn't known yet the mark is skipped rather than placed on the wrong person.
- **Load UMAD preset.** One click seeds automark rules for every known Dancing Mad (Ultimate) player debuff (P1-P4), marking whoever gets each one. Markers are sensible defaults you edit to your strat, and it skips rules you already have. Debuff IDs were verified against cactbot, FFLogs and XIVAPI.
- **UMAD black-hole chains (P3).** A dedicated sequencer for the Primordial Crust cleanse: the party splits into three queues - DPS without Accretion, supports without Accretion (tanks can never have Accretion), and the Accretion DPS+healer pair - each ordered by the real First/Second/Third in Line statuses (`BBC`/`BBD`/`BBE`, reused from TOP). One roaming sign per queue (defaults attack1/attack2/attack3, editable): it starts on the queue's 1st in Line, jumps to the next player each time a Primordial Crust (`154E`) is cleansed by a tether, and is cleared off the last player. Toggle + per-queue markers live in the Automarkers tab; roles are read from the combat log's job data, the party roster, and a live combatant snapshot (so an app restart mid-instance still resolves them), and a queue stays unmarked rather than guess if its members, roles or cleanse order can't be resolved. While the chains are on, plain rules for the chain statuses are suspended in UMAD, since two systems must not fight over one sign. For the same reason the preset's P3 Entropy/Dynamic Fluid rules now default to `attack5`/`attack6` (rules seeded from the older preset keep their markers. Edit them if you run the chains). Marks that can't be placed yet (party slot unknown) are retried instead of silently dropped. (This also corrects `docs/UMAD-DEBUFFS.md`: First/Second/Third in Line are real DMU statuses, not just a strat label.)
- **UMAD P4 debuff hexes verified and seeded.** The Neo Exdeath real-vs-fake phase debuffs are now in the preset: Cursed Shriek `15A7`, Forked Lightning `15A8`, Compressed Water `15A9`, Acceleration Bomb `15AA`, Entropy `15AB`, Dynamic Fluid `15AC` (the status block Square Enix minted for this phase in 7.51), plus the reused classic ids Beyond Death `566` and Allagan Field `1C6`. White Wound `15A5` / Black Wound `15A6` are confirmed as well but intentionally not seeded. All 8 players get one of the pair, so there is nothing distinct to mark. P5 Celestriad tower Resistance Downs still have no pinnable hex (generic reused status names); evidence per id in `docs/UMAD-DEBUFFS.md`.
- **Clear all party marks** button (`/mk clear <1>`..`<8>`).

### Fixed
- **Linux self-update now works.** The in-app updater rejected every Linux download: the build legitimately contains symlinks (bundled Qt and NumPy libraries) and the archive-safety check treated any symlink as hostile, so the update failed to install and the app stayed on the old version. It now allows symlinks that stay inside the update folder and still blocks any that would escape it. Because the fix ships inside the app, an existing Linux install cannot self-update to get it. Download the latest build once from the releases page, and updates from then on will apply normally.

### Notes
- Placing marks via a plugin automates the game client, which is against FFXIV's Terms of Service and carries account risk. Automarkers stay off by default. Enable at your own risk.
- Existing self-only rules keep working exactly as before (rules with no target default to "me").

## v1.1.3 - 2026-06-30

Master/testing release. Adds experimental automarkers via the Telesto Dalamud plugin.

### Added
- **Automarkers (experimental, off by default).** Place an FFXIV head-sign marker (attack, bind, ignore, shapes) on **you** through the **Telesto** Dalamud plugin's local HTTP API - a direct `/mk <marker> <me>`, with no party resolution and no Triggevent Engine involved in the marking. You author the rules: pick a fight (or Any), the debuff (status effect ID or exact name), and the marker; when that debuff lands on you, the rule fires and Telesto marks you. Use **Test mark** in **Settings → Automarkers (Telesto)** to confirm the pipeline first, then tick **Enable automarkers** to let your rules fire. It just needs the Telesto plugin running in-game; NyaaTriggers makes no contact with Telesto until you test or enable it. Placing marks via a plugin automates the game client, which is against FFXIV's Terms of Service and carries account risk. It is off by default. Enable at your own risk.

## v1.1.2 - 2026-06-29

Master/testing release. Stability and crash-hardening pass across trigger loading, the engine bridges, saves, and downloads.

### Fixed
- **Wipe detection works again.** The wipe/reset signal (ActorControl) was read from the wrong field, so a wipe that did not also change zones never reset the timeline or cleared pending DoT-reapply warnings. Now read from the correct field, confirmed against a real log.
- **A corrupt or hand-edited `triggers.local.json` / settings file no longer prevents the app from opening.** A valid-JSON-but-wrong-shape file, or a single bad numeric value, is skipped and the rest loads, instead of crashing on launch.
- **The Triggevent and cactbot bridges no longer go silent on a malformed message.** A single bad message from a sidecar could stop the reader and quietly kill all further callouts mid-fight. It is now caught and logged.
- **Saves are atomic.** Triggers, settings, and the engine caches are written to a temporary file and swapped into place, so a crash, kill, or power loss mid-save can no longer truncate them.
- **Downloads verify their length.** A truncated voice or update download (for example a clean early connection close) is now detected and removed instead of being left as a "complete" file that silently breaks audio or an update.
- Smaller hardening: stack-list/inventory parsing, sequence timeouts, multi-id matching with stray whitespace, and timeline window/jump parsing all degrade gracefully on bad input instead of dropping data or raising.

## v1.1.1 - 2026-06-29

Master/testing release. Refines the Triggernometry integration and the alert sounds.

### Fixed
- **Windows: Triggernometry callouts should now fire.** The engine started and loaded its triggers, but the sidecar could not process any incoming messages (log lines, callouts, toggles), so nothing was ever spoken. A string-splitting call resolved to a method that exists under Mono on Linux but not in .NET Framework on Windows, so it threw on the very first message and stalled the whole message handler. Switched to a form present on both runtimes, and hardened the same pattern across the engine so it cannot recur. Linux was unaffected. Pending live in-game confirmation.

### Changed
- **Triggernometry triggers now appear in their own section** in the Triggers list, separate from Local and from Triggevent (previously they were lumped under the Triggevent section). Importing a Triggernometry pack no longer folds the simple triggers into Local when the engine can run them: the whole pack runs through the engine and lists under the Triggernometry section. On a build without the engine, the simple triggers still fall back to Local.
- **Collapsing a source follows the toggle direction:** turning a per-fight box or a Global button on expands that source's section; turning it off collapses it.
- **Trimmed the built-in alert sounds:** removed Vine boom, -999 Social Credit, and Smoke detector.

### Added
- **Import SFX button** (Settings - Alert Sound) to add your own `.wav` as a named, reusable alert sound. It copies into a user sounds folder, so it works on frozen installs and survives updates.
- **Triggernometry diagnostic log** at `<config>/nyaatriggers/triggernometry-core/triggernometry.log`. When the sidecar fails to start (most likely on Windows), it records the launch command and the engine's boot error, so the cause is visible instead of silent.

## v1.1.0 - 2026-06-28

Adds the Triggernometry engine: runs Triggernometry's real engine headlessly so complex scripted triggers fire 1:1. This is a Master/testing release.

### Added
- **Triggernometry engine sidecar.** A headless host (`triggernometry-core`) runs the real Triggernometry engine so its complex, scripted triggers (the ones that don't reduce to local triggers) fire 1:1. Import a Triggernometry export (Settings - Data - Import Triggernometry): the pack runs through the engine and lists in its own Triggernometry section, separate from Local and gated by the master Triggers switch (on a build without the engine, the simple triggers fall back to Local). Engine callouts appear as editable rows (edit spoken text, toggle per-trigger), and the inventory caches so rows list before the sidecar boots. The sidecar is cross-platform .NET Framework 4.6.2 MSIL: it runs natively on Windows (no Mono) and under Mono on Linux. The prebuilt sidecar is vendored in `triggernometry-core/bin/` and bundled into the release (GitHub runners ship a Mono too old to compile the engine, so it is built locally with `build-all.sh` on Mono 6.12+ and committed). Still needs live in-game verification of the combatant/zone field casing on IINACT.
- **"Revert to Stable" button** (Settings - App), shown only on the Master channel. It force-reinstalls the latest Stable release and switches you back to the Stable channel. Because a Master build's version is higher than the latest Stable, switching the channel alone never brings you back down, so this is the escape hatch. Frozen Windows/Linux builds reinstall the latest Stable in place; a source/git install is sent to the download page.

### Changed
- **Per-fight Local / Triggevent checkboxes now reflect and control on/off state.** Each box is checked when that source is fully on for the fight, and toggling it enables or disables only that source for that fight, independently of the other. So a global toggle lights the matching per-fight boxes, unchecking a box turns that tab off, and a single-row change re-derives the box. Turning a box or a global button on expands the affected trigger sections; turning it off collapses them. Reset to Default now disables the engine (Triggevent / cactbot / Triggernometry) rows too, not just local triggers.

## v1.0.9 - 2026-06-28

Adds an opt-in update channel so you can choose between finished releases and early testing builds.

### Added
- **Update channel selector** (Settings - App - Update channel). **Stable** (the default) follows only full releases. **Master (testing)** opts into pre-release builds for trying new features early. Every existing/released install stays on Stable unless the user personally switches. The choice persists. Internally: `updater.fetch_latest_release(channel=)` uses GitHub's `/releases/latest` (excludes pre-releases) for Stable and the releases list (newest, including pre-releases) for Master, and the release CI marks hyphenated tags (e.g. `v1.1.0-master`) as pre-releases.

## v1.0.8 - 2026-06-26

Hardens the Windows in-app updater so an interrupted update can no longer leave the app unable to launch.

### Fixed
- **Windows: a self-update interrupted at the wrong moment could leave the app unable to start.** While swapping a new build in, the updater briefly moved the program file aside and wrote a fresh `_internal/` full of unsigned DLLs; if antivirus quarantined a file (or the staged updater was killed/power was lost) during that window, the install could be left half-swapped and un-launchable, with no way to self-repair (a broken `_internal/` can't boot to fix itself). The updater now (1) keeps the `.exe` present at all times - a copy-backup plus a single atomic overwrite, never a move-aside - and (2) after swapping, **relaunches the new build and waits for it to confirm a successful boot** (via a boot-ok marker written once `QApplication` is up); if the new build doesn't come up, it automatically rolls back `_internal/` and the exe to the previous version and relaunches that, so an update can never strand you on a broken install. Driven from the staged copy, whose own `_internal/` is intact, so rollback is always possible. Added a regression test for the "swapped build won't boot -> rollback" path. This builds on the v1.0.7 update-path guarding (which kept a broken update from crashing the app at launch). v1.0.8 also keeps the swap itself from bricking the install.
  - Note: an install already bricked by an earlier update needs one manual reinstall from the download page. Updates from this version onward are protected.

## v1.0.7 - 2026-06-26

Reliability release. Fixes the Triggevent tab silently not appearing in downloaded builds, makes the self-updater unable to take the app down, and stops the work-in-progress macOS build from breaking releases.

### Fixed
- **The Triggevent tab no longer goes missing in downloaded builds.** The feature is gated on `is_available()` (a bundled Java runtime *and* the sidecar jar). The jar is bundled as ordinary data and lands under `sys._MEIPASS`, but the JRE is bundled as a PyInstaller `Tree`, which can land next to the executable (or, on macOS, under `Contents/Resources` / `Contents/Frameworks`) instead - so the app looked for `java` where it wasn't, decided no runtime was present, and hid Triggevent until a *system* Java happened to be on `PATH` (e.g. after running the standalone Triggevent app). Java/jar discovery now searches every plausible bundle location, so the shipped runtime is found wherever PyInstaller placed it, on all platforms.
- **The self-updater can no longer crash the app on startup or during an update check.** The on-launch backup cleanup, the update check (its thread start, worker body, and result slots `_on_update_available` / `_on_update_done`), and the update banner are now guarded, and the Windows `--apply-update` hand-off wraps its whole body (module import + argument parsing, not just the swap). A broken or interrupted update path now costs only the update feature for that launch - never the ability to start the app.

### Changed
- **macOS builds are now marked work-in-progress.** The release workflow treats the Mac build as best-effort (`continue-on-error`), so a failing or missing Mac build no longer fails the release run or blocks the Windows/Linux uploads. Release uploads use `--clobber` so a re-run re-attaches assets cleanly. The README flags macOS as WIP and notes the Mac builds (the Intel one in particular) are best-effort and may be absent; build from source as a fallback.

## v1.0.6 - 2026-06-25

Fixes the bundled Triggevent Engine failing to start after Triggevent's upgrade to Jackson 3.

### Fixed
- **The bundled Triggevent Engine failed to start.** A leftover Jackson 2.x pin in the sidecar (`triggevent-core`) clashed with Triggevent's upgrade to Jackson 3, so the engine hit a startup error and most of its components (callouts, easy triggers, duties, overlays) failed to initialize. The sidecar now inherits the engine's own Jackson version instead of pinning one, so it boots cleanly again. Same engine build on every platform.

## v1.0.5 - 2026-06-25

Adds in-app self-update on Windows.

### Added
- **Windows can update itself in-app.** When a new version is out, **Install** downloads and applies it and reopens NyaaTriggers, instead of sending you to the download page to do it by hand. Because Windows locks a running program's own files, the new build is staged and finished by a fresh copy on the way out, so the app briefly closes and reopens to complete the update.
- **The updater is crash-safe.** It swaps the new files in with a full backup of the old ones and rolls back to the previous version if anything goes wrong, so a failed or interrupted update can't leave the app unable to start. If it can't update (locked files, antivirus blocking it, no write permission, or low disk space) it tells you and falls back to the manual download. (Hardened further in v1.0.8.)

## v1.0.4 - 2026-06-24

Windows fixes. The Windows build now launches normally instead of spawning a cascade of first-run setup windows.

### Fixed
- **Windows: the app opened a pile of "First Run Setup" windows and never started.** First-run setup assumed a source install (create a `~/.venv/ffxiv`, pip-install piper) and ran that install through `sys.executable` - which in a frozen build is `NyaaTriggers.exe` itself, so each step relaunched the whole app and its setup dialog, over and over. Piper ships inside release builds, so the frozen app now skips setup and opens straight to the main window.
- **Windows: a console window popped up whenever the Triggevent Engine started.** The bundled `java.exe` sidecar is now launched with `CREATE_NO_WINDOW`.
- **Windows: the in-game overlay rendered opaque.** It now loads `opengl32` correctly on Windows, so the overlay background clears to fully transparent.
- **Editing triggers no longer crashes the app when it is installed to a read-only folder** (e.g. extracted into Program Files); the change is kept for the session if it cannot be written to disk.
- **Piper / onnxruntime is no longer loaded at startup when the system voice is selected** (the Windows default), so startup is lighter.

## v1.0.3 - 2026-06-24

Adds a one-click importer for Triggernometry triggers.

### Added
- **Import Triggernometry** button (Settings - Data). Point it at a Triggernometry `.xml` export and it converts the triggers that pin a literal ability ID and carry a plain (non-token) text-to-speech line into Local triggers - deduplicated against what you already have and disabled by default, so enable the ones you want in the Triggers tab. Complex, wildcard, or scripted triggers are skipped. It runs the same converter the bundled set is built with, so there is nothing to install.

## v1.0.2 - 2026-06-24

Bug-fix release. Fixes a startup crash that prevented the Windows and macOS builds from launching at all. Linux was unaffected.

### Fixed
- **Windows and macOS builds failed to start**, dying immediately with "This application failed to start because no Qt platform plugin could be initialized" (available plugins: minimal, offscreen, windows). The app was forcing the Linux-only `xcb` Qt platform plugin on every OS before the window was created. `xcb` does not exist in the Windows or macOS Qt builds, so Qt aborted at launch. It is now set only on Linux, where the in-game overlay needs it.

## v1.0.1 - 2026-06-21

Security patch. Updates the bundled Triggevent sidecar's `jackson-databind`; no behavior changes.

### Security
- Bumped `com.fasterxml.jackson.core:jackson-databind` from 2.13.2.2 to 2.13.4.2 in the Triggevent sidecar (`triggevent-core`), clearing two "uncontrolled resource consumption" advisories (CVE-2022-42003 / CVE-2022-42004 - deep-nested-array resource exhaustion). The exploit needs `UNWRAP_SINGLE_VALUE_ARRAYS`, which the sidecar never enables, so it was not exploitable in practice. The dependency is now on the patched release regardless.

## v1.0 - 2026-06-21

First stable release. Brings together everything since v0.9.2: the everything-off-by-default inversion, the category-first fight tree, the overlay upgrades, alert sounds, and a round of bundled-trigger cleanup.

### Added
- **Six or seven built-in alert sounds.** The Alert Sound picker (Settings - Alert Sound) offers six or seven built-in sounds - **Ding / Alert** plus the meme-style **Vine boom / Coin / -999 Social Credit / Smoke detector** - alongside the Custom file option. The alert is off by default; choose whether it plays for every alert or only urgent ones, and Test it. It plays on its own channel, so it sounds alongside the spoken callout instead of delaying it.
- **Restore from Repo** button (Settings - Data), next to Update Triggers: re-downloads the bundled `triggers.json` from the GitHub repo and reloads it, so you can restore the bundled set if it gets out of shape. Your own (custom and edited) triggers are never removed.
- **Overlay: colour options.** Pick the colours for the alert tiers (info / alert / alarm), the timeline bars, and the bar text from **Settings - Overlay Appearance**. Defaults match the existing theme.
- **Overlay: text size.** A slider (60-250%) scales all overlay text, and the text itself is now drawn as crisp vector outlines so it stays sharp at any size.

### Changed
- **The whole app now starts silent - opt in to what you want.** The Triggevent (and cactbot) engine callouts now ship **off by default**, matching the bundled Local triggers: the first time a callout is seen it is seeded as disabled, so a fresh install, and each new patch's new engine triggers, makes no noise until you check the rows you want. Your per-trigger on/off choices persist as before.
- **"Enable All / Disable All" replaced by two global source toggles.** **Global - Local On/Off** flips every Local trigger across all fights; **Global - Triggevent On/Off** flips every Triggevent trigger across all fights. Each click simply flips that source (a stable on -> off -> on) and resets the per-fight checkboxes. Running both Local and Triggevent at once double-fires callouts, so a hint nudges you to keep one on.
- **Per-fight Local / Triggevent checkboxes.** Selecting a fight shows a bar with mutually exclusive **Local** and **Triggevent** boxes: ticking one enables that source for *this fight* and turns the other off, so you can choose which engine calls a given fight without touching the rest.
- **The Triggers list groups into collapsible sections** - **General**, **DoT**, **Local**, and **Triggevent** - so the table is easier to scan. All start collapsed; a search flattens them into one result list.
- **Fight tree reorganised category-first.** The top level is now the content type - **Ultimates, Savage Raids, Extreme Trials, Deep Dungeons, Field Operations, Normal Raids, Normal Trials, Alliance Raids** - then expansion, then fight. It was expansion-first before.
- **The "Custom" tree section is now "Unsorted", and zone-locked triggers auto-file by zone.** A trigger with a zone regex sorts to that zone (under its fight, or under the new **TBD** node if its content has no slot in the curated tree); only zone-less triggers - which fire everywhere - land in **Unsorted**.
- **Collapsing a tree section now also collapses every sub-section nested inside it**, so re-opening it shows the children closed rather than in whatever state they were left in.
- **Hundreds more bundled triggers**, converted from Triggernometry repositories (both the pipe and the cactbot/colon log-line formats are now parsed). They ship disabled like the rest. Fights that do not yet have a place in the curated fight tree are grouped under a new **TBD** node (by fight) so none are hidden - sort or zone-lock them later. Duplicate callouts (the same fight and spoken line under different ability IDs) are merged into one trigger that matches all of them, so e.g. a zone's many "raidwide" casts become a single row.
- **Cactbot controls moved into the Settings tab.** Cactbot's on/off, URL, and *use cactbot timelines with my own triggers* option now live under **Settings - Cactbot**; the separate **Engine Settings** tab is gone. Cactbot stays mutually exclusive with the master Triggers switch (turning it on turns Triggers off). **Current Instance** is its own top-level tab again, and the Settings sections gained gold underlined headers.
- **The alert-sound Volume slider is now a dB (loudness) fader** instead of linear amplitude: 100% is full, 50% is about -20 dB, 0% is silent, default 50%, and it is scaled by the master volume. Loudness spans the whole slider instead of being crammed into the bottom tenth.
- **Reset to Default is now global, and disables every trigger - custom ones included.** One click unchecks (disables) every trigger across all fights, not just the ones currently visible, and it no longer asks for confirmation. It only clears the on/off checkmarks - it does not remove any trigger or restore edited values.
- **Cleaner interface.** All hover tooltips are gone across the app, the theme's darkest base colours were lightened, and the custom-row tint was dropped so pure-custom rows render the same as Local rows (only the engine cactbot/triggevent rows stay tinted).
- **Overlay setup simplified to two toggles.** **See-through** and **Lock (click-through)** replace the old three-checkbox flow - locking now both passes clicks through to the game and switches the overlay from sample placeholder content to real callouts only, so the separate *Accept position* toggle is gone.
- **The overlay is now two independent windows.** The timeline bars and the alert pop-ups each get their own window, shown or hidden by the checkbox in its **Timeline bars** / **Alert pop-ups** group - run either one on its own or both, each positioned, sized, and locked separately (at least one stays shown; the **Enable in-game overlay** checkbox turns it off entirely). This replaces the old *Separate windows* toggle and *Show* dropdown.
- Dropped the redundant description line under the old **Engine Settings** header.

### Fixed
- **The overlay no longer comes up invisible.** While unlocked it always shows placeholder content, so re-enabling it (or opening it with no fight running) shows the box where it sits instead of a fully transparent window; lock it for the clean, real-data-only look.
- **Bundled triggers: ambiguous either/or callouts removed.** Calls the local engine cannot resolve to one direction were dropped: **"In or Out"** (O10N), **"IN or OUT"** (The Wreath of Snakes), **"Stack or spread"** (UMAD), and **"North / South"** (DSR). A few were rewritten to a single clear call instead: P7N now says **"In"**, M10S **"Out of Middle - Group Stacks"**, and M11S **"Bait Puddles x3"**.
- **Eureka Orthos: the four "Break Line of Sight" callouts merged into one** multi-ID trigger matching `7ED3|7EF4|81AA|81AB`.
- **Renamed the speculative "Dancing Mad" ultimate to "UMAD"** - the fight tag and zone, the fight tree, and the Triggernometry converter (`convert_event_trigger.py`).
- **Ticking a per-fight Local / Triggevent box no longer expands the trigger sections** - the table keeps whatever you had collapsed.
- **The fight tree no longer collapses for a split second on Reset or a global toggle.** Its saved expanded-state never matched after a rebuild, so every refresh quietly collapsed it; it now keeps its expansion.

### Removed
- The **Current Instance** sub-tab's Add / Edit / Delete / Enable All / Disable All buttons are gone. It is a live log, so you make a trigger by right-clicking a line in it (the hint text points the way). The **Save log** button is unchanged.
- The green tint on locally edited bundled triggers is gone - only pure-custom triggers stay tinted (purple).

## v0.9.2 - 2026-06-18

### Fixed
- **Trigger-correctness pass against Cactbot and Triggevent.** Around 35 bundled callouts had their ability ID or spoken text corrected after cross-checking every fight against both engines. Highlights: UCoB's "'Neath the red moon" now calls **Away from Tank** (it is the Ravensbeak tankbuster, not a spread); FRU's **Akh Rhai** now fires on the right cast instead of ~14s early; and several callouts that wrongly said "Tank Swap" on non-tankbuster mechanics (TEA Optical Spread, Zoraal Ja Forged Track) now say the real mechanic.
- **Split two triggers that called one direction for opposite mechanics:** Zoraal Ja's **Chasm of Vollok** (Lean West vs Lean East) and M8S's **Eminent / Revolutionary Reign** (In Later vs Out Later) are now separate triggers per ability ID.

### Changed
- **Fixed mis-filed fights.** 14 triggers filed under **Zoraal Ja EX** are actually **Valigarmanda EX** mechanics (Skyruin, Disaster Zone, Crackling Cataclysm, ...) and have been moved to that fight; two exact duplicates were removed.
- **Eden Savage tiers corrected.** The **Inundation / Refulgence / Eternity** trigger sets used Savage ability IDs but sat in Normal folders (so they never fired); they are now tagged **E3S / E8S / E12S** and the empty Normal folders are gone.
- Every bundled trigger ships **disabled by default** - enable the ones you want per fight. The Triggevent Engine's own callouts still run automatically when the engine is on.

### Dev
- `triggevent-core/build.bat` now does a clean build, matching `build.sh`, so a refreshed engine checkout can't ship a stale jar on Windows.

## v0.9.1 - 2026-06-18

### Added
- **Reapply-warning triggers for common single-target DoTs**, alongside the existing Death's Design one and using the same pre-expiry timer: Combust III, Dia, Higanbana, Caustic Bite, Stormbite, Eukrasian Dosis, Thunder III, Chaos Thrust, Bio II, and Aero II. Each is **off by default** (enable the one for your job) and matched by effect ID, so it calls out "reapply" a few seconds before *your* DoT falls off the target.
- **Bundled Triggevent Engine refreshed to upstream master**, which refines the existing **Dancing Mad (Ultimate) P3** callouts in the Triggevent source (corrects the Earthquake line callout that was labelled for the wrong role, adds the partner name to the Forsaken tower calls) and updates timeline data.

### Fixed
- **Reapply warnings no longer spam once per target.** When you apply a tracked effect to a whole pack with one AoE (e.g. Reaper's **Whorl of Death** landing Death's Design on every add), the engine armed one pre-expiry timer per target, so the "reapply" callout fired once for *every* enemy hit - dozens of times in a big pull - for the single reapply you actually owe. The per-target timers still track each enemy independently (so an early drop still cancels its own warning), but the spoken callout is now collapsed by the trigger's cooldown (the minimum gap between reminders), so a simultaneous AoE application calls out once instead of once per enemy. Previously the gain path bypassed the cooldown entirely, leaving the burst undeduplicated. (A staggered, target-by-target application spread wider than the cooldown can still produce more than one reminder.)
- **The bundled "Death's Design (reapply warning)" trigger now matches by effect ID (`A1A`)** instead of the effect name, so name formatting can't keep it from matching.

## v0.9 - 2026-06-16

### Added
- **Save log** button on the Current Instance raw-log panel: exports the captured raw feed to a text file for trigger debugging / sharing a pull. It captures the *complete* feed (every line, including effects you apply to the target, which the enemy-only view hides), and honors the Filter box so you can export just the lines you want (e.g. filter `Death's Design`) - keeping the file small and free of unrelated party data.
- **Status triggers can now follow effects you apply to the target, not just debuffs on you.** Status-effect triggers (types `26`/`30`) gained an **Applies to** selector: **You** - the effect is on you (the previous, unchanged default); **Target - you applied it** - an effect *you* keep up on the enemy, matched by source instead of target, e.g. Reaper's **Death's Design**; or **Anyone** - no source/target filter. Existing status triggers are untouched - a trigger with no `Applies to` set behaves as **You**, exactly as before.
- **Reapply warnings (pre-expiry timer).** A GainsEffect (`26`) trigger can now set a **Reapply warning** lead time so it speaks a few seconds *before* the effect runs out instead of when it lands. It reads the effect's own duration off the apply line, re-arms each time you refresh the effect, and cancels itself if the effect drops early (boss death, dispel) or you change zone. A bundled **"Death's Design (reapply warning)"** trigger (under General, off by default) uses this to call "Reapply Death's Design" ~7s before it expires - a proactive reminder rather than the after-the-fact alert you'd get from matching the effect falling off.

## v0.8 - 2026-06-15

### Changed
- **Triggevent works out of the box in release builds.** The downloadable Windows and Linux packages now bundle the prebuilt engine sidecar (`triggevent-core.jar`) plus a self-contained Java 17 runtime, so the Triggevent Engine needs no Java install and no manual `build.sh`/`build.bat`. Running from source still builds the sidecar once (JDK 17 + Maven). The in-app hint now only appears in an unbuilt checkout, and links to the Temurin download if a Java runtime is genuinely missing. The sidecar also runs the engine against a display where you never see it - a throwaway Xvfb display on Linux when present, the session display otherwise - so Triggevent's own Swing overlays render off-screen and no longer appear on any OS.
- **Voices are now managed manually.** The built-in voice-library downloads (and per-voice Remove) are gone. To add a voice, download a Piper `.onnx` model and its `.onnx.json` config from the [Piper voice samples page](https://rhasspy.github.io/piper-samples/) and drop both files into the voices folder. The Model dropdown lists whatever is in that folder. Settings -> Voice has "Open voices folder" and "Refresh list" buttons.
- **Cactbot is now mutually exclusive with the master Triggers switch.** Cactbot isn't editable and its callouts double up with your Local/Triggevent triggers, so turning Cactbot on turns the master Triggers switch off (and vice versa). Cactbot is no longer auto-started by the master switch; it has its own toggle in Engine Settings.
- **Triggevent triggers list on open.** The engine's read-only rows are cached and shown in the Triggers list at launch (from the last harvest), so you can see and pre-disable them before the engine is started.

### Fixed
- The **Triggers tab no longer loads blank** - it shows your triggers immediately instead of only after switching sub-tabs. The trigger table also rebuilds without a flash when the engine inventory arrives.

### Removed
- The separate **Recorder tab** is gone; the **Current Instance** sub-tab (under Triggers) covers the live log.
- The separate **Triggevent on/off** toggle is gone; the engine now starts and stops with the master **Triggers** switch.
- The **Triggevent section and the find->replace override tables** are gone from the Engine Settings tab. Rewrite or silence a Triggevent callout by double-clicking it in the Triggers list instead.

## v0.7 - 2026-06-14

### Added
- **Engine triggers moved into the Triggers list and became editable.** The Cactbot and Triggevent Engines' own triggers now live in the main Triggers list under their fight (tagged by source) alongside your Local triggers, and the separate Engine Triggers sub-tab is gone. Double-click a Triggevent callout (or right-click -> Edit spoken text) to change what it says: the headless sidecar rewrites that trigger's own callout, so your wording fires live and Triggevent tokens (e.g. `{event.target}`, `{event.estimatedRemainingDuration}`) still substitute. Edits persist and re-apply each time the engine starts. Reset to Default restores the original.
- **Test TTS for engine triggers** - right-click -> Test TTS, or the play button in the edit dialog, speaks the callout with sample values for any tokens.

## v0.6 - 2026-06-14

### Added
- **Unified Triggers tab.** One master **Triggers** switch enables your Local triggers and auto-starts the Cactbot and Triggevent Engines, so every callout fires from one toggle. The engines' own triggers appear as read-only rows grouped by fight: uncheck one to ignore that callout, or right-click a static callout to rewrite or silence it. Triggevent suppression is per-trigger and live (the sidecar reports its full trigger inventory and tags each callout with a stable id), and Cactbot gained the same find->replace override layer with mid-session per-trigger disables.
- **Dancing Mad (Ultimate)** - the 8 static cast callouts are bundled and zone-gated; the converter now preserves Triggevent's `{event.target}`/`{event.source}` as `{target}`/`{source}`.
- **Authoring improvements** - a TTS preview button by the TTS field, a fight-tag picker, a Stacks min/max window with a new `{count}` token, smarter right-click prefill (cleans `unknown_`/hex junk and appends `on you` / `on {target}`), a quick-mute button by the volume slider, global search with a live result count and Type-column coverage, and a duplicate-trigger warning on save.

### Changed
- The old "CactEvent Watcher" tab became **Engine Settings** (URLs, build notes, override tables).

## v0.5.1 - 2026-06-14

### Added
- **Windows system TTS** - a new **Settings - Voice - Engine** selector chooses between **System** (the OS's built-in voice: Windows SAPI via PowerShell, macOS `say`, Linux `spd-say`/`espeak`) and **Piper** (offline neural). System is the default on Windows (no model download needed). Piper remains the default on Linux/macOS and is still selectable everywhere.
- **Changes tab** - an in-app "what's new" list.

### Fixed
- **Upgrade regression**: existing installs kept their Local triggers on instead of silently going quiet (Local triggers now default off only for brand-new installs).
- **Triggevent sidecar**: `stop()` no longer blocks the UI (process-group reap off-thread); Xvfb now uses 24-bit depth (8-bit could break Java AWT init); restart races that could steal/duplicate the writer/reader generation are fixed.
- **Trigger import converter**: `\n`/`\t` escapes no longer glue onto words; `=>` separator + token edge cases normalize cleanly.
- **Windows build script**: failed `git checkout` now aborts; cosmetic echo fixes.
- Stale in-app/README text that said Local triggers pause while Cactbot is on (they coexist now).

## v0.5 - 2026-06-14

### Added
- **Triggevent Engine** - run Triggevent's real engine (`xpdota/event-trigger`, GPL-3.0) headlessly as a sidecar, fed by a tee of your IINACT WebSocket stream, and speak **every** Triggevent trigger - built-in, EasyTriggers, and your Groovy scripts - including the complex code-based ones that can't be converted to local triggers. Toggle **Triggevent: ON/OFF** in the Cactbot/engine settings tab. Needs Java 17 + a one-time build of `triggevent-core` (`build.sh` / `build.bat`). On Linux it runs under Xvfb so Triggevent's own overlays stay hidden.
- **Coexisting sources** - Local, Cactbot, and Triggevent now run independently and can all be on at once; all three default to off and persist their on/off state between launches.

### Changed
- Callout text: `=>` separators are normalized to ", then" (e.g. "Stack => spread" -> "Stack, then spread"), and stray `=` is removed.

## v0.4 - 2026-06-01

### Added
- **Auto-update** - NyaaTriggers can now update itself. It quietly checks GitHub for a newer release on startup (toggle in **Settings - App - Check for updates on startup**), or via the **Check for Updates** button. When a newer version is found, a banner offers a one-click **Install**; nothing downloads without confirmation, and the app restarts itself when done.
  - **Source / git installs** (and macOS): runs `git pull --ff-only` then restarts.
  - **Linux release builds**: downloads the `.tar.gz`, swaps the app files in place, and preserves your settings, local triggers, and timelines. Old files are backed up and cleaned on the next launch.
  - **Windows release builds**: opens the GitHub releases page for a manual install (built-in self-replace planned).
  - Downloaded archives are written to a temp `.part` file and verified to extract within the install dir (path-traversal guard) before anything is swapped.

## v0.3 - 2026-06-01

### Added
- **In-game overlay** - an optional transparent overlay that draws timeline bars and on-screen alert pop-ups, driven in-process by the same trigger and timeline engine that speaks callouts (no separate process or IPC). Toggle it under **Settings - In-Game Overlay**.
  - Opens as a movable, resizable box: drag to move, drag the edges/corners to resize. Position and size are remembered between launches.
  - Three independent checkboxes: **See-through** (transparent background, still draggable), **Click-through** (passes mouse events to the game), **Accept position** (switches from sample placeholder to live data - tick this last).
  - **Show** option: Timeline + Alerts, Timeline only, or Alerts only.
  - Alerts are color-coded by severity (gold / peach / red); timeline bars deplete toward each cue and pulse red in the final seconds.
  - Linux: composited over the game when FFXIV runs inside gamescope (via gamescope overlay atoms; optional `python-xlib` dependency), otherwise a translucent window for positioning. Windows: a translucent always-on-top window over borderless FFXIV.
- **Self-scoped status triggers** - status-effect triggers (log types 26 GainsEffect / 30 LosesEffect) now fire only when the effect lands on you, never on other players. Your character name is auto-detected from the game's login/zone-in line and can be set or corrected in **Settings - Connection - My character**. The self-check runs before the cooldown, so another player's line for the same effect can't suppress yours.
- **Duration-window matching** - status triggers can match an effect's remaining duration (min/max seconds), so the same debuff with a different timer can drive a different callout (e.g. ordered tower/tether mechanics where 9s vs 18s means a different action).

### Changed
- Release CI workflow bumped to Node 24 actions (`setup-python@v6`, `action-gh-release@v3`).

## v0.2.1 - 2026-05-31

- Version-bump release over v0.2: untracked `triggers.experimental.json` and added it to `.gitignore`, then re-ran the release so the published Windows and Linux assets report 0.2.1. No functional changes.

## v0.2 - 2026-05-25

### Fixed
- Sequential (follow-up step) triggers no longer crash on completion. The final step removed its runner twice, raising a ValueError inside the log handler.
- Status-effect triggers (log types 26 GainsEffect and 30 LosesEffect) now fire. Matching read the wrong log field for the effect ID, so all 57 bundled status triggers (Walking Dead, M3S Short/Long Fuse, M12S Curtain Call, and more) never matched. `{source}` and `{target}` tokens now resolve correctly for these types as well.
- Follow-up sequence steps now honor the Ability ID field. Previously a step keyed by ID matched any ability of its log type.
- Test Fire no longer speaks `{source}` and `{target}` literally.
- Timeline labels containing a `#` are no longer truncated.
- `install.py` now downloads the correct default voice (`en_US-arctic-medium`) plus its `.onnx.json` config, instead of the old Amy model with no config.
- Experimental "Import to Triggers" now places the trigger inside the folder it creates when the source has no fight tag.
- Experimental recording no longer crashes when an existing trigger has a malformed Ability ID.
- Crash logs are now written next to the executable in frozen builds, instead of the temporary bundle directory that is wiped on exit.

### Changed
- Trigger table now tints rows: purple for custom triggers, green for locally modified bundled triggers.
- Windows build: the executable now carries the app icon, and only piper voice files are bundled (stray local `.pth` / `.index` models are excluded).

## v0.1.4 - 2026-05-25

### Added
- **Experimental tab** - records live combat and automatically drafts triggers from enemy casts as the instance progresses. The main trigger set is paused while recording; only experimental triggers fire. Draft triggers are stored in `triggers.experimental.json` and persist across sessions. The tab has its own sidebar tree grouping drafts by fight/zone. Right-click any draft row to Edit, Test Fire, Import to Triggers, or Delete it. Importing moves the trigger into a Custom folder on the Triggers tab.
- **Reset to Default toolbar button** - right-aligned red button on the second toolbar row of the Triggers tab. Resets all currently visible bundled triggers back to their original values from `triggers.json`. No confirmation prompt. Button placement acts as the safety.
- **UCoB Nael quote triggers** - 14 triggers covering Nael's in-game chat lines (log type 00) for Phase 2 main quotes (In/Stack/Spread/Out two-step sequences), Phase 2 divebombs, Phase 3 Fellruin (three-step sequences), and Phase 4 Adds phase (three-step sequences). Based on post-Stormblood quote pool.

### Changed
- Custom section in the fight tree is now always present at the bottom, even when empty. Pure-custom triggers no longer bleed into the top-level General group.
- Brighter base palette - all background and surface colors shifted lighter by approximately 8 per channel.
- INFO_HTML inline styles updated to match new palette.

### Removed
- "Reset All to Default" button removed from Settings tab.

### Fixed
- Custom > General group no longer shows bundled official triggers - only pure-custom triggers (not in `triggers.json`) appear there.
- Fight tree selection restoring to the wrong node (e.g. top-level General instead of Custom > General) after a tree refresh.
- `icon_nyaa.png` was missing from the Windows PyInstaller spec - the app icon now appears correctly in the Windows build.
- Removed dead `_reset_all` method that was no longer reachable from any UI element.

---

## v0.1.3 - 2026-05-23

- FFXIV gold/navy theme replacing the Catppuccin Mocha base
- Master volume slider in the tab bar corner (0-200%)
- Animated first-run setup dialog with progress bar
- Default voice switched to `en_US-arctic-medium`
- Fixed release workflow (voice model asset)

## v0.1.2 - 2026-05-23

- Settings tab: voice library, export/import triggers, update triggers from GitHub
- Zone dot column - live green/red indicator per trigger row
- Sequence builder in trigger dialog
- TTS speed and interrupt controls
- Current Instance tab improvements

## v0.1 - 2026-05-23

- Initial public release
