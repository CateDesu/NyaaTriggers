"""Merged regression suite for the audit/review fix batches.

Consolidates these five one-off scripts (all checks preserved verbatim,
215 in total on this machine):
  - test_review_fixes.py        (2026-07 review pass)
  - test_review_fixes_2.py      (2026-07-22 review pass)
  - test_review_fixes_3.py      (third review pass, main_window.py)
  - test_audit_setup_fixes.py   (2026-08 audit setup pass)
  - test_audit_low_fixes.py     (low-severity audit pass)

Each section banner names its source file. The five originals' module-level
checks are wrapped in per-section test functions so one failure aborts only
its own group; monkeypatch restore-in-finally, temp dirs, lru_cache clears
and the drop_log redirection are kept as the originals had them.

Each test_* function is both a pytest case and a step of the direct-run
script.

Run directly:  python test_regressions.py   (exit 0 = all pass)
        or:    python -m pytest test_regressions.py -q
"""
import hashlib
import io
import json
import os
import queue
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drop_log
import fflogs
import install
import locale_util
import main
import app_common
import main_window as mw
import triggevent_bridge
import trigger_engine as te
import tts
import updater
from convert_cactbot import strip_js_comments
from convert_triggernometry import extract_ids, expand_id_expr
from timeline_parser import _strip_comment, parse
from trigger_engine import (
    Trigger, _HAVE_REGEX, _as_bool, _id_set, _looks_catastrophic,
    _safe_search, _safe_sub, compile_user_regex,
)
from triggernometry_bridge import TriggernometryBridge
from triggevent_bridge import TriggeventBridge
from umad_chains import ACCRETION, CRUST, ORDER_IDS, STALE_S, BlackHoleChains

REPO_DIR = Path(__file__).parent
FAILS = []


def _program_sources():
    """main_window.py plus the modules the MainWindow split moved code into,
    one combined source for the source level checks below."""
    parts = [REPO_DIR / "main_window.py", REPO_DIR / "app_common.py",
             REPO_DIR / "updater_ui.py"]
    parts += sorted((REPO_DIR / "ui").glob("*.py"))
    return "\n".join(p.read_text(encoding="utf-8") for p in parts if p.exists())


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)
        # Under pytest this fails the calling test; the direct-run loop below
        # catches it and moves on to the next test function.
        raise AssertionError(name)


# ═════════════════════════════════════════════════════════════════════════════
# From test_review_fixes.py — version-compare padding, fail-closed release
# verification, the shared user-regex guard, trigger matcher hygiene, and
# from_dict validation.
# ═════════════════════════════════════════════════════════════════════════════

# ── Version comparison: trailing zeros must not read as "newer" ────────────
def test_r1_version_trailing_zeros():
    check("1.0.0 == 1.0", updater.parse_version("1.0.0") == updater.parse_version("1.0"))
    check("v1.0.0 is not newer than v1.0", not updater.is_newer("1.0.0", "1.0"))
    check("v1.0 is not newer than v1.0.0", not updater.is_newer("1.0", "1.0.0"))
    check("v1.0.1 is newer than v1.0", updater.is_newer("1.0.1", "1.0"))
    check("vv-prefix over-strip fixed", updater.parse_version("vv1.2") == (0, 2))


# ── Release verification fails closed with no checksum sidecar ─────────────
def test_r1_release_verification_fails_closed():
    release = updater.Release(tag="v9.9", version="9.9", html_url="",
                              assets={"NyaaTriggers-linux.tar.gz": "https://x/a.tar.gz"})
    ok, msg = updater.verify_release_asset(release, "NyaaTriggers-linux.tar.gz",
                                           Path(__file__))
    check("missing .sha256 sidecar refuses to verify", not ok)


# ── User-regex guard: catastrophic shapes rejected, normal ones cached ─────
def test_r1_user_regex_guard():
    check("(a+)+ rejected", compile_user_regex("(a+)+$") is None)
    check("(\\w*)* rejected", compile_user_regex(r"(\w*)*!") is None)
    check("oversized pattern rejected", compile_user_regex("a" * 600) is None)
    check("plain regex compiles", compile_user_regex("Limit Break", 0) is not None)
    check("same pattern returns the cached object",
          compile_user_regex("Limit Break", 0) is compile_user_regex("Limit Break", 0))
    check("invalid regex returns None, not raises", compile_user_regex("(") is None)


# ── _safe_* wrappers: timeout reads as no-match, bad backref keeps text ────
def test_r1_safe_wrappers():
    check("bad backref sub leaves text unchanged",
          _safe_sub(compile_user_regex(r"(abc)"), r"\2", "abc") == "abc")
    check("normal sub still replaces",
          _safe_sub(compile_user_regex(r"abc"), "x", "abc") == "x")
    if _HAVE_REGEX:
        _t0 = time.monotonic()
        _m = _safe_search(compile_user_regex(r"(a|aa)+$"), "a" * 30 + "b")
        check("catastrophic pattern times out as no-match",
              _m is None and time.monotonic() - _t0 < 4.0)
        check("timeout guard does not break normal matches",
              _safe_search(compile_user_regex(r"(a|aa)+$"), "aaaa") is not None)

    # Stdlib-compiled patterns must also work through the wrappers (the timeout
    # kwarg exists only on the regex module, so the wrappers dispatch on type).
    _stdlib_rx = re.compile(r"abc")
    check("stdlib pattern search works via wrapper",
          _safe_search(_stdlib_rx, "xxabcxx") is not None)
    check("stdlib pattern sub works via wrapper",
          _safe_sub(_stdlib_rx, "y", "abc") == "y")
    check("stdlib pattern bad backref leaves text unchanged",
          _safe_sub(_stdlib_rx, r"\2", "abc") == "abc")


# ── Literal ability IDs: no regex smuggling, whitespace/case tolerated ─────
def test_r1_literal_ability_ids():
    check("id set splits and uppercases", _id_set("a55d | A55E") == {"A55D", "A55E"})
    t = Trigger(log_type="21", ability_id="A55D|A55E", tts_text="x", cooldown_s=0.0)
    line = ["21", "ts", "40001234", "Boss", "a55e", "Some Ability", "10001111", "Player"]
    check("pipe id matches case-insensitively", t.matches(line) is not None)
    evil = Trigger(log_type="21", ability_id="(a+)+$", tts_text="x", cooldown_s=0.0)
    check("regex chars in ability_id match literally (never as a pattern)",
          evil.matches(line) is None)


# ── from_dict hygiene: scope fallback + cooldown clamp ─────────────────────
def test_r1_from_dict_scope_and_cooldown():
    check("unknown status_scope narrows to self",
          Trigger.from_dict({"status_scope": "byme"}).status_scope == "self")
    check("valid status_scope kept",
          Trigger.from_dict({"status_scope": "by_me"}).status_scope == "by_me")
    check("negative cooldown clamped to 0",
          Trigger.from_dict({"cooldown_s": -5}).cooldown_s == 0.0)


# ── Cooldown map stays bounded across churning source IDs ──────────────────
def test_r1_cooldown_map_bounded():
    t2 = Trigger(log_type="21", ability_id="BEEF", tts_text="x", cooldown_s=5.0)
    old = time.monotonic() - 60.0
    t2._last_fired = {f"4000{i:04X}": old for i in range(300)}
    t2.matches(["21", "ts", "40009999", "Boss", "BEEF", "Ability", "10001111", "P"])
    check("expired cooldown entries pruned on write", len(t2._last_fired) < 300)


# ── Cooldown key case: _last_fired is shared with _on_status_timer ─────────
def test_r1_cooldown_key_uppercased():
    t3 = Trigger(log_type="26", tts_text="x", cooldown_s=5.0)
    line = ["26", "ts", "8d1", "Vulnerability Up", "60.0",
            "40001234", "Boss", "10001111", "Player"]
    check("lower-case effect id fires", t3.matches(line, me="Player") is not None)
    check("cooldown keyed upper-case like the firing path",
          "8D1" in t3._last_fired and "8d1" not in t3._last_fired)
    check("same effect suppressed inside the cooldown",
          t3.matches(line, me="Player") is None)


# ── from_dict: non-finite counts degrade like _as_float, never raise ───────
def test_r1_from_dict_nonfinite_counts():
    check('"inf" count_min degrades to default (no OverflowError)',
          Trigger.from_dict({"count_min": "inf"}).count_min == 0)
    check('"-inf" count_max degrades to default',
          Trigger.from_dict({"count_max": "-inf"}).count_max == 0)
    check("float inf count_min degrades to default",
          Trigger.from_dict({"count_min": float("inf")}).count_min == 0)
    check("float nan count_max degrades to default",
          Trigger.from_dict({"count_max": float("nan")}).count_max == 0)
    check('"5.0" count_min still tolerated',
          Trigger.from_dict({"count_min": "5.0"}).count_min == 5)
    check("plain int count_min unchanged",
          Trigger.from_dict({"count_min": 3}).count_min == 3)


# ── from_dict: a truthy non-list sequence is skipped, trigger kept ─────────
def test_r1_from_dict_nonlist_sequence():
    check("int sequence skipped, trigger kept",
          Trigger.from_dict({"sequence": 42}).sequence == [])
    check("bool sequence skipped",
          Trigger.from_dict({"sequence": True}).sequence == [])
    check("string sequence skipped",
          Trigger.from_dict({"sequence": "soon"}).sequence == [])
    check("dict sequence skipped",
          Trigger.from_dict({"sequence": {"log_type": "21"}}).sequence == [])
    check("list sequence still keeps only dict steps",
          Trigger.from_dict({"sequence": [{"log_type": "21"}, "x", 7]}).sequence
          == [{"log_type": "21"}])


# ── from_dict: non-string scalars in match fields coerced or rejected ──────
def test_r1_from_dict_nonstring_fields():
    check("int ability_id coerced to str",
          Trigger.from_dict({"ability_id": 123}).ability_id == "123")
    check("list ability_id rejected to empty",
          Trigger.from_dict({"ability_id": ["A55D"]}).ability_id == "")
    check("float ability_regex coerced to str",
          Trigger.from_dict({"ability_regex": 4.5}).ability_regex == "4.5")
    check("int tts_text coerced to str",
          Trigger.from_dict({"tts_text": 42}).tts_text == "42")
    check("dict zone_regex rejected to empty",
          Trigger.from_dict({"zone_regex": {"x": 1}}).zone_regex == "")
    check("int fight coerced to str",
          Trigger.from_dict({"fight": 9}).fight == "9")
    check("list sound_file rejected to empty",
          Trigger.from_dict({"sound_file": ["a.wav"]}).sound_file == "")
    check("int name coerced to str",
          Trigger.from_dict({"name": 7}).name == "7")
    check("list name falls back to Unnamed",
          Trigger.from_dict({"name": ["x"]}).name == "Unnamed")
    check("non-string id coerced to str",
          Trigger.from_dict({"id": 5}).id == "5")
    check("unhashable id replaced with a generated str",
          isinstance(Trigger.from_dict({"id": ["x"]}).id, str))
    check("falsy scalar ability_id stays empty",
          Trigger.from_dict({"ability_id": 0}).ability_id == "")
    check("int log_type still coerced",
          Trigger.from_dict({"log_type": 26}).log_type == "26")
    # The coerced id must reach the matcher as a working literal, not raise in
    # _id_set the way the raw int/list used to.
    t_num = Trigger.from_dict({"log_type": "21", "ability_id": 123, "cooldown_s": 0})
    check("coerced ability_id matches literally downstream",
          t_num.matches(["21", "ts", "40001234", "Boss", "123", "Ability",
                         "10001111", "P"]) is not None)


# ═════════════════════════════════════════════════════════════════════════════
# From test_review_fixes_2.py — engine-jar verification fails closed, the
# nested-quantifier ReDoS guard, the 261 pair parser bound, converter fixes,
# timeline comment escapes, chain-loss staleness, and the DPS logger close
# flush.
# ═════════════════════════════════════════════════════════════════════════════

