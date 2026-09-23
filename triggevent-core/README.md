# Triggevent engine host

Run Triggevent's built-in Java triggers and user Groovy scripts in a separate JVM process. NyaaTriggers supplies the IINACT feed and handles speech and display.

The program's visual callout builder uses the same engine's `SequentialTrigger` controller, event types, status repository and recovery clock. Definitions arrive through the `custom_triggers` command before pull recovery starts. Configuration changes run on the engine event queue. Unchanged definitions keep their pending waits, while changed or removed definitions stop them. Callouts carry their saved `nyaa:` IDs for the normal row and profile controls.

The host advertises `custom=1` in its ready message and acknowledges each definition update. The replay check is `python3 triggevent-core/test_custom_triggers.py` from the program root after building the jar. It covers conditional speech, status state, sequence cancellation, invalid edits, zone restrictions, timeouts and recovery using a builder definition.

Python integration lives in `../nyaatriggers/triggevent_bridge.py`.

---

## Design

Groovy triggers depend on Triggevent's Java API. The host runs that engine using the
program's existing WebSocket feed and returns resolved callouts as JSON.

## License

`triggevent-core` links to the GPL-3.0 `event-trigger` engine and is GPL-3.0. NyaaTriggers remains MIT and communicates with it through stdin/stdout. Keep the JVM in a separate process.

---

## Wire protocol

stdin accepts raw IINACT WebSocket JSON and local control messages, one per line.
Keep `ws_client.py`'s `_SUBSCRIBE` list aligned with the events the engine needs.
stdout emits JSON callouts and status. Message fields and commands are defined in
`src/main/java/gg/xp/nyaa/TriggeventCore.java` and the Python bridge.

Severity is inferred from color because Triggevent has no severity enum: red means
alarm, another override means alert, and no override means info. Non-JSON stdout
is treated as diagnostics.

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

Each engine process runs from a private temporary copy of the jar. Rebuilding or updating the source jar leaves the running engine's classes available. The copy is removed after the process stops, and the next engine start loads the updated jar. Standalone debug runs use the supplied path directly, so stop them before rebuilding that jar.

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

## Build notes

- Use Xvfb to hide Swing windows. Without it, enabled engine overlays may appear on
  the session display. Forced AWT headless mode aborts with `HeadlessException`.
- Boot reflects `XivMain.requiredComponents()` to omit `ActWsLogSource`. If upstream renames it, update the reflection target or expose an engine initializer.
- `AutoHandlerConfig.setNotLive(true)` is public. The persistence provider loads EasyTriggers, settings, and startup Groovy from `~/.triggevent` before `InitEvent`.
- `triggers` is a Maven aggregator, so depend on its code submodules. The build uses Java 17 and Groovy `5.0.6`.
- Do not add an explicit `jackson-databind` pin to `pom.xml`. Inherit it through `xivsupport`. The old Jackson 2 pin conflicted with the engine's Jackson 3 annotations and caused `NoSuchFieldError` during startup.
- Use `NYAA_TV_DIAG=1` to compare pipeline event counts. Parsed events without callouts
  can indicate missing `~/.triggevent` configuration, as in an early UwU replay.

## Automatic pull recovery

On startup or reconnection, the program matches its first live log line against the
local IINACT network log. It restores actors at the last wipe or zone entry and
silently replays the current pull before continuing with buffered live events.
This restores trigger counters, buffs and pending waits without fight-specific
recovery rules. Historical callouts and engine automarks are suppressed. Delayed
events use the replay clock and continue on the live clock after the handoff.

History reconstruction and replay ordering belong to the engine. Python sends the
log folder, first buffered line, and current state through `recover_log`.

`catchup=1` advertises acknowledged batches. Each ends with `recover_checkpoint`.
Python buffers new input until every batch is acknowledged, then sends `recover_end`.
Due timers run before later events to preserve chained waits. Recovery, pull changes,
and automark setting changes cancel pending marks before HTTP dispatch.

Feed queue overflow restarts recovery in a fresh engine. It cannot evict a queued
recovery command and leave the engine silently waiting. A missing acknowledgement
also triggers a fresh recovery after 60 seconds without progress.

Malformed history timestamps and rejected parser fields are skipped and counted,
including failures in buffered input during recovery. The final acknowledgement
reports complete, degraded, unavailable or failed history restoration. Current
state without a history request is reported separately. These results are written
to `triggevent.log`.

The existing IINACT log folder is found automatically. Recovery needs a matching
local log containing the pull boundary and player. It cannot reconstruct events
that IINACT never recorded, or read logs from a remote ACT machine. If history is
unavailable, the engine receives current world state and the buffered live feed,
and the reason is written to `triggevent.log`. This does not repair unrelated bugs
inside individual triggers or custom scripts that use their own timers.

Repeated announcements of the same zone preserve buffs and ongoing sequences.

Recovery commands are local stdin controls. WebSocket frames cannot send them.
An older engine jar without local history support receives only current state and
the buffered live feed. The engine advertises local history support as `history=1`.

Verify the shared engine behavior after building:

```bash
python3 test_recovery.py --compare
python3 test_recovery_protocol.py
```

These compare call IDs, text, order, and timing against uninterrupted replay, and
check buffered input, malformed history, queue overflow, and delayed automark cleanup.
To test automatic history reconstruction, also supply `--recording`, `--cut`,
`--history-folder`, `--zone` and `--player`. The recording must retain the original
log lines so the selected cut can be matched exactly in the network log. It must
also contain the complete original state and pull for the independent reference.
The reference does not use the history reader.
