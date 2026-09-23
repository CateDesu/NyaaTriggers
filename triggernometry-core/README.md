# Triggernometry engine host

Host Triggernometry's conditions, variables, delayed actions, trigger chains, and C# `ExecuteScript` actions in a separate process. Windows uses native .NET; Linux uses Mono.

Python integration is in `../nyaatriggers/triggernometry_bridge.py`.

First shipped in v1.1.0. Replay checks pass; live IINACT validation is pending. See the [build record](SPIKE-LOG.md) and [design](DESIGN.md).

---

## Design

Scripted packs depend on the original engine's .NET types and shared state. Its
WinForms interface registers triggers and must be constructed, but remains hidden.
See the [compatibility notes](DESIGN.md) for Mono and engine constraints.

Both Triggernometry and NyaaTriggers are MIT licensed. The subprocess isolates the runtime and platform dependencies.

## Wire protocol

stdin and stdout carry one JSON object per line. Input includes `log`, `zone`,
`combatants`, and Telesto `endpoint` messages. Output includes `callout`, `sound`,
and `status`. Message fields and control commands are defined in `host/Program.cs`
and the Python bridge. Engine diagnostics go to stderr and `triggernometry.log`.

---

## Build

The prebuilt `bin/` is committed and bundled in releases. Rebuild only when changing the host or engine pin. Requirements: Mono 6.12+ with `mcs` and `xbuild` or `msbuild`, Git, and curl.

```bash
cd triggernometry-core
bash build-all.sh
```

Output goes to `bin/` and targets .NET Framework 4.6.2.

The pack editor uses `bin/triggernometry-validate.exe` to check XML types and .NET regular expressions without starting the engine or running actions. Packaging builds it automatically. To rebuild only this helper, run `bash triggernometry-core/build-validator.sh` from the program root. This leaves the running engine's executable and DLLs alone.

For an existing engine checkout, run `build-engine.sh` then `build-host.sh`, which also runs `package.sh`.

## Packaging / release

`NyaaTriggers.spec` bundles the committed `triggernometry-core/bin/` directly. CI does not rebuild it.

- **Windows:** run the executable with native .NET Framework. No Mono or Xvfb is needed.
- **Linux:** use system Mono and the [Linux dependencies](../README.md#linux-system-dependencies). A bundled Mono runtime remains future work.

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

## Remaining work

- Validate live IINACT behavior, including `_map_combatants` field casing in `../nyaatriggers/ws_client.py`.
- Legacy auras and direct game-memory scripts remain unsupported. ACT combat-state and encounter-duration hooks return initial values.
- The first Roslyn compilation under Mono can exceed 2.5 seconds. A representative warm-up may help.
- Consider bundling Mono for Linux releases.

`python3 tools/validate_triggernometry_live.py` runs a silent check against a live IINACT feed. It uses a temporary pack and configuration, forwards real combatant snapshots and sampled live logs, and checks both network and ACT log sources. Pass `--output /path/to/report.json` to keep the result. A successful probe validates feed integration, not every pack's mechanics.

## Replay checks

Run `python3 -m tests.test_triggernometry_host` from the program's root with Mono and Xvfb installed.
The suite exercises the vendored executable, using isolated temporary configuration directories.
Fixtures in `test/packs/paissa` preserve the original TOP marker and Zelenia Bloom actions.
The checks cover marker offsets, player filtering, wipe resets, ACT and network source separation,
shared variables, generated log messages, delayed follow-ups and C# script results.
These are replay checks, not live fight validation.

Zelenia tests wait for the original pack's asynchronous setup before sending
dependent events, including a replay with deliberately delayed setup.

## Telesto integration

`nyaatriggers/triggernometry_telesto.py` relays bundles, drawings, and memory
expressions to the Telesto URL in Automarkers settings. Its loopback callbacks need
no Windows HTTP URL reservation. Private resource names isolate each engine run,
and failed unsubscribe requests remain queued for shutdown cleanup.

Game commands and macros obey the Automarkers switch. Memory subscriptions and
doodles run with Triggernometry while Cactbot is off.

`python3 -m tests.test_triggernometry_telesto` checks resource ownership, callbacks and failure cleanup.
The host suite also replays the original TOP Party Synergy and Pantokrator XML through an isolated Telesto protocol peer.
Telesto supplies the actual game memory reads and rendering. Memory offsets remain the responsibility of the pack.
