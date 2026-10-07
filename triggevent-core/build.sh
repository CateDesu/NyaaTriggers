#!/usr/bin/env bash
# Requires JDK 17, Maven and Python 3. The engine and jar are GPL-3.0.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ET_DIR="${EVENT_TRIGGER_DIR:-$HERE/event-trigger}"
ET_REPO="${EVENT_TRIGGER_REPO:-https://github.com/CateDesu/event-trigger.git}"
# Keep the engine commit pin in sync with build.bat.
ET_REF="${EVENT_TRIGGER_REF:-28069f332b762edc2c3ea326f2056dcc5050ddda}"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have java; then
  echo "ERROR: JDK 17 not found.  Arch/CachyOS:  sudo pacman -S jdk17-openjdk" >&2
  exit 1
fi
if ! have mvn; then
  echo "ERROR: Maven not found.   Arch/CachyOS:  sudo pacman -S maven" >&2
  exit 1
fi
if ! have python3; then
  echo "ERROR: Python 3 is required to prepare the engine source." >&2
  exit 1
fi

if [ ! -d "$ET_DIR/.git" ]; then
  echo ">> cloning event-trigger ($ET_REF) into $ET_DIR"
  git clone "$ET_REPO" "$ET_DIR"
  git -C "$ET_DIR" checkout "$ET_REF"
else
  echo ">> reusing existing clone at $ET_DIR"
  if [ "$(git -C "$ET_DIR" remote get-url origin 2>/dev/null || true)" != "$ET_REPO" ]; then
    echo ">> repointing origin at $ET_REPO"
    git -C "$ET_DIR" remote add origin "$ET_REPO" 2>/dev/null || git -C "$ET_DIR" remote set-url origin "$ET_REPO"
  fi
  if ! git -C "$ET_DIR" cat-file -e "$ET_REF^{commit}" 2>/dev/null; then
    echo ">> fetching $ET_REPO"
    git -C "$ET_DIR" fetch origin
  fi
  if [ "$(git -C "$ET_DIR" rev-parse HEAD)" != "$ET_REF" ]; then
    echo ">> existing clone is not at the pinned ref; checking out $ET_REF"
    git -C "$ET_DIR" checkout "$ET_REF"
  fi
fi

echo ">> installing Triggevent Engine modules to local Maven repo"
python3 "$HERE/build_engine.py" "$ET_DIR" "$HERE/patches/same-zone-history.patch"

echo ">> building triggevent-core.jar"
ET_COMMIT="$(git -C "$ET_DIR" rev-parse HEAD)"
( cd "$HERE" && mvn -q -Dmaven.test.skip=true "-Dnyaa.engine.commit=$ET_COMMIT" clean package )

echo ""
echo "Built: $HERE/target/triggevent-core.jar"
echo "Debug run:  xvfb-run -a -s \"-screen 0 1024x768x24\" java -jar target/triggevent-core.jar"
echo "(needs a display - the engine builds Swing overlays at boot, so do NOT force"
echo " -Djava.awt.headless=true. Then paste raw IINACT WS JSON lines on stdin;"
echo " callout JSON appears on stdout. Set NYAA_TV_DIAG=1 for pipeline event counts.)"
