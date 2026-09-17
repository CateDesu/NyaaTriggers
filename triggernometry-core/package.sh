#!/usr/bin/env bash
# Assemble the host and dependencies in triggernometry-core/bin for bridge discovery.
# Run after building the engine and host.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_BIN="${ENGINE_BIN:-$HERE/.engine/Source/Triggernometry/bin/Release}"
OUT="$HERE/bin"

[ -f "$ENGINE_BIN/triggernometry-core.exe" ] || { echo "ERROR: build the host first (build-host.sh)" >&2; exit 1; }

mkdir -p "$OUT"
cp -f "$ENGINE_BIN/triggernometry-core.exe" "$OUT/"
cp -f "$ENGINE_BIN"/*.dll "$OUT/"
echo "packaged $(ls "$OUT"/*.dll | wc -l) DLLs + triggernometry-core.exe -> $OUT"
echo "set NYAA_TRIGGERNOMETRY_EXE=$OUT/triggernometry-core.exe (or rely on default discovery)"
