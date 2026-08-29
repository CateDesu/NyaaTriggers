#!/bin/sh
# NyaaTriggers launcher. Runs the pre boot recovery the frozen exe cannot run
# itself. A hard kill in the middle of an update can leave no _internal next
# to the exe, and without it the exe cannot load Python, so no in app code
# ever gets the chance to repair the install. If the update backup survived,
# put it back, then start the real binary.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exe="$here/NyaaTriggers"
internal="$here/_internal"

internal_ok=0
if [ -d "$internal" ] && [ -n "$(ls -A "$internal" 2>/dev/null)" ]; then
    internal_ok=1
fi

if [ "$internal_ok" -eq 0 ]; then
    # The pid in a backup name is not zero padded, so glob order is not age
    # order: _internal.1000.nyaa-old sorts before _internal.999.nyaa-old. ls -t
    # puts the newest backup first by mtime, and splitting its output on
    # newlines only keeps a space in the path from breaking a name. Stop only
    # once a backup actually moves back, a failed mv tries the next one.
    ifs=$IFS
    IFS='
'
    for cand in $(ls -dt "$here/_internal.nyaa-old" "$here"/_internal.*.nyaa-old 2>/dev/null); do
        if [ -d "$cand" ]; then
            rm -rf "$internal" 2>/dev/null
            if mv "$cand" "$internal" 2>/dev/null; then
                echo "NyaaTriggers: restored _internal from $(basename "$cand") after an interrupted update" >&2
                break
            fi
            echo "NyaaTriggers: _internal is missing and the backup would not move back." >&2
            echo "To recover by hand: mv '$cand' '$internal'" >&2
        fi
    done
    IFS=$ifs
fi

if [ ! -x "$exe" ]; then
    # The one failure this script cannot recover from. Say what happened and
    # where to reinstall from instead of dying on exec with a bare 127.
    echo "NyaaTriggers: the program binary is missing: $exe" >&2
    echo "Reinstall from https://github.com/CateDesu/NyaaTriggers/releases" >&2
    exit 1
fi

exec "$exe" "$@"
