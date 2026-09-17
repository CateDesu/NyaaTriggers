# Triggernometry engine host

Host Triggernometry's conditions, variables, delayed actions, trigger chains, and C# `ExecuteScript` actions in a separate process. Windows uses native .NET; Linux uses Mono.

This directory contains the C# host and prebuilt `bin/`. Python integration is in `../nyaatriggers/triggernometry_bridge.py`, with feed and interface hooks in `ws_client.py` and `main_window.py`.

First shipped in v1.1.0. Replay checks pass; live IINACT validation is pending. See the [build record](SPIKE-LOG.md) and [design](DESIGN.md).

---

## Design

Scripted packs use Triggernometry's .NET types and shared engine state, so the host runs the original engine. Its WinForms interface registers triggers and must be constructed, but remains hidden. Config flags disable the SharpDX overlay; build fixups and no-op audio assemblies allow Mono operation without changing engine logic.

Both Triggernometry and NyaaTriggers are MIT licensed. The subprocess isolates the runtime and platform dependencies.

## Architecture / data flow

```
FFXIV ─► IINACT/OverlayPlugin (ws://localhost:10501/ws)
              │  (single WS connection, owned by NyaaTriggers)
              ▼
        NyaaTriggers (Python, PyQt6)
        ws_client.py:  log_line  ── extracted pipe-delimited log lines ──┐
                       combatants ── getCombatants poll (positions/HP) ──┤ stdin (1 json/line)
              ▲                                                          ▼
        triggernometry_bridge.py ◄──────────────────  triggernometry-core (C#, headless; .NET on Win / Mono+Xvfb on Linux)
              │  callout JSON (1/line) stdout            RealPlugin.InitPlugin (real UI, invisible)
              ├─► _overlay_alert(text, severity)         OnLogLineRead(line) ► triggers + ExecuteScript
              └─► speak(tts)  [Piper]                     TtsPlaybackHook ► {"t":"callout"} ► stdout
                                                          InstanceHook ► fake combatant data (${_me} etc.)
```

### Wire protocol - NyaaTriggers ► sidecar (stdin), one JSON object per line
```json
{"t":"log","line":"21|..."}                          // a raw pipe-delimited FFXIV log line
{"t":"zone","id":<n>,"name":"<zone>"}                // explicit zone change (also derived from 01| lines)
{"t":"combatants","me":<id>,"list":[{...}, ...]}     // a fresh combatant snapshot (id,name,job,hp,x,y,z,h,party,...)
{"t":"endpoint","body":"<Telesto notification JSON>"} // delivered to the engine's Endpoint source
```
### Wire protocol - sidecar ► NyaaTriggers (stdout), one JSON object per line
```json
{"t":"callout","tts":"..."}                          // a resolved callout (route to overlay + Piper)
{"t":"sound","file":"...","volume":100}              // a sound the engine wanted to play
{"t":"status","active":true|false,"msg":"..."}       // lifecycle
```

---

## Build

The prebuilt `bin/` is committed and bundled in releases. Rebuild only when changing the host or engine pin. Requirements: Mono 6.12+ with `mcs` and `xbuild` or `msbuild`, Git, and curl.

```bash
cd triggernometry-core
bash build-all.sh
```

The script clones the pinned engine into `$HERE/.engine`, applies `engine-fixups.patch` and the designer filename correction, builds `shims/src/*.cs`, restores NuGet packages, and assembles `bin/`. The output targets .NET Framework 4.6.2.

For an existing engine checkout, run `build-engine.sh` then `build-host.sh`, which also runs `package.sh`.

## Packaging / release

`NyaaTriggers.spec` bundles the committed `triggernometry-core/bin/` directly. CI does not rebuild it.

