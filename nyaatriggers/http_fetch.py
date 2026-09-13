"""HTTP setup and bounded reads for small program data files."""

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
