"""Watchdog tests for the six network read sites.

Each site reads an HTTP body in a loop and used to check its total deadline
only after resp.read returned. A peer trickling one byte per window keeps
the per recv socket timeout alive forever, so the deadline never fired and
the UI latch behind the site stayed set. Every site now runs its read loop
on a daemon helper thread while the calling thread enforces a stall window
and the total deadline from outside the read, the same guard main.py runs.

These tests stand up a raw socket server on 127.0.0.1 that sends a large
Content-Length and then trickles one byte per interval, parks after a
partial body, or drips whole chunks. Against it each site must be cut off
by its watchdog in well under its real deadline. A normal local server
serving the full body proves healthy transfers still succeed, and the
drip mode proves a slow link with flowing bytes is left alone until the
total deadline itself overruns. No real network.

Run directly:  python test_download_deadline.py   (exit 0 = all pass)
        or:    python -m pytest test_download_deadline.py -q
"""
import hashlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fflogs
import fight_catalog as fc
import install
import tts
import updater

# Stall window used by the cutoff tests. Small enough to keep the suite at
# a few seconds, large enough that scheduling jitter cannot trip it on a
# healthy transfer.
_STALL = 0.3


def _wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class _TrickleServer:
    """Raw socket HTTP server for the stall cases. Sends a big Content-Length
    and then one of:
      trickle - one byte per interval, forever. Bytes keep arriving inside
                every recv window, so the client socket timeout never fires.
      park    - a partial body, then nothing. The read sits parked.
      drip    - one whole chunk per interval, so the transfer keeps making
                progress and only a total deadline may cut it.
    `connections` counts live client sockets, so a test can prove the
    watchdog really closed its side instead of leaking the reader."""

    def __init__(self, mode="trickle", interval=0.05, chunk=65536,
                 partial=0, content_length=1 << 20, body=None):
        assert mode in ("trickle", "park", "drip")
        self.mode = mode
        self.interval = interval
        self.chunk = chunk
        self.partial = partial
        # A drip with an explicit body serves those exact bytes, so a test
        # can compare what the client saved. Otherwise filler bytes.
        self.body = body
        if body is not None:
            content_length = len(body)
        self.content_length = content_length
        self.connections = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.base = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self.url = self.base + "/body"
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with self._lock:
            self.connections += 1
        try:
            conn.settimeout(10)
            req = b""
            while b"\r\n\r\n" not in req:
                data = conn.recv(4096)
                if not data:
                    return
                req += data
            head = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {self.content_length}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Connection: close\r\n\r\n"
            )
            conn.sendall(head.encode())
            if self.partial:
                conn.sendall(b"A" * self.partial)
            if self.mode == "park":
                # Hold the rest of the body back. The recv notices the client
                # going away, so the connection count drops on a cutoff.
                while not self._stop.is_set():
                    try:
                        conn.settimeout(0.5)
                        if not conn.recv(4096):
                            return
                    except socket.timeout:
                        pass
                return
            sent = self.partial
            while not self._stop.is_set() and sent < self.content_length:
                if self.mode == "trickle":
                    n = 1
                else:
                    n = min(self.chunk, self.content_length - sent)
                piece = (self.body[sent:sent + n] if self.body is not None
                         else b"A" * n)
                conn.sendall(piece)
                sent += n
                self._stop.wait(self.interval)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self.connections -= 1

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class _HealthyHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def _answer(self):
        if self.command == "POST":
            # Drain the request body so the response is not racing unread
            # request bytes on the same connection.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args):
        pass


class _HealthyServer:
    """Plain threaded HTTP server handing full bodies out of a routes dict."""

    def __init__(self, routes):
        handler = type("BoundHandler", (_HealthyHandler,), {"routes": dict(routes)})
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self.url = self.base + "/body"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


# ── Watchdog cutoffs, one per site, each against trickle and park ─────────

