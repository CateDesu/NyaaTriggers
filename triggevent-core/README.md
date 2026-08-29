# triggevent-core - headless Triggevent Engine sidecar for NyaaTriggers

Run **all** of Triggevent's triggers (built-in Java + user Groovy) inside NyaaTriggers
**without opening Triggevent**, by reusing Triggevent's own engine
(`xpdota/event-trigger`, GPL-3.0) headlessly as a subprocess.

This directory is the Java half. The Python half lives in `../triggevent_bridge.py`
plus small hooks in `../ws_client.py` and `../main_window.py`.

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
- [ ] v2: combatant-poll relay for live position/HP triggers - **NOT implemented** (see Limitations)

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

## Limitations (v1 - current) vs. v2 (NOT implemented)

**v1 (what ships now):** one-way tee. NyaaTriggers forwards the IINACT WS stream to the sidecar;
the sidecar runs the engine and emits callouts. This covers triggers driven by the **log + event
stream**: ability casts, ability use, status gain/lose, headmarkers, tethers, plus
job/zone/party state (reconstructed from log lines 01/02/03/11/12).

**v2 (planned, NOT built):** a **bidirectional combatant-poll relay**. Some triggers read live
**combatant positions / current HP%**, which come from OverlayPlugin's combatant-poll WebSocket
(Triggevent issues `RefreshCombatantsRequest`), data the one-way tee does not carry. v2 would let
the sidecar request a poll → NyaaTriggers issues the WS call → the response is teed back. Until
then, position/HP-dependent triggers are degraded. **This is a separate concern from the
0-callout above** (which is trigger activation, affecting all triggers, not just position/HP ones).

- If NyaaTriggers connects mid-session, player/zone/job/party stay unknown until the next
  01/02/03/11/12 line (or the corresponding WS state message) arrives.
