"""Read progress follows incoming bytes even before a large buffer fills."""

from contextlib import contextmanager
import http.server
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nyaatriggers import fflogs, fight_catalog, tts, updater


@contextmanager
def streaming_peer(body, pause=.02, header_delay=0):
    stopped = threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if stopped.wait(header_delay):
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for offset in range(0, len(body), 1024):
                    self.wfile.write(body[offset:offset + 1024])
                    self.wfile.flush()
                    if stopped.wait(pause):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
        do_POST = do_GET
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class StreamingProgressTests(unittest.TestCase):
    def test_kokoro_accepts_continuous_small_chunks(self):
        body = b"x" * 32768
        with tempfile.TemporaryDirectory() as directory, streaming_peer(body) as url:
            root = Path(directory)
            dest = root / "model.onnx"
            with patch.object(tts, "_MODEL_DIR", root), \
                    patch.object(tts, "_KOKORO_URLS", {dest: url}), \
                    patch.object(tts, "_KOKORO_SHA256", {dest: hashlib.sha256(body).hexdigest()}), \
                    patch.object(tts, "_KOKORO_DL_STALL_S", .15), \
                    patch.object(tts, "_KOKORO_DL_DEADLINE_S", 3):
                self.assertTrue(tts.download_kokoro_model())
            self.assertEqual(dest.read_bytes(), body)
            self.assertEqual(list(root.glob("*.part")), [])

    def test_catalog_header_wait_obeys_its_deadline(self):
        with tempfile.TemporaryDirectory() as directory, \
                streaming_peer(b'{"tree":[]}', header_delay=.8) as url, \
                patch.object(fight_catalog, "_CACTBOT_TREE_API", url), \
                patch.object(fight_catalog, "_TREE_STALL_S", .1), \
                patch.object(fight_catalog, "_TREE_DEADLINE_S", .15):
            fight_catalog.refresh_from_cactbot_async(Path(directory) / "catalog.json")
            deadline = time.monotonic() + .5
            while fight_catalog._REFRESH_RUNNING.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            running = fight_catalog._REFRESH_RUNNING.is_set()
        deadline = time.monotonic() + 2
        while fight_catalog._REFRESH_RUNNING.is_set() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(running)

    def test_fflogs_accepts_continuous_small_chunks(self):
        body = b"x" * 32768
        with streaming_peer(body) as url, \
                patch.object(fflogs, "_READ_STALL_S", .15), \
                patch.object(fflogs, "_RESPONSE_DEADLINE_S", 3):
            self.assertEqual(fflogs.FflogsClient._urllib_post(url, {}, b"", 1), (200, body))

    def test_release_lookup_accepts_continuous_small_chunks(self):
        body = json.dumps({"tag_name": "v1.2.3", "body": "x" * 32768, "assets": []}).encode()
        with streaming_peer(body) as url, \
                patch.object(updater, "API_LATEST_URL", url), \
                patch.object(updater, "_READ_STALL_S", .15), \
                patch.object(updater, "_RELEASE_DEADLINE_S", 3), \
                patch.object(updater, "_write_release_cache"):
            self.assertEqual(updater.fetch_latest_release().version, "1.2.3")

    def test_fight_catalog_accepts_continuous_small_chunks(self):
        body = json.dumps({"tree": [{"type": "blob", "path":
            "ui/raidboss/data/07-dt/ultimate/futures_rewritten.ts"}], "padding": "x" * 32768}).encode()
        with tempfile.TemporaryDirectory() as directory, streaming_peer(body) as url, \
                patch.object(fight_catalog, "_CACTBOT_TREE_API", url), \
                patch.object(fight_catalog, "_TREE_STALL_S", .15), \
                patch.object(fight_catalog, "_TREE_DEADLINE_S", 3):
            path = Path(directory) / "catalog.json"
            fight_catalog.refresh_from_cactbot_async(path)
            deadline = time.monotonic() + 4
            while fight_catalog._REFRESH_RUNNING.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(fight_catalog._REFRESH_RUNNING.is_set())
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())[0]["folder_name"], "FRU")


if __name__ == "__main__":
    unittest.main()
