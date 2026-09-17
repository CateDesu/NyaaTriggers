#!/usr/bin/env bash
# Build the host beside the engine dependencies. Requires the built engine at ENGINE_BIN
# and Mono with mcs.
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
  "$HERE/host/Program.cs" "$HERE/host/CombatantBridge.cs" "$HERE/host/ActLogLine.cs"

echo "built: $ENGINE_BIN/triggernometry-core.exe ($(stat -c %s triggernometry-core.exe) bytes)"

# Refresh the bundled sidecar after building the host.
bash "$HERE/package.sh"
