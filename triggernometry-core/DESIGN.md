# Triggernometry host design

Design record for engine commit `61ce1c6`, targeting .NET Framework 4.6.2 with Mono and Xvfb on Linux. The host shipped in v1.1.0-master on 2026-06-28, commit `e3d2f91`. See the [build record](SPIKE-LOG.md) for validation and the [README](README.md) for current setup and limitations.

## Engine boot

The host uses the real `RealPlugin.InitPlugin` and constructs its WinForms interface without showing it. This was called Strategy A during development. It needs build fixups, configuration, and audio stubs, but no engine logic patch.

At the reviewed commit, `InitPlugin` constructs `UserInterface` unconditionally at `RealPlugin.cs:2261`. The interface's tree walk calls `AddTrigger` to populate the active lists. Skipping it would leave no registered triggers. The initial Mono smoke test verified that the interface can construct under Xvfb and reach `isInitialized=true`.

A proposed fallback would have patched `InitPlugin` to skip the interface and replicate its registration walk. The successful boot test made that unnecessary.

## Host initialization

The host follows `TriggernometryProxy/ProxyPlugin.cs` on a dedicated STA thread with a live message pump:

1. Create hidden forms and a TabPage, forcing the main form's handle before initialization. `ReadyForOperation` requires it.
2. Call `RealPlugin.ResetPlugin()` and obtain `RealPlugin.plug`.
3. Set `mainform`, `pluginName`, the writable configuration `path`, and engine `pluginPath`.
4. Register hooks before `InitPlugin`.
5. Write the preset configuration and imported packs, then call `InitPlugin` with the TabPage and a status Label.
6. Check `isInitialized` and the status Label for failure. Keep the message pump running for `ui.Invoke` and `mainform.Invoke`.

| Hook | Host behavior |
|---|---|
| `RealPlugin.InstanceHook` | Supply the combatant bridge described below |
| `ActInitedHook` | Return true so worker threads can run |
| `TtsPlaybackHook` | Emit resolved speech |
| `SoundPlaybackHook` | Emit sound path and volume |
| `CurrentZoneHook` | Return the current zone name |
| `InCombatHook`, `EncounterDurationHook` | Return initial values; live ACT encounter tracking is unsupported |
| `ActiveEncounterHook`, `LastEncounterHook` | Return empty values |
| `SetCombatStateHook`, `LogAllNetworkHook`, `UseDeucalionHook`, `ACTEncounterLogHook` | No-op ACT controls |
| `CornerShowHook`, `CornerHideHook` | No-op toast hooks |

Toast hooks are required even with an invisible interface: `QueueToast` calls `CornerShowHook` without a null check. Exceptions in `InitPlugin` can leave initialization false without escaping its outer catch.

## Configuration and platform dependencies

Write `<pluginName>.config.xml` under `plug.path` using the engine's `Configuration` type and an explicit UTF-8 writer. A UTF-16 declaration in a UTF-8 file can lead to a hidden modal error dialog.

| Setting or dependency | Purpose |
|---|---|
| `UseScarborough=false` | Disable the SharpDX overlay, enabled by default at the reviewed commit |
| `WindowToMonitor=""` | Avoid `user32` window lookup from the unguarded aura thread |
| `TtsMethod=ACT`, `SoundMethod=ACT` | Route audio through host hooks |
| `StartEndpointOnLaunch=false` | Leave the unused engine listener off |
| `UpdateNotifications=No`, `DefaultRepository=No` | Suppress first-run prompts |
| No configured repositories | Avoid automatic downloads and repository audio-routing overrides |
| `System.Speech` stub | Allow `SpeechSynthesizer` construction under Mono without using native speech |
| `Interop.WMPLib` stub | Satisfy WMP references without COM or Windows Media Player |
| On-disk Roslyn dependencies | Give assembly reference discovery valid `.Location` paths |

Imported actions can override global audio routing. Rewrite `TTSRouting` and `SoundRouting` from Triggernometry to ACT so those actions also reach the hooks.

