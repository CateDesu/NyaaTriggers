#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== NyaaTriggers Setup ==="
echo

# Install system packages needed to run the app.
# websockets backs the plugin link and regex backs user trigger patterns.
# Both are hard requirements in requirements.txt, but the app still starts
# without them, so install them here where pip cannot be blocked by PEP 668.
# piper-tts and the voice model are handled automatically on first launch.

# Root shells and minimal containers often have no sudo. Only prefix when
# needed; without root or sudo the package install fails with its own error.
SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    SUDO=(sudo)
fi

# The ${SUDO[@]+"${SUDO[@]}"} form expands to nothing when the array is empty.
# A plain "${SUDO[@]}" errors under set -u on bash older than 4.4.
if command -v pacman &>/dev/null; then
    echo "Detected pacman - installing system packages..."
    ${SUDO[@]+"${SUDO[@]}"} pacman -S --needed --noconfirm python-pyqt6 python-websockets python-regex alsa-utils
elif command -v apt &>/dev/null; then
    echo "Detected apt - installing system packages..."
    # Fresh minimal images ship empty package lists, install cannot locate
    # anything until they are refreshed.
    ${SUDO[@]+"${SUDO[@]}"} apt update
    ${SUDO[@]+"${SUDO[@]}"} apt install -y python3-pyqt6 python3-websockets python3-regex alsa-utils
else
    echo "Could not detect pacman or apt. Install these manually:"
    echo "  python-pyqt6 (or python3-pyqt6)   python-websockets (or python3-websockets)"
    echo "  python-regex (or python3-regex)   alsa-utils"
    echo
fi

echo
echo "Run the app with:  python3 main.py"
echo "On first launch it will download the voice model and set up TTS automatically."
