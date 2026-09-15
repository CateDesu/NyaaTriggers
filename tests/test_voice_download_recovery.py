"""Connection deadlines, cleanup and retry for comparison and voice downloads."""

import contextlib
import hashlib
import io
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nyaatriggers import fflogs, http_fetch, tts
from tests.test_transport_deadlines import HttpPeer, wait_for


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.enterContext(patch.object(fflogs, "log_drop"))

    def peer(self, tls=False, mode="headers"):
        peer = HttpPeer(mode, tls=tls)
        self.addCleanup(peer.close)
        if tls:
            context = ssl.create_default_context(cafile=peer.certificate)
            self.enterContext(patch.object(ssl, "_create_default_https_context", return_value=context))
        return peer

    def assert_reader_stopped(self):
        self.assertTrue(wait_for(lambda: not any(
            t.name == "http-response-reader" for t in threading.enumerate()), timeout=1))

    def check_fflogs_headers(self, tls=False):
        peer = self.peer(tls)
        with patch.object(fflogs, "_READ_STALL_S", .2), \
                patch.object(fflogs, "_RESPONSE_DEADLINE_S", .3):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                fflogs.FflogsClient._urllib_post(peer.uri, {}, b"{}", 2)
            self.assertLess(time.monotonic() - started, 1)
            self.assert_reader_stopped()
            status, body = fflogs.FflogsClient._urllib_post(peer.uri, {}, b"{}", 2)
        self.assertEqual((status, body), (200, b'{"response":[]}'))
        self.assertEqual(len(peer.requests), 2)

    def test_fflogs_header_timeout_closes_reader_and_allows_retry(self):
        self.check_fflogs_headers()

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_fflogs_https_header_timeout_keeps_certificate_verification(self):
        self.check_fflogs_headers(tls=True)

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_untrusted_https_certificate_is_rejected(self):
        import urllib.error
        peer = HttpPeer("headers", tls=True)
        self.addCleanup(peer.close)
        with self.assertRaises(urllib.error.URLError) as raised:
            fflogs.FflogsClient._urllib_post(peer.uri, {}, b"{}", 2)
        self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(peer.requests, [])
        self.assert_reader_stopped()

    def test_failed_lookup_releases_its_client_lock(self):
        peer = self.peer()
        client = fflogs.FflogsClient("id", "secret")
        client._token = "test-token"
        client._token_expiry = time.time() + 3600
        with patch.object(fflogs, "API_URL", peer.uri), \
                patch.object(fflogs, "_READ_STALL_S", .2), \
                patch.object(fflogs, "_RESPONSE_DEADLINE_S", .3), \
                patch.object(fflogs.FflogsClient, "_zones_cache", [{"id": 1, "name": "Duty"}]):
            self.assertIsNone(client.fetch_best("Player", "server", "NA", "Duty"))
        self.assertTrue(client._fetch_lock.acquire(blocking=False))
        client._fetch_lock.release()
        self.assert_reader_stopped()

    def test_redirect_body_cannot_hold_response_acquisition(self):
        peer = self.peer(mode="redirect")
        with patch.object(fflogs, "_READ_STALL_S", .2), \
                patch.object(fflogs, "_RESPONSE_DEADLINE_S", .3):
            with self.assertRaises(TimeoutError):
                fflogs.FflogsClient._urllib_post(peer.uri, {}, b"{}", 2)
        self.assert_reader_stopped()
        self.assertEqual(len(peer.requests), 1)

    def check_kokoro_headers(self, tls=False):
        peer = self.peer(tls)
        destination = self.folder / "kokoro-v1.0.onnx"
        body = b'{"response":[]}'
        pin = hashlib.sha256(body).hexdigest()
        with patch.object(tts, "_MODEL_DIR", self.folder), \
                patch.object(tts, "_KOKORO_URLS", {destination: peer.uri}), \
                patch.object(tts, "_KOKORO_SHA256", {destination: pin}), \
                patch.object(tts, "_KOKORO_DL_STALL_S", .2), \
                patch.object(tts, "_KOKORO_DL_DEADLINE_S", .3), \
                patch.object(tts, "_kokoro_failed", True), \
                patch.object(tts, "_kokoro_import_failed", True), \
                contextlib.redirect_stderr(io.StringIO()):
            started = time.monotonic()
            self.assertFalse(tts.download_kokoro_model())
            self.assertLess(time.monotonic() - started, 1)
            self.assert_reader_stopped()
            self.assertEqual(list(self.folder.iterdir()), [])
            self.assertTrue(tts._kokoro_failed)
            self.assertTrue(tts.download_kokoro_model())
            self.assertEqual(destination.read_bytes(), body)
            self.assertFalse(tts._kokoro_failed)
            self.assertFalse(tts._kokoro_import_failed)
            destination.unlink()
            with patch.object(tts, "_KOKORO_SHA256", {destination: "0" * 64}):
                self.assertFalse(tts.download_kokoro_model())
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_kokoro_header_timeout_cleans_up_and_allows_verified_retry(self):
        self.check_kokoro_headers()

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_kokoro_https_header_timeout_and_verified_retry(self):
        self.check_kokoro_headers(tls=True)

    def test_late_response_is_closed_and_cannot_create_a_model(self):
        release, closed = threading.Event(), threading.Event()
        response = SimpleNamespace(close=closed.set)

        def delayed_open(*args, **kwargs):
            release.wait(2)
            return response

        destination = self.folder / "kokoro-v1.0.onnx"
        with patch.object(tts, "_MODEL_DIR", self.folder), \
                patch.object(tts, "_KOKORO_URLS", {destination: "https://example.invalid/model"}), \
                patch.object(tts, "_KOKORO_DL_STALL_S", .1), \
                patch.object(http_fetch.urllib.request, "build_opener",
                             return_value=SimpleNamespace(open=delayed_open)), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                self.assertFalse(tts.download_kokoro_model())
                self.assertEqual(list(self.folder.iterdir()), [])
            finally:
                release.set()
                self.assertTrue(closed.wait(1))
        self.assert_reader_stopped()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_late_http_error_is_closed(self):
        import urllib.error
        release, closed = threading.Event(), threading.Event()
        error = urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {},
                                       SimpleNamespace(close=closed.set))

        def delayed_open(*args, **kwargs):
            release.wait(2)
            raise error

        with patch.object(http_fetch.urllib.request, "build_opener",
                          return_value=SimpleNamespace(open=delayed_open)):
            try:
                with self.assertRaises(TimeoutError), http_fetch.open_response(
                        "https://example.invalid", 2, time.monotonic() + .1):
                    self.fail("An expired response reached the caller")
            finally:
                release.set()
                self.assertTrue(closed.wait(1))
        self.assert_reader_stopped()

    def test_header_time_counts_towards_the_body_deadline(self):
        for client in ("fflogs", "kokoro"):
            with self.subTest(client=client):
                peer = self.peer()

                def reply(handler):
                    handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
                    try:
                        handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Test: ")
                        end = time.monotonic() + .65
                        while time.monotonic() < end:
                            handler.wfile.write(b"a")
                            handler.wfile.flush()
                            time.sleep(.02)
                        handler.wfile.write(b"\r\nContent-Length: 1000000\r\n\r\npartial")
                        handler.wfile.flush()
                        peer.release.wait(2)
                    except OSError:
                        pass

                peer.server.RequestHandlerClass.do_GET = reply
                peer.server.RequestHandlerClass.do_POST = reply
                with patch.object(fflogs, "_READ_STALL_S", 2), \
                        patch.object(fflogs, "_RESPONSE_DEADLINE_S", 1), \
                        patch.object(tts, "_MODEL_DIR", self.folder), \
                        patch.object(tts, "_KOKORO_URLS", {self.folder / "kokoro.onnx": peer.uri}), \
                        patch.object(tts, "_KOKORO_DL_STALL_S", 2), \
                        patch.object(tts, "_KOKORO_DL_DEADLINE_S", 1), \
                        contextlib.redirect_stderr(io.StringIO()):
                    started = time.monotonic()
                    if client == "fflogs":
                        with self.assertRaises(TimeoutError):
                            fflogs.FflogsClient._urllib_post(peer.uri, {}, b"{}", 2)
                    else:
                        self.assertFalse(tts.download_kokoro_model())
                    self.assertLess(time.monotonic() - started, 1.45)
                peer.release.set()
                self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
