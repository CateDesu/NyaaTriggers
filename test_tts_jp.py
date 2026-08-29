"""Tests for the Japanese-voice routing in tts._system_speak (M1).

Drives _system_speak with _run_speak_proc stubbed to capture the command, and
platform/shutil monkeypatched to simulate each OS. Verifies the JP voice is
selected only when auto-route is on AND the text is Japanese, per platform.

Run directly:  python test_tts_jp.py   (exit 0 = all pass)
"""
import contextlib
import io
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tts

FAILS = []
CAP = {}
_PY = sys.executable


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# fix (c): the module default flipped False->True so localized callouts speak in
# Japanese out of the box. Asserted at import, before any set_jp_auto call.
check("_jp_auto module default is True (fix c)", tts._jp_auto is True)


def _fake_proc(cmd, text, stdin_text, no_window=False, gen=None):
    CAP.clear()
    CAP.update(cmd=cmd, text=text, stdin_text=stdin_text)
    return True


def speak_via(os_name, text, *, jp_auto, jp_voice="", avail=("spd-say",)):
    """Run _system_speak under a simulated OS. Return the captured command."""
    tts.platform.system = lambda: os_name
    shutil.which = lambda n: ("/usr/bin/" + n) if n in avail else None
    tts.set_jp_auto(jp_auto)
    tts.set_jp_voice(jp_voice)
    CAP.clear()
    tts._system_speak(text, 1.0, 1.0)
    return CAP.get("cmd", [])


JP = "フレア来ます"
EN = "stack"

_orig_proc, _orig_sys, _orig_which = tts._run_speak_proc, tts.platform.system, shutil.which
_orig_auto = tts._jp_auto            # restore the module default, not force it False
tts._run_speak_proc = _fake_proc
try:
    # ── Linux spd-say ──
    cmd = speak_via("Linux", JP, jp_auto=True, avail=("spd-say",))
    check("linux spd-say: JP -> -l ja", "-l" in cmd and cmd[cmd.index("-l") + 1] == "ja")
    cmd = speak_via("Linux", EN, jp_auto=True, avail=("spd-say",))
    check("linux spd-say: English not routed", "-l" not in cmd)

    # ── Linux espeak (no spd-say) ──
    cmd = speak_via("Linux", JP, jp_auto=True, avail=("espeak",))
    check("linux espeak: JP -> -v ja", cmd and cmd[0] == "espeak" and "-v" in cmd and cmd[cmd.index("-v") + 1] == "ja")
    cmd = speak_via("Linux", EN, jp_auto=True, avail=("espeak",))
    check("linux espeak: English not routed", "-v" not in cmd)

    # ── Linux with neither backend: JP reports handled (silent) so it does NOT drop
    #    to the English-only Piper model. English stays unhandled so Piper (the correct
    #    voice for English) still runs. ──
    tts.platform.system = lambda: "Linux"
    shutil.which = lambda n: None
    tts.set_jp_auto(True)
    check("linux none: JP with no backend -> True (handled, no English-Piper drop)",
          tts._system_speak(JP, 1.0, 1.0) is True)
    check("linux none: English with no backend -> False (falls to Piper)",
          tts._system_speak(EN, 1.0, 1.0) is False)

    # ── Windows PowerShell ──
    cmd = speak_via("Windows", JP, jp_auto=True, jp_voice="Microsoft Haruka Desktop")
    ps = cmd[-1] if cmd else ""
    check("win: explicit voice -> SelectVoice(...)", "SelectVoice('Microsoft Haruka Desktop')" in ps)
    check("win: PS forces UTF-8 InputEncoding (fix a)", "InputEncoding" in ps and "UTF8" in ps)
    cmd = speak_via("Windows", JP, jp_auto=True, jp_voice="")
    ps = cmd[-1] if cmd else ""
    check("win: no voice -> SelectVoiceByHints ja-JP", "SelectVoiceByHints" in ps and "ja-JP" in ps)
    cmd = speak_via("Windows", EN, jp_auto=True)
    ps = cmd[-1] if cmd else ""
    check("win: English not routed -> no Select", "Select" not in ps)
    cmd = speak_via("Windows", JP, jp_auto=False)
    ps = cmd[-1] if cmd else ""
    check("win: auto off -> no Select", "Select" not in ps)
    cmd = speak_via("Windows", JP, jp_auto=True, jp_voice="O'Brien JP")
    ps = cmd[-1] if cmd else ""
    check("win: single-quote in voice name is PS-escaped", "SelectVoice('O''Brien JP')" in ps)

    # ── reading= reaches espeak as KANA (not the kanji display), and unknown kanji
    #    with no reading is stripped, so espeak never announces "Chinese letter". This
    #    is the whole point of the reading pipeline. Assert the actual spoken text. ──
    tts.platform.system = lambda: "Linux"
    shutil.which = lambda n: ("/usr/bin/" + n) if n == "espeak" else None
    tts.set_jp_auto(True); tts.set_jp_voice("")
    CAP.clear(); tts._system_speak("全体攻撃", 1.0, 1.0, "ぜんたいこうげき")
    check("linux: espeak speaks the kana reading, not the kanji display",
          CAP.get("text") == "ぜんたいこうげき")
    CAP.clear(); tts._system_speak("全体攻撃カナ", 1.0, 1.0)   # no reading known
    check("linux: unknown kanji stripped from espeak input (keeps kana)",
          CAP.get("text") == "カナ")
    tts.platform.system = lambda: "Windows"
    CAP.clear(); tts._system_speak("全体攻撃", 1.0, 1.0, "ぜんたいこうげき")
    check("win: SAPI receives the kanji display (it reads kanji natively)",
          "全体攻撃" in (CAP.get("text") or ""))

    # ── master volume past 100%: espeak and spd-say have native headroom and
    #    honor the slider's 200% ceiling. SAPI caps at 100. ──
    tts.set_master_volume(2.0)
    cmd = speak_via("Linux", EN, jp_auto=True, avail=("espeak",))
    check("linux espeak: 200% master volume -> -a 200",
          "-a" in cmd and cmd[cmd.index("-a") + 1] == "200")
    cmd = speak_via("Linux", EN, jp_auto=True, avail=("spd-say",))
    check("linux spd-say: 200% master volume -> -i 100",
          "-i" in cmd and cmd[cmd.index("-i") + 1] == "100")
    cmd = speak_via("Windows", EN, jp_auto=True)
    ps = cmd[-1] if cmd else ""
    check("win: SAPI caps 200% master volume at Volume=100", "$s.Volume=100;" in ps)
    tts.set_master_volume(0.5)
    cmd = speak_via("Linux", EN, jp_auto=True, avail=("espeak",))
    check("linux espeak: 50% master volume -> -a 50",
          "-a" in cmd and cmd[cmd.index("-a") + 1] == "50")
    tts.set_master_volume(1.0)
