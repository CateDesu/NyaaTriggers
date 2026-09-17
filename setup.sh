#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== NyaaTriggers Setup ==="
echo

# Install system dependencies through the package manager. Voice setup runs on first
# launch.

# Use sudo only when needed and available.
SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
fi

# The guarded array expansion supports empty arrays under set -u on older Bash versions.
if command -v pacman &>/dev/null; then
    echo "Detected pacman - installing system packages..."
    ${SUDO[@]+"${SUDO[@]}"} pacman -S --needed --noconfirm python-pyqt6 python-websockets python-regex alsa-utils
elif command -v apt &>/dev/null; then
    echo "Detected apt - installing system packages..."
    # Refresh package lists for minimal installations.
    ${SUDO[@]+"${SUDO[@]}"} apt update
    # Debian packages the Qt WebSockets binding separately.
    ${SUDO[@]+"${SUDO[@]}"} apt install -y python3-pyqt6 python3-pyqt6.qtwebsockets python3-websockets python3-regex python3-venv alsa-utils
else
    echo "Could not detect pacman or apt. Install these manually:"
    echo "  python-pyqt6 (or python3-pyqt6)   python-websockets (or python3-websockets)"
    echo "  python-regex (or python3-regex)   alsa-utils"
    echo
fi

echo
echo "Run the app with:  python3 main.py"
echo "On first launch it will download the voice model and set up TTS automatically."
