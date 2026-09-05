#!/usr/bin/env bash
# Build triggevent-core: install Triggevent's engine modules into the local Maven
# repo, then shade the sidecar into one runnable jar.
#
#   target/triggevent-core.jar   <- what NyaaTriggers launches
#
# Requires JDK 17 + Maven. event-trigger is GPL-3.0. The produced jar is GPL-3.0.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ET_DIR="${EVENT_TRIGGER_DIR:-$HERE/event-trigger}"
ET_REPO="${EVENT_TRIGGER_REPO:-https://github.com/CateDesu/event-trigger.git}"
# Pinned to a commit on the fork's guards branch, which carries the engine
# guards as real commits. Bump this after a deliberate upstream sync or when
# new guard commits land.
ET_REF="${EVENT_TRIGGER_REF:-2491d56d3ed66c79085a78fd1090dc0f3bbb3409}"

have() { command -v "$1" >/dev/null 2>&1; }

if ! have java; then
  echo "ERROR: JDK 17 not found.  Arch/CachyOS:  sudo pacman -S jdk17-openjdk" >&2
  exit 1
fi
if ! have mvn; then
  echo "ERROR: Maven not found.   Arch/CachyOS:  sudo pacman -S maven" >&2
  exit 1
fi

# 1. Fetch event-trigger (the engine we link against).
if [ ! -d "$ET_DIR/.git" ]; then
  echo ">> cloning event-trigger ($ET_REF) into $ET_DIR"
  git clone "$ET_REPO" "$ET_DIR"
  git -C "$ET_DIR" checkout "$ET_REF"
else
  echo ">> reusing existing clone at $ET_DIR"
  # Older clones point origin at upstream. The engine comes from the fork now.
  if [ "$(git -C "$ET_DIR" remote get-url origin 2>/dev/null || true)" != "$ET_REPO" ]; then
    echo ">> repointing origin at $ET_REPO"
    git -C "$ET_DIR" remote set-url origin "$ET_REPO"
  fi
  # Re-assert the pin: a reused clone may have drifted (branch pull, local
  # checkout), and only an exact HEAD match proves the tree IS the pinned source.
  if [ "$(git -C "$ET_DIR" rev-parse HEAD)" != "$ET_REF" ]; then
    echo ">> existing clone is not at the pinned ref; checking out $ET_REF"
    git -C "$ET_DIR" checkout "$ET_REF"
  fi
fi

# 2. Install just the engine modules (+ their upstream deps via -am) into ~/.m2.
#    Skip tests AND test compilation (testutils/GUI tests are irrelevant here).
#    `clean` is REQUIRED, not optional: after updating event-trigger (e.g. a new
#    master with more triggers), an incremental build recompiles the changed
#    sources but the jar/shade plugins reuse the prior artifacts, so the engine
#    jar silently ships the OLD trigger set. clean forces a fresh jar every time.
echo ">> installing Triggevent Engine modules to local Maven repo"
#    `triggers` is a pom aggregator. Its trigger code is in triggers-* sub-modules,
#    so list those explicitly. `:artifactId` selectors resolve regardless of nesting.
( cd "$ET_DIR" && mvn -q -Dmaven.test.skip=true \
    -pl :xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines,:telesto-core -am \
    clean install )

# 3. Shade the sidecar fat jar (clean, so a stale shade can't survive a source update).
echo ">> building triggevent-core.jar"
( cd "$HERE" && mvn -q -Dmaven.test.skip=true clean package )

echo ""
echo "Built: $HERE/target/triggevent-core.jar"
echo "Debug run:  xvfb-run -a -s \"-screen 0 1024x768x24\" java -jar target/triggevent-core.jar"
echo "(needs a display - the engine builds Swing overlays at boot, so do NOT force"
echo " -Djava.awt.headless=true. Then paste raw IINACT WS JSON lines on stdin;"
echo " callout JSON appears on stdout. Set NYAA_TV_DIAG=1 for pipeline event counts.)"