finally:
    tts._run_speak_proc = _orig_proc
    tts.platform.system = _orig_sys
    shutil.which = _orig_which
    tts.set_jp_auto(_orig_auto)
    tts.set_jp_voice("")

# ── a backend spawn failure must leave a diagnostic. JP callouts go silent on
#    this path, handled instead of garbled, so without a log line they would
#    vanish without a trace. ──
def _raising_proc(*a, **k):
    raise OSError("spawn blew up")


_o_proc_sp = tts._run_speak_proc
_logged_snapshot = set(tts._logged_once)
tts._logged_once.discard("system-spawn")
tts._run_speak_proc = _raising_proc
try:
    tts.platform.system = lambda: "Linux"
    shutil.which = lambda n: ("/usr/bin/" + n) if n == "espeak" else None
    tts.set_jp_auto(True)
    _buf = io.StringIO()
    with contextlib.redirect_stderr(_buf):
        _r = tts._system_speak(JP, 1.0, 1.0)
    check("spawn failure still reports JP handled", _r is True)
    check("spawn failure leaves a one-shot diagnostic",
          "system voice spawn failed" in _buf.getvalue())
finally:
    tts._run_speak_proc = _o_proc_sp
    tts._logged_once.clear()
    tts._logged_once.update(_logged_snapshot)
    tts.platform.system = _orig_sys
    shutil.which = _orig_which
    tts.set_jp_auto(_orig_auto)

# ── fix (b): _run_speak_proc reports the child's real exit status, so a failed
#    synth falls back instead of going silent. Uses sys.executable exit codes
#    (always present, unlike /bin/true|false). ──
check("exit 0 -> True (handled/spoke)",
      tts._run_speak_proc([_PY, "-c", "import sys;sys.exit(0)"], "", stdin_text=False) is True)
check("nonzero exit -> False (caller falls back)",
      tts._run_speak_proc([_PY, "-c", "import sys;sys.exit(1)"], "", stdin_text=False) is False)

# ── fix (b) regression guard: an interrupt()-terminated proc exits nonzero too,
#    but that is intentional. It must return True so _pipeline does NOT replay the
#    cut-off callout through Piper. ──
_res = {}
_th = threading.Thread(
    target=lambda: _res.__setitem__(
        "r", tts._run_speak_proc([_PY, "-c", "import time;time.sleep(5)"], "", stdin_text=False)))
_th.start()
for _ in range(500):        # wait until the proc registers as _current_proc, up to 10 s
    time.sleep(0.02)
    if tts._current_proc is not None:
        break
tts.interrupt()             # terminates it -> nonzero exit, but intentional
_th.join(timeout=5)
check("interrupt-terminated proc returns True (no Piper replay)", _res.get("r") is True)

# ── fix (b) contract via _pipeline: system handled -> skip Piper. System failed
#    -> fall back to Piper. Engine forced to 'system' so _system_speak is invoked. ──
_o_ss, _o_lp, _o_pw, _o_eng = tts._system_speak, tts._load_piper, tts._play_wav, tts._engine
_calls = []
try:
    tts.set_engine("system")
    tts._system_speak = lambda *a, **k: True
    tts._load_piper = lambda: _calls.append("piper")
    tts._pipeline("hello", 1.0, 1.0)
    check("_pipeline skips Piper when the system voice handled it", "piper" not in _calls)

    _calls.clear()
    tts._system_speak = lambda *a, **k: False

    class _FakeVoice:
        def synthesize_wav(self, text, wf, **kw):
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
            wf.writeframes(b"\x00\x00")

    tts._load_piper = lambda: _FakeVoice()   # _load_piper returns the voice
    tts._play_wav = lambda p, gen=None: _calls.append("played")
    tts._pipeline("hello", 1.0, 1.0)
    check("_pipeline falls back to Piper when the system voice failed", "played" in _calls)
