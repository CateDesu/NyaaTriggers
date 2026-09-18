#!/usr/bin/env bash
# Install the engine modules and package target/triggevent-core.jar. Requires JDK 17 and
# Maven. The linked event-trigger engine and resulting jar are GPL-3.0.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ET_DIR="${EVENT_TRIGGER_DIR:-$HERE/event-trigger}"
ET_REPO="${EVENT_TRIGGER_REPO:-https://github.com/CateDesu/event-trigger.git}"
# Keep the engine commit pin in sync with build.bat.
ET_REF="${EVENT_TRIGGER_REF:-06015a947b5b8c7d67f4863b8033e1c14e185494}"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have java; then
  echo "ERROR: JDK 17 not found.  Arch/CachyOS:  sudo pacman -S jdk17-openjdk" >&2
  exit 1
fi
if ! have mvn; then
  echo "ERROR: Maven not found.   Arch/CachyOS:  sudo pacman -S maven" >&2
  exit 1
fi

if [ ! -d "$ET_DIR/.git" ]; then
  echo ">> cloning event-trigger ($ET_REF) into $ET_DIR"
  git clone "$ET_REPO" "$ET_DIR"
  git -C "$ET_DIR" checkout "$ET_REF"
else
  echo ">> reusing existing clone at $ET_DIR"
  # Update older checkouts to use the engine fork.
  if [ "$(git -C "$ET_DIR" remote get-url origin 2>/dev/null || true)" != "$ET_REPO" ]; then
    echo ">> repointing origin at $ET_REPO"
    git -C "$ET_DIR" remote add origin "$ET_REPO" 2>/dev/null || git -C "$ET_DIR" remote set-url origin "$ET_REPO"
  fi
  # Fetch when the pinned commit is missing locally.
  if ! git -C "$ET_DIR" cat-file -e "$ET_REF^{commit}" 2>/dev/null; then
    echo ">> fetching $ET_REPO"
    git -C "$ET_DIR" fetch origin
  fi
  # Reset reused checkouts to the pinned source.
  if [ "$(git -C "$ET_DIR" rev-parse HEAD)" != "$ET_REF" ]; then
    echo ">> existing clone is not at the pinned ref; checking out $ET_REF"
    git -C "$ET_DIR" checkout "$ET_REF"
  fi
fi

# Build clean engine artifacts so incremental packaging cannot retain an old trigger
# set. Skip engine tests and test compilation.
echo ">> installing Triggevent Engine modules to local Maven repo"
# List trigger submodules explicitly because triggers is only an aggregator.
( cd "$ET_DIR" && mvn -q -Dmaven.test.skip=true \
    -pl :actimport,:xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines,:telesto-core -am \
    clean install )

# Build a clean sidecar jar too.
echo ">> building triggevent-core.jar"
( cd "$HERE" && mvn -q -Dmaven.test.skip=true clean package )

echo ""
echo "Built: $HERE/target/triggevent-core.jar"
echo "Debug run:  xvfb-run -a -s \"-screen 0 1024x768x24\" java -jar target/triggevent-core.jar"
echo "(needs a display - the engine builds Swing overlays at boot, so do NOT force"
echo " -Djava.awt.headless=true. Then paste raw IINACT WS JSON lines on stdin;"
echo " callout JSON appears on stdout. Set NYAA_TV_DIAG=1 for pipeline event counts.)"
