#!/usr/bin/env bash
# Build the prepared Triggernometry checkout under Mono. ENGINE_SRC must already have
# the Linux build fixes described in SPIKE-LOG.md. Output goes to the engine's Release
# directory.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_SRC="${ENGINE_SRC:-$HERE/.engine}"
SRC="$ENGINE_SRC/Source"
SHIMS="$SRC/shims"
NUGET="${NUGET:-$HERE/.nuget/nuget.exe}"

command -v mcs   >/dev/null || { echo "ERROR: mcs (mono) not found; pacman -S mono" >&2; exit 1; }
command -v xbuild >/dev/null || { echo "ERROR: xbuild not found" >&2; exit 1; }
[ -d "$SRC/Triggernometry" ] || { echo "ERROR: engine source not at $SRC" >&2; exit 1; }

# Compile the audio stubs required by Mono.
mkdir -p "$SHIMS/src"
if [ ! -f "$SHIMS/System.Speech.dll" ] || [ ! -f "$SHIMS/Interop.WMPLib.dll" ]; then
  echo "ERROR: stub sources/dlls missing in $SHIMS (see SPIKE-LOG.md Phase 0)." >&2
  echo "       Expected $SHIMS/System.Speech.dll and $SHIMS/Interop.WMPLib.dll" >&2
  exit 1
fi

# Restore packages with nuget.exe. Download it from
# https://dist.nuget.org/win-x86-commandline/latest/nuget.exe if needed.
if [ ! -d "$SRC/packages" ]; then
  [ -f "$NUGET" ] || { echo "ERROR: $SRC/packages missing and nuget.exe not at \$NUGET" >&2; exit 1; }
  mono "$NUGET" restore "$SRC/Triggernometry.sln" -PackagesDirectory "$SRC/packages" -NonInteractive
fi

# Build the engine only. The separate ACT Proxy requires ACT.exe.
xbuild /p:Configuration=Release /verbosity:minimal "$SRC/Triggernometry/TriggernometryPlugin.csproj"
echo "built: $SRC/Triggernometry/bin/Release/TriggernometryPlugin.dll ($(stat -c %s "$SRC/Triggernometry/bin/Release/TriggernometryPlugin.dll") bytes)"