finally:
    tts._system_speak, tts._load_piper, tts._play_wav = _o_ss, _o_lp, _o_pw
    tts._engine, tts._piper_voice = _o_eng, None

# ── M1 route: Japanese text uses the system voice even under the Piper engine
#    (the `_jp_auto and has_japanese` clause), while English under Piper does not.
#    Guards the second clause of the _pipeline route, which the tests above (all
#    engine="system") never exercised. ──
_o_ss2, _o_lp2, _o_pw2, _o_eng2, _o_auto2 = (
    tts._system_speak, tts._load_piper, tts._play_wav, tts._engine, tts._jp_auto)
_routed: list = []


class _FakeVoice2:
    def synthesize_wav(self, text, wf, **kw):
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050); wf.writeframes(b"\x00\x00")


try:
    tts.set_engine("piper")          # English-only neural engine
    tts.set_jp_auto(True)
    tts._system_speak = lambda text, *a, **k: (_routed.append(text), True)[1]
    tts._pipeline("フレア来ます", 1.0, 1.0)
    check("_pipeline routes JP text to the system voice under the Piper engine",
          len(_routed) == 1 and tts.has_japanese(_routed[0]))

    _routed.clear()
    tts._load_piper = lambda: _FakeVoice2()  # _load_piper returns the voice
    tts._play_wav = lambda p, gen=None: None
    tts._pipeline("stack", 1.0, 1.0)   # English under Piper -> Piper, not the system voice
    check("_pipeline: English under Piper does not force the system voice", not _routed)
finally:
    (tts._system_speak, tts._load_piper, tts._play_wav, tts._engine, tts._jp_auto) = (
        _o_ss2, _o_lp2, _o_pw2, _o_eng2, _o_auto2)
    tts._piper_voice = None

# ── fix (a), stdin half: the stdin_text path must encode JP as UTF-8 onto the child's
#    stdin (a non-JP Windows locale can't encode it in the ANSI default). Every
#    _system_speak test stubs _run_speak_proc, so drive the REAL function and assert
#    the Japanese round-trips to the child intact. ──
_reader = [_PY, "-c",
           "import sys; sys.stdin.reconfigure(encoding='utf-8'); "
           "sys.exit(0 if sys.stdin.read() == 'フレア来ます' else 7)"]
check("stdin_text: Japanese round-trips as UTF-8 to the child",
      tts._run_speak_proc(_reader, "フレア来ます", stdin_text=True) is True)

# ── TTS3: the native-volume notification path must refuse a FIFO the same way
#    _play_wav_file does. Without the isfile guard aplay would park the daemon
#    thread and its chime slot for the 60 s timeout. _play_wav_detached is
#    stubbed, so a regression shows as a recorded play, not a stuck suite. ──
if hasattr(os, "mkfifo"):
    import tempfile
    _dir = tempfile.mkdtemp()
    _fifo = os.path.join(_dir, "chime.fifo")
    os.mkfifo(_fifo)
    _played = []
    _o_detached = tts._play_wav_detached
    tts._play_wav_detached = lambda p: _played.append(p)
    try:
        # The worker releases a chime slot in finally, so take one first or the
        # bounded semaphore raises on the extra release.
        tts._notification_slots.acquire()
        _t = threading.Thread(target=tts._notification_worker, args=(_fifo, 1.0), daemon=True)
        _t.start()
        _t.join(5)
        check("notification worker refuses a FIFO without hanging", not _t.is_alive())
        check("FIFO never played", _played == [])
        _empty = os.path.join(_dir, "empty.wav")
        open(_empty, "wb").close()
        tts._notification_slots.acquire()
        tts._notification_worker(_empty, 1.0)
        check("empty sound file never played", _played == [])
    finally:
        tts._play_wav_detached = _o_detached

# ── piper speed goes through SynthesisConfig. piper 1.4 dropped the
#    length_scale kwarg from synthesize_wav, so a Speed other than 1.0 raised
#    TypeError and the callout died in the worker loop. piper is not
#    importable here, so stub piper.config in sys.modules. ──
_o_lp3, _o_pw3, _o_eng3 = tts._load_piper, tts._play_wav, tts._engine
_prior = {k: sys.modules.get(k) for k in ("piper", "piper.config")}
_seen: dict = {}


class _FakeSynCfg:
    def __init__(self, length_scale=None):
        self.length_scale = length_scale


class _FakeVoice3:
    def synthesize_wav(self, text, wf, **kw):
        _seen.update(kw)
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
        wf.writeframes(b"\x00\x00")


try:
    tts.set_engine("piper")
    tts._load_piper = lambda: _FakeVoice3()
    tts._play_wav = lambda p, gen=None: None
    _cfg = types.ModuleType("piper.config")
    _cfg.SynthesisConfig = _FakeSynCfg
    sys.modules["piper.config"] = _cfg
    tts._pipeline("hello", 1.0, 2.0)
    check("piper speed passes syn_config, not a length_scale kwarg",
          "length_scale" not in _seen and isinstance(_seen.get("syn_config"), _FakeSynCfg))
    check("syn_config carries 1/speed as length_scale",
          _seen["syn_config"].length_scale == 0.5)