# ── S1: the Triggevent Engine download verifies against the .sha256 sidecar ──
def test_r2_engine_jar_verification():
    def _fake_env(assets, download_body=b"PK" + b"\0" * 200_000):
        """Point _download_engine at a temp jar + stubbed release/download."""
        td = tempfile.TemporaryDirectory()
        jar = Path(td.name) / "triggevent-core.jar"
        jar.write_bytes(b"PK\x03\x04old-jar" + b"\0" * 200_000)
        os.environ["NYAA_TRIGGEVENT_JAR"] = str(jar)
        rel = updater.Release(tag="v9.9", version="9.9", html_url="", assets=assets)
        orig_fetch, orig_dl = updater.fetch_latest_release, updater.download
        updater.fetch_latest_release = lambda timeout=8, channel="stable": rel
        updater.download = lambda url, dest, *a, **k: dest.write_bytes(download_body)
        # Sandbox the build stamp too. The success path of _download_engine
        # unlinks it, and an unsandboxed run would delete the live stamp in
        # triggevent-core/target and force a full jar rebuild on the next
        # app launch.
        orig_stamp = triggevent_bridge._JAR_STAMP
        triggevent_bridge._JAR_STAMP = jar.parent / "stamp"

        def restore():
            updater.fetch_latest_release, updater.download = orig_fetch, orig_dl
            triggevent_bridge._JAR_STAMP = orig_stamp
            os.environ.pop("NYAA_TRIGGEVENT_JAR", None)
            td.cleanup()
        return jar, restore

    jar, restore = _fake_env({"triggevent-core.jar": "https://x/core.jar"})
    try:
        old_bytes = jar.read_bytes()
        changed, msg = triggevent_bridge._download_engine("stable")
        check("jar download w/o .sha256 sidecar is rejected", not changed and "rejected" in msg)
        check("rejected download leaves the current jar untouched", jar.read_bytes() == old_bytes)
        check("rejected download leaves no .new droppings",
              not (jar.parent / "triggevent-core.jar.new").exists())
    finally:
        restore()

    # Correct sidecar -> accepted and swapped in.
    body = b"PK" + b"\1" * 200_000
    digest = hashlib.sha256(body).hexdigest()
    jar, restore = _fake_env({"triggevent-core.jar": "https://x/core.jar",
                              "triggevent-core.jar.sha256": "https://x/core.jar.sha256"},
                             download_body=body)
    orig_urlopen_verify = updater.verify_release_asset

    def _verify_via_local(release, asset_name, archive, timeout=15):
        # Same hashing path as production, minus the network fetch of the sidecar.
        h = hashlib.sha256(archive.read_bytes()).hexdigest()
        return (h == digest, "verified" if h == digest else "mismatch")

    updater.verify_release_asset = _verify_via_local
    try:
        changed, msg = triggevent_bridge._download_engine("stable")
        check("verified jar download is installed", changed and jar.read_bytes() == body)
    finally:
        updater.verify_release_asset = orig_urlopen_verify
        restore()


# ── C5: nested-quantifier shapes rejected, field-skip idiom kept ─────────────
def test_r2_nested_quantifier_guard():
    check("(?:a(?:b+)c)+ rejected (nested unbounded)",
          compile_user_regex(r"(?:a(?:b+)c)+") is None)
    check("((a)+b)+ rejected (quantified group in quantified group)",
          compile_user_regex(r"((a)+b)+") is None)
    check("(a|b(c+)+)+ rejected", compile_user_regex(r"(a|b(c+)+)+") is None)
    check("(?:x(?:y*)z){2,} rejected (open-ended brace)",
          compile_user_regex(r"(?:x(?:y*)z){2,}") is None)
    # Pre-existing level-1 strictness, pinned: ANY brace after a group containing a
    # quantifier is rejected, bounded or not ((a+){50} is a degree-50 polynomial).
    check("(?:[^|]*\\|){5} still rejected by the legacy brace guard",
          compile_user_regex(r"(?:[^|]*\|){5}") is None)
    check("unquantified field-skip group allowed",
          compile_user_regex(r"(?:[^|]*\|)9CFF") is not None)
    check("groupless field skip allowed",
          compile_user_regex(r"[^|]*\|[^|]*\|9CFF") is not None)
    check("(a|b)+ allowed (no inner quantifier)",
          compile_user_regex(r"(a|b)+") is not None)
    check("plain (\\d+) allowed", compile_user_regex(r"(\d+) stacks") is not None)
    check("escaped parens \\(a+\\)+ allowed (no real group)",
          not _looks_catastrophic(r"x\)a+y+z"))
    check("class contents can't fake a group: [(+)]+ allowed",
          compile_user_regex(r"[(+)]+") is not None)
    check("legacy (x+)+ still rejected", compile_user_regex(r"(x+)+$") is None)


# ── C8/C9: Triggernometry converter type collapse + escaped ']' classes ──────
def test_r2_triggernometry_converter():
    pairs = extract_ids(r"^.{14}1[56]:[^:]*:[^:]*:9CFF:")
    check("1[56] colon type maps to pipe 21|22",
          pairs and all(lt == "21|22" for lt, _ in pairs) and pairs[0][1] == "9CFF")
    check("literal 15: still maps to 21",
          extract_ids(r"^.{14}15:[^:]*:[^:]*:9CFF:") == [("21", "9CFF")])
    check("class with escaped ] returns no ids (not garbage)",
          expand_id_expr(r"9C[A\]B]F") is None)
    check("normal class still expands",
          expand_id_expr(r"9CF[0-2]") == ["9CF0", "9CF1", "9CF2"])


# ── Colon 1A/1E status lines pin the effect id at field 0 ────────────────────
def test_r2_triggernometry_colon_status_types():
    # 7.x packs anchor these as \A.{25}1A:AB6:..., effect id first, unlike the
    # 15/1[56] casts where the id sits at field 2.
    check("colon 1A maps to 26 with the effect id first",
          extract_ids(r"\A.{25}1A:AB6:[^:]*:") == [("26", "AB6")])
    check("colon 1E maps to 30 with the effect id first",
          extract_ids(r"\A.{25}1E:AB6:[^:]*:") == [("30", "AB6")])


# ── review 5: strip loop bounded, ACT type word colon form ───────────────────
def test_r5_triggernometry_strip_cap_and_type_word():
    # A crafted export nesting thousands of parens ran the strip loop once per
    # level with a full string scan each, quadratic, on the GUI thread. The
    # loop is capped now, so degenerate input bails fast instead.
    deep = "(" * 5000 + "5CFF" + ")" * 5000
    start = time.monotonic()
    result = expand_id_expr(deep)
    check("deeply nested groups bail fast, no quadratic hang",
          result is None and time.monotonic() - start < 1.0)
    check("ordinary nesting still strips",
          expand_id_expr("((5CFF))") == ["5CFF"])
    # The ACT message log dialect writes a type word between .{N} and the hex
    # type, "^.{15}StatusAdd 1A:..." or "^.{15}\S+ 15:...". The skip clause
    # only matched the literal (?:[^:]*) form, so these yielded no ids.
    check("colon form with a \\S+ type word maps the cast",
          extract_ids(r"^.{15}\S+ 15:[^:]*:[^:]*:9CFF:") == [("21", "9CFF")])
    check("colon form with StatusAdd before 1A maps the effect",
          extract_ids(r"^.{15}StatusAdd 1A:4A6F:") == [("26", "4A6F")])


# ── review 5: a hand edited sound setting must not kill the alert emit ───────
def test_r5_alert_sound_path_tolerates_non_string_setting():
    import types as _t
    w = _t.SimpleNamespace(_settings={"overlay_sound_file": 5})
    check("non-string sound setting reads as no sound, no raise",
          mw.MainWindow._alert_sound_path(w) is None)


# ── C4: cactbot comment stripper survives regex literals ─────────────────────
def test_r2_cactbot_comment_stripper():
    src = "const r = /\\/\\//;\nconst x = 1; // real comment\n"
    out = strip_js_comments(src)
    check("regex literal with // is kept", "const r = /\\/\\//;" in out)
    check("real line comment still stripped", "real comment" not in out)
    src2 = "netRegex: /a[/]b/,\n"
    check("slash inside a regex class doesn't end it",
          "netRegex: /a[/]b/," in strip_js_comments(src2))
    src3 = "const halves = total / 2; // note\nconst q = a / b;\n"
    out3 = strip_js_comments(src3)
    check("division is not eaten as a regex", "total / 2;" in out3 and "a / b;" in out3)
    check("comment after division still stripped", "note" not in out3)
    src4 = "x = { /* it's a comment */ y: 'a/b' };\n"
    check("block comment + string with slash unchanged",
          "y: 'a/b'" in strip_js_comments(src4))


# ── C10: timeline comment stripper honors escaped quotes ─────────────────────
def test_r2_timeline_comment_escapes():
    check("# outside quotes stripped", _strip_comment('12.3 "label" # c') == '12.3 "label" ')
    check("# inside quotes kept", _strip_comment('12.3 "a # b"') == '12.3 "a # b"')
    check("escaped quote doesn't flip string state",
          _strip_comment('12.3 "say \\"hi\\"" # c') == '12.3 "say \\"hi\\"" ')


# ── L1/N7: timeline parser honors single quotes and quoted } or ] ───────────
def test_r2_timeline_single_quotes_and_braces():
    check("# inside single-quoted value kept",
          _strip_comment("1.0 \"x\" Ability { id: 'a#b' }") == "1.0 \"x\" Ability { id: 'a#b' }")
    check("double quote inside single-quoted value keeps string state",
          _strip_comment("1.0 \"x\" Ability { id: 'a\"b' } # c") == "1.0 \"x\" Ability { id: 'a\"b' } ")
    e = parse("1.0 \"x\" Ability { id: 'a#b' }")[0]
    check("# inside single-quoted field survives parse", e.event_fields.get("id") == "a#b")
    e = parse('1.0 "x" StartsUsing { id: "AB}C", source: "Kefka" }')[0]
    check("} inside quoted value stays in the field block",
          e.event_fields == {"id": "AB}C", "source": "Kefka"})
    e = parse('1.0 "x" StartsUsing { id: ["66[01]F", "6620"], source: "Kefka" }')[0]
    check("] inside quoted array item keeps the id key",
          e.event_fields == {"id": "(?:66[01]F|6620)", "source": "Kefka"})


# ── timeline parser drops non-finite times, windows and jump targets ──────
def test_r2_timeline_nonfinite_dropped():
    # The time, window and jump regexes take plain digits, and float() of a
    # long enough digit string is inf. That used to reach the engine and the
    # plugin frame, where bare Infinity breaks the strict JSON parse.
    huge = "9" * 400
    check("400-digit time yields no entry", parse(f'{huge} "x"') == [])
    check("finite entries around an overflowing one survive",
          [e.time for e in parse(f'1.0 "a"\n{huge} "b"\n2.0 "c"')] == [1.0, 2.0])
    check("overflowing window drops the entry",
          parse(f'1.0 "x" window {huge}') == [])
    check("overflowing jump target drops the entry",
          parse(f'1.0 "x" jump {huge}') == [])
    # timeline_frame mirrors the guard, a non-finite t must never reach the wire
    import plugin_link
    check("timeline_frame drops non-finite times",
          plugin_link.timeline_frame([(float("inf"), "x"), (float("nan"), "y"),
                                      (1.0, "z")])
          == {"c": "timeline", "v": [[1.0, "z"]]})


# ── C7: BlackHoleChains.on_loss ignores stale Crust losses ───────────────────
def test_r2_chain_loss_staleness():
    roles = {"A1": "dps", "A2": "dps", "A3": "dps", "B1": "dps", "H1": "support"}

    def _seed(engine, t):
        """A minimal-but-valid instance: role queues only start once BOTH 644
        (Accretion) players are known, so seed the pair too."""
        acts = []
        order = list(ORDER_IDS)                          # BBC, BBD, BBE
        for i, actor in enumerate(("A1", "A2", "A3")):   # the non-Accretion DPS queue
            acts += engine.on_gain(CRUST, actor, t)
            acts += engine.on_gain(order[i], actor, t)
        for i, actor in enumerate(("B1", "H1")):         # the Accretion pair
            acts += engine.on_gain(CRUST, actor, t)
            acts += engine.on_gain(ACCRETION, actor, t)
            acts += engine.on_gain(order[i], actor, t)
        acts += engine.flush(t + 1)
        return acts

    t = 100.0
    eng = BlackHoleChains(role_of=lambda a: roles.get(a))
    check("chain queue started (sign placed)",
          any(a[0] == "mark" for a in _seed(eng, t)))
    stale_actions = eng.on_loss(CRUST, "A1", t + 1 + STALE_S + 5)
    check("stale Crust loss clears the held signs",
          stale_actions == [("clear", "A1"), ("clear", "B1")])
    check("stale Crust loss resets the instance", not eng._players)
    # A timely loss still walks the sign.
    eng2 = BlackHoleChains(role_of=lambda a: roles.get(a))
    _seed(eng2, t)
    walked = eng2.on_loss(CRUST, "A1", t + 2)
    check("timely Crust loss still walks the sign",
          any(a[0] == "mark" and a[1] == "A2" for a in walked))


# ── S4: locale catalog loader refuses non-supported codes outright ───────────
def test_r2_locale_catalog_guard():
    check("_load_catalog rejects a path-shaped locale",
          locale_util._load_catalog("../../etc/passwd") == {})
    check("_load_catalog rejects an unknown code", locale_util._load_catalog("xx") == {})
    check("_load_catalog still serves supported locales",
          isinstance(locale_util._load_catalog("ja"), dict))


