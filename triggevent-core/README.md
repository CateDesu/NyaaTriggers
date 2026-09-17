# Triggevent engine host

Run Triggevent's built-in Java triggers and user Groovy scripts in a separate JVM process. NyaaTriggers supplies the IINACT feed and handles speech and display.

The Java host is here. Python integration lives in `../nyaatriggers/triggevent_bridge.py`, with feed and interface hooks in `ws_client.py` and `main_window.py`.

---

## Design

Groovy triggers depend on Triggevent's Java API. Hosting the engine preserves that logic without manually translating each trigger. The host boots the engine without `GuiMain`, forwards the existing WebSocket feed, and returns resolved callouts as JSON.

## License

`triggevent-core` links to the GPL-3.0 `event-trigger` engine and is GPL-3.0. NyaaTriggers remains MIT and communicates with it through stdin/stdout. Keep the JVM in a separate process.

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

Each line is a raw IINACT/OverlayPlugin WebSocket message. The host wraps it in `ActWsRawMsg` and pushes it through `EventMaster`. Triggevent dispatches logs, combatants, player changes, zones, and party changes. Keep `ws_client.py`'s `_SUBSCRIBE` list aligned with the events the engine needs.

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

## Key event-trigger entry points

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

Releases bundle the engine jar and Temurin JRE 17. Source users can install Java 17 and download the jar through **Settings - Program - Update Triggevent Engine**.

To rebuild, install JDK 17 and Maven:

```bash
cd triggevent-core
./build.sh
```

The script clones the engine, installs its modules into the local Maven repository, and builds `target/triggevent-core.jar`. Windows uses `build.bat`.

For a standalone debug run on Linux:

```bash
xvfb-run -a java -jar target/triggevent-core.jar
```

Paste raw IINACT JSON lines into stdin; callout JSON appears on stdout. A session X display can replace Xvfb. Do not force `java.awt.headless=true`: engine components construct Swing windows.

The bridge finds `triggevent-core/target/triggevent-core.jar` or `$NYAA_TRIGGEVENT_JAR`. `is_available()` requires both Java and the jar.

---

## Engine source: the CateDesu fork and the main branch

The engine builds from `CateDesu/event-trigger` on `main`. Former `patches/*.patch` guards are commits in that fork. `build.sh` pins `ET_REF`; `update_engine` builds the latest fork commit.

Merge upstream changes manually:

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

## Initial release

The sidecar first shipped in v0.5.1 and became a working primary engine in v0.7/v0.8. Initial validation covered boot, persistence, trigger discovery, feed dispatch, subprocess cleanup, and combatant polling. See the [main README](../README.md#choosing-callouts) for current controls.

## Early replay issue

An early UwU replay produced parsed ability and status events but no `RawModifiedCallout` events. The cause was a near-empty test `~/.triggevent` configuration. Live callouts were later verified. Use `NYAA_TV_DIAG=1` to compare pipeline event counts when investigating similar failures.

## Display requirements

The bridge uses `xvfb-run -a` when available so Swing components render invisibly. Without it, the engine uses the session display and enabled Triggevent overlays may appear. See [Linux dependencies](../README.md#linux-system-dependencies). Forced AWT headless mode aborts startup with `HeadlessException`.

## Build notes

- Boot reflects `XivMain.requiredComponents()` to omit `ActWsLogSource`. If upstream renames it, update the reflection target or expose an engine initializer.
- `AutoHandlerConfig.setNotLive(true)` is public. The persistence provider loads EasyTriggers, settings, and startup Groovy from `~/.triggevent` before `InitEvent`.
- `triggers` is a Maven aggregator, so depend on its code submodules. The build uses Java 17 and Groovy `5.0.6`.
- Severity remains a color-based heuristic. Broadcast-wrapped IINACT messages were not covered by the initial validation.
- Do not add an explicit `jackson-databind` pin to `pom.xml`. Inherit it through `xivsupport`. The old Jackson 2 pin conflicted with the engine's Jackson 3 annotations and caused `NoSuchFieldError` during startup.

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
before entering the Telesto queue. Their configured delays remain valid, while
recovery, pull changes and automark configuration changes cancel pending requests
before the HTTP request.

Feed queue overflow restarts recovery in a fresh engine. It cannot evict a queued
recovery command and leave the engine silently waiting. A missing acknowledgement
also triggers a fresh recovery after 60 seconds without progress.

Malformed history timestamps and rejected parser fields are skipped and counted,
including failures in buffered input during recovery. The final acknowledgement
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
It also fills the production feed queue during the handoff and checks that a new
engine resumes live callouts. The shared checks use a local HTTP server to verify
delayed automarks and cancellation across recovery, disabling automarks and wipes.
To test automatic history reconstruction, also supply `--recording`, `--cut`,
`--history-folder`, `--zone` and `--player`. The recording must retain the original
log lines so the selected cut can be matched exactly in the network log. It must
also contain the complete original state and pull for the independent reference.
The reference does not use the history reader.
