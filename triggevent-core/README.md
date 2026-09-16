# triggevent-core - headless Triggevent Engine sidecar for NyaaTriggers

Run **all** of Triggevent's triggers (built-in Java + user Groovy) inside NyaaTriggers
**without opening Triggevent**, by reusing Triggevent's own engine
(`xpdota/event-trigger`, GPL-3.0) headlessly as a subprocess.

This directory is the Java half. The Python half lives in `../nyaatriggers/triggevent_bridge.py`
plus small hooks in `../nyaatriggers/ws_client.py` and `../nyaatriggers/main_window.py`.

---

## Why this shape (decision record)

- Triggevent's complex triggers are **Groovy** bound to its Java API. There is **no
  pure-Python way** to run them. Groovy is JVM bytecode. "Run all their triggers
  automatically, standalone" therefore *requires a JVM in the loop*.
- Hand-porting every Groovy trigger to NyaaTriggers' JSON format (the pure-Python
  alternative) can never be "all, automatically" and is perpetual manual work,
  especially now that **cactbot is no longer updating its triggers** (so Triggevent
  is the only living source).
- A source dive of `event-trigger` found the engine is **separable from its Swing
  GUI at runtime** (the GUI only instantiates if you register `GuiMain`), and their
  own test harness (`XivMain.testingMasterInit` + `testutils/.../ExampleSetup`) is a
  ready-made headless template. Verdict: **MODERATE, ~days**, not a fork-and-surgery
  job.

So: build a small headless Java program (`triggevent-core`) that boots Triggevent's
engine, feeds it the live FFXIV WS stream **teed from NyaaTriggers**, and streams
every resolved callout back as JSON. NyaaTriggers stays the UI/overlay/TTS front end.

## License

`event-trigger` is **GPL-3.0**; NyaaTriggers is MIT. `triggevent-core` is a **separate
process** that NyaaTriggers spawns and talks to over stdin/stdout (arms-length IPC,
"mere aggregation"), so NyaaTriggers can stay MIT - same as it already aggregates the
GPL Piper TTS binary. `triggevent-core` itself is GPL-3.0 (it links event-trigger).
Do **not** embed the JVM in-process (jpype/py4j). That would make the combined work GPL.

---

## Architecture / data flow

```
FFXIV ─► IINACT/OverlayPlugin (ws://localhost:10501/ws)
              │  (single WS connection, owned by NyaaTriggers)
              ▼
        NyaaTriggers (Python, PyQt6)
        ws_client.py: raw_message signal ── tees every raw WS JSON msg ──┐
              ▲                                                          │ stdin (1 json/line)
              │ callout JSON (1/line) stdout                             ▼
        triggevent_bridge.py  ◄───────────────────────  triggevent-core (Java, headless JVM)
              │                                          XivMain.masterInit() - no GuiMain
              ├─► _overlay_alert(text, severity)         ActWsRawMsg(line) ► EventMaster.pushEvent
              └─► speak(tts)  [Piper]                     CalloutEvent ► JSON ► stdout
```

### Wire protocol (stdin → sidecar)
One JSON object per line: the **raw IINACT/OverlayPlugin WS message, verbatim** (the
exact string NyaaTriggers receives in `WSClient._on_message`). The sidecar wraps each
line as `new ActWsRawMsg(line)` and pushes it; Triggevent's `ActWsHandlers` dispatches
`LogLine → ACTLogLineEvent`, `CombatData`/combatants, `ChangePrimaryPlayer`,
`ChangeZone`, `PartyChanged`, etc. - exactly as in live mode.

NyaaTriggers must therefore **subscribe to the events Triggevent needs**, not just
`LogLine`/`CombatData`. See `ws_client.py` `_SUBSCRIBE`.

### Wire protocol (sidecar → stdout)
One JSON object per line, `{"t":"callout", ...}` for callouts and `{"t":"status",...}`
for lifecycle. Callout fields (from `CalloutEvent`):
```json
{"t":"callout","tts":"stack","text":"Stack","severity":"info",
 "color":"#RRGGBB|null","sound":"id|null","expired":false,
 "key":"<trackingKey>","replaces":"<id|null>"}
```
`severity` is derived: alarm if a red `colorOverride`, else alert if a non-default
color, else info. (Triggevent has no first-class severity enum. Color encodes urgency.)
Any non-JSON stdout line is treated as a log/diagnostic and forwarded to stderr.

---

## Key event-trigger entry points (verified against master)

