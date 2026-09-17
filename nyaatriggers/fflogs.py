"""Optional FFLogs v2 comparisons for the DPS meter using OAuth and GraphQL. Failures
return None. The injectable HTTP transport returns a status and response bytes. Tokens
expire from cache a minute early, and the zone list is shared across instances.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from nyaatriggers.drop_log import log_drop
from nyaatriggers.http_fetch import open_response

TOKEN_URL = "https://www.fflogs.com/oauth/token"
API_URL = "https://www.fflogs.com/api/v2/client"

# Bound response size to limit memory use.
_MAX_RESPONSE_BYTES = 8 << 20
# Enforce stall and total deadlines outside the read because socket timeouts reset on
# each received byte.
_READ_STALL_S = 15
_RESPONSE_DEADLINE_S = 60


def _unblock_reader(resp) -> None:
    """Try to shut down the socket without waiting for the reader to release its buffer
    lock.
    """
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


_ZONES_QUERY = "query { worldData { zones { id name } } }"
_RANKINGS_QUERY = (
    "query($name:String!,$server:String!,$region:String!,$zone:Int!) {"
    " characterData { character(name:$name, serverSlug:$server,"
    " serverRegion:$region) { zoneRankings(zoneID:$zone) } } }")


class FflogsClient:
    """Fetch a character's best parse for one zone."""

    _zones_cache: "list[dict] | None" = None   # process-wide, per class
    _zones_lock = threading.Lock()

    def __init__(self, client_id: str, client_secret: str, http_post=None) -> None:
        self._id = str(client_id or "")
        self._secret = str(client_secret or "")
        self._http = http_post or self._urllib_post
        self._token = ""
        self._token_expiry = 0.0
        self._fetch_lock = threading.Lock()

    @staticmethod
    def _urllib_post(url: str, headers: dict, body: bytes,
                     timeout: float) -> "tuple[int, bytes]":
        req = urllib.request.Request(url, data=body, headers=dict(headers),
                                     method="POST")
        deadline = time.monotonic() + _RESPONSE_DEADLINE_S
        headers_deadline = min(deadline, time.monotonic() + _READ_STALL_S)
        with open_response(req, timeout, headers_deadline) as resp:
            # Read in a helper that reports progress so the caller can enforce both
            # deadlines.
            done = threading.Event()
            progress = [0]
            reader_error = [None]
            buf = bytearray()

            def _reader() -> None:
                try:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        buf.extend(chunk)
                        progress[0] = len(buf)
                        if len(buf) > _MAX_RESPONSE_BYTES:
                            raise ValueError("fflogs response exceeded the size cap")
                        if len(chunk) < 1 << 16:
                            # urllib's read only returns short at the end of the body.
                            break
                except BaseException as exc:
                    reader_error[0] = exc
                finally:
                    done.set()

            threading.Thread(target=_reader, daemon=True).start()
            last_seen = progress[0]
            last_change = time.monotonic()
            while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                now = time.monotonic()
                if progress[0] == last_seen or now > deadline:
                    # Shut down the socket to wake the reader without waiting for its
                    # read lock.
                    _unblock_reader(resp)
                    # Report a stall only after the full quiet window has elapsed.
                    if now - last_change >= _READ_STALL_S:
                        raise TimeoutError(
                            f"fflogs response stalled, no new bytes for {_READ_STALL_S} seconds")
                    raise TimeoutError("fflogs response timed out after 60 s")
                last_seen = progress[0]
                last_change = now
            if reader_error[0]:
                raise reader_error[0]
            data = bytes(buf)
        return resp.status, data

    def _get_token(self) -> "str | None":
        if self._token and time.time() < self._token_expiry - 60.0:
            return self._token
        if not self._id or not self._secret:
            return None
        auth = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        try:
            status, data = self._http(
                TOKEN_URL,
                {"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
                b"grant_type=client_credentials", 10.0)
            payload = json.loads(data.decode("utf-8"))
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if status != 200 or not token:
                log_drop("fflogs-token", f"token request failed (status {status})")
                return None
            self._token = str(token)
            try:
                self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
            except (TypeError, ValueError):
                self._token_expiry = time.time() + 3600.0
            return self._token
        except urllib.error.HTTPError as exc:
            # HTTPError is also a response object and needs closing.
            exc.close()
            log_drop("fflogs-token", f"token request failed (status {exc.code})")
            return None
        except Exception as exc:  # noqa: BLE001
            log_drop("fflogs-token", f"token request error: {exc}")
            return None

    def _graphql(self, query: str, variables: "dict | None" = None) -> "dict | None":
        """Return GraphQL data or None on failure. Retry once with a fresh token after a
        401.
        """
        try:
            body = json.dumps({"query": query,
                               "variables": variables or {}}).encode()
        except (TypeError, ValueError):
            return None
        for attempt in range(2):
            token = self._get_token()
            if not token:
                return None
            try:
                status, data = self._http(
                    API_URL,
                    {"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
                    body, 15.0)
                payload = json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Preserve HTTP error status for token refresh handling and close the
                # response.
                exc.close()
                status, payload = exc.code, None
            except Exception as exc:  # noqa: BLE001
                log_drop("fflogs-api", f"api request error: {exc}")
                return None
            if status == 401 and attempt == 0:
                log_drop("fflogs-api", "api request returned 401; refreshing token")
                self._token = ""
                self._token_expiry = 0.0
                continue
            if status != 200 or not isinstance(payload, dict):
                log_drop("fflogs-api", f"api request failed (status {status})")
                return None
            result = payload.get("data")
            return result if isinstance(result, dict) else None
        return None

    def _zone_id(self, zone_name: str) -> "tuple[int, str] | None":
        """Find the closest zone by exact name, substring, then token superset."""
        with FflogsClient._zones_lock:
            if FflogsClient._zones_cache is None:
                data = self._graphql(_ZONES_QUERY)
                zones = (((data or {}).get("worldData") or {}).get("zones"))
                if not isinstance(zones, list):
                    return None
                zones = [
                    z for z in zones
                    if isinstance(z, dict) and isinstance(z.get("id"), int)
                    and isinstance(z.get("name"), str)]
                # Leave empty results uncached so the next encounter retries.
                if not zones:
                    return None
                FflogsClient._zones_cache = zones
            zones = FflogsClient._zones_cache
        wanted = (zone_name or "").strip().casefold()
        if not wanted:
            return None
        for z in zones:
            if z["name"].casefold() == wanted:
                return z["id"], z["name"]
        for z in zones:
            if wanted in z["name"].casefold():
                return z["id"], z["name"]
        # Game zone names include floor numbers inside the FFLogs tier name. Match token
        # supersets and prefer the most specific zone to preserve the difficulty.
        wanted_tokens = set(re.findall(r"[a-z0-9]+", wanted))
        best, best_len = None, 0
        for z in zones:
            tokens = set(re.findall(r"[a-z0-9]+", z["name"].casefold()))
            if tokens and tokens <= wanted_tokens and len(tokens) > best_len:
                best, best_len = z, len(tokens)
        if best is not None:
            return best["id"], best["name"]
        return None

    @staticmethod
    def _num(value) -> "float | None":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def fetch_best(self, char_name: str, server_slug: str, region: str,
                   zone_name: str) -> "dict | None":
        """Return percent, amount and zone for the character's best performance, or None if
        unavailable.
        """
        with self._fetch_lock:
            return self._fetch_best(char_name, server_slug, region, zone_name)

    def _fetch_best(self, char_name: str, server_slug: str, region: str,
                    zone_name: str) -> "dict | None":
        try:
            if not (char_name and server_slug and region and zone_name):
                return None
            zone = self._zone_id(zone_name)
            if zone is None:
                return None
            zone_id, zone_label = zone
            data = self._graphql(_RANKINGS_QUERY, {
                "name": str(char_name), "server": str(server_slug),
                "region": str(region), "zone": zone_id})
            character = (((data or {}).get("characterData") or {})
                         .get("character"))
            if not isinstance(character, dict):
                return None
            rankings = character.get("zoneRankings")
            if isinstance(rankings, str):      # JSON-over-JSON, some proxies
                try:
                    rankings = json.loads(rankings)
                except ValueError:
                    rankings = None
            if not isinstance(rankings, dict):
                return None

            percent = amount = None
            entries = rankings.get("rankings")
            if isinstance(entries, list):
                wanted = str(zone_name).strip().casefold()
                best = None
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    # Skip malformed entries without discarding other encounters.
                    encounter = entry.get("encounter")
                    if not isinstance(encounter, dict):
                        encounter = {}
                    enc_name = str(encounter.get("name") or "")
                    if enc_name.casefold() == wanted:
                        best = entry
                        break
                    if best is None and wanted and wanted in enc_name.casefold():
                        best = entry
                if best is not None:
                    percent = self._num(best.get("rankPercent"))
                    amount = self._num(best.get("bestAmount"))
            if percent is None and amount is None:
                percent = self._num(rankings.get("rankPercent"))
                amount = self._num(rankings.get("bestAmount"))
            if percent is None:
                stars = rankings.get("allStars")
                if isinstance(stars, list):
                    vals = [self._num(s.get("rankPercent"))
                            for s in stars if isinstance(s, dict)]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        percent = max(vals)
            if percent is None and amount is None:
                return None
            return {"percent": percent, "amount": amount, "zone": zone_label}
        except Exception as exc:  # noqa: BLE001
            log_drop("fflogs", f"fetch_best failed: {exc}")
            return None
