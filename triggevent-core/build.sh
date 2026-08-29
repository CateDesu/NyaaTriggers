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
ET_REPO="${EVENT_TRIGGER_REPO:-https://github.com/xpdota/event-trigger.git}"
# Pinned to a specific event-trigger commit so the vendored patches/ apply
# deterministically (upstream master drifts and would break them). Bump this
# together with re-checking patches/ against the new source.
ET_REF="${EVENT_TRIGGER_REF:-43bcf52782922360daf66bfb57e22d9251111a0e}"

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
  # Re-assert the pin: a reused clone may have drifted (branch pull, local
  # checkout), and the patch checks below only say whether the patch applies,
  # not that the tree IS the pinned source.
  if [ "$(git -C "$ET_DIR" rev-parse HEAD)" != "$ET_REF" ]; then
    echo ">> existing clone is not at the pinned ref; checking out $ET_REF"
    git -C "$ET_DIR" checkout "$ET_REF"
  fi
fi

# 1b. Apply the vendored engine patches on top of the pinned source. Idempotent:
#     skip if already applied, FAIL the build if one applies neither way (source
#     drifted from the pin). A jar without the DMU crash guards must never ship.
if [ -d "$HERE/patches" ]; then
  for p in "$HERE"/patches/*.patch; do
    [ -e "$p" ] || continue
    if git -C "$ET_DIR" apply --reverse --check "$p" 2>/dev/null; then
      echo ">> patch already applied: $(basename "$p")"
    elif git -C "$ET_DIR" apply --check "$p" 2>/dev/null; then
      echo ">> applying patch: $(basename "$p")"
      git -C "$ET_DIR" apply "$p"
    else
      echo "ERROR: patch does not apply cleanly (event-trigger drifted from the pin?): $(basename "$p")" >&2
      echo "Refusing to build a jar without the vendored engine guards. Re-check patches/ against $ET_REF or bump the pin." >&2
      exit 1
    fi
  done
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
