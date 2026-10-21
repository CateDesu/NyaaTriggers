#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== NyaaTriggers Setup ==="
echo


SUDO=()
RUN_PYTHON=python3
if [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
fi

# The guarded array expansion supports empty arrays under set -u on older Bash versions.
if command -v pacman &>/dev/null; then
    echo "Detected pacman - installing system packages..."
    ${SUDO[@]+"${SUDO[@]}"} pacman -S --needed --noconfirm python-pyqt6 qt6-websockets python-websockets python-regex python-packaging alsa-utils
elif command -v apt &>/dev/null; then
    echo "Detected apt - installing system packages..."
    ${SUDO[@]+"${SUDO[@]}"} apt update
    # Debian packages the Qt WebSockets binding separately.
    ${SUDO[@]+"${SUDO[@]}"} apt install -y python3-pyqt6 python3-pyqt6.qtwebsockets python3-websockets python3-regex python3-packaging python3-venv alsa-utils
    # Older distro WebSocket packages lack the overlay client's sync API.
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    RUN_PYTHON=.venv/bin/python
else
    echo "Could not detect pacman or apt. Install these manually:"
    echo "  PyQt6 with Qt WebSockets support   python-websockets (or python3-websockets)"
    echo "  python-regex (or python3-regex)   alsa-utils"
    echo "  python-packaging (or python3-packaging)"
    echo
fi

echo
echo "Run the program with:  $RUN_PYTHON main.py"
echo "On first launch it will download the voice model and set up TTS automatically."