def test_fetch_latest_release_cut():
    for mode in ("trickle", "park"):
        srv = _TrickleServer(mode=mode)
        saved = (updater.API_LATEST_URL, updater._READ_STALL_S)
        updater.API_LATEST_URL = srv.url
        updater._READ_STALL_S = _STALL
        try:
            t0 = time.monotonic()
            raised = None
            try:
                updater.fetch_latest_release()
            except OSError as exc:
                raised = exc
            elapsed = time.monotonic() - t0
            assert raised is not None and "timed out after 30 seconds" in str(raised), mode
            assert elapsed < 10, mode
        finally:
            updater.API_LATEST_URL, updater._READ_STALL_S = saved
            srv.close()


def test_download_cut():
    for mode in ("trickle", "park"):
        with tempfile.TemporaryDirectory() as td:
            srv = _TrickleServer(mode=mode, partial=100)
            saved = updater._READ_STALL_S
            updater._READ_STALL_S = _STALL
            try:
                dest = Path(td) / "update.zip"
                t0 = time.monotonic()
                raised = None
                try:
                    updater.download(srv.url, dest)
                except OSError as exc:
                    raised = exc
                elapsed = time.monotonic() - t0
                assert raised is not None and "stalled, no new bytes" in str(raised), mode
                assert elapsed < 10, mode
                assert not dest.exists() and not list(Path(td).glob("*.part")), mode
                # The watchdog closed the response, so the server sees the
                # connection die instead of serving a leaked reader thread.
                assert _wait_for(lambda: srv.connections == 0), mode
            finally:
                updater._READ_STALL_S = saved
                srv.close()


def test_fight_catalog_cut():
    for mode in ("trickle", "park"):
        with tempfile.TemporaryDirectory() as td:
            srv = _TrickleServer(mode=mode)
            drops = []
            saved = (fc._CACTBOT_TREE_API, fc._TREE_STALL_S, fc.log_drop)
            fc._CACTBOT_TREE_API = srv.url
            fc._TREE_STALL_S = _STALL
            fc.log_drop = lambda site, detail, *a, **k: drops.append(detail)
            cache = Path(td) / "fight_catalog.json"
            try:
                fc.refresh_from_cactbot_async(cache)
                # The latch must free even though the read never finishes.
                assert _wait_for(lambda: not fc._REFRESH_RUNNING.is_set()), mode
                assert not cache.exists(), mode
                assert any("stalled, no new bytes" in d for d in drops), mode
            finally:
                fc._CACTBOT_TREE_API, fc._TREE_STALL_S, fc.log_drop = saved
                srv.close()


def test_tts_kokoro_cut():
    for mode in ("trickle", "park"):
        with tempfile.TemporaryDirectory() as td:
            srv = _TrickleServer(mode=mode)
            dest = Path(td) / "kokoro-v1.0.onnx"
            saved = (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_DL_STALL_S)
            tts._MODEL_DIR = Path(td)
            tts._KOKORO_URLS = {dest: srv.url}
            tts._KOKORO_DL_STALL_S = _STALL
            try:
                import io
                import contextlib
                t0 = time.monotonic()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    ok = tts.download_kokoro_model()
                elapsed = time.monotonic() - t0
                assert ok is False, mode
                assert elapsed < 10, mode
                assert "stalled, no new bytes" in err.getvalue(), mode
                assert not dest.exists() and not list(Path(td).glob("*.part")), mode
            finally:
                tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_DL_STALL_S = saved
                srv.close()


def test_install_voice_cut():
    for mode in ("trickle", "park"):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            srv = _TrickleServer(mode=mode)
            saved = (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
                     install._READ_STALL_S)
            install.VOICES_DIR = td
            install.VOICE_FILE = td / f"{install.VOICE_STEM}.onnx"
            install.VOICE_BASE = srv.base
            install._READ_STALL_S = _STALL
            try:
                t0 = time.monotonic()
                raised = None
                try:
                    install.download_voice()
                except OSError as exc:
                    raised = exc
                elapsed = time.monotonic() - t0
                assert raised is not None and "stalled, no new bytes" in str(raised), mode
                assert elapsed < 10, mode
                assert not list(td.glob("*.part")), mode
            finally:
                (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
                 install._READ_STALL_S) = saved
                srv.close()