Direct process memory, keyboard, mouse, and window-message actions depend on Windows APIs and remain outside this host's support. Telesto's supported memory and drawing relay is documented in the [README](README.md#telesto-integration).

## Trigger loading and log dispatch

Deserialize a `TriggernometryExport`, attach its `ExportedFolder` beneath `cfg.Root.Folders`, set parentage, and serialize the configuration before boot. `LoadConfigFromFile` and `BuildFullTreeFromConfiguration` then register triggers and rebuild condition parentage. The relevant reviewed paths are `RealPlugin.cs:2242/2319` and `UserInterface.cs:441-465`.

After initialization, feed both engine sources:

- `BeforeLogLineRead` receives raw IINACT network lines for `FFXIVNetwork` triggers.
- `OnLogLineRead` receives formatted ACT lines for `Log` triggers. Current formatting lives in `host/ActLogLine.cs`.

The original spike fed the same raw line to both methods. Current dispatch separates them because real packs use different formats. Both methods queue events for the engine's log processor and ignore input before initialization. The `OnLogLineRead` path also excludes lines ending in `] FB:`.

Maintain the zone name for restrictions and raise `DataSubscription.ZoneChanged` for numeric zone state. Set `BridgeFFXIV.ZoneID` directly as well, because the first event can precede subscription.

The live action dispatcher at the reviewed commit is `Action.Execute` in `Action.cs`. The parallel `Actions/Action*.cs` refactor was unused.

## Callout capture and scripts

`TtsPlaybackHook` receives resolved speech through ACT audio routing. `SoundPlaybackHook` receives a filename and volume. The configuration directory must be writable because remote sounds download beneath it.

The engine also exposes `RegisterNamedCallback`, whose delegate takes `(object obj, string val)`. This is a possible structured output channel. The current host captures speech and sound; visual-only `TextAura` output is not captured.

Roslyn compiles `ExecuteScript` under Mono. Empty `ExecScriptAssembliesExpression` values cause the reviewed engine to call `AddReferences("")`. The host's `FixupExecuteScriptAssemblies` replaces those values with a valid loaded assembly name. The first compilation can take more than 2.5 seconds, so validation must wait for the script result before checking dependent speech.

## Combatant bridge

`${_me}`, `${_entity[..]}`, `${_party[..]}`, and `BridgeFFXIV` script accessors reach the ACT plugin through `RealPlugin.InstanceHook`. Return a `PluginWrapper` with `state=1` and a fake plugin exposing these members by name:

| Member | Contract |
|---|---|
| `DataRepository.GetCurrentPlayerID()` | Local player ID |
| `DataRepository.GetCombatantList()` | Stable combatant snapshot |
| `DataRepository.GetCurrentFFXIVProcess()` | Null for a feed-only host |
| `DataSubscription.ZoneChanged` | Event with numeric zone ID and name |

Providing `DataRepository` bypasses the legacy private-field memory path in `BridgeFFXIV.cs:301-333`. The original bridge remains unchanged.

Combatants are consumed through C# `dynamic` in `PopulateClumpFromCombatant`. Missing members can drop an entity or abort a state update. Match the ACT model's member names and numeric widths:

| Members | Required types |
|---|---|
| `Name` | `string` |
| `IsCasting` | `bool` |
| `CastBuffID` | Integer supporting hexadecimal formatting |
| `PartyType` | `byte` or byte enum |
| `ID`, `OwnerID`, `CastTargetID`, `TargetID` | `uint` |
| `Address` | `IntPtr`, zero when unavailable |
| `Job` | `byte` job ID |
| `CurrentHP/MP/GP/CP`, `MaxHP/MP/GP/CP`, `Level`, `PosX/Y/Z`, `Heading`, `CastDurationCurrent/Max`, `EffectiveDistance`, `WorldID`, `WorldName`, `CurrentWorldID`, `BNpcNameID`, `BNpcID` | Matching numeric or string fields used by the engine |

The IINACT snapshot replaces the backing list atomically. Never mutate a list while the engine reads it: the bridge's internal lock does not protect the host's list. The local player ID identifies `${_me}`. `PartyType=1` identifies party members and `2` alliance members; reliable job IDs are needed for party ordering.

The reviewed bridge throttles party and self updates to 500 ms. Per-entity queries read the latest snapshot without that throttle. Missing memory addresses use zero, as do unavailable non-self GP and CP values. Before feed data is ready, `state=0` with a null plugin returns empty combatants and a warning.

## Validation record

The initial checks established engine build, hidden WinForms boot, pack registration, a resolved callout, Roslyn script execution, combatant substitution, stdin/stdout server mode, and Python bridge integration. Mono-specific boot failures and their fixes are recorded in [SPIKE-LOG.md](SPIKE-LOG.md).

These results do not establish full pack compatibility or live fight behavior. Current replay coverage and remaining limitations are listed in the [engine README](README.md#replay-checks).
