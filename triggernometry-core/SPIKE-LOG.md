# Triggernometry build record

Historical validation from 2026-06-27 and 2026-06-28. Engine: `paissaheavyindustries/Triggernometry` at `61ce1c6`. Toolchain: Mono 6.12 with `mcs`, `xbuild`, and Xvfb; .NET 10 was also installed. The host shipped in v1.1.0-master, commit `e3d2f91`.

The host kept the real WinForms interface hidden and used configuration plus audio stubs without changing engine logic. See the [design](DESIGN.md) for rationale and the [README](README.md) for current behavior. Controls and protocol handling have changed since this record.

## Verified results

| Check | Result |
|---|---|
| Engine build | Mono built the engine and dependencies |
| Hidden boot | `InitPlugin` reached `isInitialized=true` and Ready |
| Pack load | A `TriggernometryExport` registered through the UI tree walk |
| Speech | `TtsPlaybackHook` emitted `spike hello from the real engine` |
| ExecuteScript | Roslyn set the scalar variable `spikevar` to `42` |
| Combatants | `${_me.x}` and `${_me.currenthp}` resolved to `12.5` and `50000` |
| Server mode | JSON input changed those values to `99.5` and `48000` in a callout |
| Python integration | The bridge received a callout with values `55.5` and `42000` |
| Program startup | MainWindow constructed offscreen without changing saved data |

Live IINACT validation remained open, including combatant field casing, party payloads, zone events, and non-UTF-8 locales. Initial Roslyn compilation exceeded 2.5 seconds. Later replay coverage is documented in the README.

## Build

Restore packages, then build the engine project. The ACT proxy requires `Advanced Combat Tracker.exe` and was excluded.

```bash
mono nuget.exe restore Source/Triggernometry.sln -PackagesDirectory Source/packages
xbuild /p:Configuration=Release Source/Triggernometry/TriggernometryPlugin.csproj
```

Build-file fixes:

- Remove the stale System.Text.Json 6.0.3 target references. The package pin used 8.0.4 and `buildTransitive/`.
- Rename `RepositoryListForm.designer.cs` to `.Designer.cs` to match the project on Linux.
- Point `System.Speech` and `WMPLib` references at the stub assemblies. Replace the WMP COM reference with a normal assembly reference.
- Add explicit `System.Net.Http` and the Mono `netstandard` facade for Roslyn.

The stubs provide the speech synthesizer methods and WMP types used during engine construction. Audio routes through host hooks. Output was `TriggernometryPlugin.dll` version 1.2.0.6 and 24 dependency DLLs.

The first host build and boot used:

```bash
mcs -target:exe -out:triggernometry-core.exe -r:TriggernometryPlugin.dll -r:System.Windows.Forms.dll \
    -r:System.Drawing.dll -r:System.Xml.dll -r:System.dll host/Program.cs
xvfb-run -a mono triggernometry-core.exe <configDir>
```

Dependencies were colocated with the executable. The host used an STA thread, a message pump, hidden forms with created handles, and a TabControl-hosted TabPage. It checked the private initialization flag and kept pumping for five seconds to expose asynchronous boot failures. Use `build-all.sh` for current builds.

## Boot fixes

- **Configuration encoding:** `StringWriter` declared UTF-16 while the file was written as UTF-8. Engine parsing opened a hidden modal error dialog. An explicit UTF-8 `XmlWriter` fixed the mismatch. `GenericExceptionHandler` can still block a hidden host with a message box.
- **Silent initialization failure:** the outer catch wrote to the status Label while leaving `isInitialized=false`. Reading that Label exposed the cause.
- **First-run toasts:** undefined update and repository preferences called an unset `CornerShowHook`. Setting both preferences to No and stubbing `CornerShowHook` and `CornerHideHook` allowed boot.

## Script fix

The reviewed engine defaults `ExecScriptAssembliesExpression` to an empty string. `Interpreter.Evaluate` then passes an empty name to `AddReferences`, causing an `ArgumentException`. This affected 103 of the 139 scripted archive packs examined.

A test action referencing `System` proved Roslyn worked. The shipped host fix, `FixupExecuteScriptAssemblies`, replaces empty expressions with a loaded assembly name during pack loading. No engine patch was needed. Validation checked the settled scalar result because a dependent TTS action could run before the first cold compilation finished.

## Integration fixes

The initial reviews led to these changes:

- Parse zone ID from `01|` field 2 and zone name from field 3. Raise the zone event and set `BridgeFFXIV.ZoneID` directly to cover startup before subscription. Forward WebSocket `ChangeZone` messages too.
- Exit on stdin EOF so the engine's foreground threads cannot leave Mono and Xvfb running.
- Connect sound output to program playback and import the required `os` module.
- Derive party membership from `PartyChanged`, since combatant snapshots could report zero party type.
- Set UTF-8 stdin/stdout and escape JSON control characters.
- Request combatants immediately on enable to reduce the empty `${_me}` window. Feeding logs still proceeds when snapshots are unavailable.
- Forward cast, target, timing, world, and distance fields, and catch Python dispatch failures.
- Restore executable permissions for a future bundled Mono runtime.

## Imported packs and editable callouts

At shipment, importing XML copied the original pack into managed storage and restarted the enabled engine. The host only started when packs existed. Triggernometry kept its own source group in the trigger list.

After boot, the host walked live engine actions and identified `UseTTS` entries as `triggerGuid#ttsIndex`. It emitted an inventory and accepted `set_callout` and `set_disabled` commands. Edits changed spoken templates while preserving `${...}` substitution and script logic. Empty speech was suppressed.

The Python bridge exposed inventory, edits, resets, and disabled IDs. The interface saved overrides and replayed them after engine restart. `triggernometry_inventory.json` cached imported rows for display before engine boot. There was no bundled inventory seed because packs were user-imported.

Visual-only Text Aura output remained uncaptured. The initial `getCombatants` request-tag behavior and the short empty-player window still needed live verification.
