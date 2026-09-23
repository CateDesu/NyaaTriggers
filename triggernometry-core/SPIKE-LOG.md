# Triggernometry build record

Initial validation on 2026-06-27 and 2026-06-28 used engine commit `61ce1c6`,
Mono 6.12, `mcs`, `xbuild`, and Xvfb. The host shipped in v1.1.0-master at
`e3d2f91`. Use the [README](README.md#build) for current build commands.

Verified: hidden WinForms boot, pack registration, resolved speech, Roslyn scripts,
combatant substitution, JSON server mode, and Python integration. These checks did
not establish live fight compatibility. Current coverage is in the
[replay checks](README.md#replay-checks).

## Build fixes

- Replace stale System.Text.Json 6.0.3 targets with the pinned 8.0.4 `buildTransitive/` targets.
- Correct `RepositoryListForm.designer.cs` to `.Designer.cs` on Linux.
- Replace System.Speech and WMP references with the stub assemblies.
- Add System.Net.Http and Mono's `netstandard` facade for Roslyn.
- Exclude the ACT proxy, which requires `Advanced Combat Tracker.exe`.

The build produced TriggernometryPlugin 1.2.0.6 and 24 dependency DLLs.

## Boot and script failures

An incorrect UTF-16 configuration declaration opened a hidden modal error dialog.
Unset toast hooks and first-run preferences prevented initialization. Empty script
assembly expressions failed in 103 of 139 examined archive packs. The fixes and
remaining engine constraints are recorded in the [design notes](DESIGN.md).
