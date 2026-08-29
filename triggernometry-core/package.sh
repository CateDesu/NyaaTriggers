#!/usr/bin/env bash
# Assemble the runnable sidecar into triggernometry-core/bin/ : the host exe + the engine
# DLLs + stubs, so triggernometry_bridge.py (_find_exe) discovers it at
# triggernometry-core/bin/triggernometry-core.exe. Run AFTER build-engine.sh + build-host.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_BIN="${ENGINE_BIN:-$HERE/.engine/Source/Triggernometry/bin/Release}"
OUT="$HERE/bin"

[ -f "$ENGINE_BIN/triggernometry-core.exe" ] || { echo "ERROR: build the host first (build-host.sh)" >&2; exit 1; }

mkdir -p "$OUT"
# Copy the exe + every managed dependency DLL (engine, Roslyn, SharpDX, stubs, etc.)
cp -f "$ENGINE_BIN/triggernometry-core.exe" "$OUT/"
cp -f "$ENGINE_BIN"/*.dll "$OUT/"
echo "packaged $(ls "$OUT"/*.dll | wc -l) DLLs + triggernometry-core.exe -> $OUT"
echo "set NYAA_TRIGGERNOMETRY_EXE=$OUT/triggernometry-core.exe (or rely on default discovery)"
