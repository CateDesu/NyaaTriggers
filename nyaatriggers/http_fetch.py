"""HTTP setup and response deadlines for program downloads."""

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


@contextmanager
def open_response(request, timeout: float, deadline: float):
    """Acquire a response before an absolute monotonic deadline.

    The caller owns body reads and their deadlines after this handoff.
    """
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
    """Use the host trust store when bundled OpenSSL points at a missing file."""
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
    """Fetch one body with a size cap and a deadline outside the reader."""
    done = threading.Event()
    cancelled = threading.Event()
    state = {"response": None, "progress": time.monotonic()}
    result = []
    errors = []

    def read():
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                state["response"] = response
                body = bytearray()
                read_chunk = getattr(response, "read1", response.read)
                while not cancelled.is_set():
                    chunk = read_chunk(min(65536, max_bytes + 1 - len(body)))
                    if not chunk:
                        result.append(bytes(body))
                        return
                    body.extend(chunk)
                    state["progress"] = time.monotonic()
                    if len(body) > max_bytes:
                        raise ValueError(f"response exceeds {max_bytes} bytes")
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
        try:
            state["response"].fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        raise TimeoutError("download timed out" if now >= end else "download stalled")
    if errors:
        raise errors[0]
    return result[0]
