# NyaaTriggers

An FFXIV trigger manager. It connects to [IINACT](https://github.com/marzent/IINACT) or any ACT fork over WebSocket and speaks callouts when configured abilities appear in the combat log, with an optional alert sound alongside each one.

Everything starts off, so you check the callouts you want. Triggevent and Triggernometry rows are editable. Bottom right checkboxes function as a per-fight toggle. I.e. DMU/UMAD -> local checked -> you get the local triggers for only that fight specifically.

There is also a live DPS meter parsed by the program itself, which includes DPS logs, party automarkers via the Telesto plugin, and a Japanese UI. If you'd like other languages, feel free to create an issue.

**Platform:** Linux · Windows  ·  **[Full guide](docs/GUIDE.md)**  ·  [Discord](https://discord.com/invite/TQJrbZcgKF)

---

## Installation

### Windows

Download `NyaaTriggers-windows.zip` from the [latest release](https://github.com/CateDesu/NyaaTriggers/releases/latest), extract it, and run `NyaaTriggers.exe`. The voice model downloads on first launch.

### Linux (Arch / CachyOS / Debian / Ubuntu)

```bash
git clone https://github.com/CateDesu/NyaaTriggers
cd NyaaTriggers
bash setup.sh
python3 main.py
```

`setup.sh` installs PyQt6, websockets, regex and aplay via pacman or apt. First launch downloads the voice model and sets up piper-tts.

### Linux (packaged tarball)

Download `NyaaTriggers-linux.tar.gz` from the [latest release](https://github.com/CateDesu/NyaaTriggers/releases/latest), extract it, and run the bundled launcher. Triggernometry needs `mono`.

Arch / CachyOS:

```bash
sudo pacman -S mono
tar -xzf NyaaTriggers-linux.tar.gz
cd NyaaTriggers
./NyaaTriggers.sh
```

Debian / Ubuntu:

```bash
sudo apt install mono-devel
tar -xzf NyaaTriggers-linux.tar.gz
cd NyaaTriggers
./NyaaTriggers.sh
```

Bazzite / Fedora: Layer the Fedora package once with `rpm-ostree install mono-core` and reboot, or run the binary from a `distrobox` / `toolbox` where you `dnf install` it instead.

Bugs and feedback go to [Discord](https://discord.com/invite/TQJrbZcgKF) or an issue.

## Connecting to IINACT

1. In IINACT, go to **Plugins - OverlayPlugin - WSServer**
2. Default is `ws://127.0.0.1:10501/ws`

---

## Documentation

**Companion plugin:** [NyaaTriggers Overlay](https://github.com/CateDesu/NyaaTriggers-Overlay) draws
the timeline bars, callouts, and the live DPS meter inside the game through Dalamud.

Everything is in the **[guide](docs/GUIDE.md)**:

- [Triggers tab](docs/GUIDE.md#triggers-tab) - the editor, the fight tree, enabling callouts
- [Engine triggers](docs/GUIDE.md#engine-triggers) - cactbot, Triggevent, and Triggernometry rows, editing spoken text
- [Triggevent Engine](docs/GUIDE.md#triggevent-engine) - what it runs, building from source
- [Triggernometry engine](docs/GUIDE.md#triggernometry-engine) - complex scripted triggers 1:1 (WIP)
- [Voice](docs/GUIDE.md#voice) - System vs offline Piper, Japanese voices, adding voices
- [Alert sound](docs/GUIDE.md#alert-sound) - built-in chimes, importing your own, and volume slider
- [DPS tab](docs/GUIDE.md#dps-tab) - live meter parsed by the program, encounter recording
- [Automarkers tab](docs/GUIDE.md#automarkers-tab) - party marks via the Telesto plugin, the UMAD preset
- [In-game display](docs/GUIDE.md#in-game-display) - drawing bars and callouts in the game via the plugin
- [Current Instance tab](docs/GUIDE.md#current-instance-tab) - live log, making triggers from it
- [Trigger fields](docs/GUIDE.md#trigger-fields) - every field in the editor
- [Settings](docs/GUIDE.md#settings) - language and connection
- [Personal triggers](docs/GUIDE.md#personal-triggers) · [Updating](docs/GUIDE.md#updating) - your data, kept across updates
- [Requirements](docs/GUIDE.md#requirements) - for running from source on Linux

---

## License

MIT. See [LICENSE](LICENSE).
