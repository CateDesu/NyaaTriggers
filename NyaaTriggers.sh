#!/bin/sh
# nyaa-linux-recovery: 1
# NyaaTriggers launcher. Runs the pre boot recovery the frozen exe cannot run
# itself. A hard kill in the middle of an update can leave no _internal next
# to the exe, and without it the exe cannot load Python, so no in app code
# ever gets the chance to repair the install. If the update backup survived,
# put it back, then start the real binary.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exe="$here/NyaaTriggers"
internal="$here/_internal"
pending="$here/.nyaa-linux-update"

# Use the same lock as the updater before touching recovery files.
locked=0
if command -v flock >/dev/null 2>&1; then
    if [ -e "$here/.nyaa-update.lock" ] && [ ! -w "$here/.nyaa-update.lock" ]; then
        exec 9<"$here/.nyaa-update.lock"
        locked=1
    elif [ -e "$here/.nyaa-update.lock" ] || [ -w "$here" ]; then
        exec 9>>"$here/.nyaa-update.lock"
        locked=1
    fi
    if [ "$locked" -eq 1 ] && ! flock -n 9; then
        echo "NyaaTriggers: an update is running. Try again when it finishes." >&2
        exit 1
    fi
fi

if [ -f "$pending" ]; then
    if [ "$locked" -eq 0 ]; then
        echo "NyaaTriggers: recovery needs flock and access to the install lock." >&2
        exit 1
    fi
    {
        IFS= read -r update_id
        IFS= read -r update_exe
    } < "$pending"
    case "$update_id" in
        ''|*[!0-9]*) echo "NyaaTriggers: invalid update recovery record." >&2; exit 1 ;;
    esac
    case "$update_exe" in
        ''|.|..|_internal|*/*) echo "NyaaTriggers: invalid update executable." >&2; exit 1 ;;
    esac
    runtime_backup="$here/_internal.$update_id.nyaa-old"
    exe_backup="$here/$update_exe.$update_id.nyaa-old"
    if [ -d "$runtime_backup" ]; then
        if ! rm -rf "$internal" || ! mv "$runtime_backup" "$internal"; then
            echo "NyaaTriggers: could not restore the previous runtime." >&2
            exit 1
        fi
    fi
    if [ -f "$exe_backup" ]; then
        if ! mv -f "$exe_backup" "$here/$update_exe"; then
            echo "NyaaTriggers: could not restore the previous executable." >&2
            exit 1
        fi
    fi
    rm -f "$pending" || exit 1
    echo "NyaaTriggers: recovered the interrupted update." >&2
fi

internal_ok=0
if [ -d "$internal" ] && [ -n "$(ls -A "$internal" 2>/dev/null)" ]; then
    internal_ok=1
fi

if [ "$internal_ok" -eq 0 ]; then
    if [ "$locked" -eq 0 ]; then
        echo "NyaaTriggers: runtime recovery needs flock and access to the install lock." >&2
        exit 1
    fi
    # The pid in a backup name is not zero padded, so glob order is not age
    # order: _internal.1000.nyaa-old sorts before _internal.999.nyaa-old. ls -t
    # puts the newest backup first by mtime, and splitting its output on
    # newlines only keeps a space in the path from breaking a name. Stop only
    # once a backup actually moves back, a failed mv tries the next one.
    ifs=$IFS
    IFS='
'
    for cand in $(ls -dt "$here/_internal.nyaa-old" "$here"/_internal.*.nyaa-old 2>/dev/null); do
        if [ -d "$cand" ] && [ -n "$(ls -A "$cand" 2>/dev/null)" ]; then
            case "$cand" in *.new.nyaa-old) continue ;; esac
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

if [ "$locked" -eq 1 ]; then
    flock -u 9
    exec 9>&-
fi
exec "$exe" "$@"
