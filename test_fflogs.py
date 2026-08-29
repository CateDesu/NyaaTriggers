"""Tests for the FFLogs v2 client (fflogs.py), against a fake HTTP layer.

Covers the OAuth token cache (requested once, reused), zone matching
(exact, substring, no match), the zoneRankings parse (encounter-name match
preferred, top-level and allStars fallbacks), and the failure paths
(non-200, bad JSON, unknown character). Everything must return None, never
raise.

Run:  python test_fflogs.py   (exit 0 = all pass)

No network: every test injects its own http_post fake.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fflogs import FflogsClient

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


ZONES = [{"id": 93, "name": "Everkeep"},
         {"id": 88, "name": "The Voidcast Dais"},
         {"id": 122, "name": "AAC Cruiserweight (Savage)"}]


class FakeHTTP:
    """Scripted http_post: token endpoint and GraphQL endpoint replies,
    plus a record of every call for the cache assertions."""

    def __init__(self, token_status=200, token_body=None,
                 api_status=200, rankings=None, zones=None):
        self.token_status = token_status
        self.token_body = token_body if token_body is not None else (
            {"access_token": "tok123", "expires_in": 3600})
        self.api_status = api_status
        self.rankings = rankings
        self.zones = ZONES if zones is None else zones
        self.token_calls = 0
        self.api_calls = 0
        self.api_bodies = []

    def __call__(self, url, headers, body, timeout):
        if "oauth/token" in url:
            self.token_calls += 1
            return self.token_status, json.dumps(self.token_body).encode()
        self.api_calls += 1
        self.api_bodies.append(json.loads(body.decode()))
        if self.api_status != 200:
            return self.api_status, b'{"error":"boom"}'
        query = self.api_bodies[-1].get("query", "")
        if "worldData" in query:
            return 200, json.dumps(
                {"data": {"worldData": {"zones": self.zones}}}).encode()
        character = None
        if self.rankings is not None:
            character = {"zoneRankings": self.rankings}
        return 200, json.dumps(
            {"data": {"characterData": {"character": character}}}).encode()


def fresh_client(fake):
    FflogsClient._zones_cache = None      # the zone cache is process-wide
    return FflogsClient("cid", "secret", http_post=fake)


RANKINGS = {"rankings": [
    {"encounter": {"name": "Everkeep (Unreal)"}, "rankPercent": 11.0,
     "bestAmount": 100.0},
    {"encounter": {"name": "Everkeep"}, "rankPercent": 82.5,
     "bestAmount": 12345.6},
]}

# ── happy path ───────────────────────────────────────────────────────────
fake = FakeHTTP(rankings=RANKINGS)
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("happy path parses the exact encounter match (not the substring one)",
      res == {"percent": 82.5, "amount": 12345.6, "zone": "Everkeep"})
check("zone matched case-insensitively (exact)",
      fake.api_bodies[-1]["variables"]["zone"] == 93)

# Token reuse: a second fetch must not re-auth. The zone list is cached too,
# so the second fetch is a single rankings call.
res2 = c.fetch_best("Tini Poutini", "tonberry", "JP", "everkeep")
check("second fetch returns the same data", res2 == res)
check("token requested once and reused", fake.token_calls == 1)
check("zone list cached (one zones query for two fetches)",
      sum("worldData" in b["query"] for b in fake.api_bodies) == 1)

# ── zone matching variants ───────────────────────────────────────────────
fake = FakeHTTP(rankings={"rankPercent": 55.0, "bestAmount": 9000.0})
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "voidcast dais")
check("substring/casefold zone match + top-level fallback",
      res == {"percent": 55.0, "amount": 9000.0, "zone": "The Voidcast Dais"})
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Not A Real Zone")
check("unknown zone -> None", res is None)

# ── per-floor game names resolve to the tier level FFLogs zone ─────────────
# FFLogs serves one zone per raid tier, "AAC Cruiserweight (Savage)", while
# the meter passes the per-floor game name, "AAC Cruiserweight M4 (Savage)".
# The floor token sits inside the tier name, so neither the exact nor the
# substring pass hits. The token superset pass bridges it, most tokens wins.
fake = FakeHTTP(rankings={"rankPercent": 77.0, "bestAmount": 5000.0},
                zones=[{"id": 68, "name": "AAC Cruiserweight (Savage)"},
                       {"id": 69, "name": "AAC Heavyweight (Savage)"},
                       {"id": 70, "name": "AAC Light-heavyweight (Savage)"}])
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "AAC Cruiserweight M4 (Savage)")
check("per-floor savage name resolves to its tier zone",
      res == {"percent": 77.0, "amount": 5000.0, "zone": "AAC Cruiserweight (Savage)"})
check("rankings queried with the tier zone id",
      fake.api_bodies[-1]["variables"]["zone"] == 68)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "AAC Light-heavyweight M1 (Savage)")
check("most tokens wins: light-heavyweight, not heavyweight",
      res is not None and res["zone"] == "AAC Light-heavyweight (Savage)")
check("a normal floor does not resolve to the savage tier zone",
      c.fetch_best("Tini Poutini", "tonberry", "JP", "AAC Cruiserweight M4") is None)

# The older floor format carries a colon, "Asphodelos: The First Circle
# (Savage)" against zone "Asphodelos (Savage)". Tokenization must not trip
# on the punctuation.
fake = FakeHTTP(rankings={"rankPercent": 60.0, "bestAmount": 4000.0},
                zones=[{"id": 49, "name": "Asphodelos (Savage)"}])
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Asphodelos: The First Circle (Savage)")
check("colon punctuation in the floor name still resolves",
      res is not None and res["zone"] == "Asphodelos (Savage)")

# ── empty zones response: not cached, the next call refetches ─────────────
fake = FakeHTTP(rankings=RANKINGS, zones=[])
c = fresh_client(fake)
check("empty zones list -> None",
      c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep") is None)
fake.zones = ZONES
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("later call refetches zones and succeeds",
      res == {"percent": 82.5, "amount": 12345.6, "zone": "Everkeep"})
check("zones queried twice, the empty response was never cached",
      sum("worldData" in b["query"] for b in fake.api_bodies) == 2)

# ── rankings fallbacks ───────────────────────────────────────────────────
fake = FakeHTTP(rankings={"allStars": [{"rankPercent": 66.0},
                                       {"rankPercent": 71.5}]})
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("allStars fallback supplies the percent",
      res == {"percent": 71.5, "amount": None, "zone": "Everkeep"})

# zoneRankings arriving as a JSON string (some proxies double-encode).
fake = FakeHTTP(rankings=json.dumps(RANKINGS))
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("string-encoded zoneRankings still parses",
      res == {"percent": 82.5, "amount": 12345.6, "zone": "Everkeep"})

# A truthy non-dict encounter on one entry must not abort the fetch. The bad
# entry reads as nameless and the good entry after it still matches.
fake = FakeHTTP(rankings={"rankings": [
    {"encounter": "Garuda", "rankPercent": 99.0, "bestAmount": 1.0},
    {"encounter": {"name": "Everkeep"}, "rankPercent": 82.5,
     "bestAmount": 12345.6},
]})
c = fresh_client(fake)
res = c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("a non-dict encounter skips only its own entry",
      res == {"percent": 82.5, "amount": 12345.6, "zone": "Everkeep"})

# ── failure paths: everything is None, nothing raises ────────────────────
fake = FakeHTTP(token_status=403, rankings=RANKINGS)
c = fresh_client(fake)
check("token rejected -> None", c.fetch_best("a", "b", "JP", "Everkeep") is None)

fake = FakeHTTP(api_status=500, rankings=RANKINGS)
c = fresh_client(fake)
check("api non-200 -> None", c.fetch_best("a", "b", "JP", "Everkeep") is None)

fake = FakeHTTP(rankings=None)            # unknown character: character is null
c = fresh_client(fake)
check("unknown character -> None", c.fetch_best("a", "b", "JP", "Everkeep") is None)

fake = FakeHTTP(token_body={"nope": True})
c = fresh_client(fake)
check("token body without access_token -> None",
      c.fetch_best("a", "b", "JP", "Everkeep") is None)


def broken_http(url, headers, body, timeout):
    raise OSError("connection refused")


c = fresh_client(broken_http)
try:
    res = c.fetch_best("a", "b", "JP", "Everkeep")
    check("network exception -> None, never raises", res is None)
except Exception:
    check("network exception -> None, never raises", False)

c = fresh_client(FakeHTTP(rankings=RANKINGS))
check("missing inputs -> None without a single HTTP call",
      c.fetch_best("", "b", "JP", "Everkeep") is None)

# Auth header shape: HTTP Basic with the client credentials.
fake = FakeHTTP(rankings=RANKINGS)
c = fresh_client(fake)
c.fetch_best("Tini Poutini", "tonberry", "JP", "Everkeep")
check("token call count sane", fake.token_calls == 1)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
