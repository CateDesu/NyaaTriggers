# triggernometry-core - Strategy A spike log

Engine: `paissaheavyindustries/Triggernometry` @ `61ce1c6` cloned to `/home/cate/Projects/Triggernometry`.
Host toolchain: Mono 6.12 (`mcs`/`xbuild`), `xvfb-run`, .NET 10 runtime. Design rationale: `DESIGN.md`.

## STATUS: concluded / SHIPPED in v1.1.0-master (commit e3d2f91, 2026-06-28). FULL PIPELINE BUILT + PROVEN (2026-06-27), Strategy A, no engine source patch.

All phases PASS under Mono+Xvfb, end to end from the PyQt6 app down to the real engine:
- Phase 0: the real engine builds under Mono.
- Phase 1: `RealPlugin.InitPlugin` boots headless to `isInitialized=true` / `"Ready"`, constructing the real
  (never-shown) WinForms `UserInterface`, SharpDX overlay disabled by config, System.Speech/WMPLib stubbed.
- Phase 2: a real `TriggernometryExport` pack grafts into `cfg.Root`, registers via the UI tree-walk, and a fed
  log line produces a callout through `TtsPlaybackHook`: `{"t":"callout","tts":"spike hello from the real engine"}`.
- ExecuteScript: a C# script compiled+run by Roslyn under Mono set an engine scalar var (`spikevar='42'`).
- Phase 3 combatant bridge: a fake `InstanceHook` instance fed combatant data made `${_me.x}`/`${_me.currenthp}`
  resolve (`{"t":"callout","tts":"my x is 12.5 hp 50000"}`). `host/CombatantBridge.cs`.
- Server mode: `--serve` reads `{"t":"log"|"combatants"|"zone"}` on stdin, emits callouts on stdout. Validated by
  piping JSON (combatant x=99.5 + a log line → `"my x is 99.5 hp 48000"`).
- Python bridge: `../triggernometry_bridge.py` spawns the Mono sidecar and harvested a callout end-to-end
  (`"my x is 55.5 hp 42000"`, combatant data flowing through the live feed).
- NyaaTriggers wiring: `../ws_client.py` (combatants signal + getCombatants poll + player-id) and `../main_window.py`
  (guarded bridge wiring, auto-start, closeEvent) - MainWindow constructs clean offscreen, data files unchanged.

Remaining: live in-game IINACT test; verify `_map_combatants` field casing. (Release packaging: the prebuilt `bin/`
is now VENDORED + shipped in v1.1.0-master; only bundling a Mono RUNTIME on Linux remains.)
See `README.md` for usage + the open list.

### No-assembly ExecuteScript fix SHIPPED (host-side, no engine patch)
`FixupExecuteScriptAssemblies` in `host/Program.cs` rewrites empty `ExecScriptAssembliesExpression` -> a loaded
assembly name on pack load (the user chose this "pure A / no fork" option over a 1-line engine patch). Verified: a
no-assembly script now runs (`spikevar='42'`).

## Adversarial review (2026-06-27, workflow wf_5d588160-96e, 23 agents) - 8 confirmed, 0 critical; fixes APPLIED

The review validated the architecture (no critical findings; integration respects the no-brick rule) and found 8
confirmed bugs. All but the two live-test-only items are FIXED:
- **H1 zone (FIXED)**: `Program.cs` parsed `01|` field `f[2]` (hex territory id) as the zone NAME; the name is `f[3]`.
  Now uses `f[3]` for the name AND parses the hex `f[2]` to raise `RaiseZoneChanged(zoneId, name)` so `${_ffxivzoneid}`
  + name/id folder restrictions work. (Removed the parallel dead `parts[2]` zone-tracking in `triggernometry_bridge.feed_log`.)
- **H2 self-terminate (FIXED)**: the engine's worker threads are FOREGROUND, so on stdin EOF `Main` returning left
  a mono+Xvfb orphan. The stdin reader's `finally` now `Environment.Exit(0)` (also resolves the EOF-before-Run race).
- **M1 sound dropped (FIXED)**: the host emits `{"t":"sound"}` and the bridge re-emits a `sound` signal, but
  `main_window` never connected it. Added `sound.connect(_on_triggernometry_sound)` -> `play_sound(file, vol/100)`.
