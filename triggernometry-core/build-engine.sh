#!/usr/bin/env bash
# Build the real Triggernometry engine under Mono (Strategy A). Assumes the engine is
# cloned at $ENGINE_SRC with the Linux build fixups already applied (see SPIKE-LOG.md
# "Phase 0" for the exact fixups: System.Text.Json.6.0.3 import removed, RepositoryListForm
# designer case-rename, System.Speech/WMPLib stub references, +System.Net.Http +netstandard).
# Produces $ENGINE_SRC/Source/Triggernometry/bin/Release/TriggernometryPlugin.dll + deps.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_SRC="${ENGINE_SRC:-$HERE/.engine}"
SRC="$ENGINE_SRC/Source"
SHIMS="$SRC/shims"
NUGET="${NUGET:-$HERE/.nuget/nuget.exe}"

command -v mcs   >/dev/null || { echo "ERROR: mcs (mono) not found; pacman -S mono" >&2; exit 1; }
command -v xbuild >/dev/null || { echo "ERROR: xbuild not found" >&2; exit 1; }
[ -d "$SRC/Triggernometry" ] || { echo "ERROR: engine source not at $SRC" >&2; exit 1; }

# 1) stub assemblies (System.Speech + WMPLib) - no-op shims so the engine compiles + boots on Mono
mkdir -p "$SHIMS/src"
if [ ! -f "$SHIMS/System.Speech.dll" ] || [ ! -f "$SHIMS/Interop.WMPLib.dll" ]; then
  echo "ERROR: stub sources/dlls missing in $SHIMS (see SPIKE-LOG.md Phase 0)." >&2
  echo "       Expected $SHIMS/System.Speech.dll and $SHIMS/Interop.WMPLib.dll" >&2
  exit 1
fi

# 2) restore NuGet packages if absent (needs nuget.exe. Fetch with:
#    curl -L https://dist.nuget.org/win-x86-commandline/latest/nuget.exe -o nuget.exe)
if [ ! -d "$SRC/packages" ]; then
  [ -f "$NUGET" ] || { echo "ERROR: $SRC/packages missing and nuget.exe not at \$NUGET" >&2; exit 1; }
  mono "$NUGET" restore "$SRC/Triggernometry.sln" -PackagesDirectory "$SRC/packages" -NonInteractive
fi

# 3) build the engine in Release config. The ACT Proxy is intentionally NOT built: it needs ACT.exe
xbuild /p:Configuration=Release /verbosity:minimal "$SRC/Triggernometry/TriggernometryPlugin.csproj"
echo "built: $SRC/Triggernometry/bin/Release/TriggernometryPlugin.dll ($(stat -c %s "$SRC/Triggernometry/bin/Release/TriggernometryPlugin.dll") bytes)"
