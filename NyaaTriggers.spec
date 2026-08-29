import glob
import importlib.util
import os

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_all

datas    = []
binaries = []
hiddenimports = []

# piper_* : the English Piper voice. kokoro_onnx + espeakng_loader + phonemizer +
# language_tags : the in-app neural Japanese voice. espeakng_loader ships the
# espeak-ng library and its data (incl. ja), and phonemizer/language_tags carry data
# files, so all four must be collected whole or the frozen JP voice can't phonemize.
for pkg in ('piper', 'piper_phonemize', 'onnxruntime',
            'kokoro_onnx', 'espeakng_loader', 'phonemizer', 'language_tags'):
    d, b, h = collect_all(pkg)
    datas         += d
    binaries      += b
    hiddenimports += h

# Pure-stdlib converter behind the "Import Triggernometry" button. main_window
# imports it at module scope (so PyInstaller follows it), but pin it explicitly so
# the frozen build can never drop it.
hiddenimports += ['convert_triggernometry']

# Cactbot reader: cactbot_reader.py imports WebEngine lazily inside start() so a
# source run survives without it. That also hides it from PyInstaller's
# analysis, leaving frozen builds with "Cactbot: Not in this build". Pin the
# trio here: their hooks pull in QtWebEngineProcess, the .pak resources and the
# ICU data, so the packaged app can run the headless raidboss page and forward
# its callouts to the companion plugin.
hiddenimports += [
    'PyQt6.QtWebChannel',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
]

# A build env without PyQt6-WebEngine earns only a PyInstaller warning for the
# trio above, and the frozen app then reports WebEngine failed to load and
# blames system libraries. Fail the build instead, like the websockets guard.
try:
    _webengine_spec = importlib.util.find_spec('PyQt6.QtWebEngineCore')
except ModuleNotFoundError:
    _webengine_spec = None
if _webengine_spec is None:
    raise SystemExit(
        '[spec] PyQt6-WebEngine is not installed in the build environment: '
        'pip install PyQt6-WebEngine==6.11.0 (keep in sync with requirements.txt)')

# The plugin link needs websockets at runtime. plugin_link.py imports it behind
# a guard so source runs survive without it, but that also means a build env
# missing the package ships a frozen app whose overlay link can never connect
# ("the websockets package is not installed"). Fail the build instead.
# find_spec walks parent packages, so with websockets absent entirely it
# raises ModuleNotFoundError instead of returning None. Both shapes mean the
# same thing here.
try:
    _ws_client_spec = importlib.util.find_spec('websockets.sync.client')
except ModuleNotFoundError:
    _ws_client_spec = None
if _ws_client_spec is None:
    raise SystemExit(
        '[spec] websockets is not installed in the build environment: '
        'pip install websockets==16.1.1 (keep in sync with requirements.txt)')

# Bundle only the default piper voice, not stray RVC models (.pth/.index) or
# the kokoro model that may sit in a local voices/ dir. Kokoro resolves its
# files next to the exe at runtime, so a bundled copy is dead weight.
for f in sorted(glob.glob('voices/en_US-*.onnx') + glob.glob('voices/en_US-*.onnx.json')):
    datas.append((f, 'voices'))

datas += [
    ('triggers.json', '.'),
    # Withdrawn trigger ids, and shipped rewrites for sidecar callout text. Both must
    # ship: without retired.json a removed trigger survives in triggers.local.json.
    ('retired.json', '.'),
    # Zone id -> English name: without it the local engine can only match the
    # zone name the client reports, so a non-English client gets no callouts.
    ('zone_names.json', '.'),
    # Zone id -> cactbot timeline index (every fight cactbot ships one for):
    # timeline bars for fights with no local trigger file.
    ('cactbot_timelines.json', '.'),
    ('callout_defaults.json', '.'),
    # Hand-written local timeline for the UMAD fight tag. Onedir packing drops
    # every datas entry under _internal, but TIMELINES_DIR reads timelines/
    # next to the exe, so the release workflow copies it out beside the exe.
    # Only this file ships: the *.cactbot.txt caches are fetched at runtime and
    # Sample Fight.txt stays a source checkout example.
    ('timelines/UMAD.txt', 'timelines'),
    ('icon_nyaa.png', '.'),
]

