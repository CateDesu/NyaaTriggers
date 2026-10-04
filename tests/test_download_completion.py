from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import nullcontext
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

from nyaatriggers.http_fetch import fetch_bytes
from nyaatriggers import updater
from nyaatriggers.ui import timeline_tab
from tests.test_transport_deadlines import HttpPeer, wait_for


class DownloadCompletionTests(unittest.TestCase):
    def serve(self, body, length, *, chunked=False):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

        peer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        peer.daemon_threads = True
        worker = threading.Thread(target=peer.serve_forever,
                                  kwargs={"poll_interval": .02}, daemon=True)
        worker.start()

        def close():
            peer.shutdown()
            peer.server_close()
            worker.join(timeout=2)

        self.addCleanup(close)
        return f"http://127.0.0.1:{peer.server_port}"

    def test_early_eof_is_not_a_complete_response(self):
        body = b'10 "First mechanic"\n'
        url = self.serve(body, len(body) + 100)
        with self.assertRaises(IncompleteRead):
            fetch_bytes(url, 1000)

    def test_release_lookup_preserves_size_and_json_errors(self):
        for limit, error in ((3, OSError), (100, ValueError)):
            with self.subTest(limit=limit):
                body = b"not JSON"
                with patch.object(updater, "API_LATEST_URL", self.serve(body, len(body))), \
                        patch.object(updater, "_MAX_RELEASE_BYTES", limit):
                    with self.assertRaises(error):
                        updater.fetch_latest_release()

    def test_complete_responses_with_and_without_a_declared_length_work(self):
        body = b'10 "First mechanic"\n'
        for length in (len(body), None):
            with self.subTest(length=length):
                self.assertEqual(fetch_bytes(self.serve(body, length), len(body)), body)

    def test_chunked_response_uses_its_framing_instead_of_content_length(self):
        body = b'10 "First mechanic"\n'
        framed = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        url = self.serve(framed, len(body) + 100, chunked=True)
        self.assertEqual(fetch_bytes(url, len(body)), body)

    def test_interrupted_chunked_response_fails(self):
        url = self.serve(b"20\r\nshort", None, chunked=True)
        with self.assertRaises(IncompleteRead):
            fetch_bytes(url, 1000)

    def check_timeout_cleanup(self, mode, *, tls=False):
        peer = HttpPeer(mode, tls=tls)
        self.addCleanup(peer.close)
        before = set(threading.enumerate())
        request = urllib.request.Request(peer.uri, data=b"{}")
        context = ssl.create_default_context(cafile=peer.certificate) if tls else None
        trust = patch.object(ssl, "_create_default_https_context", return_value=context) if tls else nullcontext()
        with trust:
            with self.assertRaises(TimeoutError):
                fetch_bytes(request, 1000, timeout=2, stall=.2, deadline=.3)
            self.assertTrue(peer.started.is_set())
            self.assertTrue(wait_for(lambda: not any(
                thread not in before and thread.name in ("http-data-reader", "http-response-reader")
                for thread in threading.enumerate()), timeout=.5))
            self.assertEqual(fetch_bytes(request, 1000), b'{"response":[]}')

    def test_header_timeout_releases_the_download_reader(self):
        self.check_timeout_cleanup("headers")

    def test_redirect_body_timeout_releases_the_download_reader(self):
        self.check_timeout_cleanup("redirect")

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    def test_https_header_timeout_releases_readers_and_allows_a_retry(self):
        self.check_timeout_cleanup("headers", tls=True)

    def check_timeline_refresh(self, complete):
        old = b'10 "First mechanic"\n30 "Later mechanic"\n'
        new = b'12 "Updated first mechanic"\n40 "Updated later mechanic"\n'
        body = new if complete else new.splitlines(keepends=True)[0]
        url = self.serve(body, len(new))
        refreshed = []
        host = SimpleNamespace(_cactbot_tl_lock=threading.Lock(), _cactbot_tl_fetching=set(),
                               _cactbot_tl_signal=SimpleNamespace(emit=refreshed.append))
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "duty.cactbot.cache.txt"
            dest.write_bytes(old)
            with patch.object(timeline_tab, "_CACTBOT_DATA_RAW", url), \
                    patch.object(timeline_tab.ac, "TIMELINES_DIR", Path(folder)):
                timeline_tab.TimelineTabMixin._fetch_cactbot_timeline(host, "duty", "timeline.txt")
                self.assertTrue(wait_for(lambda: not host._cactbot_tl_fetching))
            self.assertEqual(dest.read_bytes(), new if complete else old)
            self.assertEqual(refreshed, ["duty"] if complete else [])
            self.assertEqual(list(Path(folder).iterdir()), [dest])

    def test_partial_timeline_cannot_replace_a_healthy_cache(self):
        self.check_timeline_refresh(False)

    def test_complete_timeline_still_replaces_the_cache(self):
        self.check_timeline_refresh(True)


if __name__ == "__main__":
    unittest.main()
