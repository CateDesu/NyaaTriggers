"""Replay complex callouts through the vendored Triggernometry engine."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET


CORE = Path(__file__).resolve().parents[1] / "triggernometry-core"
PACKS = CORE / "test" / "packs"


class HostReplay:
    def __init__(self, proc):
        self.proc = proc
        self.diagnostics = []
        self.diagnostic_lock = threading.Lock()
        self.frames = queue.Queue()
        self.calls = []
        self.write_lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            if line.startswith("{"):
                self.frames.put(json.loads(line))
            else:
                with self.diagnostic_lock:
                    self.diagnostics.append(line)
        self.frames.put(None)

    def errors(self):
        with self.diagnostic_lock:
            return "".join(self.diagnostics)

    def send(self, **frame):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()

    def log(self, kind, *fields):
        self.send(t="log", line="|".join(
            [kind, "2026-09-13T12:00:00.1230000-05:00", *fields, "test-checksum"]))

    def until(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.frames.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if frame is None:
                raise AssertionError("Host exited before the expected event")
            if frame.get("t") == "callout":
                self.calls.append(frame["tts"])
            if predicate(frame):
                return frame
        raise AssertionError(f"Host replay timed out. Callouts: {self.calls!r}")

    def call(self, timeout=20):
        return self.until(lambda f: f.get("t") == "callout", timeout)["tts"]


@contextmanager
def replay(pack, relay=None, extra_packs=()):
    with tempfile.TemporaryDirectory() as temp:
        environment = os.environ.copy()
        if relay:
            environment["NYAA_TRIGGERNOMETRY_TELESTO_RELAY"] = relay.url
            environment["NYAA_TRIGGERNOMETRY_CALLBACK_URI"] = relay.callback_url
        # Ubuntu's xvfb-run merges stderr into stdout. Collect both streams
        # and keep diagnostics alongside the JSON replies on every distro.
        proc = subprocess.Popen(
            ["xvfb-run", "-a", "mono", str(CORE / "bin" / "triggernometry-core.exe"),
             temp, "--serve", str(pack), *map(str, extra_packs)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, env=environment)
        host = HostReplay(proc)
        if relay:
            relay.callback = lambda body: host.send(t="endpoint", body=body)
        try:
            host.until(lambda f: f.get("t") == "inventory")
            yield host
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{host.errors()}") from exc
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            host.reader.join(timeout=2)
            proc.stdout.close()


class TriggernometryHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or not shutil.which("mono") or not shutil.which("xvfb-run"):
            message = "Requires Mono and Xvfb"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)

    def test_top_party_synergy_reads_telesto_callbacks(self):
        from nyaatriggers.triggernometry_telesto import TriggernometryTelesto
        from tests.test_triggernometry_telesto import FakeTelesto, post

        with FakeTelesto() as peer:
            errors = []
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                with replay(PACKS / "paissa" / "top-party-synergy.xml", relay) as host:
                    for changed, expected in ((True, "shield, legs"), (False, "sword, staff")):
                        host.log("20", "40001234", "Omega", "7B3F", "Party Synergy")
                        subscription = peer.next("Subscribe")["payload"]
                        self.assertEqual(subscription["start"], "${_addr[GameObject]}+6922")
                        self.assertEqual(subscription["length"], "1")
                        for gender, actor in (("M", "40001235"), ("F", "40001236")):
                            notification = {"version": 1, "id": 1,
                                            "notificationid": subscription["id"], "notificationtype": "memory",
                                            "payload": {"objectid": actor, "name": "Omega-" + gender,
                                                        "oldvalue": "", "newvalue": "0"}}
                            self.assertEqual(post(subscription["endpoint"], notification)[0], 200)
                            if changed:
                                notification["payload"].update(oldvalue="0", newvalue="4")
                                self.assertEqual(post(subscription["endpoint"], notification)[0], 200)
                        self.assertEqual(host.call(timeout=12), expected)
                        peer.next("Unsubscribe")
                    self.assertFalse(errors, errors)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.subscriptions)

    def test_top_drawings_reach_telesto_with_resolved_targets(self):
        from nyaatriggers.triggernometry_telesto import TriggernometryTelesto
        from tests.test_triggernometry_telesto import FakeTelesto

        with FakeTelesto() as peer:
            errors = []
            relay = TriggernometryTelesto(lambda _body: None, errors.append, peer.url)
            relay.start()
            try:
                with replay(PACKS / "paissa" / "top-pantokrator.xml", relay) as host:
                    host.send(t="combatants", me=0x10001234, list=[
                        {"id": 0x10001234, "name": "Spike Player", "party": 1, "job": 19}])
                    host.log("26", "D60", "Guided Missile", "6.1", "40001234", "Omega", "10001234", "Spike Player")
                    for _ in range(2):
                        circle = peer.next("EnableDoodle")["payload"]
                        self.assertEqual(circle["type"], "circle")
                        self.assertEqual(circle["radius"], "5")
                        self.assertEqual(circle["position"], {"coords": "entity", "name": "Spike Player"})
                    self.assertEqual(host.call(), "next missile")
                    host.log("26", "DB3", "Condensed Wave Cannon", "6.1", "40001234", "Omega", "10001234", "Spike Player")
                    beam = peer.next("EnableDoodle")["payload"]
                    self.assertEqual(beam["type"], "beam")
                    self.assertEqual(beam["from"]["name"], "Omega")
                    self.assertEqual(beam["at"]["name"], "Spike Player")
                    self.assertEqual(beam["expiresin"], "6000")
                    self.assertEqual(host.call(), "next beam")
                    self.assertFalse(errors, errors)
            finally:
                relay.close(wait=True)
            self.assertFalse(peer.drawings)

    def test_script_errors_reach_the_diagnostic_log(self):
        with replay(PACKS / "script-error.xml") as host:
            host.send(t="log", line="TN_SCRIPT_ERROR")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                errors = host.errors()
                if "expected replay failure" in errors:
                    break
                time.sleep(0.05)
            self.assertIn("[engine]", errors)
            self.assertIn("expected replay failure", errors)
            host.send(t="ping")
            host.until(lambda f: f.get("t") == "pong")

    def test_log_sources_receive_their_own_format_once(self):
        cases = [
            ("00", ["0038", "Spike Player", "日本語: hello"],
             "ChatLog 00:0038:Spike Player:日本語: hello"),
            ("20", ["40001234", "Zelenia", "A9B8", "Out", "E0000000", "", "4.0", "100", "100", "0", "0"],
             "StartsCasting 14:40001234:Zelenia:A9B8:Out:E0000000::4.0:100:100:0:0"),
            ("26", ["D80", "Looper", "15.00", "40001234", "Omega", "10001234", "Spike Player", "00", "50000", "100000"],
             "StatusAdd 1A:D80:Looper:15.00:40001234:Omega:10001234:Spike Player:00:50000:100000"),
            ("257", ["80000001", "01000040", "05", "00", "0000"],
             "257 101:80000001:01000040:05:00:0000"),
        ]
        with replay(PACKS / "log-sources.xml") as host:
            for kind, fields, formatted in cases:
                with self.subTest(kind=kind):
                    raw = "|".join([kind, "2026-09-13T12:00:00.1230000-05:00", *fields, "test-checksum"])
                    host.send(t="log", line=raw)
                    self.assertCountEqual([host.call(), host.call()], [
                        "network " + raw, "act [12:00:00.123] " + formatted])
            formatted = "[12:00:00.123] ChatLog 00:0038::already formatted"
            host.send(t="log", line=formatted)
            self.assertEqual(host.call(), "act " + formatted)
            self.assertEqual(len(host.calls), 9)

    def test_top_headmarkers_keep_offset_and_filter_other_players(self):
        with replay(PACKS / "paissa" / "top-headmarkers.xml") as host:
            host.send(t="combatants", me=0x10001234, list=[
                {"id": 0x10001234, "name": "Spike Player", "party": 1, "job": 19}])
            for offset, expected in ((0x30, "Circle"), (0x50, "Square")):
                host.log("33", "80000001", "40000003")
                time.sleep(0.2)
                host.log("27", "10005678", "Other Player", "0000", "0000", f"{0x17 + offset:04X}")
                time.sleep(0.2)
                host.log("27", "10005678", "Other Player", "0000", "0000", f"{0x1A1 + offset:04X}")
                time.sleep(0.2)
                marker = 0x1A0 if expected == "Circle" else 0x1A2
                host.log("27", "10001234", "Spike Player", "0000", "0000", f"{marker + offset:04X}")
                self.assertEqual(host.call(), expected)
            self.assertEqual(host.calls, ["Circle", "Square"])

    def test_zelenia_bloom_resolves_state_and_delayed_followups(self):
        self.check_zelenia_bloom(PACKS / "paissa" / "zelenia-bloom.xml")

    def test_zelenia_bloom_waits_for_delayed_state_actions(self):
        tree = ET.parse(PACKS / "paissa" / "zelenia-bloom.xml")
        for name in ("0. Init", "1. MapEffect"):
            for action in tree.findall(f'.//Trigger[@Name="{name}"]/Actions/Action'):
                action.set("ExecutionDelayExpression", "750")
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "delayed-bloom.xml"
            tree.write(pack, encoding="utf-8", xml_declaration=True)
            self.check_zelenia_bloom(pack)

    def wait_zelenia_state(self, host, expected, timeout=10):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            # Unique replies stay visible through the engine's TTS repeat filter.
            probe = time.monotonic_ns()
            prefix = f"NYAA_REPLAY_STATE:{probe}:"
            host.send(t="log", line=f"NYAA_REPLAY_PROBE {probe}")
            last = host.call(timeout=max(.01, deadline - time.monotonic()))
            self.assertTrue(last.startswith(prefix), last)
            if last == prefix + expected:
                return
            time.sleep(.02)
        self.fail(f"Zelenia state did not reach {expected!r}. Last reply: {last!r}")

    def check_zelenia_bloom(self, pack):
        with replay(pack, extra_packs=[PACKS / "zelenia-state-observer.xml"]) as host:
            # The marker proves the asynchronous cleanup finished as well
            # as the phase assignment before the map event arrives.
            host.send(t="log", line="NYAA_REPLAY_PREPARE")
            self.assertEqual(host.call(), "NYAA_REPLAY_PREPARED")
            host.log("20", "40001234", "Zelenia", "AA14", "Bloom", "E0000000", "", "4.0", "100", "100", "0", "0")
            self.wait_zelenia_state(host, "2::")
            host.log("257", "80000001", "01000040", "05", "00", "0000")
            self.wait_zelenia_state(host, "2::1")
            host.calls.clear()
            host.log("20", "40001234", "Zelenia", "A9B8", "Out", "E0000000", "", "4.0", "100", "100", "0", "0")
            started = time.monotonic()
            self.assertEqual(host.call(), "southwest，Out")
            self.assertEqual(host.call(), "diagonal move")
            self.assertGreater(time.monotonic() - started, 6)
            self.assertEqual(host.call(), "move, tether")
            self.assertEqual(len(host.calls), 3)

    def test_script_variables_reach_delayed_speech(self):
        with replay(CORE / "test" / "spike-script-pack.xml") as host:
            host.log("00", "0038", "Spike Player", "SPIKESCRIPT")
            self.assertEqual(host.call(), "script computed 42")

    def test_mixed_case_extensions_load_the_same_inventory(self):
        source = (CORE / "test" / "packs" / "spike-me-pack.xml").read_bytes()
        inventories = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for suffix in (".xml", ".XML", ".Xml"):
                with self.subTest(suffix=suffix):
                    pack = root / ("pack" + suffix)
                    pack.write_bytes(source)
                    proc = subprocess.Popen(
                        ["xvfb-run", "-a", "mono", str(CORE / "bin" / "triggernometry-core.exe"),
                         str(root / ("config" + suffix)), "--serve", str(pack)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, start_new_session=True)
                    try:
                        out, err = proc.communicate("", timeout=25)
                    finally:
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGTERM)
                            try:
                                proc.communicate(timeout=3)
                            except subprocess.TimeoutExpired:
                                os.killpg(proc.pid, signal.SIGKILL)
                                proc.communicate()
                    self.assertEqual(proc.returncode, 0, err)
                    frames = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
                    inventory = next(frame["triggers"] for frame in frames if frame.get("t") == "inventory")
                    self.assertTrue(inventory)
                    inventories.append(inventory)
        self.assertEqual(inventories[0], inventories[1])
        self.assertEqual(inventories[0], inventories[2])


if __name__ == "__main__":
    unittest.main()