# Bundled UI font (Kosugi Maru, Apache-2.0, license ships alongside): the
# sidebar brand block and nav pills. Best-effort glob like lang/.
for f in sorted(glob.glob('fonts/*')):
    datas.append((f, 'fonts'))

# Bundled UI localization: lang/<code>.json string maps. Small and needed before
# any network call, so shipped in-build. Absent on a checkout with no translations
# yet, so best-effort glob.
for f in sorted(glob.glob('lang/*.json')):
    datas.append((f, 'lang'))

# First-run seed for the Triggevent Engine-trigger rows, so they list on a fresh
# install before the sidecar has ever booted. Best-effort: only bundled if a harvest
# has been committed (the writable cache supersedes it once the engine runs once).
if os.path.isfile('triggevent_inventory.seed.json'):
    datas.append(('triggevent_inventory.seed.json', '.'))

# Committed Japanese callout overlay (id -> translated tts_text). Bundled as the
# offline/first-run copy; the app refreshes a separate writable cache from GitHub.
if os.path.isfile('callouts_ja.json'):
    datas.append(('callouts_ja.json', '.'))

# Built-in alert chimes (sounds/*.wav). Resolved at runtime from
# _BUNDLE_DIR/sounds; a custom path the user picks is read from disk instead.
for f in sorted(glob.glob('sounds/*.wav')):
    datas.append((f, 'sounds'))

# Triggevent sidecar: the prebuilt fat jar plus a self-contained Temurin JRE 17
# (the bridge runs jre/bin/java), so the engine works with nothing installed.
#
# Both are REQUIRED. Shipping without them produces a build whose Triggevent
# section is simply empty, with nothing on screen saying why. That is exactly
# what testers reported, so a missing piece fails the build here instead.
# Set NYAA_ALLOW_NO_ENGINE=1 for a local dev build that deliberately has neither.
_allow_no_engine = os.environ.get('NYAA_ALLOW_NO_ENGINE') == '1'


def _require_engine(problem):
    if _allow_no_engine:
        print(f'[spec] WARNING: {problem} (NYAA_ALLOW_NO_ENGINE=1, building anyway)')
        return False
    raise SystemExit(
        f'[spec] {problem}. The Triggevent Engine must ship with every build; '
        'run triggevent-core/build.sh (or restore the CI artifact), or set '
        'NYAA_ALLOW_NO_ENGINE=1 to build without it on purpose.')


_jar = os.path.join('triggevent-core', 'target', 'triggevent-core.jar')
if os.path.isfile(_jar):
    # A jar is a zip, and the fat jar is tens of MB: an HTML error page or a
    # truncated artifact download must not be packaged as if it were the engine.
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

# Triggernometry sidecar: bundle the prebuilt sidecar (host exe + engine DLLs + stubs) as a
# Tree under triggernometry-core/bin. The assemblies are cross-platform .NET Framework 4.6.2
# MSIL: they run NATIVELY on Windows (no Mono) and under Mono on Linux. The prebuilt bin/ is
# vendored (git-committed in triggernometry-core/bin/) and bundled as-is. There is no CI build step.
_tn_bin = os.path.join('triggernometry-core', 'bin')
tn_bin_tree = (Tree(_tn_bin, prefix=_tn_bin)
               if os.path.isfile(os.path.join(_tn_bin, 'triggernometry-core.exe')) else None)

# App icon: Windows wants an .ico, falling back to the .png (PyInstaller converts
# it when Pillow is available), else no custom icon. (Linux ignores the icon arg.)
_icon_candidates = ('icon_nyaa.ico', 'icon_nyaa.png')
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