finally:
    tts._load_piper, tts._play_wav, tts._engine = _o_lp3, _o_pw3, _o_eng3
    for _k, _v in _prior.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v
    tts._piper_voice = None

# ── kokoro loader tests share a pair of dummy model files. ──
_tmpdir = tempfile.mkdtemp()
_kmodel = os.path.join(_tmpdir, "kokoro-v1.0.onnx")
_kvoices = os.path.join(_tmpdir, "voices-v1.0.bin")
open(_kmodel, "wb").close()
open(_kvoices, "wb").close()
_prior_kokoro_mod = sys.modules.get("kokoro_onnx")
_o_km, _o_kv = tts._KOKORO_MODEL, tts._KOKORO_VOICES
_o_k, _o_kf = tts._kokoro, tts._kokoro_failed
_o_timeout = tts._SYNTH_TIMEOUT_S


def _restore_kokoro():
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = _o_km, _o_kv
    tts._kokoro, tts._kokoro_failed = _o_k, _o_kf
    if _prior_kokoro_mod is None:
        sys.modules.pop("kokoro_onnx", None)
    else:
        sys.modules["kokoro_onnx"] = _prior_kokoro_mod


# ── a purge-broken kokoro import, set_venv_path sweeping sys.modules while the
#    worker imports, is retried once and must not stick the failed marker. ──
_attempts = {"n": 0}
_purges = {"n": 0}


class _FakeKokoro:
    def __init__(self, model, voices):
        pass


_kokoro_stub = types.ModuleType("kokoro_onnx")


def _flaky_getattr(name):
    if name == "Kokoro":
        _attempts["n"] += 1
        if _attempts["n"] == 1:
            # what a sys.modules purge mid import raises in the importer
            raise KeyError("kokoro_onnx")
        return _FakeKokoro
    raise AttributeError(name)


_kokoro_stub.__getattr__ = _flaky_getattr
_o_purge = tts._purge_stale_venv_modules
try:
    sys.modules["kokoro_onnx"] = _kokoro_stub
    tts._purge_stale_venv_modules = lambda: _purges.__setitem__("n", _purges["n"] + 1)
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro, tts._kokoro_failed = None, False
    _k = tts._load_kokoro()
    check("purge-broken kokoro import is retried and builds",
          isinstance(_k, _FakeKokoro) and _attempts["n"] == 2)
    check("purge-broken import does not stick the failed marker",
          tts._kokoro_failed is False)
    check("the loader re-purges around the import", _purges["n"] >= 2)
finally:
    tts._purge_stale_venv_modules = _o_purge
    _restore_kokoro()

# ── a wedged kokoro model load is capped by _SYNTH_TIMEOUT_S instead of
#    blocking the single TTS worker forever. ──
class _WedgedKokoro:
    def __init__(self, model, voices):
        time.sleep(30)   # stands in for a native hang, abandoned when the cap fires


_kokoro_stub2 = types.ModuleType("kokoro_onnx")
_kokoro_stub2.Kokoro = _WedgedKokoro
try:
    sys.modules["kokoro_onnx"] = _kokoro_stub2
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro, tts._kokoro_failed = None, False
    tts._SYNTH_TIMEOUT_S = 0.2
    _t0 = time.monotonic()
    _r = tts._load_kokoro()
    _dt = time.monotonic() - _t0
    check("wedged kokoro load returns inside the synth timeout", _r is None and _dt < 5)
    check("wedged kokoro load sticks the failed marker", tts._kokoro_failed is True)
    check("wedged kokoro load keeps the model files",
          os.path.exists(_kmodel) and os.path.exists(_kvoices))
finally:
    tts._SYNTH_TIMEOUT_S = _o_timeout
    _restore_kokoro()

# ── a set_jp_neural off landing mid build keeps the in-flight build from
#    publishing the session that call just dropped. The fake constructor plays
#    the GUI thread and bumps the epoch while the build is inside _synth_call. ──
class _EpochBumpKokoro:
    def __init__(self, model, voices):
        tts.set_jp_neural(False)


_kokoro_stub3 = types.ModuleType("kokoro_onnx")
_kokoro_stub3.Kokoro = _EpochBumpKokoro
try:
    sys.modules["kokoro_onnx"] = _kokoro_stub3
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro, tts._kokoro_failed = None, False
    _e0 = tts._kokoro_epoch
    _k = tts._load_kokoro()
    check("a mid-build set_jp_neural bumps the kokoro epoch",
          tts._kokoro_epoch == _e0 + 1)
    check("the superseded kokoro build publishes nothing",
          _k is None and tts._kokoro is None)
    check("the superseded kokoro build leaves the failed marker alone",
          tts._kokoro_failed is False)
finally:
    tts._kokoro_epoch = _e0
    _restore_kokoro()