| Purpose | Class / method |
|---|---|
| Headless container boot | `gg.xp.xivsupport.sys.XivMain#masterInit(Consumer<MutablePicoContainer>)` |
| Test boot (no live ACT) | `XivMain#testingMasterInit()` |
| Push a raw WS msg in | `new gg.xp.xivsupport.events.ws.ActWsRawMsg(String json)` → `EventMaster#pushEvent` |
| Push a raw log line in | `new gg.xp.xivsupport.events.ACTLogLineEvent(String rawLine)` |
| Event bus master | `gg.xp.reevent.events.EventMaster#pushEvent / pushEventAndWait` |
| Subscribe to output | `EventDistributor#registerHandler(CalloutEvent.class, handler)` |
| Callout object | `gg.xp.xivsupport.speech.CalloutEvent` - `getCallText/getVisualText/getColorOverride/getSound/isExpired/trackingKey/replaces` |
| Central emit site | `gg.xp.xivsupport.callouts.CalloutProcessor` (post-Groovy resolution) |
| Groovy scripts dir | `gg.xp.xivsupport.sys.Platform#getGroovyDir()` → `~/.triggevent/userscripts` (Linux) |
| Headless template | `testutils/testutils-xiv/.../events/ExampleSetup.java` |

Build: **Maven**, **Java 17**, Groovy `5.0.6`. `act-stub-plugin-assembly` is a
C#/.NET ACT plugin - irrelevant here (NyaaTriggers replaces its role).

---

## Build (from source, developers only)

> **End users don't build this.** Since v0.8, release builds bundle the prebuilt sidecar
> jar plus a self-contained Temurin JRE 17, so there is no separate Java install and no
> build step for users. The steps below are only for developers building from source.

```bash
cd triggevent-core
./build.sh            # installs event-trigger to ~/.m2, builds the fat jar
# produces: triggevent-core/target/triggevent-core.jar
```
Requires JDK 17 + Maven (the script checks and tells you how to install on Arch).
The build clones/uses `event-trigger` and `mvn install`s the engine modules into your
local Maven repo, then shades them into one runnable jar.

Run standalone (debug):
```bash
java -Djava.awt.headless=true -jar target/triggevent-core.jar
# then paste raw IINACT WS JSON lines on stdin; callout JSON appears on stdout
```

NyaaTriggers auto-discovers the jar at `triggevent-core/target/triggevent-core.jar`
(or `$NYAA_TRIGGEVENT_JAR`). `TriggeventBridge.is_available()` returns False if either
`java` or the jar is missing, and the feature silently stays off.

---

## Engine source: the CateDesu fork and the main branch

Since 2026-09 the engine builds from `CateDesu/event-trigger`, branch `main`,
not directly from upstream. The guards that used to be `patches/*.patch` now
live there as plain commits. `build.sh` pins a commit on that branch (`ET_REF`),
and `update_engine` fast-forwards the local clone to the branch head and builds
that instead.

Syncing upstream is a deliberate manual ritual, never automatic:

```bash
cd triggevent-core/event-trigger
git remote add upstream https://github.com/xpdota/event-trigger.git   # once
git fetch upstream master
git merge upstream/master        # into main, resolve conflicts, test
git push fork main
# then bump ET_REF in build.sh to the new main tip and rebuild
```

Use merge, not rebase, and never force-push main. Update the engine commit
in both build scripts after testing the merge.

---

## STATUS (built + shipped in v0.5.1, 2026-06-14)

> **Historical snapshot (v0.5.1).** This records the initial sidecar bring-up. Since v0.8
> the engine is bundled in release builds and follows the master **Triggers** switch -
> there is no separate Triggevent toggle or status label, and cactbot is configured in
> **Settings - Cactbot** (no engine tab). See the main README for current behavior.

Implemented, built, validated-to-boot, code-reviewed, and shipped. The then-open item
(proving a real callout fires) is now **RESOLVED**: Triggevent runs as a working primary
engine since v0.7/v0.8 and real callouts fire live in-game (see "Validation findings" below).

- [x] Source dive, design + wire protocol (this file)
- [x] Python `ws_client.py` - raw-message tee + expanded `_SUBSCRIBE`
- [x] Python `triggevent_bridge.py` - subprocess mgmt + tee + callout JSON. Hardened in
      v0.5.1: non-blocking `stop()` (process-group reap off the GUI thread),
      generation-bound reader/writer/stderr threads (no restart races), 24-bit Xvfb
