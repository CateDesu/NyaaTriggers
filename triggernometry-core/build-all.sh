#!/usr/bin/env bash
# Rebuild the committed Triggernometry sidecar from pinned sources. Requires Mono 6.12
# or newer, a Mono build tool, git and curl. The resulting .NET Framework assemblies run
# on Windows and through Mono on Linux. This is a local regeneration tool.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="${ENGINE_DIR:-$HERE/.engine}"
ENGINE_COMMIT="${ENGINE_COMMIT:-61ce1c6f4b3a14847c9f6362346c77026b7a0458}"
ENGINE_REPO="${ENGINE_REPO:-https://github.com/paissaheavyindustries/Triggernometry.git}"
NUGET="${NUGET:-$HERE/.nuget/nuget.exe}"

for t in mono mcs git curl; do
  command -v "$t" >/dev/null || { echo "ERROR: '$t' not found (apt install mono-complete / pacman -S mono)"; exit 1; }
done
if   command -v xbuild  >/dev/null; then BUILDTOOL=xbuild
elif command -v msbuild >/dev/null; then BUILDTOOL=msbuild
else echo "ERROR: need xbuild or msbuild (mono-complete)"; exit 1; fi

# Reset the engine checkout to the pinned commit.
if [ ! -d "$ENGINE_DIR/.git" ]; then
  git clone "$ENGINE_REPO" "$ENGINE_DIR"
fi
git -C "$ENGINE_DIR" fetch origin "$ENGINE_COMMIT" 2>/dev/null || git -C "$ENGINE_DIR" fetch origin
git -C "$ENGINE_DIR" checkout -f "$ENGINE_COMMIT"
git -C "$ENGINE_DIR" reset --hard "$ENGINE_COMMIT"
git -C "$ENGINE_DIR" clean -fd Source/Triggernometry/Forms Source/Triggernometry >/dev/null 2>&1 || true

SRC="$ENGINE_DIR/Source"

# Apply the engine build fixes and stub references.
git -C "$ENGINE_DIR" apply "$HERE/engine-fixups.patch"

# Match the designer filename case on Linux.
if [ -f "$SRC/Triggernometry/Forms/RepositoryListForm.designer.cs" ]; then
  mv -f "$SRC/Triggernometry/Forms/RepositoryListForm.designer.cs" "$SRC/Triggernometry/Forms/RepositoryListForm.Designer.cs"
fi

# Use this machine's netstandard facade path.
NETSTD="$(find /usr/lib/mono -name netstandard.dll -path '*Facades*' 2>/dev/null | sort | tail -1)"
if [ -n "$NETSTD" ]; then
  sed -i "s#<HintPath>/usr/lib/mono/4.5/Facades/netstandard.dll</HintPath>#<HintPath>${NETSTD//\//\\/}</HintPath>#" \
    "$SRC/Triggernometry/TriggernometryPlugin.csproj"
fi

# Compile audio stubs because playback uses host hooks.
mkdir -p "$SRC/shims"
mcs -target:library -out:"$SRC/shims/System.Speech.dll"  "$HERE/shims/src/SystemSpeechStub.cs"
mcs -target:library -out:"$SRC/shims/Interop.WMPLib.dll" "$HERE/shims/src/WMPLibStub.cs"

if [ ! -d "$SRC/packages" ]; then
  mkdir -p "$(dirname "$NUGET")"
  [ -f "$NUGET" ] || curl -sL https://dist.nuget.org/win-x86-commandline/latest/nuget.exe -o "$NUGET"
  mono "$NUGET" restore "$SRC/Triggernometry.sln" -PackagesDirectory "$SRC/packages" -NonInteractive
fi

# Build the engine only. The separate ACT Proxy requires ACT.exe.
"$BUILDTOOL" /p:Configuration=Release /verbosity:minimal "$SRC/Triggernometry/TriggernometryPlugin.csproj"
ENGINE_BIN="$SRC/Triggernometry/bin/Release"
[ -f "$ENGINE_BIN/TriggernometryPlugin.dll" ] || { echo "ERROR: engine build produced no dll"; exit 1; }

# Build the host beside its dependencies.
( cd "$ENGINE_BIN" && mcs -target:exe -out:triggernometry-core.exe \
    -r:TriggernometryPlugin.dll -r:System.Windows.Forms.dll -r:System.Drawing.dll \
    -r:System.Xml.dll -r:System.dll -r:System.Core.dll -r:System.Text.Json.dll -r:System.Memory.dll \
    "$HERE/host/Program.cs" "$HERE/host/CombatantBridge.cs" "$HERE/host/ActLogLine.cs" )

rm -rf "$HERE/bin"; mkdir -p "$HERE/bin"
cp -f "$ENGINE_BIN/triggernometry-core.exe" "$HERE/bin/"
cp -f "$ENGINE_BIN"/*.dll "$HERE/bin/"
echo "built sidecar -> $HERE/bin ($(ls "$HERE/bin"/*.dll | wc -l) DLLs + triggernometry-core.exe)"