# ── C1: TriggernometryBridge replays the disabled set on start ───────────────
def test_r2_triggernometry_disabled_replay():
    br = TriggernometryBridge()
    br.set_disabled({"guid#0", "guid#1"})           # sidecar down: command dropped...
    check("disabled set cached while sidecar is down",
          br._disabled == frozenset({"guid#0", "guid#1"}))
    # ...but start() must replay it. Simulate the tail of start(): active + queue up.
    br._wq = queue.Queue(maxsize=100)
    br._active = True
    br._send_command({"t": "set_disabled", "ids": sorted(br._disabled)})
    replayed = json.loads(br._wq.get_nowait())
    check("start-path replay carries the cached ids",
          replayed == {"t": "set_disabled", "ids": ["guid#0", "guid#1"]})
    br._active = False
    src_start = Path("triggernometry_bridge.py").read_text(encoding="utf-8")
    check("start() itself contains the replay",
          'self._send_command({"t": "set_disabled", "ids": sorted(self._disabled)})'
          in src_start.split("def start", 1)[1].split("def stop", 1)[0])


# ── M4: observed-phrase dedup resets with the session ────────────────────────
def test_r2_triggevent_seen_reset():
    tv = TriggeventBridge()
    tv._record_seen("Spread!")
    check("phrase recorded", tv.seen_phrases() == ["Spread!"])
    tv._active = True          # a "running" bridge with no proc: stop() must clean up
    tv.stop()
    check("stop() clears seen phrases", tv.seen_phrases() == [])


# ═════════════════════════════════════════════════════════════════════════════
# From test_review_fixes_3.py — main_window.py trigger-store hygiene, import
# backup, bad-name rotation, zone/fight tag guards, stale .part sweep,
# inventory parse logging, and source pins. Drives the real methods unbound on
# duck-typed windows (no QApplication).
# ═════════════════════════════════════════════════════════════════════════════

# ── isolate the trigger store in a temp dir (same pattern as the other suites)
R3_TMP = Path(tempfile.mkdtemp(prefix="nyaa_review3_"))
R3_SHIPPED = R3_TMP / "triggers.json"
R3_LOCAL = R3_TMP / "triggers.local.json"
R3_REPO = R3_TMP / "triggers.repo.json"
R3_REPO_RETIRED = R3_TMP / "retired.repo.json"
R3_REPO_VERSION = R3_TMP / "triggers.repo.version"
R3_RETIRED = R3_TMP / "retired.json"


def _isolate_store():
    """Re-point main_window's trigger-store globals at this suite's tmp dir.
    Under pytest, another suite's import-time checks may re-point them at its
    own tmp between collection and test execution, so every test re-asserts."""
    app_common.TRIGGERS_FILE = R3_SHIPPED
    app_common.TRIGGERS_LOCAL_FILE = R3_LOCAL
    app_common._REPO_TRIGGERS_FILE = R3_REPO
    app_common._REPO_RETIRED_FILE = R3_REPO_RETIRED
    app_common._REPO_TRIGGERS_VERSION = R3_REPO_VERSION
    app_common.RETIRED_FILE = R3_RETIRED


_isolate_store()


class _TrigWin:
    """The trigger-store half of MainWindow, minus Qt (see test_zone_redetect)."""
    _load_triggers = mw.MainWindow._load_triggers
    _load_retired_ids = mw.MainWindow._load_retired_ids
    _trigger_files_stamp = mw.MainWindow._trigger_files_stamp
    _save_triggers = mw.MainWindow._save_triggers

    def __init__(self):
        self._triggers = []
        self._triggers_mtime = ()
        self.refreshes = 0
        self.corrupt = 0
        self.warned = []

    def _refresh_table(self):
        self.refreshes += 1

    def _handle_local_corrupt(self):
        self.corrupt += 1

    def _warn_save_failed(self, what, exc):
        self.warned.append((what, exc))


def write_shipped(triggers):
    R3_SHIPPED.write_text(json.dumps([t.to_dict() for t in triggers]), encoding="utf-8")


def write_local(triggers=(), deleted=None, folders=()):
    """Write triggers.local.json with `deleted` verbatim (None -> JSON null)."""
    R3_LOCAL.write_text(json.dumps({
        "triggers": [t.to_dict() if isinstance(t, Trigger) else t for t in triggers],
        "deleted": deleted,
        "folders": list(folders),
    }), encoding="utf-8")


def _with_recorded_drops(fn):
    orig = app_common.log_drop
    drops = []
    app_common.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
    try:
        fn(drops)
    finally:
        app_common.log_drop = orig


# ── L-16: null / mixed-type "deleted" must not quarantine the file ──────────
def test_deleted_null_loads_as_empty_set():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1", zone_regex="Zone One")])
    write_local(deleted=None)                       # present-but-null
    w = _TrigWin()
    w._load_triggers()
    check("null deleted loads as empty set", w._deleted_ids == set())
    check("null deleted does not quarantine the file", w.corrupt == 0)


def test_deleted_mixed_types_keep_only_strings():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    write_local(deleted=[123, "abc", None, "def"])  # mixed junk
    w = _TrigWin()
    w._load_triggers()
    check("mixed deleted keeps only str ids", w._deleted_ids == {"abc", "def"})
    check("mixed deleted does not quarantine the file", w.corrupt == 0)


def test_deleted_string_container_is_not_iterated_into_chars():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    write_local(deleted="abc")                      # wrong container type
    w = _TrigWin()
    w._load_triggers()
    check("string deleted is not iterated into chars", w._deleted_ids == set())


def test_valid_tombstone_still_hides_its_trigger():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    write_local(deleted=["aaa"])
    w = _TrigWin()
    w._load_triggers()
    check("valid tombstone still hides the trigger", w._triggers == [])


# ── L-20: a poisoned in-memory tombstone set cannot kill saves ──────────────
def test_save_with_mixed_type_set_degrades_to_warning():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    write_local(deleted=[])
    w = _TrigWin()
    w._load_triggers()
    before = R3_LOCAL.read_bytes()
    w._deleted_ids = {123, "abc"}                   # sorted() TypeErrors on this
    w._save_triggers()                              # must not raise
    check("save with mixed-type set degrades to the warning",
          len(w.warned) == 1 and isinstance(w.warned[0][1], TypeError))
    check("failed save leaves the file untouched", R3_LOCAL.read_bytes() == before)
    w._deleted_ids = {"zzz", "aaa"}                 # sane set: save works
    w._save_triggers()
    check("clean save writes sorted tombstones",
          json.loads(R3_LOCAL.read_text(encoding="utf-8"))["deleted"] == ["aaa", "zzz"])
    check("clean save does not warn again", len(w.warned) == 1)


# ── L-13/L-18: _next_bad_name rotates and caps ──────────────────────────────
def test_next_bad_name_rotates_then_caps():
    d = Path(tempfile.mkdtemp(prefix="nyaa_bad_"))
    f = d / "nyaatriggers_settings.json"
    f.write_text("{}", encoding="utf-8")
    check("first backup is .bad", mw._next_bad_name(f) == d / (f.name + ".bad"))
    (d / (f.name + ".bad")).write_text("x", encoding="utf-8")
    check("second backup is .bad.1", mw._next_bad_name(f) == d / (f.name + ".bad.1"))
    # cap=3: names are .bad, .bad.1, .bad.2 - the last is reused at the cap.
    (d / (f.name + ".bad.1")).write_text("x", encoding="utf-8")
    check("cap picks the last numbered name",
          mw._next_bad_name(f, cap=3) == d / (f.name + ".bad.2"))
    (d / (f.name + ".bad.2")).write_text("x", encoding="utf-8")
    check("at the cap the last name is reused, not .bad.3",
          mw._next_bad_name(f, cap=3) == d / (f.name + ".bad.2"))


# ── H-3: _fight_tag_for_zone goes through the ReDoS guards ──────────────────
class _ZoneWin:
    _fight_tag_for_zone = mw.MainWindow._fight_tag_for_zone

    def __init__(self, triggers):
        self._triggers = triggers


def test_fight_tag_skips_catastrophic_pattern():
    z = _ZoneWin([Trigger(fight="BadFight", zone_regex="(.*)*x"),
                  Trigger(fight="GoodFight", zone_regex="Zone One")])
    check("catastrophic pattern is skipped, next fight still resolves",
          z._fight_tag_for_zone("Zone One") == ("GoodFight", "Zone One"))


def test_fight_tag_catastrophic_pattern_returns_promptly():
    z = _ZoneWin([Trigger(fight="BadFight", zone_regex="(.*)*x")])
    out = {}
    th = threading.Thread(
        target=lambda: out.setdefault("r", z._fight_tag_for_zone("a" * 30)),
        daemon=True)
    th.start()
    th.join(5.0)
    check("catastrophic pattern returns promptly (no GUI freeze)", "r" in out)
    check("unmatched zone falls back to the escaped name",
          out.get("r") == ("", "a" * 30))


def test_fight_tag_uncompilable_pattern_skipped_like_old_re_error_path():
    z = _ZoneWin([Trigger(fight="BadRe", zone_regex="("),
                  Trigger(fight="GoodFight", zone_regex="Zone One")])
    check("uncompilable pattern is skipped like the old re.error path",
          z._fight_tag_for_zone("Zone One") == ("GoodFight", "Zone One"))


# ── L-17: corrupt repo override falls back to the bundled set ───────────────
def test_corrupt_override_falls_back_to_bundled():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    R3_REPO.write_text('{"corrupt', encoding="utf-8")
    R3_REPO_VERSION.write_text(json.dumps(mw._VERSION), encoding="utf-8")
    R3_LOCAL.unlink(missing_ok=True)

    def body(drops):
        w = _TrigWin()
        w._load_triggers()
        check("corrupt override falls back to bundled triggers.json",
              [t.id for t in w._triggers] == ["aaa"])
        check("the corrupt override is logged",
              any(site == "triggers" and "triggers.repo.json" in detail
                  for site, detail in drops))
    _with_recorded_drops(body)


def test_healthy_override_still_preferred():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    R3_REPO.write_text(
        json.dumps([Trigger(id="bbb", fight="F2").to_dict()]), encoding="utf-8")
    R3_REPO_VERSION.write_text(json.dumps(mw._VERSION), encoding="utf-8")
    R3_LOCAL.unlink(missing_ok=True)

    def body(drops):
        w = _TrigWin()
        w._load_triggers()
        check("healthy override still preferred over bundled",
              [t.id for t in w._triggers] == ["bbb"] and drops == [])
    _with_recorded_drops(body)


def test_both_corrupt_loads_empty_but_logs_both():
    _isolate_store()
    R3_SHIPPED.write_text("{also corrupt", encoding="utf-8")
    R3_REPO.write_text("{corrupt", encoding="utf-8")
    R3_REPO_VERSION.write_text(json.dumps(mw._VERSION), encoding="utf-8")
    R3_LOCAL.unlink(missing_ok=True)

    def body(drops):
        w = _TrigWin()
        w._load_triggers()
        check("both corrupt loads an empty official set", w._triggers == [])
        check("both corrupt files are logged",
              sum(1 for site, _d in drops if site == "triggers") == 2)
    _with_recorded_drops(body)


def test_corrupt_bundled_without_override_logged_and_override_purged():
    _isolate_store()
    R3_SHIPPED.write_text("{also corrupt", encoding="utf-8")
    R3_REPO.write_text("{corrupt", encoding="utf-8")
    R3_REPO_VERSION.unlink(missing_ok=True)            # stamp gone -> purge
    R3_LOCAL.unlink(missing_ok=True)

    def body(drops):
        w = _TrigWin()
        w._load_triggers()
        check("corrupt bundled without override stays empty", w._triggers == [])
        check("corrupt bundled is logged and override file purged",
              len(drops) == 1 and not R3_REPO.exists())
    _with_recorded_drops(body)


# ── M-6: import backs up the pre-import local file ───────────────────────────
class _FakeMessageBox:
    class StandardButton:
        Yes = 1
    infos = []

    @staticmethod
    def question(*a, **k):
        return _FakeMessageBox.StandardButton.Yes

    @staticmethod
    def information(_w, _title, text):
        _FakeMessageBox.infos.append(text)

    @staticmethod
    def critical(*a, **k):
        pass


