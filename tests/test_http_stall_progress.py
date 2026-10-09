from contextlib import ExitStack
import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers import fflogs, http_fetch


class StallProgressTests(unittest.TestCase):
    def stalled_read(self, module, duration):
        clock = SimpleNamespace(value=100.0)
        monitoring, parked, release = (threading.Event() for _ in range(3))
        threads = []
        owner = self

        class Done:
            def set(self):
                pass

            def wait(self, timeout):
                wake_at = clock.value + timeout
                monitoring.set()
                owner.assertTrue(parked.wait(1))
                clock.value = max(clock.value, wake_at)
                return False

        class Response:
            first = True
            status = 200

            def read(self, size):
                if self.first:
                    self.first = False
                    owner.assertTrue(monitoring.wait(1))
                    clock.value = 100.1
                    return b"partial response"
                parked.set()
                release.wait(2)
                return b""

            read1 = read

            def __enter__(self):
                return self

            def __exit__(self, *args):
                release.set()

        def thread(*args, **kwargs):
            worker = threading.Thread(*args, **kwargs)
            threads.append(worker)
            return worker

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(module, "threading", SimpleNamespace(Event=Done, Thread=thread)))
                stack.enter_context(patch.object(module, "time", SimpleNamespace(monotonic=lambda: clock.value)))
                stack.enter_context(patch.object(module, "_unblock_reader", side_effect=lambda response: release.set()))
                if module is http_fetch:
                    with self.assertRaises(http_fetch.ReadTimeout) as error:
                        http_fetch.copy_response(Response(), io.BytesIO(), 100,
                                                 stall=.3, deadline=100 + duration)
                    stalled = error.exception.stalled
                else:
                    stack.enter_context(patch.object(fflogs, "open_response", return_value=Response()))
                    stack.enter_context(patch.object(fflogs, "_READ_STALL_S", .3))
                    stack.enter_context(patch.object(fflogs, "_RESPONSE_DEADLINE_S", duration))
                    with self.assertRaises(TimeoutError) as error:
                        fflogs.FflogsClient._urllib_post("http://127.0.0.1/", {}, b"{}", 10)
                    stalled = "stalled" in str(error.exception)
        finally:
            release.set()
            for worker in threads:
                worker.join(1)
                self.assertFalse(worker.is_alive())
        return clock.value, stalled

    def test_silence_is_measured_from_the_last_bytes(self):
        for module in (http_fetch, fflogs):
            with self.subTest(reader=module.__name__):
                finished_at, stalled = self.stalled_read(module, 2)
                self.assertAlmostEqual(finished_at, 100.4)
                self.assertTrue(stalled)

    def test_absolute_deadline_precedes_the_silence_limit(self):
        for module in (http_fetch, fflogs):
            with self.subTest(reader=module.__name__):
                finished_at, stalled = self.stalled_read(module, .2)
                self.assertAlmostEqual(finished_at, 100.2)
                self.assertFalse(stalled)

    def completed_read(self, module, received_at, observed_at, *, progress_at=None, duration=.2,
                       memory=False, eof_at=None, headers_at=None):
        clock = SimpleNamespace(value=100.0)

        class Done:
            def set(self):
                pass

            def is_set(self):
                return False

            def wait(self, timeout):
                clock.value = observed_at
                return True

        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                if headers_at is not None:
                    clock.value = headers_at
                return super().__enter__()

            def read1(self, size):
                chunk = self.read(size)
                clock.value = received_at if chunk or eof_at is None else eof_at
                return chunk

        def progress(*args):
            if progress_at is not None:
                clock.value = progress_at

        with ExitStack() as stack:
            stack.enter_context(patch.object(module, "threading", SimpleNamespace(Event=Done, Thread=InlineThread)))
            stack.enter_context(patch.object(module, "time", SimpleNamespace(monotonic=lambda: clock.value)))
            if module is http_fetch:
                if memory:
                    stack.enter_context(patch.object(http_fetch, "open_response", return_value=Response(b"complete")))
                    return len(http_fetch.fetch_bytes("http://127.0.0.1/", 100, stall=.3, deadline=duration))
                return http_fetch.copy_response(Response(b"complete"), io.BytesIO(), 100,
                                                stall=.3, deadline=100 + duration, progress_cb=progress)
            stack.enter_context(patch.object(fflogs, "open_response", return_value=Response(b"complete")))
            stack.enter_context(patch.object(fflogs, "_READ_STALL_S", .3))
            stack.enter_context(patch.object(fflogs, "_RESPONSE_DEADLINE_S", duration))
            _, body = fflogs.FflogsClient._urllib_post("http://127.0.0.1/", {}, b"{}", 10)
            return len(body)

    def test_late_worker_completion_cannot_bypass_the_deadline(self):
        for module in (http_fetch, fflogs):
            with self.subTest(reader=module.__name__), self.assertRaises(TimeoutError):
                self.completed_read(module, 100.5, 100.5)

    def test_timely_completion_remains_valid_when_observed_later(self):
        for module in (http_fetch, fflogs):
            with self.subTest(reader=module.__name__):
                self.assertEqual(self.completed_read(module, 100.1, 100.5), 8)

    def test_late_bytes_cannot_bypass_the_silence_limit(self):
        for module in (http_fetch, fflogs):
            with self.subTest(reader=module.__name__), self.assertRaisesRegex(TimeoutError, "stalled"):
                self.completed_read(module, 100.5, 100.5, duration=2)

    def test_final_progress_callback_must_complete_before_the_deadline(self):
        with self.assertRaises(http_fetch.ReadTimeout):
            self.completed_read(http_fetch, 100.1, 100.5, progress_at=100.5)

    def test_memory_reader_rejects_late_worker_completion(self):
        with self.assertRaises(http_fetch.ReadTimeout) as error:
            self.completed_read(http_fetch, 100.5, 100.5, memory=True)
        self.assertFalse(error.exception.stalled)

    def test_memory_reader_accepts_timely_completion_observed_later(self):
        self.assertEqual(self.completed_read(http_fetch, 100.1, 100.5, memory=True), 8)

    def test_memory_reader_rejects_late_bytes_after_silence(self):
        with self.assertRaises(http_fetch.ReadTimeout) as error:
            self.completed_read(http_fetch, 100.5, 100.5, duration=2, memory=True)
        self.assertTrue(error.exception.stalled)

    def test_memory_reader_rejects_late_eof_after_timely_bytes(self):
        for duration, stalled in ((.2, False), (2, True)):
            with self.subTest(duration=duration), self.assertRaises(http_fetch.ReadTimeout) as error:
                self.completed_read(http_fetch, 100.1, 100.5, duration=duration, memory=True, eof_at=100.5)
            self.assertEqual(error.exception.stalled, stalled)

    def test_memory_body_stall_starts_when_headers_arrive(self):
        self.assertEqual(self.completed_read(http_fetch, 100.4, 100.5, duration=1,
                                             memory=True, headers_at=100.25), 8)
        for duration, received_at, stalled in ((1, 100.6, True), (.35, 100.4, False)):
            with self.subTest(duration=duration), self.assertRaises(http_fetch.ReadTimeout) as error:
                self.completed_read(http_fetch, received_at, 101, duration=duration,
                                    memory=True, headers_at=100.25)
            self.assertEqual(error.exception.stalled, stalled)

    def completed_headers(self, received_at):
        clock = SimpleNamespace(value=100.0)
        response = io.BytesIO(b"headers arrived")
        self.addCleanup(lambda: self.assertTrue(response.closed))

        class Done:
            def set(self):
                pass

            def is_set(self):
                return False

            def wait(self, timeout):
                clock.value = 100.5
                return True

        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        def acquire(*args, **kwargs):
            clock.value = received_at
            return response

        with ExitStack() as stack:
            stack.enter_context(patch.object(http_fetch, "threading",
                                            SimpleNamespace(Event=Done, Thread=InlineThread, Lock=threading.Lock)))
            stack.enter_context(patch.object(http_fetch, "time", SimpleNamespace(monotonic=lambda: clock.value)))
            stack.enter_context(patch.object(http_fetch.urllib.request, "build_opener",
                                            return_value=SimpleNamespace(open=acquire)))
            with http_fetch.open_response("http://127.0.0.1/", 10, 100.2) as opened:
                return opened.read()

    def test_timely_headers_remain_valid_when_observed_later(self):
        self.assertEqual(self.completed_headers(100.1), b"headers arrived")

    def test_late_headers_are_rejected_and_closed(self):
        with self.assertRaisesRegex(TimeoutError, "headers timed out"):
            self.completed_headers(100.5)


if __name__ == "__main__":
    unittest.main()
