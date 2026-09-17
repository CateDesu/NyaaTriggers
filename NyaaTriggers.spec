import glob
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_all

datas    = []
binaries = []
hiddenimports = []

_source_version = re.search(r'^_VERSION\s*=\s*"([^"]+)"',
                            Path('nyaatriggers/app_common.py').read_text(encoding='utf-8'), re.M)
if _source_version is None:
    raise SystemExit('[spec] Could not read the release version')
_version_root = tempfile.TemporaryDirectory(prefix='nyaa-build-version-')
_version_stamp = Path(_version_root.name) / 'nyaatriggers.version'
_version_stamp.write_text(_source_version.group(1), encoding='utf-8')
datas.append((str(_version_stamp), '.'))

# Collect the voice libraries and their data together. Japanese speech needs the
# espeak-ng data as well as the Python modules.
for pkg in ('piper', 'piper_phonemize', 'onnxruntime',
            'kokoro_onnx', 'espeakng_loader', 'phonemizer', 'language_tags'):
    d, b, h = collect_all(pkg)
    datas         += d
    binaries      += b
    hiddenimports += h

# Include the Triggernometry converter explicitly for frozen imports.
hiddenimports += ['nyaatriggers.convert_triggernometry']

# The reader imports WebEngine lazily. Include its modules so their hooks bundle the
# browser process and resources.
hiddenimports += [
    'PyQt6.QtWebChannel',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
]

# Fail the build when WebEngine is missing instead of shipping an unusable reader.
try:
    _webengine_spec = importlib.util.find_spec('PyQt6.QtWebEngineCore')
except ModuleNotFoundError:
    _webengine_spec = None
if _webengine_spec is None:
    raise SystemExit(
        '[spec] PyQt6-WebEngine is not installed in the build environment: '
        'pip install PyQt6-WebEngine==6.11.0 (keep in sync with requirements.txt)')

# The plugin link imports websockets optionally in source runs, but packaged builds
# require it. find_spec can return None or raise when a parent package is missing.
try:
    _ws_client_spec = importlib.util.find_spec('websockets.sync.client')
except ModuleNotFoundError:
    _ws_client_spec = None
if _ws_client_spec is None:
    raise SystemExit(
        '[spec] websockets is not installed in the build environment: '
        'pip install websockets==16.1.1 (keep in sync with requirements.txt)')

# Bundle the default Piper voice only. Kokoro uses files beside the executable.
for f in sorted(glob.glob('voices/en_US-*.onnx') + glob.glob('voices/en_US-*.onnx.json')):
    datas.append((f, 'voices'))

datas += [
    ('assets/triggers.json', 'assets'),
    # Ship retirements and callout rewrites so withdrawn triggers are removed from local
    # overrides.
    ('assets/retired.json', 'assets'),
    # English zone names allow local triggers to match on other client languages.
    ('assets/zone_names.json', 'assets'),
    # The cactbot zone index supports timelines without local trigger files.
    ('assets/cactbot_timelines.json', 'assets'),
    ('assets/callout_defaults.json', 'assets'),
    # Bundle the local UMAD timeline. The sample timeline remains a source example.
    ('timelines/UMAD.txt', 'timelines'),
    ('assets/icon_nyaa.png', 'assets'),
]

# Bundle committed cactbot timelines for offline use. Writable downloads use separate
# cache names and take precedence.
for f in sorted(glob.glob('timelines/*.cactbot.txt')):
    datas.append((f, 'timelines'))

# Bundle the Kosugi Maru font with its Apache 2.0 license.
for f in sorted(glob.glob('fonts/*')):
    datas.append((f, 'fonts'))

# Bundle UI translations for use before any network request.
for f in sorted(glob.glob('lang/*.json')):
    datas.append((f, 'lang'))

# Seed engine rows before the first sidecar launch. The writable cache takes precedence.
if os.path.isfile('triggevent_inventory.seed.json'):
    datas.append(('triggevent_inventory.seed.json', '.'))

# Bundle Japanese callout translations as the offline copy. Downloads use a separate
# cache.
if os.path.isfile('assets/callouts_ja.json'):
    datas.append(('assets/callouts_ja.json', 'assets'))

for f in sorted(glob.glob('sounds/*.wav')):
    datas.append((f, 'sounds'))

# Bundle the engine jar and JRE so no Java installation is needed. Missing files fail
# the build unless NYAA_ALLOW_NO_ENGINE is set for development.
_allow_no_engine = os.environ.get('NYAA_ALLOW_NO_ENGINE') == '1'


def _require_engine(problem):
    if _allow_no_engine:
        print(f'[spec] WARNING: {problem} (NYAA_ALLOW_NO_ENGINE=1, building anyway)')
        return False
    raise SystemExit(
        f'[spec] {problem}. Both engines must ship with every build. '
        'Restore the bundled files or run the missing engine build script. '
        'Set NYAA_ALLOW_NO_ENGINE=1 for a local build without engines.')


_jar = os.path.join('triggevent-core', 'target', 'triggevent-core.jar')
if os.path.isfile(_jar):
    # Reject invalid or truncated jar downloads before packaging.
    with open(_jar, 'rb') as _fh:
        _magic = _fh.read(2)
    _have_jar = _magic == b'PK' and os.path.getsize(_jar) >= 1_000_000
    if not _have_jar:
        _have_jar = _require_engine(f'{_jar} is not a valid jar')
else:
    _require_engine(f'{_jar} is missing')
    _have_jar = False
if _have_jar:
    datas.append((_jar, os.path.join('triggevent-core', 'target')))

_have_jre = os.path.isdir('jre') and os.path.isfile(
    os.path.join('jre', 'bin', 'java.exe' if os.name == 'nt' else 'java'))
if not _have_jre:
    _require_engine('no bundled JRE in ./jre')
jre_tree = Tree('jre', prefix='jre') if _have_jre else None

# Bundle the committed Triggernometry host, engine and stubs. They use .NET Framework on
# Windows and Mono on Linux.
_tn_bin = os.path.join('triggernometry-core', 'bin')
_tn_exe = os.path.join(_tn_bin, 'triggernometry-core.exe')
_have_tn = os.path.isfile(_tn_exe)
if not _have_tn:
    _require_engine(f'{_tn_exe} is missing')
tn_bin_tree = Tree(_tn_bin, prefix=_tn_bin) if _have_tn else None

# Use an ICO on Windows, falling back to PNG conversion when Pillow is available.
_icon_candidates = ('assets/icon_nyaa.ico', 'assets/icon_nyaa.png')
_icon = next((c for c in _icon_candidates if os.path.exists(c)), None)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# Keep both keyboard libraries from the build environment to avoid incompatible X11
# versions.
if sys.platform.startswith('linux'):
    required_x11 = {
        'libxkbcommon.so.0', 'libxkbcommon-x11.so.0', 'libxcb-xkb.so.1',
        'libxcb-icccm.so.4', 'libxcb-shape.so.0', 'libxcb-keysyms.so.1',
        'libxcb-cursor.so.0',
    }
    collected = {Path(name).name for name, source, kind in a.binaries}
    missing = sorted(required_x11 - collected)
    if missing:
        raise SystemExit(
            '[spec] Missing Linux keyboard dependencies: ' + ', '.join(missing)
            + '. Install the X11 dependencies listed in the Release workflow before building.')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NyaaTriggers',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    *([jre_tree] if jre_tree is not None else []),
    *([tn_bin_tree] if tn_bin_tree is not None else []),
    strip=False,
    upx=False,
    upx_exclude=[],
    name='NyaaTriggers',
)

_version_root.cleanup()
