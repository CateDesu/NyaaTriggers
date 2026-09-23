# Triggernometry host design

Compatibility notes from engine commit `61ce1c6`. See the [README](README.md) for
setup and limitations and the [build record](SPIKE-LOG.md) for initial validation.

## Engine boot

`RealPlugin.InitPlugin` constructs WinForms and registers triggers through its tree
walk. Skipping the interface leaves no active triggers. Follow
`TriggernometryProxy/ProxyPlugin.cs`: use an STA thread, create the main form handle
before initialization, and keep a message pump for engine `Invoke` calls.

Check both `isInitialized` and the status Label. The outer catch can hide boot
failures. Toast hooks are required even with an invisible interface because
`QueueToast` calls `CornerShowHook` without a null check.

## Configuration and platform dependencies

Write configuration with an explicit UTF-8 writer. A mismatched UTF-16 declaration
can open a hidden modal error dialog.

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

Imported actions can override global audio routing, so rewrite their `TTSRouting`
and `SoundRouting` to ACT too. Remote sounds require a writable configuration folder.

## Scripts and feed

- Raw network and formatted ACT lines serve different trigger sources. Formatting
  lives in `host/ActLogLine.cs`. `OnLogLineRead` excludes lines ending in `] FB:`.
- Set `BridgeFFXIV.ZoneID` as well as raising `ZoneChanged`, since the first event
  can precede subscription.
- Empty `ExecScriptAssembliesExpression` makes Roslyn call `AddReferences("")`.
  `FixupExecuteScriptAssemblies` supplies a loaded assembly name. First compilation
  can exceed 2.5 seconds, so wait for results before testing dependent speech.
- At the reviewed commit, `Action.Execute` is the live dispatcher. The parallel
  `Actions/Action*.cs` refactor is unused.

## Combatant bridge

`InstanceHook` supplies `host/CombatantBridge.cs`. Its `DataRepository` bypasses the
engine's legacy private-field memory access. Combatants are read through C#
`dynamic`, so member names and numeric widths are part of the contract. Missing
members can drop entities or abort updates.

Never mutate a published combatant list. The engine's lock does not protect the
host's backing list. `PartyType=1` means party and `2` means alliance. Party ordering
also needs reliable job IDs. Missing memory addresses and unavailable GP or CP use zero.

The reviewed engine throttles party and self updates to 500 ms. Per-entity queries
read the latest snapshot without that throttle.