# ── a failed kokoro_onnx import sticks in kokoro_ready, so broken deps do not
#    re-run the heavy import on every check. None in sys.modules makes the
#    import raise, no real kokoro install needed. ──
try:
    sys.modules["kokoro_onnx"] = None
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro_import_failed = False
    with contextlib.redirect_stderr(io.StringIO()):
        _r1 = tts.kokoro_ready()
        _stuck = tts._kokoro_import_failed
        _r2 = tts.kokoro_ready()
    check("kokoro_ready reports broken deps not ready", _r1 is False)
    check("a failed kokoro import sticks", _stuck is True)
    check("the stuck import marker short-circuits later checks", _r2 is False)
finally:
    tts._kokoro_import_failed = False
    _restore_kokoro()

# ── the piper session build gets the same cap. Stub the piper and onnxruntime
#    imports, which are not importable here, the same way the syn_config test
#    stubs piper.config. ──
_pmodel = os.path.join(_tmpdir, "voice.onnx")
open(_pmodel, "wb").close()
with open(_pmodel + ".json", "w", encoding="utf-8") as _f:
    _f.write("{}")

_ort_stub = types.ModuleType("onnxruntime")


class _FakeSessOpts:
    def add_session_config_entry(self, k, v):
        pass


_ort_stub.SessionOptions = _FakeSessOpts

_pcfg_stub = types.ModuleType("piper.config")


class _FakePiperConfig:
    @classmethod
    def from_dict(cls, d):
        return cls()


_pcfg_stub.PiperConfig = _FakePiperConfig

_pvoice_stub = types.ModuleType("piper.voice")


class _FakePiperVoice:
    def __init__(self, config=None, session=None):
        self.session = session


_pvoice_stub.PiperVoice = _FakePiperVoice

_mod_names = ("onnxruntime", "piper", "piper.config", "piper.voice")
_prior_mods = {k: sys.modules.get(k) for k in _mod_names}
_o_pmodel = tts._PIPER_MODEL
_o_pv, _o_pf = tts._piper_voice, tts._piper_failed
try:
    sys.modules["onnxruntime"] = _ort_stub
    sys.modules["piper"] = types.ModuleType("piper")
    sys.modules["piper.config"] = _pcfg_stub
    sys.modules["piper.voice"] = _pvoice_stub
    tts._PIPER_MODEL = Path(_pmodel)
    tts._piper_voice, tts._piper_failed = None, False

    _ort_stub.InferenceSession = lambda *a, **k: time.sleep(30)
    tts._SYNTH_TIMEOUT_S = 0.2
    _t0 = time.monotonic()
    with contextlib.redirect_stderr(io.StringIO()):
        _v = tts._load_piper()
    _dt = time.monotonic() - _t0
    check("wedged piper session build returns inside the synth timeout",
          _v is None and _dt < 5)
    check("wedged piper session build sticks the failed marker",
          tts._piper_failed is True)

    _ort_stub.InferenceSession = lambda *a, **k: "session-marker"
    tts._piper_voice, tts._piper_failed = None, False
    _v = tts._load_piper()
    check("piper session build hands the session to the voice",
          isinstance(_v, _FakePiperVoice) and _v.session == "session-marker")
finally:
    tts._SYNTH_TIMEOUT_S = _o_timeout
    tts._PIPER_MODEL = _o_pmodel
    tts._piper_voice, tts._piper_failed = _o_pv, _o_pf
    for _k2, _v2 in _prior_mods.items():
        if _v2 is None:
            sys.modules.pop(_k2, None)
        else:
            sys.modules[_k2] = _v2

# ── _wav_seconds falls back to the RIFF chunks for wavs the wave module
#    refuses, IEEE float and extensible formats, so their runtime still bounds
#    the playback kill instead of collapsing to the 60 s floor. ──
def _wav_blob(tag, sr=8000, channels=1, bits=32, frames=4000):
    block = channels * bits // 8
    data = b"\x00" * (frames * block)
    fmt = struct.pack("<HHIIHH", tag, channels, sr, sr * block, block, bits)
    if tag == 0xFFFE:
        # Extensible appends 22 bytes, cbSize, valid bits, channel mask and
        # the subformat GUID, the float one here.
        fmt += struct.pack("<H", 22) + struct.pack("<HI", bits, 0) \
            + bytes.fromhex("0300000000001000800000aa00389b71")
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


_f32 = _wav_blob(3)
try:
    with wave.open(io.BytesIO(_f32), "rb"):
        _refused = False
except wave.Error:
    _refused = True
check("the stdlib wave module refuses the tag-3 float wav under test", _refused)
check("a float wav still has a duration via the RIFF fallback",
      abs(tts._wav_seconds(_f32) - 0.5) < 0.01)
check("an extensible wav still has a duration via the RIFF fallback",
      abs(tts._wav_seconds(_wav_blob(0xFFFE)) - 0.5) < 0.01)
check("plain PCM wav duration is unchanged",
      abs(tts._wav_seconds(_wav_blob(1, bits=16)) - 0.5) < 0.01)
_fwav = os.path.join(_tmpdir, "float.wav")
with open(_fwav, "wb") as _f:
    _f.write(_f32)
check("the RIFF fallback works from a path too",
      abs(tts._wav_seconds(_fwav) - 0.5) < 0.01)
check("a truncated float wav clamps to the bytes really there",
      0.0 < tts._wav_seconds(_f32[:-100]) < 0.5)
