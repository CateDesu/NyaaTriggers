#!/bin/sh
# nyaa-linux-recovery: 1
# Restore a missing runtime from an update backup before starting the program.
# The frozen executable cannot run Python recovery without that runtime.
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
    # Try backups from newest to oldest. Split only on newlines to preserve spaces in
    # paths and continue if a restore fails.
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
    echo "NyaaTriggers: the program binary is missing: $exe" >&2
    echo "Reinstall from https://github.com/CateDesu/NyaaTriggers/releases" >&2
    exit 1
fi

if [ "$locked" -eq 1 ]; then
    flock -u 9
    exec 9>&-
fi
exec "$exe" "$@"
