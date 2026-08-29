# triggernometry-core - headless Triggernometry engine sidecar for NyaaTriggers

Run **all** of Triggernometry's triggers - including the complex runtime-compiled **C# `ExecuteScript`**
ones - inside NyaaTriggers **1:1**, by hosting Triggernometry's **own engine**
(`paissaheavyindustries/Triggernometry`, MIT) headlessly as a sidecar subprocess (native .NET on Windows, Mono on Linux).

This directory is the C# half (the host + a `bin/` of the built sidecar). The Python half is
`../triggernometry_bridge.py` plus small hooks in `../ws_client.py` and `../main_window.py`.

> Status: **shipped in v1.1.0** (Master/testing channel), end-to-end: build → boot → log callout → ExecuteScript →
> `${_me}`/combatant → stdin/stdout server → Python bridge. See `SPIKE-LOG.md` for the reproducible log and `DESIGN.md`
> for the source-dive design record. Still open: not yet validated against a live in-game IINACT feed.

---

## Why this shape (decision record)

- Triggernometry's complex triggers are **runtime-compiled C#** (`ActionType="ExecuteScript"`, 559 in the on-disk
  packs) that reference Triggernometry's **own .NET types** and live singleton (`RealPlugin.plug`,
  `BridgeFFXIV.GetAllEntities()`, `using static Triggernometry.Interpreter.StaticHelpers`). A pure-Python
  reimplementation is categorically impossible. "1:1" **requires hosting the genuine engine**.