check("a junk wav still reads as 0", tts._wav_seconds(b"RIFF\x00\x00\x00\x00WAVEjunk") == 0.0)
check("a missing wav still reads as 0",
      tts._wav_seconds(os.path.join(_tmpdir, "nope.wav")) == 0.0)

# ── a transient kokoro build failure, an AV lock or out of memory mid session
#    build, sticks the failed marker but keeps the ~330 MB model files. ──
class _LockFailKokoro:
    def __init__(self, model, voices):
        raise MemoryError("stands in for a transient build failure")


_kokoro_stub4 = types.ModuleType("kokoro_onnx")
_kokoro_stub4.Kokoro = _LockFailKokoro
try:
    sys.modules["kokoro_onnx"] = _kokoro_stub4
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro, tts._kokoro_failed = None, False
    with contextlib.redirect_stderr(io.StringIO()):
        _k = tts._load_kokoro()
    check("a transient kokoro build failure loads nothing", _k is None)
    check("a transient kokoro build failure sticks the failed marker",
          tts._kokoro_failed is True)
    check("a transient kokoro build failure keeps the model files",
          os.path.exists(_kmodel) and os.path.exists(_kvoices))
finally:
    _restore_kokoro()

# ── the download flow over existing model files fetches nothing and clears
#    both failed markers, the recovery path the kept files rely on. ──
_o_urls = tts._KOKORO_URLS
_o_kif = tts._kokoro_import_failed
_o_mdir = tts._MODEL_DIR
try:
    tts._KOKORO_URLS = {Path(_kmodel): "unused", Path(_kvoices): "unused"}
    tts._MODEL_DIR = Path(_tmpdir)
    tts._kokoro_failed = True
    tts._kokoro_import_failed = True
    _ok = tts.download_kokoro_model()
    check("the download flow over existing files succeeds without fetching",
          _ok is True and os.path.exists(_kmodel) and os.path.exists(_kvoices))
    check("the download flow clears the failed markers",
          tts._kokoro_failed is False and tts._kokoro_import_failed is False)
finally:
    tts._KOKORO_URLS = _o_urls
    tts._MODEL_DIR = _o_mdir
    tts._kokoro_failed, tts._kokoro_import_failed = _o_kf, _o_kif

# ── the notification worker enforces the same 32 MiB cap as the TTS worker. ──
_big = os.path.join(_tmpdir, "big.wav")
with open(_big, "wb") as _f:
    _f.truncate(tts._MAX_SOUND_BYTES + 1)
_played, _drops = [], []
_o_pwd, _o_ld, _o_mv = tts._play_wav_detached, tts.log_drop, tts._master_volume
try:
    tts._play_wav_detached = lambda p: _played.append(p)
    tts.log_drop = lambda site, detail, throttle_s=1.0: _drops.append(site)
    tts._master_volume = 1.0
    # The worker releases a chime slot in its finally, so hold one first.
    check("a chime slot is free for the notification test",
          tts._notification_slots.acquire(timeout=5))
    tts._notification_worker(_big, 1.0)
    check("an oversized notification sound is not played", not _played)
    check("an oversized notification sound leaves a tts-notify drop",
          "tts-notify" in _drops)
    check("a chime slot is free for the small-file notification test",
          tts._notification_slots.acquire(timeout=5))
    tts._notification_worker(_fwav, 1.0)
    check("a normal notification sound still plays", _played == [_fwav])
finally:
    tts._play_wav_detached, tts.log_drop, tts._master_volume = _o_pwd, _o_ld, _o_mv

# ── the purge scans a snapshot of the stale set. A live iteration would let a
#    GUI thread set_venv_path break it with a set-changed-size RuntimeError. ──
class _IterBomb(set):
    def __iter__(self):
        raise RuntimeError("iterated the live stale set")


_o_stale = tts._stale_venv_sps
_probe = types.ModuleType("zz_stale_probe")
_probe.__file__ = "/nonexistent/site-packages/zz_stale_probe.py"
sys.modules["zz_stale_probe"] = _probe
try:
    tts._stale_venv_sps = _IterBomb(("/nonexistent/site-packages",))
    _raised = False
    try:
        tts._purge_stale_venv_modules()
    except RuntimeError:
        _raised = True
    check("the purge never iterates the live stale set", not _raised)
    check("the purge still drops modules under a stale site-packages",
          "zz_stale_probe" not in sys.modules)
finally:
    tts._stale_venv_sps = _o_stale
    sys.modules.pop("zz_stale_probe", None)

# ── a configured venv with no interpreter fails the deps install instead of
#    pip installing kokoro-onnx into the app interpreter. ──
_o_venv = tts._FFXIV_VENV
try:
    _bogus = Path(_tmpdir) / "bogus_venv"
    _bogus.mkdir()
    tts._FFXIV_VENV = _bogus
    with contextlib.redirect_stderr(io.StringIO()):
        _ok, _msg = tts.install_kokoro_deps(timeout=30)
    check("a bogus configured venv fails the deps install",
          _ok is False and "bogus_venv" in _msg)
    (_bogus / "bin").mkdir()
    (_bogus / "bin" / "python").touch()
    check("a real interpreter in the configured venv is used",
          tts._venv_python() == str(_bogus / "bin" / "python"))
    tts._FFXIV_VENV = None
    check("no configured venv still falls back to the app python",
          tts._venv_python() == sys.executable)