def test_import_triggers_keeps_bak_of_previous_local_file():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    R3_REPO_VERSION.unlink(missing_ok=True)            # no override in play
    R3_REPO.unlink(missing_ok=True)
    R3_LOCAL.write_text(json.dumps(
        {"triggers": [], "deleted": ["old-id"], "folders": []}), encoding="utf-8")
    export = R3_TMP / "export.json"
    export.write_text(json.dumps(
        {"triggers": [], "deleted": [], "folders": []}), encoding="utf-8")
    orig_mb, orig_fd = app_common.QMessageBox, app_common.QFileDialog
    app_common.QMessageBox = _FakeMessageBox
    app_common.QFileDialog = type("FD", (), {
        "getOpenFileName": staticmethod(lambda *a, **k: (str(export), ""))})
    try:
        w = _TrigWin()
        w._import_triggers = mw.MainWindow._import_triggers.__get__(w)
        w._import_triggers()
        bak = R3_TMP / "triggers.local.json.bak"
        check("import leaves a .bak of the previous local file", bak.exists())
        check(".bak holds the pre-import content",
              json.loads(bak.read_text(encoding="utf-8"))["deleted"] == ["old-id"])
        check("local file replaced by the import",
              json.loads(R3_LOCAL.read_text(encoding="utf-8"))["deleted"] == [])
        check("success dialog mentions the backup",
              _FakeMessageBox.infos and ".bak" in _FakeMessageBox.infos[-1])
    finally:
        app_common.QMessageBox, app_common.QFileDialog = orig_mb, orig_fd


# ── L-9: stale .part sweep respects the age guard ────────────────────────────
def test_sweep_stale_update_parts_respects_age_guard():
    pd = Path(tempfile.mkdtemp(prefix="nyaa_parts_"))
    old_part = pd / "NyaaTriggers-linux.tar.gz.111.222.part"
    old_part.write_bytes(b"x" * 64)
    old = time.time() - 7200
    os.utime(old_part, (old, old))
    fresh_part = pd / "NyaaTriggers-windows.zip.333.444.part"
    fresh_part.write_bytes(b"y" * 64)
    unrelated = pd / "something-else.part"
    unrelated.write_bytes(b"z")
    mw._sweep_stale_update_parts(pd)
    check("aged .part leftover is swept", not old_part.exists())
    check("fresh .part (in-flight download) is untouched", fresh_part.exists())
    check("non-update .part is untouched", unrelated.exists())


# ── L-15: sidecar inventory parse failures log a drop ────────────────────────
def test_sidecar_inventory_parse_failures_log_drop():
    def body(drops):
        mw.MainWindow._on_triggernometry_inventory(object(), "{not json")
        mw.MainWindow._on_triggevent_inventory(object(), "{not json")
        mw.MainWindow._on_cactbot_triggers_enumerated(object(), "{not json")
        sites = [s for s, _d in drops]
        check("triggernometry inventory parse failure logged",
              "tn-inventory" in sites)
        check("triggevent inventory parse failure logged", "te-inventory" in sites)
        check("cactbot inventory parse failure logged", "cactbot-inventory" in sites)
    _with_recorded_drops(body)


# ── L-6/L-14: source pins (same style as the start()-replay pin in fixes_2) ──
def test_dialogs_delete_later_and_close_event_stops_timers():
    src = _program_sources()
    check("all six exec()'d dialogs are deleteLater()'d",
          src.count("dlg.deleteLater()") == 6)
    close_body = src.split("def closeEvent", 1)[1].split("def ", 1)[0]
    for timer in ("_umad_chain_flush_timer", "_umad_gaze_flush_timer",
                  "_ability_filter_timer"):
        check(f"closeEvent stops {timer}", f"self.{timer}.stop()" in close_body)


# ═════════════════════════════════════════════════════════════════════════════
# From test_audit_setup_fixes.py — piper-tts pinned in both installers,
# nyaatriggers.log owner-only (drop_log and main.py), install.py skips voice
# files already on disk, and both download loops enforce a hard byte ceiling.
# ═════════════════════════════════════════════════════════════════════════════

# ── L-7: drop_log writes nyaatriggers.log owner-only ─────────────────────────
def test_setup_drop_log_owner_only():
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "nyaatriggers.log"
        orig_file = drop_log._LOG_FILE
        drop_log._LOG_FILE = log
        try:
            drop_log._last.clear()
            drop_log._perms_tightened = False
            drop_log.log_drop("audit-fresh", "first line", throttle_s=0)
            check("fresh drop log is created 0600",
                  stat.S_IMODE(log.stat().st_mode) == 0o600)
            check("fresh drop log holds the line",
                  "[audit-fresh] first line" in log.read_text(encoding="utf-8"))

            # A pre-existing 0644 log (written by an older build) is tightened once.
            log.write_text("old\n", encoding="utf-8")
            os.chmod(log, 0o644)
            drop_log._perms_tightened = False
            drop_log.log_drop("audit-tighten", "second line", throttle_s=0)
            check("pre-existing 0644 drop log tightened to 0600",
                  stat.S_IMODE(log.stat().st_mode) == 0o600)
            check("tightened log kept its content and the new line",
                  log.read_text(encoding="utf-8").startswith("old\n")
                  and "[audit-tighten] second line" in log.read_text(encoding="utf-8"))
        finally:
            drop_log._LOG_FILE = orig_file
            drop_log._last.clear()
            drop_log._perms_tightened = False


# ── L-7: main._log_crash writes the same log owner-only ──────────────────────
def test_setup_main_crash_log_owner_only():
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "nyaatriggers.log"
        # _log_crash routes through drop_log.log_crash, so the file it writes
        # is drop_log's. Patch there.
        orig_file = drop_log._LOG_FILE
        drop_log._LOG_FILE = log
        try:
            main._log_crash(ValueError, ValueError("audit boom"), None)
            check("crash log is created 0600",
                  stat.S_IMODE(log.stat().st_mode) == 0o600)
            check("crash log holds the CRASH entry",
                  "CRASH" in log.read_text(encoding="utf-8")
                  and "audit boom" in log.read_text(encoding="utf-8"))
        finally:
            drop_log._LOG_FILE = orig_file
            drop_log._perms_tightened = False


# ── H-4: both installers pin piper-tts ───────────────────────────────────────
def test_setup_installers_pin_piper():
    install_src = (REPO_DIR / "install.py").read_text(encoding="utf-8")
    main_src = (REPO_DIR / "main.py").read_text(encoding="utf-8")

    check("install.py pins piper-tts", '"piper-tts==1.4.2"' in install_src)
    check("main.py pins piper-tts", '"piper-tts==1.4.2"' in main_src)
    check("install.py no longer uses unbounded urlretrieve",
          "urllib.request.urlretrieve" not in install_src)


class _FakeResp:
    """urlopen stand-in: one-shot payload, or an endless stream with repeat."""

    def __init__(self, data, repeat=False):
        self._data, self._repeat = data, repeat
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._repeat:
            return b"\0" * n
        data, self._data = self._data, b""
        return data


# ── L-11/L-23: install.py fetches only missing files, caps runaway streams ───
def test_setup_install_voice_download():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        saved = (install.VOICES_DIR, install.VOICE_FILE, install._MAX_DOWNLOAD_BYTES,
                 install.VOICE_ONNX_SHA256)
        install.VOICES_DIR = td
        install.VOICE_FILE = td / f"{install.VOICE_STEM}.onnx"
        install.VOICE_FILE.write_bytes(b"keep-me")
        # Pin the hash to the fixture content, so the pre-existing model passes
        # the integrity check the skip path runs before keeping it.
        install.VOICE_ONNX_SHA256 = install._sha256(install.VOICE_FILE)
        fetched = []
        orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda url, timeout=None: (fetched.append(url), _FakeResp(b"{}"))[1]
        try:
            install.download_voice()
            check("present .onnx is not re-downloaded when only the .json is missing",
                  install.VOICE_FILE.read_bytes() == b"keep-me"
                  and fetched == [f"{install.VOICE_BASE}/{install.VOICE_STEM}.onnx.json"])

            # A stream that never ends is cut at the ceiling and the partial removed.
            urllib.request.urlopen = lambda url, timeout=None: _FakeResp(b"", repeat=True)
            install._MAX_DOWNLOAD_BYTES = 1 << 16
            cfg = td / f"{install.VOICE_STEM}.onnx.json"
            cfg.unlink()
            try:
                install.download_voice()
                raised = None
            except OSError as e:
                raised = e
            check("endless stream trips the byte ceiling and unlinks the partial",
                  raised is not None and not cfg.exists()
                  and not list(td.glob("*.part")))

            # A truncated survivor at the final path fails the pinned hash and
            # is re-downloaded, never skipped on bare existence.
            install.VOICE_FILE.write_bytes(b"truncated")
            fetched.clear()
            urllib.request.urlopen = lambda url, timeout=None: (fetched.append(url), _FakeResp(b"{}"))[1]
            try:
                install.download_voice()
            except SystemExit:
                pass   # the stubbed content can never pass the pinned hash
            check("hash-failed pre-existing model is re-downloaded, not skipped",
                  fetched[:1] == [f"{install.VOICE_BASE}/{install.VOICE_STEM}.onnx"])
        finally:
            urllib.request.urlopen = orig_urlopen
            (install.VOICES_DIR, install.VOICE_FILE, install._MAX_DOWNLOAD_BYTES,
             install.VOICE_ONNX_SHA256) = saved


# ── L-23: download_kokoro_model honors the ceiling, cleans the .part ──────────
def test_setup_kokoro_download_ceiling():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.onnx"
        src.write_bytes(b"\0" * 4096)
        dest = td / "out.onnx"
        saved = (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_SHA256,
                 tts._KOKORO_MAX_BYTES)
        tts._MODEL_DIR = td
        tts._KOKORO_URLS = {dest: src.as_uri()}
        tts._KOKORO_SHA256 = {dest: hashlib.sha256(src.read_bytes()).hexdigest()}
        try:
            tts._KOKORO_MAX_BYTES = 1024
            ok = tts.download_kokoro_model()
            check("oversized download is refused", not ok)
            check("oversized download leaves no dest or .part",
                  not dest.exists() and not list(td.glob("*.part")))

            tts._KOKORO_MAX_BYTES = 1 << 30
            ok = tts.download_kokoro_model()
            check("in-limit download with a good hash succeeds",
                  ok and dest.read_bytes() == src.read_bytes())
        finally:
            (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_SHA256,
             tts._KOKORO_MAX_BYTES) = saved


# ═════════════════════════════════════════════════════════════════════════════
# From test_audit_low_fixes.py — _as_bool, compile_user_regex without the
# bounded engine, the dispatch budget, fflogs 401 retry, the bounded TTS
# queue, bounded sidecar stderr, _play_wav_file validation, and the WS
# inbound cap.
# ═════════════════════════════════════════════════════════════════════════════

# Keep DROP lines from the budget/sound tests out of the real nyaatriggers.log.
LOW_TMP = Path(tempfile.mkdtemp(prefix="nyaa_lowfixes_"))
drop_log._LOG_FILE = LOW_TMP / "nyaatriggers.log"


# ── L5: _as_bool ─────────────────────────────────────────────────────────────
def test_as_bool_truth_table():
    check("bool passthrough True", _as_bool(True, False) is True)
    check("bool passthrough False", _as_bool(False, True) is False)
    check("None -> default True", _as_bool(None, True) is True)
    check("None -> default False", _as_bool(None, False) is False)
    check("int 0 -> False", _as_bool(0, True) is False)
    check("float 0.0 -> False", _as_bool(0.0, True) is False)
    check("int 1 -> True", _as_bool(1, False) is True)
    check("int 2 -> True", _as_bool(2, False) is True)
    check('"false" -> False', _as_bool("false", True) is False)
    check('"FALSE" -> False', _as_bool("FALSE", True) is False)
    check('" False " -> False', _as_bool(" False ", True) is False)
    check('"0" -> False', _as_bool("0", True) is False)
    check('"no" -> False', _as_bool("no", True) is False)
    check('"" -> False', _as_bool("", True) is False)
    check('"true" -> True', _as_bool("true", False) is True)
    check('"1" -> True', _as_bool("1", False) is True)
    check('"yes" -> True', _as_bool("yes", False) is True)
    check("list -> default", _as_bool([1], True) is True)
    check("dict -> default", _as_bool({}, False) is False)


def test_from_dict_uses_as_bool():
    check('from_dict enabled "false"', Trigger.from_dict({"enabled": "false"}).enabled is False)
    check('from_dict enabled "true"', Trigger.from_dict({"enabled": "true"}).enabled is True)
    check("from_dict enabled missing", Trigger.from_dict({}).enabled is True)
    check("from_dict enabled 0", Trigger.from_dict({"enabled": 0}).enabled is False)
    check('from_dict interrupt "true"', Trigger.from_dict({"interrupt": "true"}).interrupt is True)
    check('from_dict interrupt "0"', Trigger.from_dict({"interrupt": "0"}).interrupt is False)
    check("from_dict interrupt 1", Trigger.from_dict({"interrupt": 1}).interrupt is True)
    check("from_dict interrupt missing", Trigger.from_dict({}).interrupt is False)


# ── L0a: compile_user_regex refuses without the regex engine ─────────────────
def test_compile_user_regex_refuses_without_regex_module():
    orig = te._HAVE_REGEX
    try:
        te._HAVE_REGEX = False
        te.compile_user_regex.cache_clear()
        check("no regex engine: plain pattern refused",
              te.compile_user_regex("Limit Break") is None)
        check("no regex engine: invalid pattern still None",
              te.compile_user_regex("(") is None)
    finally:
        te._HAVE_REGEX = orig
        te.compile_user_regex.cache_clear()
    check("regex engine restored: plain pattern compiles",
          te.compile_user_regex("Limit Break") is not None)