def test_fflogs_cut():
    for mode in ("trickle", "park"):
        srv = _TrickleServer(mode=mode)
        saved = fflogs._READ_STALL_S
        fflogs._READ_STALL_S = _STALL
        try:
            t0 = time.monotonic()
            raised = None
            try:
                fflogs.FflogsClient._urllib_post(srv.url, {}, b"{}", 10.0)
            except TimeoutError as exc:
                raised = exc
            elapsed = time.monotonic() - t0
            assert raised is not None and "stalled, no new bytes" in str(raised), mode
            assert "timed out" not in str(raised), mode
            assert elapsed < 10, mode
        finally:
            fflogs._READ_STALL_S = saved
            srv.close()


# ── The deadline still fires while bytes flow, and only then ──────────────

def test_download_deadline_fires_mid_flow():
    # Drip whole chunks so progress never stalls. The stall window must not
    # fire. The total deadline must, on time.
    srv = _TrickleServer(mode="drip", interval=0.15, chunk=262144,
                         content_length=100 << 20)
    saved = (updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S)
    updater._READ_STALL_S = 30
    updater._DOWNLOAD_DEADLINE_S = 0.6
    try:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "update.zip"
            t0 = time.monotonic()
            raised = None
            try:
                updater.download(srv.url, dest)
            except OSError as exc:
                raised = exc
            elapsed = time.monotonic() - t0
            assert raised is not None and "timed out after 60 minutes" in str(raised)
            assert 0.4 < elapsed < 10, elapsed
    finally:
        updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S = saved
        srv.close()


def test_download_quiet_inside_final_window_says_deadline():
    # The stall window is clamped short near the total deadline. A transfer
    # that went quiet less than one full stall window before the deadline
    # must report the deadline, not claim a stall that never fully elapsed.
    srv = _TrickleServer(mode="park", partial=100)
    saved = (updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S)
    updater._READ_STALL_S = 30
    updater._DOWNLOAD_DEADLINE_S = 0.6
    try:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "update.zip"
            raised = None
            try:
                updater.download(srv.url, dest)
            except OSError as exc:
                raised = exc
            assert raised is not None and "timed out after 60 minutes" in str(raised)
            assert "stalled" not in str(raised)
    finally:
        updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S = saved
        srv.close()


def test_install_voice_quiet_inside_final_window_says_deadline():
    # Same label rule at the voice install site.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        srv = _TrickleServer(mode="park", partial=100)
        saved = (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
                 install._READ_STALL_S, install._DOWNLOAD_DEADLINE_S)
        install.VOICES_DIR = td
        install.VOICE_FILE = td / f"{install.VOICE_STEM}.onnx"
        install.VOICE_BASE = srv.base
        install._READ_STALL_S = 30
        install._DOWNLOAD_DEADLINE_S = 0.6
        try:
            raised = None
            try:
                install.download_voice()
            except OSError as exc:
                raised = exc
            assert raised is not None and "timed out after 60 minutes" in str(raised)
            assert "stalled" not in str(raised)
        finally:
            (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
             install._READ_STALL_S, install._DOWNLOAD_DEADLINE_S) = saved
            srv.close()


def test_fflogs_quiet_inside_final_window_says_deadline():
    # Same label rule at the fflogs response site, stall 15 s under a 60 s
    # deadline, so a parked response near the deadline reports the deadline.
    import io
    import contextlib
    srv = _TrickleServer(mode="park", partial=100)
    saved = (fflogs._READ_STALL_S, fflogs._RESPONSE_DEADLINE_S)
    fflogs._READ_STALL_S = 30
    fflogs._RESPONSE_DEADLINE_S = 0.6
    try:
        raised = None
        try:
            fflogs.FflogsClient._urllib_post(srv.url, {}, b"{}", 10.0)
        except TimeoutError as exc:
            raised = exc
        assert raised is not None and "timed out after 60 s" in str(raised)
        assert "stalled" not in str(raised)
    finally:
        fflogs._READ_STALL_S, fflogs._RESPONSE_DEADLINE_S = saved
        srv.close()