- A source dive (DESIGN.md) found the engine has **no UI-less boot seam**: the WinForms `UserInterface` IS the
  trigger loader (`AddTrigger`'s only callers are inside the UI tree-walk). So Strategy A **keeps** the real UI but
  constructs it **invisibly** (never shown; under Mono+Xvfb on Linux, native WinForms with no Xvfb on Windows), with the SharpDX/Direct2D overlay disabled by config.
  This needs **no engine source patch**, only build-file fixups, two no-op stub assemblies, config flags, and a
  host harness. (The fallback, Strategy B, was a small in-assembly patch. The spike proved A works, so B is unused.)
- **License is a non-issue** (engine is MIT, NyaaTriggers is MIT): unlike `triggevent-core` (GPL engine, which
  *required* the arms-length subprocess), here the subprocess is just clean architecture and the Linux/Windows split.

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
```
### Wire protocol - sidecar ► NyaaTriggers (stdout), one JSON object per line
```json
{"t":"callout","tts":"..."}                          // a resolved callout (route to overlay + Piper)
{"t":"sound","file":"...","volume":100}              // a sound the engine wanted to play
{"t":"status","active":true|false,"msg":"..."}       // lifecycle
```

---

## Build

The prebuilt `bin/` (exe + ~25 DLLs) is **vendored and git-committed** and bundled into releases **as-is**, so you
normally never build it. `build-all.sh` is a **local regeneration tool**, run only when you change the host or bump the
pinned engine commit. It needs **Mono 6.12+** (`pacman -S mono` / `apt install mono-complete` → `mcs`, `xbuild`/`msbuild`),
`git`, `curl`. GitHub's runners ship Mono 6.8, too old to compile the engine, which is why `bin/` is checked in rather
than built in CI.

**Regenerate `bin/` from a clean checkout:**
```bash
cd triggernometry-core
bash build-all.sh       # clone engine @ pinned commit -> apply fixups -> stubs -> nuget restore -> build -> bin/
```
This is fully reproducible: it clones `paissaheavyindustries/Triggernometry` into `$HERE/.engine`, applies `engine-fixups.patch` +
the designer case-rename, compiles the `shims/src/*.cs` stubs, restores NuGet, and assembles `bin/`. The output is
cross-platform **.NET Framework 4.6.2 MSIL**. It runs **natively on Windows (no Mono)** and under Mono on Linux.

**Incremental (engine already cloned at `$HERE/.engine`):** `build-engine.sh`, then
`build-host.sh` (which also re-runs `package.sh`).

## Packaging / release

- **CI** (`.github/workflows/release.yml`): there is **no** triggernometry build job; the committed
  `triggernometry-core/bin` is bundled directly. `NyaaTriggers.spec` adds it as a `Tree` under `triggernometry-core/bin`,
  so the `build-windows` and `build-linux` jobs just package what's already in git.
- **Windows**: the bridge runs `triggernometry-core.exe` natively via the bundled .NET Framework (4.6+ ships with
  Windows 10/11) - no Mono, no Xvfb. The stub `System.Speech`/`WMPLib` DLLs are no-ops (audio routes through our hooks),
  and `UseScarborough=false` keeps the SharpDX overlay off, so the real WinForms UI just builds invisibly.
- **Linux**: the same `bin/` is bundled, but needs **system Mono** on the user's box (`is_available()` is False and the
  feature stays off otherwise). Bundling a Mono runtime like the Temurin JRE is a future item.

Run standalone (debug - test mode boots, loads a pack, feeds one line, expects a callout):
```bash
xvfb-run -a mono bin/triggernometry-core.exe /tmp/cfg test/spike-me-pack.xml "00|t|0839|S|SPIKEME go|x"
```
Server mode (the real sidecar - NyaaTriggers drives it):
```bash
xvfb-run -a mono bin/triggernometry-core.exe <cfgDir> --serve <pack1.xml> <pack2.xml> ...
# then write {"t":"log",...} / {"t":"combatants",...} lines on stdin; callouts appear on stdout
```

NyaaTriggers auto-discovers `bin/triggernometry-core.exe` (or `$NYAA_TRIGGERNOMETRY_EXE`); the user's packs go in
`$NYAA_TRIGGERNOMETRY_PACKS` (default `~/.config/nyaatriggers/triggernometry-packs/`). `is_available()` returns
False (feature stays off) if Mono or the exe is missing.

---

## How it works (key facts, all verified)

- **No engine patch.** Build fixups + 2 stub assemblies (`System.Speech`, `WMPLib`) + config only. See SPIKE-LOG.md.
- **Config preset** (`<cfgDir>/Triggernometry.config.xml`, written by the host as the engine's own `Configuration`,
  UTF-8): `UseScarborough=false` (kill the SharpDX overlay), `WindowToMonitor=""` (skip the AuraUpdateThread user32
  crash), `TtsMethod/SoundMethod=ACT` (route audio through our hooks), `UpdateNotifications/DefaultRepository=No`
  (skip first-run toasts → a `CornerShowHook` NPE).
- **Triggers** load by grafting each `TriggernometryExport` pack into `cfg.Root` before `InitPlugin`, so the real UI
  tree-walk registers them. Log lines feed `OnLogLineRead`; callouts are captured via `TtsPlaybackHook`.
- **ExecuteScript** (Roslyn) runs under Mono. Workaround (no engine patch) for an engine bug: actions with no
  `ExecScriptAssembliesExpression` default it to `""`, which makes `Evaluate` call `AddReferences("")` → throws. The
  host rewrites empty ones to a valid loaded assembly (`FixupExecuteScriptAssemblies`).
- **Combatant bridge** (`${_me}`/position/HP/party): `InstanceHook` returns a fake `FFXIV_ACT_Plugin` whose
  `DataRepository.GetCombatantList()` serves a snapshot fed from IINACT's `getCombatants` poll - no real process
  memory needed. See `host/CombatantBridge.cs`.

## Open / TODO

- **Live in-game test** over a real IINACT feed (only synthetic/replayed lines exercised so far). Top item.
- **`_map_combatants` field casing** in `../ws_client.py` is a best guess for IINACT's `getCombatants` - verify live.
- **First-script latency**: the first Roslyn compile under Mono takes >2.5s (cold). Consider a representative warm-up.
- **Packaging for release**: bundle a Mono (Linux) / use native .NET (Windows) runtime in the release zips, like the
  Temurin JRE is bundled for Triggevent.
