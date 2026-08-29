#!/usr/bin/env bash
# LOCAL build/regeneration tool: full reproducible build of the triggernometry-core sidecar FROM A CLEAN CHECKOUT.
# Clones the Triggernometry engine at a pinned commit, applies the Linux-build fixups,
# compiles the stub assemblies, restores NuGet, builds the engine + host, and assembles
# bin/. The output is cross-platform MSIL (.NET Framework 4.6.2): it runs natively on
# Windows and under Mono on Linux. Requires Mono 6.12+ (mcs/xbuild) + git + curl. NOT run by CI
# (GitHub runners ship Mono 6.8, too old to compile the engine). The prebuilt bin/ is committed/vendored.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="${ENGINE_DIR:-$HERE/.engine}"
ENGINE_COMMIT="${ENGINE_COMMIT:-61ce1c6f4b3a14847c9f6362346c77026b7a0458}"
ENGINE_REPO="${ENGINE_REPO:-https://github.com/paissaheavyindustries/Triggernometry.git}"
NUGET="${NUGET:-$HERE/.nuget/nuget.exe}"

for t in mono mcs git curl; do
  command -v "$t" >/dev/null || { echo "ERROR: '$t' not found (apt install mono-complete / pacman -S mono)"; exit 1; }
done
# Either build tool works (xbuild is deprecated/absent in newer Mono, msbuild ships with mono-complete).
if   command -v xbuild  >/dev/null; then BUILDTOOL=xbuild
elif command -v msbuild >/dev/null; then BUILDTOOL=msbuild
else echo "ERROR: need xbuild or msbuild (mono-complete)"; exit 1; fi

# 1) clone the engine at the pinned commit (idempotent; reset to a clean tree each run)
if [ ! -d "$ENGINE_DIR/.git" ]; then
  git clone "$ENGINE_REPO" "$ENGINE_DIR"
fi
git -C "$ENGINE_DIR" fetch origin "$ENGINE_COMMIT" 2>/dev/null || git -C "$ENGINE_DIR" fetch origin
git -C "$ENGINE_DIR" checkout -f "$ENGINE_COMMIT"
git -C "$ENGINE_DIR" reset --hard "$ENGINE_COMMIT"
git -C "$ENGINE_DIR" clean -fd Source/Triggernometry/Forms Source/Triggernometry >/dev/null 2>&1 || true

SRC="$ENGINE_DIR/Source"

# 2) apply the csproj build fixups (System.Text.Json.6.0.3 import removed, System.Speech/WMPLib
#    stub HintPaths, +System.Net.Http +netstandard; see SPIKE-LOG.md "Phase 0")
git -C "$ENGINE_DIR" apply "$HERE/engine-fixups.patch"

# 3) case-rename the designer file (Windows-cased ref vs Linux case-sensitivity)
if [ -f "$SRC/Triggernometry/Forms/RepositoryListForm.designer.cs" ]; then
  mv -f "$SRC/Triggernometry/Forms/RepositoryListForm.designer.cs" "$SRC/Triggernometry/Forms/RepositoryListForm.Designer.cs"
fi

# 4) point the netstandard HintPath at THIS machine's Mono facade (patch ships an Arch path)
NETSTD="$(find /usr/lib/mono -name netstandard.dll -path '*Facades*' 2>/dev/null | sort | tail -1)"
if [ -n "$NETSTD" ]; then
  sed -i "s#<HintPath>/usr/lib/mono/4.5/Facades/netstandard.dll</HintPath>#<HintPath>${NETSTD//\//\\/}</HintPath>#" \
    "$SRC/Triggernometry/TriggernometryPlugin.csproj"
fi

# 5) compile the no-op stub assemblies (real System.Speech/WMP unused: audio routes via our hooks)
mkdir -p "$SRC/shims"
mcs -target:library -out:"$SRC/shims/System.Speech.dll"  "$HERE/shims/src/SystemSpeechStub.cs"
mcs -target:library -out:"$SRC/shims/Interop.WMPLib.dll" "$HERE/shims/src/WMPLibStub.cs"

# 6) restore NuGet packages (old-style packages.config)
if [ ! -d "$SRC/packages" ]; then
  mkdir -p "$(dirname "$NUGET")"
  [ -f "$NUGET" ] || curl -sL https://dist.nuget.org/win-x86-commandline/latest/nuget.exe -o "$NUGET"
  mono "$NUGET" restore "$SRC/Triggernometry.sln" -PackagesDirectory "$SRC/packages" -NonInteractive
fi

# 7) build the engine (only the plugin csproj; the ACT Proxy needs ACT.exe and is skipped)
"$BUILDTOOL" /p:Configuration=Release /verbosity:minimal "$SRC/Triggernometry/TriggernometryPlugin.csproj"
ENGINE_BIN="$SRC/Triggernometry/bin/Release"
[ -f "$ENGINE_BIN/TriggernometryPlugin.dll" ] || { echo "ERROR: engine build produced no dll"; exit 1; }

# 8) build the host into the engine output (so its deps colocate)
( cd "$ENGINE_BIN" && mcs -target:exe -out:triggernometry-core.exe \
    -r:TriggernometryPlugin.dll -r:System.Windows.Forms.dll -r:System.Drawing.dll \
    -r:System.Xml.dll -r:System.dll -r:System.Core.dll -r:System.Text.Json.dll -r:System.Memory.dll \
    "$HERE/host/Program.cs" "$HERE/host/CombatantBridge.cs" )

# 9) assemble bin/ (exe + every dependency DLL incl. the stubs)
rm -rf "$HERE/bin"; mkdir -p "$HERE/bin"
cp -f "$ENGINE_BIN/triggernometry-core.exe" "$HERE/bin/"
cp -f "$ENGINE_BIN"/*.dll "$HERE/bin/"
echo "built sidecar -> $HERE/bin ($(ls "$HERE/bin"/*.dll | wc -l) DLLs + triggernometry-core.exe)"