def test_fight_catalog_quiet_inside_final_window_says_deadline():
    # Same label rule at the cactbot tree fetch site.
    with tempfile.TemporaryDirectory() as td:
        srv = _TrickleServer(mode="park", partial=100)
        drops = []
        saved = (fc._CACTBOT_TREE_API, fc._TREE_STALL_S, fc._TREE_DEADLINE_S,
                 fc.log_drop)
        fc._CACTBOT_TREE_API = srv.url
        fc._TREE_STALL_S = 30
        fc._TREE_DEADLINE_S = 0.6
        fc.log_drop = lambda site, detail, *a, **k: drops.append(detail)
        cache = Path(td) / "fight_catalog.json"
        try:
            fc.refresh_from_cactbot_async(cache)
            assert _wait_for(lambda: not fc._REFRESH_RUNNING.is_set())
            assert any("timed out after 60 s" in d for d in drops), drops
            assert not any("stalled" in d for d in drops), drops
        finally:
            (fc._CACTBOT_TREE_API, fc._TREE_STALL_S, fc._TREE_DEADLINE_S,
             fc.log_drop) = saved
            srv.close()


def test_tts_kokoro_quiet_inside_final_window_says_deadline():
    # Same label rule at the kokoro model download site. The failure path
    # prints to stderr and returns False, so capture stderr for the label.
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as td:
        srv = _TrickleServer(mode="park", partial=100)
        dest = Path(td) / "kokoro-v1.0.onnx"
        saved = (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_DL_STALL_S,
                 tts._KOKORO_DL_DEADLINE_S)
        tts._MODEL_DIR = Path(td)
        tts._KOKORO_URLS = {dest: srv.url}
        tts._KOKORO_DL_STALL_S = 30
        tts._KOKORO_DL_DEADLINE_S = 0.6
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ok = tts.download_kokoro_model()
            assert ok is False
            assert "still running past" in err.getvalue()
            assert "stalled" not in err.getvalue()
        finally:
            (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_DL_STALL_S,
             tts._KOKORO_DL_DEADLINE_S) = saved
            srv.close()


def test_download_slow_but_flowing_succeeds():
    # The other half of the contract. A slow link with bytes moving inside
    # every stall window is fine and the transfer completes.
    body = b"z" * (5 * 65536)
    srv = _TrickleServer(mode="drip", interval=0.05, chunk=65536, body=body)
    saved = (updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S)
    updater._READ_STALL_S = _STALL
    updater._DOWNLOAD_DEADLINE_S = 30
    try:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "update.zip"
            updater.download(srv.url, dest)
            assert dest.read_bytes() == body
    finally:
        updater._READ_STALL_S, updater._DOWNLOAD_DEADLINE_S = saved
        srv.close()


# ── Healthy fast transfers still succeed through every site ───────────────

def test_fetch_latest_release_healthy():
    payload = json.dumps({
        "tag_name": "v9.9.9",
        "html_url": "https://example.invalid/r",
        "body": "notes",
        "assets": [{"name": "a.zip",
                    "browser_download_url": "https://example.invalid/a.zip"}],
    }).encode()
    srv = _HealthyServer({"/body": payload})
    with tempfile.TemporaryDirectory() as td:
        saved = (updater.API_LATEST_URL, updater.install_dir)
        updater.API_LATEST_URL = srv.url
        updater.install_dir = lambda: Path(td)
        try:
            rel = updater.fetch_latest_release()
            assert rel.tag == "v9.9.9"
            assert rel.assets == {"a.zip": "https://example.invalid/a.zip"}
            assert json.loads((Path(td) / "latest_release.json").read_text())["tag"] == "v9.9.9"
        finally:
            updater.API_LATEST_URL, updater.install_dir = saved
            srv.close()


def test_download_healthy():
    body = b"y" * 300_000
    srv = _HealthyServer({"/body": body})
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "update.zip"
        calls = []
        try:
            updater.download(srv.url, dest,
                             progress_cb=lambda d, t: calls.append((d, t)))
            assert dest.read_bytes() == body
            assert calls and calls[-1] == (len(body), len(body))
        finally:
            srv.close()