finally:
    tts._FFXIV_VENV = _o_venv

# ── a transient kokoro constructor failure keeps the model files, and the
#    drop log names both recoveries, the Download button for a transient
#    failure and manual deletion for a file that went bad on disk. ──
class _TransientFailKokoro:
    def __init__(self, model, voices):
        raise MemoryError("session build OOM")


_kokoro_stub4 = types.ModuleType("kokoro_onnx")
_kokoro_stub4.Kokoro = _TransientFailKokoro
_drops = []
_o_log_drop = tts.log_drop
try:
    sys.modules["kokoro_onnx"] = _kokoro_stub4
    tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
    tts._kokoro, tts._kokoro_failed = None, False
    tts.log_drop = lambda site, detail, *a, **k: _drops.append((site, detail))
    _r = tts._load_kokoro()
    check("transient kokoro load failure sticks the failed marker",
          _r is None and tts._kokoro_failed is True)
    check("transient failure keeps the model files",
          os.path.exists(_kmodel) and os.path.exists(_kvoices))
    check("the drop log names manual deletion for a bad on disk file",
          any(site == "tts-kokoro" and "delete the model files" in detail
              for site, detail in _drops))
finally:
    tts.log_drop = _o_log_drop
    _restore_kokoro()

# ── the interrupt generation vetoes. An interrupt must stop not just the
#    queue and the tracked proc but any callout already past the queue. Each
#    spawn and playback site rechecks the stamp under _proc_lock right before
#    it starts, and the piper path rechecks after synthesis. Stub Popen so a
#    regression shows as a recorded spawn, not real audio. ──
_spawned: list = []


class _FakePopen:
    def __init__(self, *a, **k):
        _spawned.append(a[0] if a else None)
        self.returncode = 0

    def communicate(self, *a, **k):
        return (b"", b"")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


_o_popen2 = tts.subprocess.Popen
tts.subprocess.Popen = _FakePopen
try:
    tts._generation = 5
    tts._current_proc = None
    check("stale gen vetoes the system voice spawn",
          tts._run_speak_proc(["espeak"], "hi", stdin_text=False, gen=4) is True
          and _spawned == [] and tts._current_proc is None)

    _o_ws2 = tts._wav_seconds
    tts._wav_seconds = lambda src: 0.0
    _spawned.clear()
    tts._play_wav("/tmp/never.wav", gen=4)
    check("stale gen vetoes the playback spawn", _spawned == [])
    tts._play_wav("/tmp/never.wav", gen=5)
    check("current gen still spawns playback", len(_spawned) == 1)
    tts._wav_seconds = _o_ws2
finally:
    tts.subprocess.Popen = _o_popen2
    tts._generation = 0
    tts._current_proc = None

#    an interrupt landing while piper synthesizes drops the callout at the
#    post synthesis check instead of playing stale audio late. The fake voice
#    plays the interrupter and bumps the generation mid synthesis.
class _InterruptingVoice:
    def synthesize_wav(self, text, wf, **kw):
        tts._generation += 1
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
        wf.writeframes(b"\x00\x00")


_o_lp4, _o_pw4, _o_eng4 = tts._load_piper, tts._play_wav, tts._engine
_played4: list = []
try:
    tts.set_engine("piper")
    tts._jp_neural = False
    tts._load_piper = lambda: _InterruptingVoice()
    tts._play_wav = lambda p, gen=None: _played4.append(p)
    _g0 = tts._generation
    tts._pipeline("hello", 1.0, 1.0, gen=_g0)
    check("interrupt mid piper synthesis drops the callout", _played4 == [])
finally:
    tts._load_piper, tts._play_wav, tts._engine = _o_lp4, _o_pw4, _o_eng4
    tts._jp_neural = False

# ── the enqueue stamp. Items carry the generation at enqueue time, so an
#    interrupt between queue and dispatch vetoes the callout, and a callout
#    queued after the interrupt carries the new stamp. ──
while not tts._queue.empty():
    tts._queue.get_nowait()
tts._generation = 100
tts._enqueue(("tts", "x", 1.0, 1.0, None))
_stamped = tts._queue.get_nowait()
check("enqueue stamps the current generation", getattr(_stamped, "gen", None) == 100)
check("a stamped item still compares as a plain tuple",
      _stamped == ("tts", "x", 1.0, 1.0, None))
tts._enqueue(("tts", "y", 1.0, 1.0, None))
tts.interrupt()
check("interrupt drains the queue and bumps the generation",
      tts._queue.empty() and tts._generation == 101)
tts._enqueue(("tts", "z", 1.0, 1.0, None))
check("a post interrupt enqueue carries the new stamp",
      tts._queue.get_nowait().gen == 101)

# ── set_readings filters junk instead of poisoning the reading map. ──
tts.set_readings({"全体攻撃": "ぜんたいこうげき", "bad": 5, 7: "x", "empty": "",
                  "k": None, ("t",): "y"})
check("set_readings keeps only str keys with non empty str values",
      tts._READINGS == {"全体攻撃": "ぜんたいこうげき"})