- **M3 party membership (FIXED, needs live check)**: getCombatants reports `PartyType=0` on IINACT; now derive
  `{id:1|2}` from the `PartyChanged` roster in `ws_client` and apply it in `_map_combatants`.
- **M4 UTF-8 stdio (FIXED)**: pin `Console.Input/OutputEncoding = UTF8` so non-ASCII names survive a non-UTF-8 locale.
- **M5 JSON control-char escaping (FIXED)**: `J()` now `\uXXXX`-escapes every char `<0x20` (a tab in a callout used
  to emit invalid JSON that the strict Python reader silently dropped); also keeps `\r`.
- **M2 `${_me}` empty window (MITIGATED)**: `set_combatant_polling(True)` now fires one immediate `getCombatants`
  to shrink the sub-second window where `Myself` is null right after enable/zone-in. (Did NOT gate all log feeding on
  a snapshot. That would silently break everything if `getCombatants` is unsupported. Residual: a script calling
  `GetMyself()` in that window can see null; placeholder grammar degrades to `""`.)
- Hardening: `_read_loop` now wraps `_dispatch` in try/except.
- Verified false-positives (no change): closeEvent reap orphan; `me`-stays-0 (subscription is connect-time, not
  enable-time); "ready" before Roslyn (the action dispatcher waits on `scriptingInited`).

## Recall code review (2026-06-28, /code-review xhigh) - fixes APPLIED

A second, recall-focused review (10 finder angles) over the diff surfaced more real issues; the clear-cut ones are FIXED:
- **Zone id dropped (FIXED)**: the first `01|` `RaiseZoneChanged` could fire before the engine attaches its handler,
  dropping it -> `BridgeFFXIV.ZoneID` stayed 0. `CombatantBridge.RaiseZoneChanged` now ALSO sets the public static
  `BridgeFFXIV.ZoneID` directly. Plus `ws_client` now handles `ChangeZone` -> `zone_changed` -> `feed_zone` so zone
  changes propagate (previously subscribed-but-dropped; `feed_zone` was unwired).
- **`os` NameError (FIXED)**: `_on_triggernometry_sound` used `os.path.isfile` but `os` wasn't imported in
  `main_window.py` -> every sound callout silently NameError'd. Added `import os`.
- **Combatant protocol gaps (FIXED)**: `_map_combatants` omitted `castid/casttargetid/casttime/maxcasttime/worldname`
  and the host never set `EffectiveDistance`, so `${_entity[..].iscasting/.castid/.casttime/.worldname/.distance}`
  were always 0/empty. Now emitted + parsed.
- **Bundled-Mono exec bit (FIXED, latent)**: added `_make_bundled_mono_executable()` (mirrors the JRE fixup) called
  in `start()`, for when a frozen build bundles Mono.
- Em dashes stripped from new files (no-em-dash rule); stale `ws_client` docstring corrected.

## Integration redesign (2026-06-28, per user: "Triggernometry triggers should just function as local, as they're imported")

The sidecar is no longer a standalone opt-in source. It now behaves like Local:
- **Driven by the master Triggers switch.** `_set_triggers_enabled` starts/stops it alongside Local + Triggevent
  (`main_window.py`). Removed the standalone `_autostart_triggernometry` QTimer + the `triggernometry_enabled` setting.
  It only actually starts when a pack has been imported (`_has_triggernometry_packs()` -> `triggernometry_bridge.has_packs()`),
  so the master switch never spawns Mono for nothing.
- **Fed by the existing "Import Triggernometry" button**, not a hand-dropped folder. `_import_triggernometry` now
  ALSO stages the raw `.xml` into the managed pack store (`triggernometry_bridge.packs_dir()`), so the complex/scripted
  triggers the converter skips run via the engine (the simple ones still convert to Local rows as before). If the master
  switch is on, the import restarts the sidecar to load the new pack immediately. Updated the button note + result message.

## Editable callouts for complex triggers (2026-06-28, per user: "Can you edit the complex ones, at least their callouts?")