- **Windows:** run the executable with native .NET Framework. No Mono or Xvfb is needed.
- **Linux:** use system Mono and the [Linux dependencies](../README.md#linux-system-dependencies). A bundled Mono runtime remains future work.

The `System.Speech` and `WMPLib` stubs leave audio to the host hooks. `UseScarborough=false` disables the SharpDX overlay while the hidden WinForms interface loads triggers.

Standalone debug test:

```bash
xvfb-run -a mono bin/triggernometry-core.exe /tmp/cfg test/spike-me-pack.xml "00|t|0839|S|SPIKEME go|x"
```

Server mode:

```bash
xvfb-run -a mono bin/triggernometry-core.exe <cfgDir> --serve <pack1.xml> <pack2.xml> ...
```

Send log and combatant JSON on stdin; callouts appear on stdout. The bridge finds `bin/triggernometry-core.exe` or `$NYAA_TRIGGERNOMETRY_EXE`. Packs use `$NYAA_TRIGGERNOMETRY_PACKS` or the [default pack folder](../README.md#updating-and-saved-data). Availability requires the executable and, on Linux, Mono.

---

## Engine integration

- The host writes UTF-8 `Triggernometry.config.xml`. `UseScarborough=false` and `WindowToMonitor=""` avoid Windows display calls. ACT audio routing uses host hooks. `UpdateNotifications/DefaultRepository=No` suppresses first-run toasts.
- Packs are grafted into `cfg.Root` before `InitPlugin`, allowing the UI tree walk to register triggers. `BeforeLogLineRead` receives raw network lines; `OnLogLineRead` receives ACT-formatted lines from `host/ActLogLine.cs`, without the wire checksum. `TtsPlaybackHook` captures callouts. Engine diagnostics go to stderr and `triggernometry.log`.
- Roslyn compiles `ExecuteScript` under Mono. `FixupExecuteScriptAssemblies` replaces empty assembly expressions with a loaded assembly name to avoid the engine's `AddReferences("")` error.
- `InstanceHook` supplies a fake ACT plugin backed by IINACT combatant snapshots for player, party, position, and HP data. See `host/CombatantBridge.cs`.

## Remaining work

- Validate live IINACT behavior, including `_map_combatants` field casing in `../nyaatriggers/ws_client.py`.
- Legacy auras and direct game-memory scripts remain unsupported. ACT combat-state and encounter-duration hooks return initial values.
- The first Roslyn compilation under Mono can exceed 2.5 seconds. A representative warm-up may help.
- Consider bundling Mono for Linux releases.

## Replay checks

Run `python3 -m tests.test_triggernometry_host` from the program's root with Mono and Xvfb installed.
The suite exercises the vendored executable, using isolated temporary configuration directories.
Fixtures in `test/packs/paissa` preserve the original TOP marker and Zelenia Bloom actions.
The checks cover marker offsets, player filtering, wipe resets, ACT and network source separation,
shared variables, generated log messages, delayed follow-ups and C# script results.
These are replay checks, not live fight validation.

The Zelenia replay loads a separate test observer to wait for phase setup,
cleanup and map state before sending dependent events. The original pack's
asynchronous actions remain intact. A second replay delays those state actions
to check that the test waits for completion even on a slow engine start.

## Telesto integration

`nyaatriggers/triggernometry_telesto.py` owns a loopback HTTP relay for each engine run.
The Python bridge passes its URL through `NYAA_TRIGGERNOMETRY_TELESTO_RELAY` and its callback address through
`NYAA_TRIGGERNOMETRY_CALLBACK_URI`. The host sets the Telesto and Triggernometry endpoint constants and routes
Telesto `GenericJson` actions to the relay. Telesto's configured destination comes from the program's existing
Automarkers settings. The callback listener binds an available local port and needs no Windows HTTP URL reservation.

The relay forwards bundles, drawing geometry and memory expressions to Telesto. It assigns private resource names,
restores the pack's notification IDs before feeding the Endpoint source, and removes its own resources on shutdown.
Repeated subscriptions retire the previous callback identity. Drawing replacements get a new expiry callback address
while keeping references between drawings valid. Failed unsubscribe requests remain recorded for shutdown cleanup.
Game commands and macros obey the Automarkers switch. Memory subscriptions and doodles run with Triggernometry while Cactbot is off.

`python3 -m tests.test_triggernometry_telesto` checks resource ownership, callbacks and failure cleanup.
The host suite also replays the original TOP Party Synergy and Pantokrator XML through an isolated Telesto protocol peer.
Telesto supplies the actual game memory reads and rendering. Memory offsets remain the responsibility of the pack.