check("reading_for resolves a known display",
      tts.reading_for("全体攻撃") == "ぜんたいこうげき")
tts.set_readings({})

# ── a muted master volume skips synthesis entirely. The quietest backend
#    settings are still audible, so muted must never reach a voice. ──
_o_mv2 = tts._master_volume
_fired: list = []
try:
    tts._master_volume = 0.0
    tts._load_piper = lambda: _fired.append("load")
    tts._pipeline("hello", 1.0, 1.0)
    check("muted master volume skips synthesis entirely", _fired == [])
finally:
    tts._master_volume = _o_mv2
    tts._load_piper = _o_lp4

# ── a missing notification path no-ops without eating a chime slot. ──
_slots_before = tts._notification_slots._value
tts.play_notification(os.path.join(_tmpdir, "no-such-chime.wav"))
check("a missing notification path no-ops and leaks no slot",
      tts._notification_slots._value == _slots_before)

# ── numpy and the pure Python fallback must scale PCM identically, or the
#    same trigger sounds different depending on whether numpy is importable.
#    Skipped without numpy, it is an optional dependency by design. ──
if tts._np is None:
    print("SKIP  scale_pcm parity needs numpy")
else:
    _np_saved = tts._np
    _vals = [-32768, -32767, -1, 0, 1, 127, 255, 32766, 32767, -128]
    _frames16 = b"".join(v.to_bytes(2, "little", signed=True) for v in _vals)
    _parity = True
    for _vol in (0.0, 0.5, 0.73, 1.0, 1.5, 2.0, 4.0):
        _with_np = tts._scale_pcm(_frames16, 2, _vol)
        tts._np = None
        _with_py = tts._scale_pcm(_frames16, 2, _vol)
        tts._np = _np_saved
        _parity = _parity and _with_np == _with_py
    check("scale_pcm 16-bit numpy and pure Python agree", _parity)
    _frames8 = bytes([0, 1, 127, 128, 129, 254, 255, 77])
    _parity = True
    for _vol in (0.0, 0.5, 1.0, 1.7, 2.5):
        _with_np = tts._scale_pcm(_frames8, 1, _vol)
        tts._np = None
        _with_py = tts._scale_pcm(_frames8, 1, _vol)
        tts._np = _np_saved
        _parity = _parity and _with_np == _with_py
    check("scale_pcm 8-bit numpy and pure Python agree", _parity)
    check("scale_pcm drops a stray trailing byte",
          tts._scale_pcm(b"\x01\x02\x03", 2, 1.0)
          == tts._scale_pcm(b"\x01\x02", 2, 1.0))

# ── kokoro synthesis output shape. Speed is clamped into kokoro's 0.5 to 2.0
#    window before create, the trigger dialog allows 0.5 to 3.0, and the wav
#    container is 16-bit mono at the model's rate with symmetric clipping.
#    Needs numpy, kokoro's float path, so it skips without it. ──
try:
    import numpy as _npmod   # noqa: F401
    _have_np = True
except Exception:
    _have_np = False

if not _have_np:
    print("SKIP  kokoro synth shape needs numpy")
else:
    _speeds: list = []
    _voices: list = []

    class _ShapeKokoro:
        def create(self, text, voice=None, speed=1.0, lang=None):
            _speeds.append(speed)
            _voices.append(voice)
            return (_npmod.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype="float32"),
                    24000)

    _shape_stub = types.ModuleType("kokoro_onnx")
    _shape_stub.Kokoro = lambda m, v: _ShapeKokoro()
    _prior_shape = sys.modules.get("kokoro_onnx")
    try:
        sys.modules["kokoro_onnx"] = _shape_stub
        tts._KOKORO_MODEL, tts._KOKORO_VOICES = Path(_kmodel), Path(_kvoices)
        tts._kokoro, tts._kokoro_failed, tts._kokoro_epoch = None, False, 0
        # a non default token, so a hardcoded jf_alpha in the synth call fails
        _o_jpv = tts._jp_neural_voice
        tts._jp_neural_voice = "jm_kumo"
        tts._kokoro_synth("こんにちは", 3.0)
        tts._kokoro_synth("こんにちは", 0.1)
        tts._kokoro_synth("こんにちは", 1.7)
        check("kokoro clamps speed into its 0.5 to 2.0 window",
              _speeds == [2.0, 0.5, 1.7])
        tts._kokoro, tts._kokoro_failed = None, False
        _wav = tts._kokoro_synth("こんにちは", 1.0)
        with wave.open(io.BytesIO(_wav), "rb") as _w:
            check("kokoro wav is 16-bit mono at the model rate",
                  _w.getsampwidth() == 2 and _w.getnchannels() == 1
                  and _w.getframerate() == 24000 and _w.getnframes() == 5)
            _samples = struct.unpack("<5h", _w.readframes(5))
        check("kokoro clips float samples symmetrically to int16",
              _samples == (0, 16383, -16383, 32767, -32767))
        check("kokoro receives the configured jp voice token",
              _voices == ["jm_kumo"] * 4)
    finally:
        tts._jp_neural_voice = _o_jpv
        if _prior_shape is None:
            sys.modules.pop("kokoro_onnx", None)
        else:
            sys.modules["kokoro_onnx"] = _prior_shape
        _restore_kokoro()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