Complex/scripted Triggernometry triggers' spoken text is now editable + toggleable in the Triggers list, exactly like
Triggevent's rows. Finding that made it feasible: all 139 ExecuteScript packs put their callout in a `UseTTS` action
(`UseTTSTextExpression`, a template with `${...}` expressions) - the script is the LOGIC, UseTTS is the CALLOUT.
- **Host** (`Program.cs`): after InitPlugin, walks the LIVE `plug.Triggers` (reflection; the grafted objects get
  serialized+re-read, so they're throwaways) and registers each UseTTS callout under id `triggerGuid#ttsIndex` -> the
  live `Action`. Emits `{"t":"inventory","triggers":[{id,name,fight,text}]}`. Commands `{"t":"set_callout","id","text"}`
  (edit; text=null reverts) and `{"t":"set_disabled","ids":[...]}` rewrite that Action's `UseTTSTextExpression` in place
  (disable = blank it). Edits keep `${...}` substitution (verified: edit "pos is ${_me.x}" -> spoke "pos is 1.1").
  TtsPlaybackHook now skips empty text (so blanked/disabled callouts emit nothing).
- **Bridge** (`triggernometry_bridge.py`): `inventory` signal + `set_callout(cid,tts=,text=,enable=)` (signature mirrors
  TriggeventBridge so main_window drives both identically) + `reset_callout` + `set_disabled`.
- **main_window**: registered "triggernometry" as a 3rd engine source in the EXISTING engine-inventory UI
  (`_engine_disabled`/`_engine_seen`/`_engine_inventory` rows, default-off-until-seen). Generalized the Triggevent
  edit/render/menu branches via `_callout_edit_dict`/`_apply_callout_edit`/`_reset_callout_edit`; added
  `_on_triggernometry_inventory` + `_set/_reset_triggernometry_callout_edit` + `_replay_triggernometry_callout_edits`;
  routed `_apply/_persist_engine_disabled`. Rows tagged "Triggernometry", editable text + per-row enable/disable,
  re-applied on each sidecar (re)start. The C# script LOGIC stays untouched. Only the spoken text is editable.

## Inventory cached for boot (2026-06-28, per user "Make Triggernometry also cache for boot")

`_TRIGGERNOMETRY_INVENTORY_CACHE = _DATA_DIR/triggernometry_inventory.json`. `_save_triggernometry_inventory_cache()`
(on each harvest) + `_load_cached_triggernometry_inventory()` (in `__init__`) mirror the Triggevent cache, so the
editable rows list on open BEFORE the sidecar boots. No bundled seed (packs are user-imported, not shipped). Verified:
save -> clear -> load round-trips.

### Triggernometry stays a SEPARATE engine source (NOT merged with Local)
The user first said "I consider Triggernometry and local triggers to be one and the same" (I grouped the rows under the
Local section), then REVERSED it: "revert everything, I'd like Triggernometry to be separated from local." So the
grouping was reverted. Triggernometry rows render in the engine section (tinted, alongside cactbot/triggevent), NOT in
Local. KEPT: the editable callouts, the boot cache (above), and the `_is_engine_key` fix to include "triggernometry"
(a genuine bug fix. Without it `_engine_entry_for_key` returned None and the edit-text lookup for Triggernometry rows
was broken, independent of grouping).
- **Visual-only Text Aura callouts are not captured** (the host hooks only TTS + sound; Triggernometry aura text
  renders invisibly under Xvfb). Capturing them needs a different channel (e.g. RegisterNamedCallback or intercepting
  the aura path). TTS/sound callouts work; aura-only triggers are silent.
- **getCombatants sent without `rseq`** (PLAUSIBLE): may get no reply on some IINACT builds. Left as-is pending a
  live test (adding rseq blindly risks the opposite failure if the server then routes the reply only to a callback).

### Still needs a LIVE in-game / IINACT test (untestable here)
- `_map_combatants` field casing + the `PartyChanged` payload shape (M3) vs real IINACT.
- `getCombatants` actually emitting `PartyType=0` and the `${_me}` empty-window timing (M2) on real combatant cadence.
- The `01|` field layout + hex-id `${_ffxivzoneid}` path (H1) from live IINACT.
- Non-UTF-8 locale repro (M4) with real JP/accented names.

## Phase 2 - pack load + callout + ExecuteScript (DONE)

Inject triggers by grafting a `TriggernometryExport` into `cfg.Root` BEFORE InitPlugin (config is file-based, read
inside InitPlugin): `TriggernometryExport.Unserialize(xml).ExportedFolder` -> `cfg.Root.Folders.Add(...)` -> serialize
the whole `Configuration` (UTF-8) -> InitPlugin's `ui.BuildFullTreeFromConfiguration()` registers them via `AddTrigger`.
Feed each IINACT line to `OnLogLineRead(false, line, zone)` (the regex matches the FULL raw line via `CheckMatch`).
Capture callouts via `TtsPlaybackHook` (+ `SoundPlaybackHook`, `RegisterNamedCallback`). Scripts use
`Triggernometry.Interpreter.StaticHelpers.*` (`SetScalarVariable`/`GetScalarVariable`/`Log`/`Serialize`/...).

### ENGINE BUG found - empty-assembly ExecuteScript throws (blocks 1:1 for 103 no-assembly scripts)
`ActionProps.cs:709` defaults `_ExecScriptAssembliesExpression = ""` (NOT null). `Context.EvaluateStringExpression`
coerces null->"" (`expr ?? ""`). `Interpreter.Evaluate` then does `if (assy != null) { assy.Split(',') ...
AddReferences("") }` -> `ArgumentException: assemblyName cannot have zero length` -> the script never runs (error
swallowed into the engine log "Action exception"). Of 139 archive packs with ExecuteScript, 103 omit the assemblies
expr, so this breaks them all. Workaround used to PROVE Roslyn works: set `ExecScriptAssembliesExpression="System"`
on the test action. PROPER FIX (needed for 1:1), pick one:
  (A) 1-line engine patch in `Interpreter.Evaluate`: `if (string.IsNullOrWhiteSpace(asmName)) continue;` (clean,
      behavior-preserving, upstreamable - but a patch, nudges us off "pure A").
  (B) host-side reflection fixup on pack load: set empty `_ExecScriptAssembliesExpression` to a loaded asm name
      (keeps engine unpatched; hackier, per-action reflection).

### Cold-compile latency: the FIRST ExecuteScript compile under Mono Roslyn takes >2.5s (subsequent are cached).
Proof used `GetScalarVariable` (settled) not the TTS (which read `${spikevar}` before the cold compile finished).
Consider a representative warm-up compile at boot beyond the ctor's trivial `"int whee;"`.

## Phase 0 - build the engine under Mono (DONE)

`mono nuget.exe restore Source/Triggernometry.sln -PackagesDirectory Source/packages` (nuget.exe fetched to scratch;
restores 25 pkgs incl Roslyn 4.1.0, SharpDX 4.2, CsvHelper). Then build ONLY the engine csproj (not the ACT Proxy,
which needs `Advanced Combat Tracker.exe`):

```
xbuild /p:Configuration=Release Source/Triggernometry/TriggernometryPlugin.csproj
```

Build fixups applied to `Source/Triggernometry/TriggernometryPlugin.csproj` (build-file/Linux fixups, NOT engine logic):
1. Removed the stale `System.Text.Json.6.0.3` `<Error>` check + `<Import>` (packages.config pins 8.0.4, whose targets
   live under `buildTransitive/` - the old hardcoded `build/...6.0.3` path failed the build).
2. Renamed `Forms/RepositoryListForm.designer.cs` -> `.Designer.cs` (csproj used capital D; Linux is case-sensitive).
3. `System.Speech` ref -> HintPath `..\shims\System.Speech.dll` (stub; real System.Speech absent on Mono).
4. `<COMReference WMPLib>` -> normal ref `..\shims\Interop.WMPLib.dll` (stub; Mono can't tlbimp COM, WMP absent on Linux).
5. Added `<Reference Include="System.Net.Http" />` (Mono needs it explicit) and `<Reference Include="netstandard">`
   HintPath `/usr/lib/mono/4.5/Facades/netstandard.dll` (net462 consuming netstandard2.0 Roslyn needs the facade).

Stub assemblies (in `Source/shims/`, sources in `shims/src/`): see `host/`-adjacent notes. Both are no-op shims that
exist only to compile + let `new SpeechSynthesizer()` / `new WindowsMediaPlayer()` succeed; all audio routes through
our hooks (TtsMethod/SoundMethod=ACT). Surfaces reverse-engineered from engine usage:
- `System.Speech.Synthesis.SpeechSynthesizer`: `Volume`, `Rate`, `SpeakAsync(string)`, `Speak`, `Dispose`.
- `WMPLib`: `WindowsMediaPlayer{URL, settings.volume, playState, PlayStateChange, MediaError, close()}`, `WMPPlayState`,
  the two `_WMPOCXEvents_*EventHandler` delegates (`void(int)` / `void(object)`), `IWMPSettings`.

Output: `Source/Triggernometry/bin/Release/TriggernometryPlugin.dll` (v1.2.0.6) + 24 dep DLLs (incl both stubs).

## Phase 1 - headless boot host (DONE: gate PASS)

Host: `host/Program.cs`, compiled into the engine `bin/Release` so all deps colocate:
```
mcs -target:exe -out:triggernometry-core.exe -r:TriggernometryPlugin.dll -r:System.Windows.Forms.dll \
    -r:System.Drawing.dll -r:System.Xml.dll -r:System.dll host/Program.cs
xvfb-run -a mono triggernometry-core.exe <configDir>
```
The host: STA thread + message pump; hidden `mainform` with forced handle; a TabControl-hosted `TabPage`; writes a
preset `Triggernometry.config.xml` (the engine's own `Configuration` type, serialized as **UTF-8**); registers hooks;
calls the real `InitPlugin`; reflects the private `isInitialized`; pumps 5s to catch async-thread crashes.

### Three bugs found+fixed during bring-up (all host-side / config, NO engine patch):
1. **Config encoding hang.** Serializing `Configuration` via `StringWriter` emits `encoding="utf-16"` in the XML decl.
   Writing those chars as UTF-8 made the engine's XML read throw -> `GenericExceptionHandler` -> **modal `MessageBox`** ->
   infinite hang under Xvfb. Fix: serialize with an explicit UTF-8 `XmlWriter`.
   LANDMINE: `RealPlugin.GenericExceptionHandler` (RealPlugin.cs:1926) ALWAYS `MessageBox.Show`s (3 call sites). Any
   exception routed there blocks headless. Mitigate by never triggering it (valid inputs); a tiny patch may be wanted later.
2. **`isInitialized` stayed false silently.** The engine's outer catch swallows InitPlugin failures into the
   `pluginStatusText` Label. Reading that Label surfaced: `NullReferenceException ... "setting up toasts"`.
3. **Toast NPE.** With `UpdateNotifications`/`DefaultRepository` both = `Undefined` (defaults), `SetupToasts` queues
   first-run YesNo toasts, and `QueueToast` (UserInterface.cs:220) calls `plug.CornerShowHook()` **unguarded**, and it
   was null. Fix: set both config fields to `No` (so no first-run toasts) AND stub `CornerShowHook`/`CornerHideHook`.
   (Corrects the design doc, which assumed CornerShow was unreachable headless.)

KEY RESULT: the big risk (can Mono construct the complex `CustomControls.UserInterface`?) is ANSWERED YES.

## CONCLUSION / SHIPPED (2026-06-28)

Spike concluded. The engine shipped in NyaaTriggers v1.1.0 on the Master/testing channel (tag `v1.1.0-master`, commit
e3d2f91, 2026-06-28) via Strategy A: the real never-shown WinForms host runs under Mono+Xvfb on Linux / native .NET on
Windows, with NO patch to the engine's source or logic (System.Speech + WMPLib neutralized by vendored no-op stub
assemblies). The prebuilt `bin/` is VENDORED (committed) because the GitHub runners ship a Mono too old to compile it;
`build-all.sh` regenerates it locally on Mono 6.12+. Remaining open items: live in-game IINACT validation
(`_map_combatants` combatant/zone field casing) and Roslyn first-script cold-compile latency.
