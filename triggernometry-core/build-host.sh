#!/usr/bin/env bash
# Build the triggernometry-core stub host (Strategy A, Mono) into the engine's output dir so all deps colocate.
# Requires: the engine already built (see build-engine.sh) at $ENGINE_BIN/TriggernometryPlugin.dll, plus mono/mcs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_BIN="${ENGINE_BIN:-$HERE/.engine/Source/Triggernometry/bin/Release}"

if [ ! -f "$ENGINE_BIN/TriggernometryPlugin.dll" ]; then
  echo "ERROR: engine not built at $ENGINE_BIN/TriggernometryPlugin.dll (run build-engine.sh first)" >&2
  exit 1
fi

cd "$ENGINE_BIN"
mcs -target:exe -out:triggernometry-core.exe \
  -r:TriggernometryPlugin.dll \
  -r:System.Windows.Forms.dll -r:System.Drawing.dll \
  -r:System.Xml.dll -r:System.dll -r:System.Core.dll \
  -r:System.Text.Json.dll -r:System.Memory.dll \
  "$HERE/host/Program.cs" "$HERE/host/CombatantBridge.cs"

echo "built: $ENGINE_BIN/triggernometry-core.exe ($(stat -c %s triggernometry-core.exe) bytes)"

# Re-assemble bin/ so the bundled sidecar the bridge discovers is never stale.
bash "$HERE/package.sh"