- [x] Python `main_window.py` - signal wiring, `triggevent_enabled` setting, startup
      auto-enable, Triggevent toggle + status label (in the Cactbot/engine tab), coexistence
- [x] Java `pom.xml`, `TriggeventCore.java`, `build.sh`, `build.bat` (Windows)
- [x] BUILT (JDK 17 + Maven) → `target/triggevent-core.jar`; `xvfb-run` installed
- [x] VALIDATED boot under Xvfb: engine boots, loads the real `~/.triggevent`
      (EasyTriggers + startup Groovy), discovers all triggers, emits the JSON protocol,
      processes a teed `ChangePrimaryPlayer`/`ChangeZone` feed (state updates, timelines load)
- [x] Adversarial code review (13 confirmed findings) fixed; `NYAA_TV_DIAG=1` pipeline
      event-count diagnostic added to the sidecar
- [x] **RESOLVED (v0.7/v0.8+): real callouts fire live.** Triggevent ships as a working
      primary engine; the offline-replay 0-callout below was a near-empty test config, not
      an engine bug (see "Validation findings")
- [x] Combatant polling and engine refresh requests share the existing IINACT connection.

## Validation findings - the 0-callout (2026-06-14)

> **RESOLVED (v0.7/v0.8+).** The 0-callout traced to the near-empty test `~/.triggevent`,
> not the engine. As a shipped primary engine Triggevent fires real callouts live in-game.
> The forensic detail below is kept for reference.

Replaying a full UwU log (`testutils-samplelogs/uwu.log`) through the sidecar with
`NYAA_TV_DIAG=1` produced **zero callouts**. The diagnostic counts pinpoint why:

```
ACTLogLineEvent=50000  AbilityUsedEvent=9634  AbilityCastStart=974  BuffApplied=6691
RawModifiedCallout=0   CalloutEvent=0         TtsRequest=0
```

