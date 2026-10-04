from contextlib import contextmanager
import http.client
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path


_LINUX_CA_BUNDLES = (
    '/etc/ssl/certs/ca-certificates.crt',
    '/etc/ssl/cert.pem',
    '/etc/pki/tls/certs/ca-bundle.crt',
    '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
)


class ResponseTooLarge(ValueError):
    pass


class ReadTimeout(TimeoutError):
    def __init__(self, stalled: bool):
        self.stalled = stalled
        super().__init__("download stalled" if stalled else "download timed out")


def _unblock_reader(response) -> None:
    """Wake a reader without waiting for its buffer lock."""
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def copy_response(response, output, max_bytes: int, *, stall: float, deadline: float,
                  progress_cb=None, total: int = 0, progress_step: int = 262144,
                  chunk_cb=None) -> int:
    """Copy a bounded response before the absolute deadline. The caller owns the files."""
    done = threading.Event()
    progress = [0]
    errors = []

    def read():
        try:
            read_chunk = getattr(response, "read1", response.read)
            notified = 0
            last_notice = time.monotonic()
            while True:
                chunk = read_chunk(65536)
                if not chunk:
                    break
                progress[0] += len(chunk)
                if progress[0] > max_bytes:
                    raise OSError(f"Download exceeded the {max_bytes} byte safety cap")
                if chunk_cb is not None:
                    chunk_cb(chunk)
                output.write(chunk)
                now = time.monotonic()
                if progress_cb and (progress[0] - notified >= progress_step or now - last_notice >= .2):
                    progress_cb(progress[0], total)
                    notified = progress[0]
                    last_notice = now
            if progress_cb and progress[0] != notified:
                progress_cb(progress[0], total)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=read, daemon=True, name="http-file-reader").start()
    last_seen = progress[0]
    last_change = time.monotonic()
    while not done.wait(timeout=min(stall, max(0.0, deadline - time.monotonic()))):
        now = time.monotonic()
        if progress[0] == last_seen or now > deadline:
            _unblock_reader(response)
            raise ReadTimeout(stalled=now - last_change >= stall)
        last_seen = progress[0]
        last_change = now
    if errors:
        raise errors[0]
    return progress[0]


@contextmanager
def open_response(request, timeout: float, deadline: float):
    """Acquire a response before the monotonic deadline. The caller owns body read deadlines."""
    done = threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()
    connections, responses, result, errors = [], [], [], []

    def check_cancelled():
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("response headers timed out")

    def connection_type(base):
        class Connection(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with lock:
                    connections.append(self)

            def connect(self):
                check_cancelled()
                super().connect()
                try:
                    check_cancelled()
                except TimeoutError:
                    self.close()
                    raise

            def send(self, data):
                check_cancelled()
                super().send(data)

            def getresponse(self):
                response = super().getresponse()
                with lock:
                    responses.append(response)
                return response
        return Connection

    http_connection = connection_type(http.client.HTTPConnection)
    https_connection = connection_type(http.client.HTTPSConnection)

    class HttpHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(http_connection, req)

    class HttpsHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(https_connection, req, context=self._context)

    opener = urllib.request.build_opener(HttpHandler(), HttpsHandler())

    def acquire():
        response = None
        try:
            check_cancelled()
            response = opener.open(request, timeout=timeout)
            with lock:
                if not cancelled.is_set():
                    result.append(response)
                    response = None
        except Exception as exc:
            with lock:
                abandoned = cancelled.is_set()
                if not abandoned:
                    errors.append(exc)
            if abandoned and hasattr(exc, "close"):
                exc.close()
        finally:
            if response is not None:
                response.close()
            done.set()

    threading.Thread(target=acquire, daemon=True, name="http-response-reader").start()
    if not done.wait(max(0.0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        with lock:
            cancelled.set()
            sockets = [conn.sock for conn in connections if conn.sock is not None]
            # Redirect handlers can read a body before the opener returns.
            for response in responses:
                try:
                    sockets.append(response.fp.raw._sock)
                except AttributeError:
                    pass
            acquired = list(result)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for response in acquired:
            response.close()
        for exc in errors:
            if hasattr(exc, "close"):
                exc.close()
        raise TimeoutError("response headers timed out")
    if errors:
        raise errors[0]
    with result[0] as response:
        yield response


def configure_ssl_trust() -> None:
    if not sys.platform.startswith('linux') or not getattr(sys, 'frozen', False):
        return
    if 'SSL_CERT_FILE' in os.environ or 'SSL_CERT_DIR' in os.environ:
        return
    import ssl
    if ssl.get_default_verify_paths().cafile:
        return
    for candidate in _LINUX_CA_BUNDLES:
        if Path(candidate).is_file():
            os.environ['SSL_CERT_FILE'] = candidate
            return


def fetch_bytes(request, max_bytes: int, timeout: float = 15,
                stall: float = 15, deadline: float = 60) -> bytes:
    done = threading.Event()
    cancelled = threading.Event()
    state = {"response": None, "progress": time.monotonic()}
    result = []
    errors = []

    def read():
        try:
            headers_deadline = min(end, state["progress"] + stall)
            with open_response(request, timeout, headers_deadline) as response:
                state["response"] = response
                body = bytearray()
                read_chunk = getattr(response, "read1", response.read)
                while not cancelled.is_set():
                    chunk = read_chunk(min(65536, max_bytes + 1 - len(body)))
                    if not chunk:
                        remaining = getattr(response, "length", None)
                        if isinstance(remaining, int) and remaining > 0:
                            raise http.client.IncompleteRead(bytes(body), remaining)
                        result.append(bytes(body))
                        return
                    body.extend(chunk)
                    state["progress"] = time.monotonic()
                    if len(body) > max_bytes:
                        raise ResponseTooLarge(f"response exceeds {max_bytes} bytes")
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    end = time.monotonic() + deadline
    threading.Thread(target=read, daemon=True, name="http-data-reader").start()
    while not done.wait(max(0.0, min(end, state["progress"] + stall) - time.monotonic())):
        now = time.monotonic()
        if now < end and now < state["progress"] + stall:
            continue
        cancelled.set()
        _unblock_reader(state["response"])
        raise ReadTimeout(stalled=now < end)
    if errors:
        raise errors[0]
    return result[0]
