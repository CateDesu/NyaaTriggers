from contextlib import ExitStack
import io
import json
import queue
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

from nyaatriggers import diagnostics
from nyaatriggers import triggevent_bridge as tb


APP = QCoreApplication.instance() or QCoreApplication([])
PRIVATE = "Private Person /home/private/player https://private.example/token"


class BridgeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.log = root / "diagnostics.log"
        self.stack.enter_context(patch.object(diagnostics, "_LOG_FILE", self.log))
        self.stack.enter_context(patch.object(diagnostics, "_ENABLED", True))
        self.stack.enter_context(patch.object(tb, "_log"))
        self.stack.enter_context(patch.object(tb, "log_drop"))
        self.bridge = tb.TriggeventBridge()
        self.bridge._active = True
        self.bridge._gen = 7
        self.text = []
        self.speech = []
        self.bridge.callout.connect(lambda text, severity, gen: self.text.append((text, gen)))
        self.bridge.tts.connect(lambda text, gen: self.speech.append((text, gen)))

    def rows(self, event):
        if not self.log.exists():
            return []
        text = self.log.read_text()
        self.assertNotIn(PRIVATE, text)
        return [entry["data"] for line in text.splitlines()
                if (entry := json.loads(line))["event"] == event]

    def callout(self, **changes):
        return dict(t="callout", id=PRIVATE, text=PRIVATE, tts=PRIVATE, seq=42) | changes

    def test_reader_receipt_and_gui_delivery_share_sequence_and_generation(self):
        worker = threading.Thread(target=self.bridge._dispatch,
                                  args=(self.callout(), {}, 7))
        worker.start()
        worker.join()
        self.assertEqual(self.text, [])
        self.assertEqual([r["result"] for r in self.rows("engine_callout")], ["received"])
        APP.processEvents()
        rows = self.rows("engine_callout")
        self.assertEqual([r["result"] for r in rows], ["received", "emitted", "emitted"])
        self.assertEqual([(r["gen"], r["seq"]) for r in rows], [(7, 42)] * 3)
        self.assertEqual([r["channel"] for r in rows[1:]], ["text", "tts"])
        self.assertTrue(all(r["queue_delay_ms"] >= 0 for r in rows[1:]))
        self.assertEqual(self.text, [(PRIVATE, 7)])
        self.assertEqual(self.speech, [(PRIVATE, 7)])

    def test_rejections_distinguish_stale_disabled_and_replacement(self):
        self.bridge._dispatch(self.callout(seq=1), {}, 6)
        self.bridge._disabled = frozenset({PRIVATE})
        self.bridge._dispatch(self.callout(seq=2), {}, 7)
        self.bridge._disabled = frozenset()
        self.bridge.set_replacements([{"find": PRIVATE, "replace": ""}])
        self.bridge._dispatch(self.callout(seq=3), {}, 7)
        rows = self.rows("engine_callout")
        self.assertEqual([r["reason"] for r in rows if r["result"] == "filtered"],
                         ["stale", "disabled", "replacement_empty"])
        self.assertEqual(self.text + self.speech, [])

    def test_gui_rechecks_speech_after_text_changes_a_gate(self):
        self.bridge.callout.connect(lambda *_: setattr(self.bridge, "_disabled", frozenset({PRIVATE})))
        self.bridge._dispatch(self.callout(), {}, 7)
        rows = self.rows("engine_callout")
        self.assertEqual([(r.get("channel"), r["result"]) for r in rows],
                         [(None, "received"), ("text", "emitted"), ("tts", "filtered")])
        self.assertEqual(rows[-1]["reason"], "disabled")
        self.assertEqual(self.speech, [])

    def test_replacement_records_only_the_channel_it_suppressed(self):
        self.bridge.set_replacements([{"find": PRIVATE, "replace": ""}])
        self.bridge._dispatch(self.callout(tts="safe instruction"), {}, 7)
        rows = self.rows("engine_callout")
        self.assertEqual([(r.get("channel"), r["result"]) for r in rows],
                         [(None, "received"), ("text", "filtered"), ("tts", "emitted")])
        self.assertEqual(rows[1]["reason"], "replacement_empty")
        self.assertEqual(self.speech, [("safe instruction", 7)])

    def test_cancel_barriers_and_acknowledgements_are_visible(self):
        self.bridge._speech_cancel_gen = 7
        self.bridge._speech_cancel_all_gen = 7
        self.bridge.cancel_all_speech()
        token = self.bridge._speech_cancel_all_pending
        self.bridge._dispatch(self.callout(), {}, 7)
        self.assertEqual(self.speech, [])
        self.assertTrue(all(r["reason"] == "cancel_all_pending"
                            for r in self.rows("engine_callout") if r["result"] == "filtered"))
        self.bridge._acknowledge_speech_cancel(token, 7)
        self.bridge._dispatch(self.callout(seq=43), {}, 7)
        self.assertEqual(self.speech, [(PRIVATE, 7)])
        self.assertEqual([r["result"] for r in self.rows("engine_cancel")], ["queued", "acknowledged"])
        self.assertEqual(self.rows("engine_cancel")[-1]["pending_ids"], 0)

    def test_individual_and_overflow_barriers_have_distinct_reasons(self):
        self.bridge._speech_cancel_pending[PRIVATE] = 1
        self.bridge._dispatch(self.callout(tts_only=True), {}, 7)
        self.bridge._speech_cancel_pending.clear()
        self.bridge._speech_cancel_overflow = 7
        self.bridge._dispatch(self.callout(tts_only=True, seq=43), {}, 7)
        self.assertEqual([r["reason"] for r in self.rows("engine_callout") if r["result"] == "filtered"],
                         ["cancel_pending", "cancel_overflow_pending"])

    def test_sequence_gaps_keep_counts_without_payloads(self):
        state = {}
        for seq in (1, 4, 3):
            self.bridge._dispatch(self.callout(seq=seq), state, 7)
        rows = self.rows("engine_protocol")
        self.assertEqual([r["reason"] for r in rows], ["sequence_gap", "sequence_regression"])
        self.assertEqual(rows[0]["dropped"], 2)

    def test_feed_queue_summary_and_overflow_are_bounded(self):
        self.bridge._feed_report_at = 0
        with patch.object(tb.time, "monotonic", return_value=10):
            for _ in range(30):
                self.bridge.feed(json.dumps({"type": "LogLine", "line": [PRIVATE]}))
        self.assertEqual(len(self.rows("engine_queue")), 1)
        self.bridge._wq = queue.Queue(maxsize=1)
        self.bridge._wq.put(PRIVATE)
        for _ in range(10):
            self.bridge.feed(json.dumps({"type": "LogLine", "line": [PRIVATE]}))
        rows = self.rows("engine_queue")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["reason"], "queue_full")
        self.assertEqual(rows[-1]["queue_depth"], 1)

    def test_repeated_bad_feed_is_counted_without_logging_content(self):
        with patch.object(tb.time, "monotonic", return_value=10):
            for _ in range(30):
                self.bridge.feed(PRIVATE)
        with patch.object(tb.time, "monotonic", return_value=16):
            self.bridge.feed(PRIVATE)
        rows = self.rows("engine_protocol")
        self.assertEqual([r["count"] for r in rows], [1, 30])
        self.assertTrue(all(r["reason"] == "invalid_json" for r in rows))

    def test_writer_failures_report_type_and_queue_without_exception_text(self):
        class BrokenWriter:
            def write(self, _data):
                raise BrokenPipeError(PRIVATE)

            def close(self):
                pass

        proc = SimpleNamespace(stdin=BrokenWriter(), _nyaa_generation=7)
        q = queue.Queue()
        q.put(PRIVATE)
        q.put(tb._STOP)
        self.bridge._write_loop(proc, q)
        row = self.rows("engine_error")[-1]
        self.assertEqual((row["reason"], row["error_type"], row["gen"]),
                         ("write_failed", "BrokenPipeError", 7))
        self.assertEqual(row["queue_depth"], 1)

    def test_protocol_errors_and_exit_are_correlated(self):
        proc = SimpleNamespace(stdout=io.StringIO("{" + PRIVATE + "\n"), poll=lambda: 17)
        self.bridge._proc = proc
        with patch.object(self.bridge, "_reap"):
            self.bridge._read_loop(proc, queue.Queue(), {}, 7)
        self.assertEqual(self.rows("engine_protocol")[-1]["reason"], "invalid_json")
        row = self.rows("engine_exit")[-1]
        self.assertEqual((row["gen"], row["returncode"], row["expected"]), (7, 17, False))

    def test_recovery_records_counts_and_checkpoints_without_history_paths(self):
        self.bridge._recovery_gen = self.bridge._history_gen = self.bridge._catchup_gen = 7
        frames = [json.dumps({"type": "LogLine", "line": [PRIVATE]})]
        self.assertTrue(self.bridge.recover(frames, PRIVATE, history={"path": PRIVATE},
                                            state=frames, checkpoint=3))
        self.bridge._dispatch({"t": "recovered", "checkpoint": 3, "skipped": 2, "reason": PRIVATE}, {}, 7)
        rows = self.rows("engine_recovery")
        self.assertEqual([r["result"] for r in rows], ["queued", "received"])
        self.assertEqual((rows[0]["frames"], rows[0]["checkpoint"], rows[0]["history"]), (1, 3, True))
        self.assertEqual(rows[1]["dropped"], 2)

    def test_recovery_history_status_uses_only_known_outcomes(self):
        statuses = ("complete", "degraded", "unavailable", "failed", "state_only")
        for status in (*statuses, PRIVATE):
            self.bridge._dispatch({"t": "recovered", "checkpoint": 3,
                                   "status": status, "reason": PRIVATE}, {}, 7)
        rows = self.rows("engine_recovery")
        self.assertEqual([row.get("history_status") for row in rows], [*statuses, None])

    def test_structured_failures_replace_legacy_signal_and_sanitize_ui_details(self):
        failures = []
        self.bridge.chain_failure.connect(lambda text, gen: failures.append((text, gen)))
        self.bridge._dispatch({"t": "diagnostic", "event": "engine_runtime", "java_version": "17.0.20"}, {}, 7)
        self.bridge._handle_diagnostic("Error in sequential trigger '" + PRIVATE + "'", 7)
        self.bridge._dispatch({"t": "diagnostic", "event": "sequence_failed",
                               "trigger_class": PRIVATE, "trigger_field": PRIVATE,
                               "error_type": PRIVATE, "message": PRIVATE, "wait_kind": "event"}, {}, 7)
        self.assertEqual(len(failures), 1)
        self.assertNotIn(PRIVATE, failures[0][0])
        self.assertIn("Exception", failures[0][0])
        self.assertEqual(len(self.rows("sequence_failed")), 1)
        self.bridge._dispatch({"t": "diagnostic", "event": "sequence_failed"}, {}, 6)
        self.assertEqual(len(failures), 1)

    def test_automark_evidence_preserves_attempt_and_endpoint_result_without_payload(self):
        for stage, fields in (("requested", {}), ("failed", {"reason": "http_error", "http_status": 503}),
                              ("accepted", {"http_status": 200})):
            self.bridge._dispatch({"t": "diagnostic", "event": "engine_automark",
                                   "stage": stage, "seq": 12, "slot": 3, "marker": "BIND1",
                                   "target_actor": 2, "command": PRIVATE, "uri": PRIVATE,
                                   **fields}, {}, 7)
        rows = self.rows("engine_automark")
        self.assertEqual([row["stage"] for row in rows], ["requested", "failed", "accepted"])
        self.assertTrue(all((row["seq"], row["gen"], row["target_actor"]) == (12, 7, 2)
                            for row in rows))
        self.assertEqual((rows[1]["reason"], rows[1]["http_status"]), ("http_error", 503))
        self.assertTrue(all("command" not in row and "uri" not in row for row in rows))

    def test_native_marker_config_records_effective_transport_and_availability(self):
        for enabled, available, transport in ((False, True, "none"), (True, False, "none"),
                                               (True, True, "telesto")):
            self.bridge._dispatch({"t": "diagnostic", "event": "engine_automark_config",
                                   "enabled": enabled, "available": available,
                                   "transport": transport, "uri": PRIVATE}, {}, 7)
        self.assertEqual(self.rows("engine_automark_config"), [
            {"gen": 7, "enabled": False, "available": True, "transport": "none"},
            {"gen": 7, "enabled": True, "available": False, "transport": "none"},
            {"gen": 7, "enabled": True, "available": True, "transport": "telesto"},
        ])
    def test_startup_stderr_cannot_double_count_a_structured_failure(self):
        failures = []
        self.bridge.chain_failure.connect(lambda text, gen: failures.append((text, gen)))
        self.bridge._handle_diagnostic("Error in sequential trigger 'M1S.mouser'", 7)
        self.bridge._dispatch({"t": "diagnostic", "event": "engine_runtime", "java_version": "17.0.20"}, {}, 7)
        self.bridge._dispatch({"t": "diagnostic", "event": "sequence_failed", "wait_kind": "event"}, {}, 7)
        self.assertEqual(len(failures), 1)

    def test_legacy_startup_failures_flush_on_ready_with_bounded_details(self):
        failures = []
        self.bridge.chain_failure.connect(lambda text, gen: failures.append((text, gen)))
        for index in range(60):
            self.bridge._handle_diagnostic(f"Error in sequential trigger 'legacy{index}'", 7)
        self.assertEqual(failures, [])
        self.assertEqual(len(self.bridge._legacy_failures), 50)
        self.bridge._handle_diagnostic("reading WS messages on stdin", 7)
        self.assertEqual(len(failures), 60)
        self.assertIn("legacy59", failures[-1][0])
        self.bridge._handle_diagnostic("Error in sequential trigger 'after_ready'", 7)
        self.assertEqual(len(failures), 61)

    def test_legacy_startup_failure_survives_exit_before_ready(self):
        failures = []
        self.bridge.chain_failure.connect(lambda text, gen: failures.append((text, gen)))
        self.bridge._handle_diagnostic("Error in sequential trigger 'legacy'", 7)
        proc = SimpleNamespace(stdout=io.StringIO(""), poll=lambda: 1)
        self.bridge._proc = proc
        with patch.object(self.bridge, "_reap"):
            self.bridge._read_loop(proc, queue.Queue(), {}, 7)
        self.assertEqual(len(failures), 1)
        self.assertFalse(self.bridge.is_active())

    def test_stale_startup_failures_do_not_cross_generations(self):
        failures = []
        self.bridge.chain_failure.connect(lambda text, gen: failures.append((text, gen)))
        self.bridge._handle_diagnostic("Error in sequential trigger 'legacy'", 7)
        self.bridge._gen = 8
        self.bridge._handle_diagnostic("reading WS messages on stdin", 8)
        self.bridge._flush_legacy_failures(7)
        self.assertEqual(failures, [])

    def test_unknown_engine_diagnostics_are_rejected_without_copying_fields(self):
        self.bridge._dispatch({"t": "diagnostic", "event": PRIVATE, "message": PRIVATE}, {}, 7)
        self.assertEqual(self.rows("engine_protocol")[-1]["reason"], "unknown_type")

    def test_ready_and_stop_record_generation_and_capabilities(self):
        self.bridge._handle_diagnostic("reading WS messages on stdin; recovery=1; history=1; custom=1; diagnostics=1", 7)
        row = self.rows("engine_ready")[-1]
        self.assertEqual((row["gen"], row["recovery"], row["history"], row["custom"], row["catchup"]),
                         (7, True, True, True, False))
        self.bridge.stop()
        row = self.rows("engine_stop")[-1]
        self.assertEqual((row["gen"], row["previous_gen"], row["reason"]), (8, 7, "requested"))

    def test_protocol_metadata_excludes_inventory_and_status_content(self):
        for msg in (
            {"t": "status", "active": False, "message": PRIVATE},
            {"t": "inventory", "triggers": [{"id": PRIVATE, "text": PRIVATE}]},
            {"t": "combatants_request", "ids": [0x10000001, 0x10000002]},
            {"t": "custom_triggers", "ok": False, "message": PRIVATE},
            {"t": "telesto", "status": "bad", "uri": PRIVATE},
        ):
            self.bridge._dispatch(msg, {}, 7)
        rows = self.rows("engine_protocol")
        self.assertEqual([r["state"] for r in rows],
                         ["status", "inventory", "combatants_request", "custom_triggers", "telesto"])
        self.assertEqual(rows[1]["count"], 1)
        self.assertEqual(rows[2]["count"], 2)
        self.assertNotIn(str(0x10000001), self.log.read_text())

    def test_start_failure_records_availability_without_runtime_paths(self):
        self.bridge._active = False
        with patch.object(tb, "_find_java", return_value=PRIVATE), \
                patch.object(tb, "_find_jar", return_value=None):
            self.bridge.start()
        row = self.rows("engine_error")[-1]
        self.assertEqual((row["reason"], row["java_available"], row["jar_available"]),
                         ("unavailable", True, False))

    def test_started_generation_keeps_the_private_runtime_build_commit(self):
        jar = self.log.parent / "selected-engine.jar"
        jar.write_bytes(b"old engine")
        stamp = jar.with_name(jar.name + ".built-from")
        stamp.write_text(json.dumps({"engine": "a" * 40}))
        proc = SimpleNamespace(pid=42)

        def spawn(*_args, **_kwargs):
            jar.write_bytes(b"updated engine")
            stamp.write_text(json.dumps({"engine": "b" * 40}))
            return proc

        self.bridge._active = False
        try:
            with patch.object(tb, "_find_java", return_value="java"), \
                    patch.object(tb, "_find_jar", return_value=jar), \
                    patch.object(tb, "_bundled_jre_dir", return_value=None), \
                    patch.object(tb.shutil, "which", return_value=None), \
                    patch.object(tb.subprocess, "Popen", side_effect=spawn), \
                    patch.object(tb.threading, "Thread"):
                self.bridge.start()
            row = self.rows("engine_started")[-1]
            self.assertEqual((row["gen"], row["engine_commit"]), (8, "a" * 40))
            self.assertEqual((Path(proc._nyaa_runtime_dir.name) / jar.name).read_bytes(), b"old engine")
            self.assertEqual(tb._launch_build_commit(jar), "b" * 40)
            stamp.write_text(json.dumps({"engine": PRIVATE}))
            self.assertIsNone(tb._launch_build_commit(jar))
        finally:
            self.bridge._active = False
            self.bridge._proc = None
            if hasattr(proc, "_nyaa_runtime_dir"):
                proc._nyaa_runtime_dir.cleanup()

    def test_oversized_protocol_line_reports_size_and_keeps_following_line(self):
        with patch.object(tb, "_MAX_LINE", 8):
            self.assertEqual(list(tb._read_lines_bounded(io.StringIO(PRIVATE + "\nok\n"))), ["ok\n"])
        row = self.rows("engine_protocol")[-1]
        self.assertEqual((row["reason"], row["chars"]), ("oversize", len(PRIVATE) + 1))


if __name__ == "__main__":
    unittest.main()