def test_fight_catalog_healthy():
    tree = {"tree": [
        {"path": "ui/raidboss/data/07-dt/raid/r1s.ts", "type": "blob"},
        {"path": "ui/raidboss/data/07-dt/ultimate/futures_rewritten.ts",
         "type": "blob"},
    ], "truncated": False}
    srv = _HealthyServer({"/body": json.dumps(tree).encode()})
    with tempfile.TemporaryDirectory() as td:
        drops = []
        saved = (fc._CACTBOT_TREE_API, fc.log_drop)
        fc._CACTBOT_TREE_API = srv.url
        fc.log_drop = lambda site, detail, *a, **k: drops.append(detail)
        cache = Path(td) / "fight_catalog.json"
        try:
            fc.refresh_from_cactbot_async(cache)
            assert _wait_for(lambda: not fc._REFRESH_RUNNING.is_set())
            folders = {e["folder_name"]
                       for e in json.loads(cache.read_text(encoding="utf-8"))}
            assert "M1S" in folders and "FRU" in folders, folders
        finally:
            fc._CACTBOT_TREE_API, fc.log_drop = saved
            srv.close()


def test_tts_kokoro_healthy():
    body = b"\1" * 100_000
    srv = _HealthyServer({"/body": body})
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "kokoro-v1.0.onnx"
        saved = (tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_SHA256)
        tts._MODEL_DIR = Path(td)
        tts._KOKORO_URLS = {dest: srv.url}
        tts._KOKORO_SHA256 = {dest: hashlib.sha256(body).hexdigest()}
        try:
            assert tts.download_kokoro_model() is True
            assert dest.read_bytes() == body
        finally:
            tts._MODEL_DIR, tts._KOKORO_URLS, tts._KOKORO_SHA256 = saved
            srv.close()


def test_install_voice_healthy():
    cfg_body = b'{"sample_rate": 22050}'
    srv = _HealthyServer({f"/{install.VOICE_STEM}.onnx.json": cfg_body})
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        saved = (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
                 install.VOICE_ONNX_SHA256)
        install.VOICES_DIR = td
        install.VOICE_FILE = td / f"{install.VOICE_STEM}.onnx"
        install.VOICE_FILE.write_bytes(b"keep-me")
        # Pin the hash to the fixture content, same as the regression suite,
        # so the pre-existing model passes the integrity check and is kept.
        install.VOICE_ONNX_SHA256 = install._sha256(install.VOICE_FILE)
        install.VOICE_BASE = srv.base
        try:
            install.download_voice()
            assert install.VOICE_FILE.read_bytes() == b"keep-me"
            assert (td / f"{install.VOICE_STEM}.onnx.json").read_bytes() == cfg_body
        finally:
            (install.VOICES_DIR, install.VOICE_FILE, install.VOICE_BASE,
             install.VOICE_ONNX_SHA256) = saved
            srv.close()


def test_fflogs_healthy():
    body = b'{"data": {"ok": true}}'
    srv = _HealthyServer({"/body": body})
    try:
        status, data = fflogs.FflogsClient._urllib_post(
            srv.url, {"X-Test": "1"}, b'{"q": 1}', 10.0)
        assert status == 200 and data == body
    finally:
        srv.close()


def test_fflogs_oversize():
    # One byte past the cap still signals too large through the same error.
    body = b"o" * (fflogs._MAX_RESPONSE_BYTES + 1)
    srv = _HealthyServer({"/body": body})
    try:
        raised = None
        try:
            fflogs.FflogsClient._urllib_post(srv.url, {}, b"{}", 30.0)
        except ValueError as exc:
            raised = exc
        assert raised is not None and "size cap" in str(raised)
    finally:
        srv.close()


if __name__ == "__main__":
    _fails = []
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except Exception as exc:
                print(f"FAIL  {_name}: {exc!r}")
                _fails.append(_name)
            else:
                print(f"PASS  {_name}")
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {', '.join(_fails)}")
        sys.exit(1)
    print("all passed")