# ── L0b: per-line dispatch budget ────────────────────────────────────────────
class _Stub:
    """Duck-typed window for MainWindow._dispatch_log_line (unbound), the same
    approach as the trigger-store window above."""

    def __init__(self, triggers):
        self._dps_meter = type("M", (), {"process": lambda s, f, r: None})()
        self._pet_ids = set()
        self._me_name = ""
        self._automark_rules = []
        self._settings = {}
        self._local_enabled = True
        self.timeline_calls = 0
        self._timeline = type("T", (), {"process_line": lambda s, f: setattr(self, "timeline_calls", self.timeline_calls + 1)})()
        self._seq_runners = []
        self._triggers = triggers
        self._zone_aliases = []
        self.fired = []
        self.appended = 0

    def _fire(self, t, m):
        self.fired.append(t.name)

    def _append_ability_line(self, fields):
        self.appended += 1


_FIELDS = "21|2025-01-01T00:00:00|40000001|Boss|9CFF|Some Ability|10000001|Me|".split("|")


def test_dispatch_budget_breaks_but_tail_runs():
    triggers = [Trigger(name=f"t{i}", log_type="21") for i in range(3)]
    w = _Stub(triggers)
    real_monotonic = time.monotonic
    vals = iter([0.0] + [100.0] * 8)   # deadline set at 0, every check past it
    mw.time.monotonic = lambda: next(vals, 100.0)
    try:
        mw.MainWindow._dispatch_log_line(w, _FIELDS, "|".join(_FIELDS))
    finally:
        mw.time.monotonic = real_monotonic
    check("over budget: no trigger fired", w.fired == [])
    check("over budget: line tail still ran", w.appended == 1)
    check("over budget: timeline still fed", w.timeline_calls == 1)


def test_dispatch_within_budget_matches_normally():
    t1 = Trigger(name="hit", log_type="21", cooldown_s=0.0)
    t2 = Trigger(name="miss", log_type="20")
    w = _Stub([t1, t2])
    mw.MainWindow._dispatch_log_line(w, _FIELDS, "|".join(_FIELDS))
    check("within budget: matching trigger fired", w.fired == ["hit"])
    check("within budget: line tail ran", w.appended == 1)


# ── L4: fflogs 401 token refresh ─────────────────────────────────────────────
class _ScriptedHTTP:
    """http_post seam: token endpoint always 200 (a fresh token per call), API
    endpoint 401 the first `failures` times, then 200."""

    def __init__(self, failures=1, fail_status=401):
        self.failures = failures
        self.fail_status = fail_status
        self.token_calls = 0
        self.api_calls = 0

    def __call__(self, url, headers, body, timeout):
        if "oauth/token" in url:
            self.token_calls += 1
            return 200, json.dumps(
                {"access_token": f"tok{self.token_calls}", "expires_in": 3600}).encode()
        self.api_calls += 1
        if self.api_calls <= self.failures:
            return self.fail_status, b'{"error":"boom"}'
        return 200, json.dumps({"data": {"ok": True}}).encode()


def test_graphql_401_clears_token_and_retries_once():
    fake = _ScriptedHTTP(failures=1)
    c = fflogs.FflogsClient("cid", "secret", http_post=fake)
    out = c._graphql("query { ok }")
    check("401 then 200: data returned", out == {"ok": True})
    check("401 then 200: two API attempts", fake.api_calls == 2)
    check("401 then 200: token refetched", fake.token_calls == 2)
    check("401 then 200: fresh token cached", c._token == "tok2")


def test_graphql_persistent_401_gives_up_after_one_retry():
    fake = _ScriptedHTTP(failures=99)
    c = fflogs.FflogsClient("cid", "secret", http_post=fake)
    out = c._graphql("query { ok }")
    check("persistent 401: None", out is None)
    check("persistent 401: exactly two API attempts", fake.api_calls == 2)
    check("persistent 401: token refetched once", fake.token_calls == 2)


def test_graphql_non_401_error_does_not_retry():
    fake = _ScriptedHTTP(failures=99, fail_status=500)
    c = fflogs.FflogsClient("cid", "secret", http_post=fake)
    out = c._graphql("query { ok }")
    check("500: None", out is None)
    check("500: single API attempt", fake.api_calls == 1)
    check("500: token kept", fake.token_calls == 1 and c._token == "tok1")


def test_graphql_401_via_real_transport():
    """Same retry through the real _urllib_post: urlopen raises HTTPError on
    the first API call (urllib raises on non-2xx, it doesn't return a status)."""
    calls = {"token": 0, "api": 0}

    class FakeResp:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self, n=-1):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        if "oauth/token" in req.full_url:
            calls["token"] += 1
            return FakeResp(200, json.dumps(
                {"access_token": f"tok{calls['token']}", "expires_in": 3600}).encode())
        calls["api"] += 1
        if calls["api"] == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        return FakeResp(200, b'{"data":{"ok":true}}')

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        c = fflogs.FflogsClient("cid", "secret")
        out = c._graphql("query { ok }")
    finally:
        urllib.request.urlopen = real_urlopen
    check("urllib 401: data returned after retry", out == {"ok": True})
    check("urllib 401: two API attempts", calls["api"] == 2)
    check("urllib 401: token refetched", calls["token"] == 2 and c._token == "tok2")


# ── L11: bounded TTS queue, drop-oldest ──────────────────────────────────────
def test_tts_enqueue_drops_oldest():
    while True:                      # start from an empty queue
        try:
            tts._queue.get_nowait()
        except queue.Empty:
            break
    try:
        for i in range(tts._queue.maxsize):
            tts._enqueue(("mark", i))
        check("queue filled to maxsize", tts._queue.qsize() == tts._queue.maxsize)
        tts._enqueue(("mark", "new"))
        items = []
        while True:
            try:
                items.append(tts._queue.get_nowait())
            except queue.Empty:
                break
        check("full enqueue keeps maxsize", len(items) == tts._queue.maxsize)
        check("oldest item dropped", ("mark", 0) not in items)
        check("second-oldest now front", items[0] == ("mark", 1))
        check("new item kept at tail", items[-1] == ("mark", "new"))
    finally:
        while True:                  # leave no callouts queued for other tests
            try:
                tts._queue.get_nowait()
            except queue.Empty:
                break


# ── N1: sidecar stderr goes through the bounded reader ───────────────────────
def test_read_lines_bounded_skips_giant_line():
    from triggernometry_bridge import _read_lines_bounded
    big = "x" * (2 << 20)
    lines = list(_read_lines_bounded(io.StringIO(big + "\nok\n")))
    check("oversized line skipped, next line kept", lines == ["ok\n"])


def test_err_loops_use_bounded_reader():
    for fname in ("triggernometry_bridge.py", "triggevent_bridge.py"):
        src = Path(fname).read_text(encoding="utf-8")
        check(f"{fname} stderr bounded", "_read_lines_bounded(proc.stderr)" in src)


# ── N2: _play_wav_file validation and bounded copy ───────────────────────────
def _patched_play():
    """Swap tts._play_wav for a recorder (no aplay in tests)."""
    played = []
    orig = tts._play_wav
    tts._play_wav = lambda p, gen=None: played.append(p)
    return played, orig


def test_play_wav_file_refuses_fifo():
    if not hasattr(os, "mkfifo"):
        return                         # Windows: no FIFOs, nothing to refuse
    fifo = LOW_TMP / "snd.fifo"
    os.mkfifo(fifo)
    played, orig = _patched_play()
    try:
        # Without the isfile guard, copy2 on a FIFO blocks forever: run on a
        # thread so a regression shows as a hang, not a stuck suite.
        t = threading.Thread(target=tts._play_wav_file, args=(str(fifo), 0.5),
                             daemon=True)
        t.start()
        t.join(5)
    finally:
        tts._play_wav = orig
    check("FIFO refused without hanging", not t.is_alive())
    check("FIFO never played", played == [])


def test_play_wav_file_refuses_oversized():
    big = LOW_TMP / "big.wav"
    big.write_bytes(b"x" * 300)
    orig_cap = tts._MAX_SOUND_BYTES
    tts._MAX_SOUND_BYTES = 100
    played, orig = _patched_play()
    try:
        tts._play_wav_file(str(big), 0.5)
    finally:
        tts._MAX_SOUND_BYTES = orig_cap
        tts._play_wav = orig
    check("oversized file never played", played == [])


def test_play_wav_file_aborts_midcopy_growth():
    """Pre-copy size check passes (getsize lies small), the running total in
    the chunked copy catches the file going over the cap mid-copy."""
    target = LOW_TMP / "grow.wav"
    target.write_bytes(b"x" * 300)
    real_getsize = os.path.getsize
    orig_cap = tts._MAX_SOUND_BYTES
    played, orig = _patched_play()
    os.path.getsize = lambda p: (10 if os.fspath(p) == os.fspath(target)
                                 else real_getsize(p))
    tts._MAX_SOUND_BYTES = 100
    try:
        tts._play_wav_file(str(target), 0.5)
    finally:
        os.path.getsize = real_getsize
        tts._MAX_SOUND_BYTES = orig_cap
        tts._play_wav = orig
    check("mid-copy growth aborted, never played", played == [])


def test_play_wav_file_valid_wav_still_plays():
    wav_path = LOW_TMP / "ok.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    played, orig = _patched_play()
    try:
        tts._play_wav_file(str(wav_path), 0.5)
    finally:
        tts._play_wav = orig
    check("valid wav played once", len(played) == 1)
    check("volume tmp wav cleaned up", played and not os.path.exists(played[0]))


def test_play_wav_file_refuses_empty_in_volume_branch():
    """A zero byte sound at a non native volume goes through the copy path.
    The empty refusal there must match the native volume branch instead of
    printing a native level fallback line and playing nothing."""
    empty = LOW_TMP / "empty.wav"
    empty.write_bytes(b"")
    played, orig = _patched_play()
    try:
        tts._play_wav_file(str(empty), 0.5)
    finally:
        tts._play_wav = orig
    check("empty sound refused in the volume branch, never played", played == [])


