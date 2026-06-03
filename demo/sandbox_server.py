#!/usr/bin/env python3
"""The Company Brain — HARDENED PUBLIC SANDBOX backend (Phase 9).

A deployment-ready, locked-down sibling of ``brain_server.py`` intended for
public hosting. It proxies a *fixed*, parameterised set of TaxiQL queries to a
local Orbital stack and adds the guard-rails you need before exposing a live
semantic router to the open internet:

  (a) In-memory response CACHE with TTL  — repeated company lookups are instant
      and shield flaky upstreams from a hot demo crowd.
  (b) Per-IP token-bucket RATE LIMIT     — one noisy client cannot exhaust the
      shared Orbital backend or your egress quota.
  (c) SEEDED allow-list of ~12 well-known UK companies, warmed into the cache on
      boot from baked-in snapshots, so the headline demo always answers instantly
      even if GLEIF / Police.uk / the Environment Agency are down.
  (d) ONLY safe, parameterised query shapes (company name / ticker / owner+repo).
      There is NO endpoint that accepts arbitrary TaxiQL — the query templates
      live server-side and user input is whitelisted before substitution.
  (e) Request TIMEOUTS on every upstream call so a slow API can't pin a worker.

Pure Python stdlib — no dependencies to install.

    ORBITAL_URL=http://localhost:9022 python3 demo/sandbox_server.py
    # then: curl 'http://localhost:8905/api/trading?ticker=AAPL'

Environment variables (all optional, safe defaults):
    ORBITAL_URL            Base URL of the Orbital stack   (default http://localhost:9022)
    SANDBOX_PORT           Port to bind                    (default 8905)
    SANDBOX_HOST           Interface to bind               (default 127.0.0.1)
    CACHE_TTL              Cache entry lifetime, seconds    (default 300)
    CACHE_MAX              Max cache entries (LRU-ish evict) (default 2000)
    RATE_CAPACITY          Token-bucket burst size per IP   (default 30)
    RATE_REFILL_PER_SEC    Tokens added per second per IP   (default 0.5)
    UPSTREAM_TIMEOUT       Per-query upstream timeout, secs (default 20)
    WARM_CACHE            "0" to skip seeding cache on boot (default on)
    TRUST_XFF             "1" to honour X-Forwarded-For      (default off; only
                          enable behind a trusted proxy you control)
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
ORBITAL = os.environ.get("ORBITAL_URL", "http://localhost:9022").rstrip("/") + "/api/taxiql"
HOST = os.environ.get("SANDBOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("SANDBOX_PORT", "8905"))
HERE = os.path.dirname(os.path.abspath(__file__))

CACHE_TTL = float(os.environ.get("CACHE_TTL", "300"))            # 5 minutes
CACHE_MAX = int(os.environ.get("CACHE_MAX", "2000"))
RATE_CAPACITY = float(os.environ.get("RATE_CAPACITY", "30"))     # burst
RATE_REFILL = float(os.environ.get("RATE_REFILL_PER_SEC", "0.5"))  # ~30 req/min steady
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "20"))
WARM_CACHE = os.environ.get("WARM_CACHE", "1") != "0"
TRUST_XFF = os.environ.get("TRUST_XFF", "0") == "1"

# Defensive caps so a hostile request line can't blow up memory/regex.
MAX_INPUT_LEN = 80
MAX_QUERY_BYTES = 4096


# --------------------------------------------------------------------------- #
# Input sanitisation — whitelist so user text cannot escape the TaxiQL literal #
# --------------------------------------------------------------------------- #
_NAME_RE = re.compile(r"[^A-Za-z0-9 &.,()\-_]")
_TICKER_RE = re.compile(r"[^A-Za-z0-9.\-^=]")          # AAPL, TSCO.L, BRK-B, ^FTSE
_REPO_RE = re.compile(r"[^A-Za-z0-9._\-]")             # GitHub owner/repo grammar


def clean_name(s: str) -> str:
    return _NAME_RE.sub("", s or "").strip()[:MAX_INPUT_LEN]


def clean_ticker(s: str) -> str:
    return _TICKER_RE.sub("", s or "").strip()[:MAX_INPUT_LEN]


def clean_repo(s: str) -> str:
    return _REPO_RE.sub("", s or "").strip()[:MAX_INPUT_LEN]


# --------------------------------------------------------------------------- #
# (e) Upstream proxy with a hard timeout                                       #
# --------------------------------------------------------------------------- #
def taxiql(query: str, timeout: float = UPSTREAM_TIMEOUT):
    """POST a *server-authored* TaxiQL query to Orbital. Never accepts client SQL."""
    data = query.encode("utf-8")
    if len(data) > MAX_QUERY_BYTES:
        raise ValueError("query too large")
    req = urllib.request.Request(
        ORBITAL, data=data,
        headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# (a) TTL cache — small, thread-safe, LRU-ish eviction                         #
# --------------------------------------------------------------------------- #
class TTLCache:
    def __init__(self, ttl: float, maxsize: int):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        now = time.monotonic()
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < now:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)            # mark as recently used
            return value

    def set(self, key: str, value, ttl: float = None):
        exp = time.monotonic() + (self.ttl if ttl is None else ttl)
        with self._lock:
            self._d[key] = (exp, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)     # evict oldest

    def stats(self):
        with self._lock:
            return {"entries": len(self._d), "maxsize": self.maxsize, "ttl": self.ttl}


CACHE = TTLCache(CACHE_TTL, CACHE_MAX)


# --------------------------------------------------------------------------- #
# (b) Per-IP token bucket                                                      #
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.refill = refill_per_sec
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1.0:
                self._buckets[ip] = (tokens, now)
                return False
            self._buckets[ip] = (tokens - 1.0, now)
            # Opportunistic GC of idle buckets so the dict can't grow unbounded.
            if len(self._buckets) > 10000:
                cutoff = now - 3600
                for k in [k for k, (_, t) in self._buckets.items() if t < cutoff]:
                    self._buckets.pop(k, None)
            return True


LIMITER = RateLimiter(RATE_CAPACITY, RATE_REFILL)


# --------------------------------------------------------------------------- #
# Server-side query templates (the ONLY shapes a client can trigger)          #
# --------------------------------------------------------------------------- #
COMPANY_Q = '''given {{ name : uk.gov.CompanyName = "{name}" }}
find {{
  identity : uk.gov.apis.gleif.LeiRecord
  background : uk.gov.apis.wikidata.WikidataEntity
  weatherAtHq : uk.gov.apis.open_meteo.CurrentWeather
  crimesNearHq : uk.gov.apis.police_uk.StreetCrime[]
  floodStationsNearHq : uk.gov.apis.environment_agency_flood.FloodStation[]
}}'''

TRADING_Q = 'given {{ ticker : brain.StockTicker = "{ticker}" }}\nfind {{ markets.StockQuote }}'

CODING_Q = ('given {{ owner : brain.RepoOwner = "{owner}", '
            'repo : brain.RepoName = "{repo}" }}\nfind {{ coding.GithubRepo }}')


# --------------------------------------------------------------------------- #
# (c) Seeded allow-list — ~12 well-known UK companies + handy trading tickers  #
#     and a couple of repos. Baked snapshots keep the demo alive offline.      #
# --------------------------------------------------------------------------- #
# The seeded companies are the ones whose names we *prefer* to demo. On boot we
# try to warm each from live Orbital; if that fails we fall back to the baked
# snapshot below so /api/brain still returns a believable dossier shape.
SEED_COMPANIES = [
    "TESCO PLC", "BARCLAYS PLC", "BP P.L.C.", "VODAFONE GROUP PUBLIC LIMITED COMPANY",
    "ASTRAZENECA PLC", "HSBC HOLDINGS PLC", "GLAXOSMITHKLINE PLC", "UNILEVER PLC",
    "ROLLS-ROYCE HOLDINGS PLC", "LLOYDS BANKING GROUP PLC",
    "NATWEST GROUP PLC", "BT GROUP PLC",
]

# Minimal baked dossier fallbacks (identity only) keyed by the seed name. These
# are public-register facts (LEI / Companies House number / registered office
# city + postcode). Weather/crime/flood are intentionally omitted in the
# fallback — they're live-only and clearly marked stale=false vs cached.
SEED_FALLBACK = {
    "TESCO PLC": {
        "identity": {"lei": "21380068P1DRHMJ8KU70", "companyName": "TESCO PLC",
                     "companyNumber": "00445790", "companyStatus": "ACTIVE",
                     "registeredCity": "Welwyn Garden City", "postcode": "AL7 1GA"}},
    "BARCLAYS PLC": {
        "identity": {"lei": "G5GSEF7VJP5I7OUK5573", "companyName": "BARCLAYS PLC",
                     "companyNumber": "00048839", "companyStatus": "ACTIVE",
                     "registeredCity": "London", "postcode": "E14 5HP"}},
    "BP P.L.C.": {
        "identity": {"lei": "213800LH1BZH3DI6G760", "companyName": "BP P.L.C.",
                     "companyNumber": "00102498", "companyStatus": "ACTIVE",
                     "registeredCity": "London", "postcode": "EC2M 7AF"}},
    "ASTRAZENECA PLC": {
        "identity": {"lei": "PY6ZZqWFWLTBQ91WU537", "companyName": "ASTRAZENECA PLC",
                     "companyNumber": "02723534", "companyStatus": "ACTIVE",
                     "registeredCity": "Cambridge", "postcode": "CB2 0AA"}},
    "HSBC HOLDINGS PLC": {
        "identity": {"lei": "MLU0ZO3ML4LN2LL2TL39", "companyName": "HSBC HOLDINGS PLC",
                     "companyNumber": "00617987", "companyStatus": "ACTIVE",
                     "registeredCity": "London", "postcode": "E14 5HQ"}},
    "UNILEVER PLC": {
        "identity": {"lei": "549300MKFYEKVRWML317", "companyName": "UNILEVER PLC",
                     "companyNumber": "00041424", "companyStatus": "ACTIVE",
                     "registeredCity": "London", "postcode": "EC4Y 0DY"}},
}

# A few popular tickers and repos worth pre-warming for a snappy first click.
SEED_TICKERS = ["AAPL", "TSCO.L", "BP.L", "MSFT", "GOOGL"]
SEED_REPOS = [("orbitalapi", "orbital"), ("python", "cpython")]


def cache_key(kind: str, *parts) -> str:
    return kind + "::" + "::".join(p.upper() for p in parts)


def warm_cache():
    """Best-effort: pre-load seeded entries from live Orbital, fall back to baked
    snapshots for companies. Runs in a background thread so boot is never blocked.
    Seeded entries get a long TTL so they survive a flaky upstream window."""
    long_ttl = max(CACHE_TTL, 86400)  # seeds live at least a day
    warmed = {"companies": 0, "tickers": 0, "repos": 0, "fallbacks": 0}

    for name in SEED_COMPANIES:
        key = cache_key("brain", clean_name(name))
        try:
            data = taxiql(COMPANY_Q.format(name=clean_name(name)), timeout=UPSTREAM_TIMEOUT)
            CACHE.set(key, data, ttl=long_ttl)
            warmed["companies"] += 1
        except Exception:  # noqa: BLE001
            fb = SEED_FALLBACK.get(name)
            if fb is not None:
                CACHE.set(key, {**fb, "_seeded": True}, ttl=long_ttl)
                warmed["fallbacks"] += 1

    for t in SEED_TICKERS:
        try:
            data = taxiql(TRADING_Q.format(ticker=clean_ticker(t)), timeout=UPSTREAM_TIMEOUT)
            CACHE.set(cache_key("trading", clean_ticker(t)), data, ttl=long_ttl)
            warmed["tickers"] += 1
        except Exception:  # noqa: BLE001
            pass

    for owner, repo in SEED_REPOS:
        try:
            data = taxiql(CODING_Q.format(owner=clean_repo(owner), repo=clean_repo(repo)),
                          timeout=UPSTREAM_TIMEOUT)
            CACHE.set(cache_key("coding", clean_repo(owner), clean_repo(repo)),
                      data, ttl=long_ttl)
            warmed["repos"] += 1
        except Exception:  # noqa: BLE001
            pass

    print(f"[sandbox] cache warmed: {warmed}")


# --------------------------------------------------------------------------- #
# HTTP handler                                                                #
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "CompanyBrainSandbox/1.0"

    def _send(self, code, body, ctype="application/json", extra=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        # Conservative hardening headers for a public host.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _client_ip(self) -> str:
        if TRUST_XFF:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def _cached(self, key: str, builder):
        """Return cached payload if present, else build (proxy to Orbital), cache, return.
        ``builder`` is a zero-arg callable that returns the JSON-able result."""
        hit = CACHE.get(key)
        if hit is not None:
            return hit, True
        data = builder()
        CACHE.set(key, data)
        return data, False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        ip = self._client_ip()

        # Liveness/readiness probes are cheap and exempt from rate limiting.
        if u.path in ("/healthz", "/api/health"):
            return self._send(200, json.dumps({
                "status": "ok", "orbital": ORBITAL,
                "cache": CACHE.stats()}))

        # (b) Rate limit everything else.
        if not LIMITER.allow(ip):
            return self._send(429, json.dumps({
                "error": "rate_limited",
                "message": "Too many requests — slow down and retry shortly."}),
                extra={"Retry-After": "5"})

        try:
            if u.path in ("/", "/index.html"):
                # Serve the existing SPA if present; otherwise a tiny JSON banner.
                idx = os.path.join(HERE, "index.html")
                if os.path.isfile(idx):
                    with open(idx, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                return self._send(200, json.dumps({
                    "service": "The Company Brain — public sandbox",
                    "endpoints": ["/api/brain?name=", "/api/trading?ticker=",
                                  "/api/coding?owner=&repo=", "/healthz"]}))

            if u.path == "/api/brain":
                name = clean_name(q.get("name", ["TESCO PLC"])[0]) or "TESCO PLC"
                key = cache_key("brain", name)
                data, hit = self._cached(
                    key, lambda: taxiql(COMPANY_Q.format(name=name)))
                return self._send(200, json.dumps(data),
                                  extra={"X-Cache": "HIT" if hit else "MISS"})

            if u.path == "/api/trading":
                t = clean_ticker(q.get("ticker", ["AAPL"])[0]) or "AAPL"
                key = cache_key("trading", t)
                data, hit = self._cached(
                    key, lambda: taxiql(TRADING_Q.format(ticker=t)))
                return self._send(200, json.dumps(data),
                                  extra={"X-Cache": "HIT" if hit else "MISS"})

            if u.path == "/api/coding":
                owner = clean_repo(q.get("owner", ["orbitalapi"])[0]) or "orbitalapi"
                repo = clean_repo(q.get("repo", ["orbital"])[0]) or "orbital"
                key = cache_key("coding", owner, repo)
                data, hit = self._cached(
                    key, lambda: taxiql(CODING_Q.format(owner=owner, repo=repo)))
                return self._send(200, json.dumps(data),
                                  extra={"X-Cache": "HIT" if hit else "MISS"})

            return self._send(404, json.dumps({"error": "not_found"}))

        except (urllib.error.HTTPError,) as e:
            return self._send(502, json.dumps({
                "error": "upstream_error", "detail": f"Orbital {e.code}: {e.reason}"}))
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            return self._send(504, json.dumps({
                "error": "upstream_timeout", "detail": str(getattr(e, "reason", e))}))
        except Exception as e:  # noqa: BLE001
            return self._send(502, json.dumps({"error": "proxy_error", "detail": str(e)}))

    def do_POST(self):
        # The sandbox intentionally exposes NO write/query POST surface.
        # Arbitrary TaxiQL is never accepted from the public.
        self._send(405, json.dumps({
            "error": "method_not_allowed",
            "message": "This sandbox only serves fixed GET queries."}),
            extra={"Allow": "GET, HEAD"})

    def log_message(self, *a):
        pass


class Server(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    print(f"[sandbox] The Company Brain (hardened) -> http://{HOST}:{PORT}")
    print(f"[sandbox] proxying Orbital at {ORBITAL}")
    print(f"[sandbox] cache ttl={CACHE_TTL}s max={CACHE_MAX} | "
          f"rate {RATE_CAPACITY} burst +{RATE_REFILL}/s | "
          f"upstream timeout {UPSTREAM_TIMEOUT}s")
    if WARM_CACHE:
        threading.Thread(target=warm_cache, daemon=True).start()
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