Log lines parse perfectly and ability/status events flow, but **no trigger ever fires**
(`RawModifiedCallout=0`). So the harvest path (`registerHandler(CalloutEvent.class, …)`) is
**correct**. The issue is upstream: triggers aren't *activating*. Most likely the trigger
enabled-state / duty activation: the test box's `~/.triggevent` config is ~199 bytes
(near-empty), so built-in triggers sit at their defaults. This is **not** the v2 combatant-poll
gap (that affects only position/HP triggers) and **not** a delayed-event/drain timing issue
(sequential triggers resume on the next event's effective time during replay). It needs a
**live in-game test**, or triggers enabled in Triggevent first. Use `NYAA_TV_DIAG=1` to re-trace.

## IMPORTANT: display / Xvfb (resolved during validation)

The engine auto-instantiates Swing overlay components at boot (`PartyOverlay`,
timeline/callout overlays), so it **cannot run under forced `-Djava.awt.headless=true`**.
That throws `HeadlessException` and aborts boot. It needs a display. The Python bridge
therefore launches it under **`xvfb-run -a`** when available (a throwaway virtual display,
so Triggevent's own overlays render invisibly and never appear on the user's screen. We
only harvest its `CalloutEvent`s). Install once:

```bash
sudo pacman -S xorg-server-xvfb
```

Without `xvfb-run`, the bridge falls back to the inherited session display and the engine
still works, but any Triggevent overlays the user has enabled **will show on screen**.

## Build notes (mostly verified during the v0.5.1 build/run)

1. **Reflection on private `XivMain.requiredComponents()`** - VERIFIED working. Gives a clean
   container with no live `ActWsLogSource`. If a future event-trigger renames it, the sidecar
   throws `NoSuchMethodException` at startup (fix the name, or add a public `headlessInit()` to
   your fork).
2. **`AutoHandlerConfig.setNotLive(true)` is public** - VERIFIED (compiles + runs).
3. **Real persistence loads EasyTriggers + settings** - VERIFIED: the boot log showed
   `easy-triggers.my-triggers-2`, custom cooldowns, and startup Groovy loading from
   `~/.triggevent`. (`UserDirPropsPersistenceProvider.inUserDataFolder("triggevent", true)`,
   read-only, added before `InitEvent`.)
4. **Engine subset builds clean** - VERIFIED. Gotcha fixed: `triggers` is a `packaging=pom`
   aggregator with no jar; depend on its code sub-modules
   (`triggers-general,-ew,-sb,-dt,titan-jails`) instead. Build uses
   `mvn -Dmaven.test.skip=true -pl :xivsupport,:trigger-support,:triggers-*,:titan-jails,:easytriggers,:timelines -am install`.
   Groovy `5.0.6` resolved fine.
5. **Severity heuristic** (`getColorOverride()` → info/alert/alarm) - still a GUESS. Tune
   `TriggeventCore.severity()` once real callout colors are observed in-game. (OPEN)
6. **Broadcast-wrapper IINACT variant** - UNTESTED. If your IINACT wraps log lines in a
   `{"type":"broadcast","msgtype":"logline","msg":...}` envelope, `ActWsHandlers` may not unwrap
   it. Unwrap in `triggevent_bridge.feed()` or `TriggeventCore` if needed (see
   `ws_client._extract_raw`). (OPEN)
7. **Do NOT add an explicit `jackson-databind` pin to `pom.xml`.** Inherit Jackson
   transitively from `xivsupport` (this is the **v1.0.6 fix**). The old explicit
   `com.fasterxml` jackson-databind 2.x pin survived Triggevent's upgrade to Jackson 3 and
   dragged Jackson-2 annotations onto the classpath, which Maven mediation forced over the
   3.x annotations the engine was compiled against, throwing `NoSuchFieldError` at boot and
   failing ~13 components. Reintroducing any explicit pin re-triggers that startup crash.

## Automatic pull recovery

On startup or reconnection, the program matches its first live log line against the
local IINACT network log. It restores actors at the last wipe or zone entry and
silently replays the current pull before continuing with buffered live events.
This restores trigger counters, buffs and pending waits without fight-specific
recovery rules. Historical callouts and engine automarks are suppressed. Delayed
events use the replay clock and continue on the live clock after the handoff.

History selection and actor reconstruction live in Triggevent's `PullHistoryReader`.
Its `PullRecovery`, `RecoveryClock` and `RecoveryQueue` own replay ordering and the
live handoff. Python sends the log folder, first buffered line and current state in
a `recover_log` command. It does not crop or reconstruct the log. The sidecar keeps
callout output and automarks muted until recovery ends.

The engine advertises `catchup=1` for the acknowledged handoff. Each batch ends
with `recover_checkpoint` and a numbered acknowledgement. Python buffers new input
while history loads and sends further batches until the engine has consumed it.
Only then does it send `recover_end`. The engine drains elapsed timers before
resuming output. Due timers also run before later log events after the handoff,
so buffered bursts preserve chained waits. Automarks are checked for freshness
both before entering the Telesto queue and before the HTTP request.

Malformed history timestamps are skipped and counted. The final acknowledgement
reports complete, degraded, unavailable or failed history restoration. Current
state without a history request is reported separately. These results are written
to `triggevent.log`.

An actor recorded before the zone announcement can be restored when a later partial
update confirms it is still present. The shared log parser preserves this state in
uninterrupted log replay too. A new actor Add starts fresh position data.
Removed actors and unconfirmed actors from a previous zone are not used as seeds.

The existing IINACT log folder is found automatically. Recovery needs a matching
local log containing the pull boundary and player. It cannot reconstruct events
that IINACT never recorded, or read logs from a remote ACT machine. If history is
unavailable, the engine receives current world state and the buffered live feed,
and the reason is written to `triggevent.log`. This does not repair unrelated bugs
inside individual triggers or custom scripts that use their own timers.

Combatant snapshots use the response tags expected by Triggevent. Engine requests
for current positions and HP are relayed to IINACT, and actor spawn and removal
lines update state immediately. Repeated announcements of the same zone preserve
buffs and ongoing sequences.

Recovery commands are local stdin controls. WebSocket frames cannot send them.
An older engine jar without local history support receives only current state and
the buffered live feed. The engine advertises local history support as `history=1`.

Verify the shared engine behavior after building:

```bash
python3 test_recovery.py --compare
python3 test_recovery_protocol.py
```

The comparison checks delivered call IDs, resolved text, order and event timing
against uninterrupted replay and passes the serialized calls through the Python
bridge. The protocol test delays the local log flush and verifies that newly
arriving input stays in recovery and malformed history is reported accurately.
To test automatic history reconstruction, also supply `--recording`, `--cut`,
`--history-folder`, `--zone` and `--player`. The recording must retain the original
log lines so the selected cut can be matched exactly in the network log. It must
also contain the complete original state and pull for the independent reference.
The reference does not use the history reader.