def test_apply_volume_bytes_unparseable_returns_input():
    """The bytes twin of _apply_volume must honor its contract of returning
    the input unchanged for what wave.open cannot parse, the same fallback
    the file twin takes, so the chime still plays at its native level."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x10\x00" * 100)
    good = buf.getvalue()
    float_wav = bytearray(good)
    float_wav[20] = 3   # fmt tag for IEEE float, wave.open refuses it
    check("float wav bytes come back unchanged",
          tts._apply_volume_bytes(bytes(float_wav), 0.5) == bytes(float_wav))
    check("truncated wav bytes come back unchanged",
          tts._apply_volume_bytes(good[:20], 0.5) == good[:20])
    check("empty bytes come back unchanged",
          tts._apply_volume_bytes(b"", 0.5) == b"")
    scaled = tts._apply_volume_bytes(good, 0.5)
    check("valid wav still scales", scaled != good and len(scaled) == len(good))


# ── L1: WS inbound message cap ───────────────────────────────────────────────
def test_ws_client_caps_incoming_message_size():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app
    from ws_client import WSClient, _MAX_WS_MESSAGE
    c = WSClient()
    check("ws cap constant is 4 MiB", _MAX_WS_MESSAGE == 4 << 20)
    check("ws socket cap applied",
          c._ws.maxAllowedIncomingMessageSize() == _MAX_WS_MESSAGE)


# ── N1: duplicate folder ids in triggers.local.json can't recurse forever ──
def test_folder_tree_duplicate_folder_ids_terminate():
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QTreeWidgetItem
    app = QApplication.instance() or QApplication([])
    _ = app

    class _FolderWin:
        _add_folder_node = mw.MainWindow._add_folder_node

    w = _FolderWin()
    w._folders = [
        {"id": "x", "name": "A", "parent_id": None},
        {"id": "c", "name": "C", "parent_id": "x"},
        {"id": "x", "name": "B", "parent_id": "x"},   # duplicate id naming itself
    ]
    root = QTreeWidgetItem()
    w._add_folder_node(w._folders[0], root)            # must not RecursionError
    check("duplicate folder id builds one top node", root.childCount() == 1)
    top = root.child(0)
    check("the first occurrence keeps its real child, the duplicate is skipped",
          top.childCount() == 1
          and top.child(0).data(0, Qt.ItemDataRole.UserRole) == "C")


def test_delete_folder_duplicate_folder_ids_terminate():
    orig_mb = app_common.QMessageBox
    app_common.QMessageBox = _FakeMessageBox
    try:
        class _FolderWin:
            _delete_folder = mw.MainWindow._delete_folder

        w = _FolderWin()
        w._folders = [
            {"id": "x", "name": "A", "parent_id": None},
            {"id": "c", "name": "C", "parent_id": "x"},
            {"id": "x", "name": "B", "parent_id": "x"},
        ]
        w._triggers = []
        w._local_ids = set()
        w._official_ids = set()
        w._save_triggers = lambda: None
        w._refresh_tree = lambda: None
        w._refresh_table = lambda: None
        w._delete_folder("x")                          # must not RecursionError
        check("delete with a duplicate id removes every folder sharing it",
              w._folders == [])
    finally:
        app_common.QMessageBox = orig_mb


# ── N22: the corrupt-settings warning is deferred until after set_locale ────
def test_corrupt_settings_defers_the_warning_dialog():
    d = Path(tempfile.mkdtemp(prefix="nyaa_cfg_"))
    sf = d / "nyaatriggers_settings.json"
    sf.write_text('{"ui_language": "ja", "trun', encoding="utf-8")   # torn write
    orig_file, orig_mb = app_common._SETTINGS_FILE, app_common.QMessageBox
    warnings = []

    class _RecMB:
        @staticmethod
        def warning(*a, **k):
            warnings.append(a)

    class _CfgWin:
        _load_settings = mw.MainWindow._load_settings

    app_common._SETTINGS_FILE = sf
    app_common.QMessageBox = _RecMB
    try:
        w = _CfgWin()
        w._load_settings()
        check("corrupt settings load shows no dialog mid-load", warnings == [])
        check("the warning is recorded for after set_locale",
              isinstance(w._settings_load_warning, tuple)
              and bool(w._settings_load_warning[0]))
        check("the corrupt file is still backed up",
              (d / (sf.name + ".bad")).exists())
        check("defaults are in use", w._settings == {})
    finally:
        app_common._SETTINGS_FILE, app_common.QMessageBox = orig_file, orig_mb


# ═════════════════════════════════════════════════════════════════════════════
# 2026-08-15 audit pass: duplicate timeline sync lines re-speaking callouts,
# Triggernometry unsorted-folder fight tags, the incombat state replay.
# ═════════════════════════════════════════════════════════════════════════════

# ── a duplicate sync line must not re-speak same-time companion callouts ──
def test_timeline_duplicate_sync_keeps_companions_fired():
    import time as _t
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app
    from timeline_engine import TimelineEngine

    eng = TimelineEngine()
    eng.load(parse(
        'hideall "--sync--"\n'
        '\n'
        '0.0 "--sync--" InCombat { inGameCombat: "1" } window 0,1\n'
        '50.0 "Stack" Ability { id: "1234" }\n'
        '50.0 "Spread"\n'
        '51.0 "Move"\n'
    ))
    spoken = []
    eng.tts.connect(spoken.append)
    eng.process_line(["260", "", "1", "1"])

    # ACT type 21: ability id at fields[4], non-player source at fields[2].
    line = ["21", "ts", "40001234", "Kefka", "1234", "Stack", "target1"]
    # The first target's line lands just before the entries, snapping forward.
    eng._t0 = _t.monotonic() - 49.95
    eng.process_line(line)
    eng._tick()          # clock crosses 50.0, the companion speaks
    check("first pass speaks both same-time entries", spoken == ["Stack", "Spread"])

    # The second target's line lands after the clock passed the entry. The
    # snap back to 50.0 must keep the companion's spoken state.
    eng._t0 = _t.monotonic() - 50.15
    eng.process_line(line)
    eng._tick()
    check("duplicate sync line does not re-speak the companion",
          spoken == ["Stack", "Spread"])

    eng._t0 = _t.monotonic() - 51.05
    eng._tick()
    check("the later entry still speaks once", spoken == ["Stack", "Spread", "Move"])


# ── unsorted sharing-channel folders default to Savage like the rest ──────
def test_convert_tn_unsorted_fight_tag_defaults_savage():
    from convert_triggernometry import path_to_fight
    check("unsorted bare phase folder defaults to Savage",
          path_to_fight("Sharing Channel/Unsorted/P4/some trigger") == "P4S")
    check("unsorted explicit Normal keeps its suffix",
          path_to_fight("Sharing Channel/Unsorted/M4N/some trigger") == "M4N")


# ── replay_state hands the cached incombat state to late sidecars ──────────
def test_ws_replay_state_includes_incombat():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app
    from ws_client import WSClient

    c = WSClient()
    got = []
    c.raw_message.connect(got.append)
    c._on_message(json.dumps({"type": "InCombat", "inACTCombat": True,
                              "inGameCombat": True}))
    c.replay_state()
    check("cached incombat state replays",
          any(json.loads(m).get("type", "").lower() == "incombat" for m in got))


# ═════════════════════════════════════════════════════════════════════════════
# 2026-08-15 audit pass, main_window fixes: teardown steps run isolated so
# one raise cannot orphan the sidecars, the shared banner only snoozes real
# update offers, a malformed roster entry no longer aborts the rest, a failed
# timeline load is not stamped as current, a manual engine update click
# during the startup run still gets its report, and a piped 26|30 expiry
# warning stays silent on the loss line.
# ═════════════════════════════════════════════════════════════════════════════

# ── MW8: one failed teardown step must not skip the rest ────────────────────
def test_teardown_step_isolates_failures():
    ran = []

    def boom():
        raise RuntimeError("stop exploded")

    w = _Stub([])
    mw.MainWindow._teardown_step(w, "first", lambda: ran.append(1))
    mw.MainWindow._teardown_step(w, "raiser", boom)   # prints, swallowed
    mw.MainWindow._teardown_step(w, "second", lambda: ran.append(2))
    check("a raising teardown step does not skip the rest", ran == [1, 2])

    src = _program_sources()
    for fn in ("_restart_for_update", "_quit_for_windows_handoff", "closeEvent"):
        body = src.split(f"def {fn}", 1)[1].split("\n    def ", 1)[0]
        check(f"{fn} routes its stops through _teardown_step",
              "self._teardown_step" in body)


# ── MW9: dismissing the cactbot banner must not snooze a pending update ─────
def test_cactbot_banner_dismiss_does_not_snooze_update():
    class _Banner:
        def __init__(self):
            self.visible = True

        def setVisible(self, v):
            self.visible = v

    for mode, expect in (("cactbot", False), ("update", True)):
        w = _Stub([])
        w._update_banner = _Banner()
        w._update_banner_mode = mode
        w.snoozed = 0
        w._snooze_offered_update = lambda w=w: setattr(w, "snoozed", w.snoozed + 1)
        mw.MainWindow._on_update_dismiss_clicked(w)
        check(f"dismiss hides the banner in {mode} mode",
              not w._update_banner.visible)
        check(f"dismiss in {mode} mode snoozes only a real update offer",
              (w.snoozed == 1) is expect)


# ── MW4: one malformed roster entry must not abort the rest ─────────────────
def test_party_jobs_one_bad_entry_does_not_abort_roster():
    w = _Stub([])
    w.jobs = []
    w.rearmed = 0
    w._note_actor_job = lambda a, j: w.jobs.append((a, j))
    w._rearm_umad_chain_flush = lambda: setattr(w, "rearmed", w.rearmed + 1)
    mw.MainWindow._on_ws_party_jobs(w, {"10": "33", "oops": "x", "12": "24"})
    check("good entries around a malformed one still land",
          w.jobs == [(10, 33), (12, 24)])
    check("chain flush still re-armed after the feed", w.rearmed == 1)


# ── MW10: a failed timeline load is not stamped as the current fight ────────
def test_timeline_failed_load_does_not_stamp_fight():
    src = _program_sources()
    body = src.split("def _load_timeline_for_zone", 1)[1].split("\n    def ", 1)[0]
    exc_tail = body.split("except Exception", 1)[1]
    stamp = exc_tail.index("self._timeline_fight = fight")
    check("except path clears fight before the stamp",
          'fight = ""' in exc_tail[:stamp])


# ── MW12: a manual engine update click during the startup run is reported ───
def test_te_manual_update_click_not_swallowed_by_in_flight_run():
    w = _Stub([])
    w._te_update_running = True
    mw.MainWindow._maybe_update_triggevent(w, manual=True)
    check("manual click during an in-flight run is remembered",
          getattr(w, "_te_update_pending_manual", False) is True)

    shown = []
    real_info = app_common.QMessageBox.information
    app_common.QMessageBox.information = lambda *a, **k: shown.append(a)
    try:
        w2 = _Stub([])
        w2._te_update_pending_manual = True
        mw.MainWindow._on_te_update_done(w2, False, "already current", False)
    finally:
        app_common.QMessageBox.information = real_info
    check("no-change result still reports to the swallowed manual click",
          len(shown) == 1)
    check("pending manual flag cleared after reporting",
          w2._te_update_pending_manual is False)


# ── TE3: a piped 26|30 expiry warning stays silent on the loss line ─────────
class _StatusStub(_Stub):
    def __init__(self, triggers):
        super().__init__(triggers)
        self.cancelled = []

    def _cancel_status_timers_for_loss(self, fields):
        self.cancelled.append(fields)

    def _umad_chain_line(self, fields):
        pass

    def _umad_gaze_line(self, fields):
        pass


def test_piped_expiry_warn_swallows_loss_line():
    t = Trigger(name="vuln", log_type="26|30", status_scope="any",
                cooldown_s=0.0, expiry_warn_s=5.0)
    w = _StatusStub([t])
    fields = ("30|2025-01-01T00:00:00|1F4|Vuln Up|30.0|40000001|Boss|"
              "40000002|Boss Add|00|").split("|")
    mw.MainWindow._dispatch_log_line(w, fields, "|".join(fields))
    check("loss line cancelled the armed warning", len(w.cancelled) == 1)
    check("loss line did not speak the expiry warning", w.fired == [])
    check("line tail still ran", w.appended == 1)


# ── MW13: a staged Triggernometry pack never clobbers a same-named one ──────
def test_tn_pack_staging_disambiguates_colliding_basenames():
    src = _program_sources()
    check("staged pack name gets a counter suffix on collision",
          'f"{src.stem}_{n}{src.suffix}"' in src)


# ── a stalled handshake is aborted and retried, never parked forever ───────
def test_ws_stalled_handshake_aborts_and_reopens():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app
    from PyQt6.QtNetwork import QAbstractSocket
    from ws_client import WSClient

    class _FakeSocket:
        def __init__(self, state):
            self.calls = []
            self._state = state

        def state(self):
            return self._state

        def abort(self):
            self.calls.append("abort")
            self._state = QAbstractSocket.SocketState.UnconnectedState

        def open(self, url):
            self.calls.append("open")

    c = WSClient()
    c._url = "ws://127.0.0.1:10505/"
    c._auto_reconnect = True   # the state connect_to leaves behind
    c._ws = _FakeSocket(QAbstractSocket.SocketState.ConnectingState)
    c._open()
    check("a parked handshake attempt is cut loose then re-opened",
          c._ws.calls == ["abort", "open"])
    check("the fresh attempt arms the watchdog timer",
          c._reconnect_timer.isActive())
    c._reconnect_timer.stop()

    c2 = WSClient()
    c2._url = "ws://127.0.0.1:10505/"
    c2._ws = _FakeSocket(QAbstractSocket.SocketState.ConnectedState)
    c2._open()
    check("a live connection is never churned", c2._ws.calls == [])


# ── sidecar stdin queues cap queued bytes, not just item count ─────────────
def test_bridge_stdin_queue_byte_budget():
    from triggevent_bridge import _ByteQueue as _TEVQueue
    from triggevent_bridge import _MAX_QUEUE_BYTES as _TEV_CAP
    from triggernometry_bridge import _ByteQueue as _TNQueue
    from triggernometry_bridge import _MAX_QUEUE_BYTES as _TN_CAP
    check("both bridges budget 64 MiB of queued stdin",
          _TEV_CAP == _TN_CAP == 64 << 20)
    for cls in (_TEVQueue, _TNQueue):
        q = cls(maxsize=100, maxbytes=10)
        q.put_nowait("abcd")
        try:
            q.put_nowait("1234567")
            overflow = False
        except queue.Full:
            overflow = True
        check(f"{cls.__module__} payload past the byte budget raises Full",
              overflow)
        got = q.get_nowait()
        check("bytes come off the budget on get",
              got == "abcd" and q._nbytes == 0)
        q.put_nowait("123456")
        check("a freed queue takes new payload again", q._nbytes == 6)
        small = cls(maxsize=1, maxbytes=10)
        small.put_nowait("x")
        try:
            small.put_nowait("y")
            count_full = False
        except queue.Full:
            count_full = True
        check("the item count cap still applies", count_full)
        sentinel = cls(maxsize=100, maxbytes=1)
        sentinel.put_nowait(object())
        check("a non-str sentinel always fits", sentinel.qsize() == 1)


# ── ws_client: dead combat_data signal removed, raw tee keeps CombatData ────
def test_ws_combatdata_still_teed_raw():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _ = app
    from ws_client import WSClient, _SUBSCRIBE

    check("CombatData stays in the subscribe list for the sidecar tee",
          "CombatData" in _SUBSCRIBE)
    c = WSClient()
    got = []
    c.raw_message.connect(got.append)
    msg = json.dumps({"type": "CombatData", "Encounter": {}, "Combatant": {}})
    c._on_message(msg)
    check("a CombatData frame is still teed verbatim", got == [msg])
    check("the unused local combat_data signal is gone",
          not hasattr(c, "combat_data"))


# ═════════════════════════════════════════════════════════════════════════════
# tools/ catalog writes, low audit batch: tmp + rename so an interrupted run
# can't truncate a shipped file, and utf-8-sig reads so a BOM'd catalog
# doesn't read as empty.
# ═════════════════════════════════════════════════════════════════════════════

def _load_tool(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, REPO_DIR / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _boom_write(orig_write):
    def boom(self, *a, **kw):
        if self.name.endswith(".tmp"):
            raise OSError("disk full mid-write")
        return orig_write(self, *a, **kw)
    return boom


# ── extract_strings: a BOM'd catalog must not read as empty ─────────────────
def test_extract_strings_bom_catalog_not_emptied():
    xs = _load_tool("extract_strings")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "mod.py").write_text('_("Hello")\n', encoding="utf-8")
    (tmp / "lang").mkdir()
    cat = tmp / "lang" / "ja.json"
    cat.write_bytes(b"\xef\xbb\xbf" + json.dumps({"Hello": "こんにちは"}).encode("utf-8"))
    orig_repo = xs._REPO
    xs._REPO = tmp
    try:
        check("BOM'd catalog reports no drift under --check", xs.main(["--check"]) == 0)
        check("sync run succeeds", xs.main([]) == 0)
        merged = json.loads(cat.read_text(encoding="utf-8"))
        check("BOM'd catalog keeps its translations through a sync",
              merged.get("Hello") == "こんにちは")
        check("no tmp sibling left behind", not (tmp / "lang" / "ja.json.tmp").exists())
    finally:
        xs._REPO = orig_repo


# ── extract_strings: a failed write keeps the previous good catalog ─────────
def test_extract_strings_failed_write_keeps_previous_catalog():
    xs = _load_tool("extract_strings")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "mod.py").write_text('_("Hello")\n', encoding="utf-8")
    (tmp / "lang").mkdir()
    cat = tmp / "lang" / "ja.json"
    good = json.dumps({"Hello": "こんにちは"}, ensure_ascii=False) + "\n"
    cat.write_text(good, encoding="utf-8")
    orig_repo, orig_write = xs._REPO, Path.write_text
    xs._REPO = tmp
    Path.write_text = _boom_write(orig_write)
    try:
        try:
            xs.main([])
            raised = False
        except OSError:
            raised = True
        check("a mid-write failure propagates", raised)
        check("the previous good catalog survives a failed write",
              cat.read_text(encoding="utf-8") == good)
    finally:
        xs._REPO = orig_repo
        Path.write_text = orig_write


# ── build_callouts_ja: output goes through tmp + rename ─────────────────────
def test_build_callouts_ja_write_is_atomic():
    bc = _load_tool("build_callouts_ja")
    tmp = Path(tempfile.mkdtemp())
    main_py = tmp / "main_window.py"
    main_py.write_text('_VERSION = "9.9.9"\n', encoding="utf-8")
    phrases = tmp / "phrases.json"
    phrases.write_text(json.dumps({"stack": "スタック"}), encoding="utf-8")
    triggers = tmp / "triggers.json"
    triggers.write_text(json.dumps([{"id": "t1", "tts_text": "stack", "name": "Stack"}]),
                        encoding="utf-8")
    out = tmp / "callouts_ja.json"
    # _NAMES stays missing on purpose, the empty map path.
    orig = {k: getattr(bc, k) for k in ("_TRIGGERS", "_PHRASES", "_NAMES", "_OUT", "_MAIN")}
    bc._TRIGGERS, bc._PHRASES, bc._NAMES = triggers, phrases, tmp / "names.json"
    bc._OUT, bc._MAIN = out, main_py
    try:
        check("build run succeeds", bc.main() == 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        check("callout landed in the output", data["callouts"].get("t1") == "スタック")
        check("no tmp sibling left behind",
              not (tmp / "callouts_ja.json.tmp").exists())

        good = out.read_text(encoding="utf-8")
        orig_write = Path.write_text
        Path.write_text = _boom_write(orig_write)
        try:
            try:
                bc.main()
                raised = False
            except OSError:
                raised = True
        finally:
            Path.write_text = orig_write
        check("a mid-write failure propagates", raised)
        check("the previous good output survives a failed write",
              out.read_text(encoding="utf-8") == good)
    finally:
        for k, v in orig.items():
            setattr(bc, k, v)


# ═════════════════════════════════════════════════════════════════════════════
# Triggevent engine update: the jar is gated on a stamp of the HEAD it was
# built from, not the git behind count alone. A failed or timed-out build
# leaves HEAD at origin/master with the old jar in place, and behind==0 used
# to report the engine current forever after.
# ═════════════════════════════════════════════════════════════════════════════

# ── update_engine: behind==0 with a missing or stale jar stamp rebuilds ──────
def test_te_update_stamp_gate():
    tev = triggevent_bridge

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    class _FakeBuild:
        """Stands in for the Maven build Popen. returncode is scripted."""
        def __init__(self, rc):
            self.returncode = rc
            self.pid = 424242
        def communicate(self, timeout=None):
            return ("", "fake build output")
        def wait(self, timeout=None):
            return self.returncode
        def kill(self):
            pass

    def run_once(behind, head, build_rc, stamp):
        """Run update_engine against a temp clone layout with git and the
        build scripted. Returns (ok, msg, build_calls, stamp_text)."""
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        et = root / "event-trigger"
        (et / ".git").mkdir(parents=True)
        build_sh = root / "build.sh"
        build_sh.write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "target").mkdir()
        stamp_p = root / "target" / "triggevent-core.jar.built-from"
        if stamp is not None:
            stamp_p.write_text(stamp, encoding="utf-8")
        builds = []

        def fake_run(argv, **kw):
            sub = argv[3:]
            if sub[0] == "fetch":
                return _R(0)
            if sub[0] == "rev-list":
                return _R(0, behind + "\n")
            if sub[0] == "rev-parse":
                return _R(0, head + "\n")
            if sub[0] in ("checkout", "merge"):
                return _R(0)
            raise AssertionError(f"unexpected git argv: {argv}")

        def fake_popen(cmd, **kw):
            builds.append(list(cmd))
            return _FakeBuild(build_rc)

        orig = (tev._ET_DIR, tev._BUILD_SCRIPT, tev._JAR_STAMP,
                tev.shutil.which, tev.subprocess.run, tev.subprocess.Popen)
        tev._ET_DIR, tev._BUILD_SCRIPT, tev._JAR_STAMP = et, build_sh, stamp_p
        tev.shutil.which = lambda t: "/usr/bin/" + t
        tev.subprocess.run = fake_run
        tev.subprocess.Popen = fake_popen
        try:
            ok, msg = tev.update_engine("stable", manual=True)
        finally:
            (tev._ET_DIR, tev._BUILD_SCRIPT, tev._JAR_STAMP,
             tev.shutil.which, tev.subprocess.run, tev.subprocess.Popen) = orig
        stamp_text = stamp_p.read_text(encoding="utf-8") if stamp_p.exists() else None
        td.cleanup()
        return ok, msg, builds, stamp_text

    # A build that fails after the merge leaves no stamp, and the next call
    # at behind==0 must rebuild instead of reporting the engine current.
    ok, msg, builds, stamp_text = run_once("3", "aaa111", 1, None)
    check("failed build reports the failure", not ok and "rebuild failed" in msg)
    check("failed build writes no stamp", stamp_text is None)
    ok, msg, builds, stamp_text = run_once("0", "aaa111", 0, None)
    check("behind==0 with no stamp rebuilds", len(builds) == 1)
    check("rebuild at behind==0 is not reported as up to date",
          ok and "already up to date" not in msg)
    check("a successful rebuild writes the HEAD stamp", stamp_text == "aaa111\n")

    # The normal nothing to do path. A matching stamp stays cheap, no build.
    ok, msg, builds, stamp_text = run_once("0", "aaa111", 0, "aaa111\n")
    check("matching stamp reports already up to date",
          not ok and "already up to date" in msg)
    check("matching stamp never starts a build", builds == [])

    # A stamp from an older HEAD is stale, the jar must be rebuilt.
    ok, msg, builds, stamp_text = run_once("0", "bbb222", 0, "aaa111\n")
    check("stale stamp rebuilds", len(builds) == 1)
    check("rebuild replaces the stale stamp", stamp_text == "bbb222\n")

    # The ordinary update path still fast forwards, builds and stamps.
    ok, msg, builds, stamp_text = run_once("5", "ccc333", 0, None)
    check("a real update builds and reports the new commits",
          ok and "5 new commit(s)" in msg)
    check("a real update stamps the new HEAD", stamp_text == "ccc333\n")


# ── a downloaded prebuilt jar invalidates the build stamp ────────────────────
def test_te_download_drops_build_stamp():
    tev = triggevent_bridge
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    jar = root / "triggevent-core.jar"
    jar.write_bytes(b"PK\x03\x04old-jar" + b"\0" * 200_000)
    stamp_p = root / "triggevent-core.jar.built-from"
    stamp_p.write_text("aaa111\n", encoding="utf-8")
    os.environ["NYAA_TRIGGEVENT_JAR"] = str(jar)
    body = b"PK" + b"\1" * 200_000
    rel = updater.Release(tag="v9.9", version="9.9", html_url="",
                          assets={"triggevent-core.jar": "https://x/core.jar"})
    orig = (tev._JAR_STAMP, updater.fetch_latest_release, updater.download,
            updater.verify_release_asset)
    tev._JAR_STAMP = stamp_p
    updater.fetch_latest_release = lambda timeout=8, channel="stable": rel
    updater.download = lambda url, dest, *a, **k: dest.write_bytes(body)
    updater.verify_release_asset = lambda release, name, archive, timeout=15: (True, "verified")
    try:
        changed, msg = tev._download_engine("stable")
        check("prebuilt jar swap succeeds", changed and jar.read_bytes() == body)
        check("prebuilt jar swap drops the build stamp", not stamp_p.exists())
    finally:
        (tev._JAR_STAMP, updater.fetch_latest_release, updater.download,
         updater.verify_release_asset) = orig
        os.environ.pop("NYAA_TRIGGEVENT_JAR", None)
        td.cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# 2026-08 audit low fixes, main_window.py: the import gate rejects a
# "triggers" value that is not a list of dicts instead of blanking the
# session under a success dialog.
# ═════════════════════════════════════════════════════════════════════════════
class _ImportMsgBox:
    """Records both dialogs so a test can tell rejection from success."""
    class StandardButton:
        Yes = 1
    infos = []
    criticals = []

    @staticmethod
    def question(*a, **k):
        return _ImportMsgBox.StandardButton.Yes

    @staticmethod
    def information(_w, _title, text):
        _ImportMsgBox.infos.append(text)

    @staticmethod
    def critical(_w, _title, text):
        _ImportMsgBox.criticals.append(text)


def _run_import(path):
    """Drive _import_triggers against `path` with the dialogs stubbed. Returns
    the window plus the recorded info and critical message lists."""
    orig_mb, orig_fd = app_common.QMessageBox, app_common.QFileDialog
    app_common.QMessageBox = _ImportMsgBox
    app_common.QFileDialog = type("FD", (), {
        "getOpenFileName": staticmethod(lambda *a, **k: (str(path), ""))})
    try:
        _ImportMsgBox.infos.clear()
        _ImportMsgBox.criticals.clear()
        w = _TrigWin()
        w._import_triggers = mw.MainWindow._import_triggers.__get__(w)
        w._import_triggers()
        return w, list(_ImportMsgBox.infos), list(_ImportMsgBox.criticals)
    finally:
        app_common.QMessageBox, app_common.QFileDialog = orig_mb, orig_fd


def test_import_triggers_rejects_bad_triggers_shapes():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    R3_REPO_VERSION.unlink(missing_ok=True)
    R3_REPO.unlink(missing_ok=True)
    write_local([Trigger(id="bbb", fight="F1")])
    before = R3_LOCAL.read_text(encoding="utf-8")
    bak = R3_TMP / "triggers.local.json.bak"
    bak.unlink(missing_ok=True)
    for bad in ({"triggers": 42}, {"triggers": "yes"}, {"triggers": ["oops"]}):
        export = R3_TMP / "export_bad.json"
        export.write_text(json.dumps(bad), encoding="utf-8")
        _w, infos, criticals = _run_import(export)
        check(f"import of triggers={bad['triggers']!r} shows the failure dialog",
              len(criticals) == 1)
        check("rejected import shows no success dialog", infos == [])
        check("rejected import leaves the local file untouched",
              R3_LOCAL.read_text(encoding="utf-8") == before)
        check("rejected import leaves no .bak behind", not bak.exists())


def test_import_triggers_still_accepts_a_real_export():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    R3_REPO_VERSION.unlink(missing_ok=True)
    R3_REPO.unlink(missing_ok=True)
    write_local([Trigger(id="bbb", fight="F1")])
    export = R3_TMP / "export_good.json"
    export.write_text(json.dumps({
        "triggers": [Trigger(id="ccc", fight="F2").to_dict()],
        "deleted": [], "folders": []}), encoding="utf-8")
    w, infos, criticals = _run_import(export)
    check("a real export still imports", not criticals and bool(infos))
    check("imported trigger merged into the live set",
          "ccc" in [t.id for t in w._triggers])


# ─────────────────────────────────────────────────────────────────────────────
# 2026-08-23 audit low batch, trigger_engine from_dict pipe and whitespace
# hygiene: a mixed pipe with a chat half drops the phantom ability id, the
# expiry warning survives only on all-status pipes, and a padded log type is
# stripped so it can match.
# ─────────────────────────────────────────────────────────────────────────────

# ── a mixed pipe keeps the id only when every part has an ID field ─────────
def test_mixed_pipe_drops_phantom_ability_id():
    check("00|21 drops the ability id",
          Trigger.from_dict({"log_type": "00|21", "ability_id": "A55B"}).ability_id == "")
    check("plain 00 still drops the ability id",
          Trigger.from_dict({"log_type": "00", "ability_id": "A55B"}).ability_id == "")
    check("21|22 keeps the ability id",
          Trigger.from_dict({"log_type": "21|22", "ability_id": "A55B"}).ability_id == "A55B")
    check("26|30 keeps the effect id",
          Trigger.from_dict({"log_type": "26|30", "ability_id": "1F4"}).ability_id == "1F4")

    # The phantom itself. With the id kept, the 00 half fell back to field 4
    # and matched chat text against the hex string.
    t = Trigger.from_dict({"log_type": "00|21", "ability_id": "A55B",
                           "ability_regex": "RareChatPhrase", "cooldown_s": 0})
    check("chat text at field 4 no longer matches the dropped id",
          t.matches("00|ts|pid|name|A55B|chat text".split("|")) is None)
    check("the regex still matches the chat text",
          t.matches("00|ts|pid|name|x|RareChatPhrase here".split("|")) is not None)

    t = Trigger.from_dict({"log_type": "21|22", "ability_id": "A55B",
                           "cooldown_s": 0})
    line = ["21", "ts", "40001234", "Boss", "a55b", "Some Ability",
            "10001111", "Player"]
    check("all-indexed pipe still matches the id at field 4",
          t.matches(line) is not None)


# ── a mixed pipe keeps the warn only when every part is a status type ──────
def test_mixed_pipe_drops_expiry_warn():
    check("26|21 drops the expiry warn",
          Trigger.from_dict({"log_type": "26|21", "expiry_warn_s": 5}).expiry_warn_s == 0.0)
    check("26|30 keeps the expiry warn",
          Trigger.from_dict({"log_type": "26|30", "expiry_warn_s": 5}).expiry_warn_s == 5.0)
    check("26 alone keeps the expiry warn",
          Trigger.from_dict({"log_type": "26", "expiry_warn_s": 5}).expiry_warn_s == 5.0)
    check("30 alone still drops the expiry warn",
          Trigger.from_dict({"log_type": "30", "expiry_warn_s": 5}).expiry_warn_s == 0.0)
    check("21 alone still drops the expiry warn",
          Trigger.from_dict({"log_type": "21", "expiry_warn_s": 5}).expiry_warn_s == 0.0)


def test_mixed_pipe_warn_trigger_fires_on_both_halves():
    # With the warn dropped at load, a 26|21 trigger is a normal trigger. It
    # speaks on both its lines instead of arming a warning on the gain and
    # swallowing the ability line.
    t = Trigger.from_dict({"name": "mixed", "log_type": "26|21",
                           "ability_id": "1F4", "status_scope": "any",
                           "cooldown_s": 0.0, "expiry_warn_s": 5.0})
    w = _StatusStub([t])
    gain = ("26|2025-01-01T00:00:00|1F4|Vuln Up|30.0|40000001|Boss|"
            "40000002|Boss Add|00|").split("|")
    mw.MainWindow._dispatch_log_line(w, gain, "|".join(gain))
    check("26|21 speaks on the gain line", w.fired == ["mixed"])
    cast = ("21|2025-01-01T00:00:00|40000001|Boss|1F4|Some Ability|"
            "10000001|Me|").split("|")
    mw.MainWindow._dispatch_log_line(w, cast, "|".join(cast))
    check("26|21 speaks on the ability line too",
          w.fired == ["mixed", "mixed"])
    check("line tails still ran", w.appended == 2)


# ── a padded log type is stripped at load ──────────────────────────────────
def test_from_dict_strips_log_type():
    check("padded log type is stripped",
          Trigger.from_dict({"log_type": " 21"}).log_type == "21")
    t = Trigger.from_dict({"log_type": " 21", "ability_id": "A55B",
                           "cooldown_s": 0})
    line = ["21", "ts", "40001234", "Boss", "a55b", "Some Ability",
            "10001111", "Player"]
    check("stripped log type now matches", t.matches(line) is not None)
    check("padded pipe still parses its parts",
          Trigger.from_dict({"log_type": " 21|22 ", "ability_id": "A55B"}).ability_id == "A55B")


# ─────────────────────────────────────────────────────────────────────────────
# 2026-08-24 hand edited settings shapes: the engine text override and callout
# edit dicts drop junk entries at load, and the folder list drops entries whose
# id or name is not a string.
# ─────────────────────────────────────────────────────────────────────────────

# ── engine text overrides keep only str keys and dicts of str fields ───────
def test_engine_text_overrides_drop_junk_entries():
    raw = {
        "cactbot:Foo": "oops",                                # not a dict
        "cactbot:Bar": {"find": "a", "replace": "b"},
        "cactbot:Baz": {"find": "a", "replace": 7},           # non-str field
        "cactbot:Qux": None,
    }
    check("junk override entries dropped at ingestion, good entry kept",
          mw._as_text_overrides(raw)
          == {"cactbot:Bar": {"find": "a", "replace": "b"}})
    check("non-dict overrides value loads as empty",
          mw._as_text_overrides("oops") == {})


# ── callout edit dicts keep only str to str entries ────────────────────────
def test_callout_edits_drop_junk_entries():
    raw = {"t1": "new text", "t2": 123, "t3": None,
           "t4": ["x"], "t5": {"find": "x"}}
    check("junk callout edits dropped at ingestion, str entries kept",
          mw._as_strdict(raw) == {"t1": "new text"})
    check("non-dict callout edits value loads as empty",
          mw._as_strdict([1, 2]) == {})


# ── the __init__ ingestion sites route through the filters ─────────────────
def test_settings_ingestion_routes_through_the_filters():
    src = _program_sources()
    check("engine text overrides ingested through the shape filter",
          '_as_text_overrides(self._settings.get("engine_text_overrides", {}))' in src)
    check("triggevent callout edits ingested through the str filter",
          '_as_strdict(self._settings.get("triggevent_callout_edits", {}))' in src)
    check("triggernometry callout edits ingested through the str filter",
          '_as_strdict(self._settings.get("triggernometry_callout_edits", {}))' in src)


# ── folder entries with non-str ids or names are dropped at load ───────────
def test_folders_with_junk_types_dropped_at_load():
    _isolate_store()
    write_shipped([Trigger(id="aaa", fight="F1")])
    write_local(folders=[
        {"id": "x", "name": "Good", "parent_id": None},
        {"id": 12, "name": "BadId", "parent_id": None},
        {"id": "y", "name": 42, "parent_id": None},
        {"id": "z", "parent_id": None},                         # missing name
        "not a dict",
    ])
    w = _TrigWin()
    w._load_triggers()                                          # must not raise
    check("folders with junk ids or names dropped at load",
          w._folders == [{"id": "x", "name": "Good", "parent_id": None}])
    check("junk folders do not quarantine the file", w.corrupt == 0)


# ─────────────────────────────────────────────────────────────────────────────
# 2026-08-24 pass 4 audit: a whitespace only log type takes the default at
# load instead of round tripping from dead to live on a later save, and the
# ability id drop on an unindexed type is drop logged so the broadened match
# leaves a trace.
# ─────────────────────────────────────────────────────────────────────────────

# ── a whitespace only log type defaults at load ─────────────────────────────
def test_whitespace_log_type_defaults_at_load():
    t = Trigger.from_dict({"log_type": " "})
    check("whitespace only log type takes the default at load",
          t.log_type == "20")
    check("the save round trip no longer flips dead to live",
          Trigger.from_dict(t.to_dict()).log_type == "20")


# ── the ability id drop on an unindexed type leaves a drop log line ────────
def test_unindexed_ability_id_drop_is_logged():
    drops = []
    real = te.log_drop
    te.log_drop = lambda site, detail, *a, **k: drops.append((site, detail))
    try:
        Trigger.from_dict({"name": "legacy", "log_type": "00|21",
                           "ability_id": "A55B"})
        check("the id drop on an unindexed type is drop logged",
              any(site == "trigger-load" and "00|21" in detail
                  for site, detail in drops))
        drops.clear()
        Trigger.from_dict({"name": "fine", "log_type": "21|22",
                           "ability_id": "A55B"})
        check("an all indexed pipe logs no drop", drops == [])
    finally:
        te.log_drop = real


# ─────────────────────────────────────────────────────────────────────────────
# 2026-08-24 audit 5: the GUI import dedup overlaps pipe joined fields per
# part, matching the CLI merge dedup. An imported 21|22 row collides with an
# existing plain 21, and a row pinning one id of an A55D|A55E pipe collides
# with the whole pipe.
# ─────────────────────────────────────────────────────────────────────────────

class _DedupWin:
    """Just enough window for _is_duplicate: the trigger list plus the two
    static helpers, no Qt and no real MainWindow."""
    _pipe_parts = staticmethod(mw.MainWindow._pipe_parts)
    _matcher_key = staticmethod(mw.MainWindow._matcher_key)
    _is_duplicate = mw.MainWindow._is_duplicate

    def __init__(self, triggers):
        self._triggers = triggers


def test_is_duplicate_pipe_overlap():
    def trig(**kw):
        return Trigger(cooldown_s=0, **kw)

    w = _DedupWin([trig(name="old", log_type="21", ability_id="A55D",
                        fight="DSR")])
    check("imported 21|22 collides with an existing plain 21",
          w._is_duplicate(trig(name="new", log_type="21|22",
                               ability_id="A55D", fight="DSR")) is not None)
    check("pipe id overlaps per part",
          w._is_duplicate(trig(name="new", log_type="21",
                               ability_id="a55d|A55E", fight="DSR")) is not None)
    check("the fight tag compares case blindly",
          w._is_duplicate(trig(name="new", log_type="21",
                               ability_id="A55D", fight="dsr")) is not None)

    w_pipe = _DedupWin([trig(name="old", log_type="21|22",
                             ability_id="A55D|A55E", fight="DSR")])
    check("a pinned single id collides with an existing pipe",
          w_pipe._is_duplicate(trig(name="new", log_type="22",
                                    ability_id="A55E", fight="DSR")) is not None)

    check("disjoint log types never collide",
          w._is_duplicate(trig(name="new", log_type="20",
                               ability_id="A55D", fight="DSR")) is None)
    check("a different fight never collides",
          w._is_duplicate(trig(name="new", log_type="21",
                               ability_id="A55D", fight="TOP")) is None)
    check("a different id on the same type does not collide",
          w._is_duplicate(trig(name="new", log_type="21",
                               ability_id="A55E", fight="DSR")) is None)
    check("a matcherless row is never a duplicate",
          w._is_duplicate(trig(name="new", log_type="21",
                               fight="DSR")) is None)
    check("an id row and a regex row never collide",
          w._is_duplicate(trig(name="new", log_type="21",
                               ability_regex="a55d", fight="DSR")) is None)
    w_rx = _DedupWin([trig(name="old", log_type="21",
                           ability_regex="Flare", fight="DSR")])
    check("same regex on the same type collides, case blind",
          w_rx._is_duplicate(trig(name="new", log_type="21|22",
                                  ability_regex="flare", fight="DSR")) is not None)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass            # check() already recorded the failed step
            except Exception as exc:
                print(f"FAIL  {_name}: {exc!r}")
                FAILS.append(_name)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("all passed")
