#!/usr/bin/env python3
"""GLOBAL MEDIA + SENTIMENT — one company name -> worldwide news volume + tone, from GDELT.

Queries the FREE, no-auth GDELT DOC 2.0 API (https://api.gdeltproject.org) for a
company name and returns the global-media lens a UK-registry / corpus lookup never
has: how loudly the world's news is talking about this entity right now, whether
that coverage is adverse (average tone — negative = adverse media), and a handful
of representative recent headlines with their source domains. This is the
"what is the planet saying about them today" signal for the Orbital Risk demo,
complementing the corpus-backed risk / insolvency / sanctions verticals.

GDELT is rate-limited (~1 request / 5s on shared IPs, and stricter under load), so
this service is built to be a GOOD CITIZEN and to NEVER break the router:

  * Per-company response cache (~60s TTL) so repeat lookups cost zero calls.
  * A single global throttle lock spaces *all* outbound GDELT calls >= MIN_SPACING
    seconds apart, regardless of how many companies are queried concurrently.
  * Every upstream failure (HTTP 429 rate-limit, timeout, empty body, bad JSON)
    degrades gracefully: the lookup still returns HTTP 200 with articleCount 0 and
    avgTone null, never a crash and never a blocked router thread.

Two upstream calls are made per cold company (then cached):
  1. mode=artlist  -> article volume (articleCount) + the sample headlines.
  2. mode=tonechart -> the global tone distribution, from which avgTone is the
     count-weighted mean of the tone bins (negative = adverse). If tonechart is
     rate-limited/unavailable, avgTone falls back to null and articleCount/sample
     are still served from the artlist call.

    python3 demo/services/gdelt_service.py        # binds 0.0.0.0:8928

    GET /gdelt?q=BARCLAYS%20BANK%20PLC
    ->  { company, articleCount:int, avgTone:number|null (negative = adverse),
          sample:[{title, domain, date, url}], available:bool }

Pure Python stdlib — no pip. Designed for the Orbital gdelt.taxi adapter
(namespace brain.gdelt, @HttpService host.docker.internal:8928). avgTone and
articleCount are emitted as numbers so they map onto Decimal semantic types.
"""
import json
import os
import socketserver
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("GDELT_HOST", "0.0.0.0")
PORT = int(os.environ.get("GDELT_PORT", "8928"))
GDELT_BASE = os.environ.get(
    "GDELT_BASE", "https://api.gdeltproject.org/api/v2/doc/doc")
HTTP_TIMEOUT = int(os.environ.get("GDELT_HTTP_TIMEOUT", "20"))
# GDELT asks for <= 1 request / 5s, but in practice (shared IP, under load) it 429s
# unless calls are spaced considerably wider — observed safe gap is ~15-20s. We space
# ALL outbound calls at least this far apart; tune down via env on a quiet IP.
MIN_SPACING = float(os.environ.get("GDELT_MIN_SPACING", "16.0"))
# On an HTTP 429 (rate-limit), retry up to this many times, waiting MIN_SPACING between.
MAX_RETRIES = int(os.environ.get("GDELT_MAX_RETRIES", "3"))
# The tone (tonechart) call is best-effort and costs a 2nd upstream slot; set to "0" to
# skip it entirely (articleCount + sample still served from the single artlist call).
FETCH_TONE = os.environ.get("GDELT_FETCH_TONE", "1") not in ("0", "false", "no")
# Per-company response cache TTL — repeat lookups inside this window cost zero calls.
CACHE_TTL = int(os.environ.get("GDELT_CACHE_TTL", "60"))
# A *failed/degraded* lookup is cached only briefly so a transient 429 can't pin a
# false articleCount-0 for the full TTL, while still shielding GDELT from hammering.
FAIL_CACHE_TTL = int(os.environ.get("GDELT_FAIL_CACHE_TTL", "8"))
MAX_RECORDS = int(os.environ.get("GDELT_MAX_RECORDS", "10"))   # artlist sample size
SAMPLE_SIZE = int(os.environ.get("GDELT_SAMPLE_SIZE", "5"))    # headlines we return
# Recency window for both volume and tone (GDELT timespan syntax, e.g. 1w, 2w, 1d).
TIMESPAN = os.environ.get("GDELT_TIMESPAN", "2w")
UA = "orbital-gdelt-demo/1.0 (global media + sentiment; jhammant@gmail.com)"

# --- global outbound throttle (one shared lock for all companies/threads) ---
_throttle_lock = threading.Lock()
_last_call_at = [0.0]   # mutable holder so the lock-guarded closure can update it

# --- per-company response cache: name(lower) -> (expires_epoch, payload) ---
_cache = {}
_cache_lock = threading.Lock()

# --- single-flight: one in-progress GDELT fetch per company name at a time, so
#     concurrent/overlapping requests for the same company share one set of upstream
#     calls instead of each firing their own (which would multiply rate-limit pressure).
_inflight = {}
_inflight_lock = threading.Lock()


def _key_lock(key):
    """Return the per-key lock for `key`, creating it once under the registry lock."""
    with _inflight_lock:
        lk = _inflight.get(key)
        if lk is None:
            lk = threading.Lock()
            _inflight[key] = lk
        return lk


def _gdelt_call_once(req, ctx):
    """One spaced GDELT call under the global throttle. Returns (raw_text, transient).

    transient=True means a rate-limit/network/timeout error worth retrying;
    transient=False means we got a genuine response body (which may legitimately be
    an empty result). The throttle lock both serialises calls and enforces
    >= MIN_SPACING between them across all threads.
    """
    with _throttle_lock:
        wait = MIN_SPACING - (time.time() - _last_call_at[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
                return r.read().decode("utf-8", "replace").strip(), False
        except urllib.error.HTTPError as e:
            # 429 = rate-limit -> transient/retryable. Other HTTP codes are terminal.
            return "", (e.code == 429 or e.code >= 500)
        except (urllib.error.URLError, TimeoutError, OSError):
            return "", True   # network/timeout -> retryable
        except Exception:
            return "", False
        finally:
            _last_call_at[0] = time.time()


def _gdelt_get(params):
    """GET the GDELT DOC API with global spacing + 429 retry. Returns (data, ok).

    ok=True  -> data is a parsed JSON object reflecting GDELT's real answer (which
                may be an empty result set, which is itself valid and cacheable).
    ok=False -> a transient failure (rate-limit/timeout) we could not get past; the
                caller should degrade gracefully AND avoid long-caching the zero.
    Callers never crash regardless.
    """
    url = GDELT_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    ctx = ssl.create_default_context()
    raw, transient = "", False
    for attempt in range(MAX_RETRIES + 1):
        raw, transient = _gdelt_call_once(req, ctx)
        if raw or not transient:
            break  # got a body, or a terminal (non-retryable) failure
        # else: transient with empty body -> loop spaces the next attempt by MIN_SPACING
    if not raw or not raw.startswith(("{", "[")):
        # GDELT serves rate-limit / error notices as plain text, not JSON.
        return None, (not transient)
    try:
        return json.loads(raw), True
    except (ValueError, TypeError):
        return None, False


def _fetch_articles(name):
    """artlist mode -> (articleCount:int, sample:[{title,domain,date,url}], ok:bool).

    articleCount is the number of matched articles GDELT returned for the window
    (capped by maxrecords). ok=False signals a transient upstream failure (so the
    caller short-caches rather than pinning a false zero); ok=True with count 0 is a
    genuine "no coverage" result.
    """
    data, ok = _gdelt_get({
        "query": f'"{name}"',
        "mode": "artlist",
        "maxrecords": MAX_RECORDS,
        "timespan": TIMESPAN,
        "format": "json",
        "sort": "datedesc",
    })
    if not isinstance(data, dict):
        return 0, [], ok
    arts = data.get("articles") or []
    if not isinstance(arts, list):
        return 0, [], ok
    sample = []
    for a in arts[:SAMPLE_SIZE]:
        if not isinstance(a, dict):
            continue
        sample.append({
            "title": (a.get("title") or "").strip(),
            "domain": a.get("domain") or "",
            "date": a.get("seendate") or "",
            "url": a.get("url") or "",
        })
    return len(arts), sample, True


def _fetch_tone(name):
    """tonechart mode -> avgTone:float|None (count-weighted mean of the tone bins).

    GDELT tonechart returns {"tonechart":[{"bin":<tone bucket>,"count":<n>,...}]}.
    avgTone = sum(bin*count)/sum(count); negative = adverse coverage overall.
    Returns None if tonechart is rate-limited/unavailable/empty (so the caller can
    still serve articleCount + sample from the artlist call).
    """
    data, _ok = _gdelt_get({
        "query": f'"{name}"',
        "mode": "tonechart",
        "timespan": TIMESPAN,
        "format": "json",
    })
    if not isinstance(data, dict):
        return None
    bins = data.get("tonechart") or []
    if not isinstance(bins, list) or not bins:
        return None
    weighted, total = 0.0, 0
    for b in bins:
        if not isinstance(b, dict):
            continue
        # GDELT labels the tone bucket "bin"; tolerate "tone" as an alias just in case.
        raw_tone = b.get("bin")
        if raw_tone is None:
            raw_tone = b.get("tone")
        try:
            tone = float(raw_tone)
            count = int(b.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        weighted += tone * count
        total += count
    if total <= 0:
        return None
    return round(weighted / total, 3)


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _cache_get_last_good(key):
    """Return a previously-cached *available* payload regardless of TTL, if any.

    Lets a transient rate-limit fall back to the last real answer for this company
    instead of blanking it to articleCount 0 — far better for the demo."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[1].get("available"):
            return hit[1]
    return None


def assess(name):
    """Build the GDELT global-media payload for a company name (uses/refreshes cache).

    Single-flighted per company: only one upstream fetch runs at a time for a given
    name; everyone else waits and then reads the just-filled cache. The cache TTL is
    stamped at *completion* (not start) so a slow cold fetch still yields a usable warm
    window, and a transient failure preserves the last good answer when one exists.
    """
    key = name.lower()
    fresh = _cache_get(key)
    if fresh is not None:
        return fresh

    # Serialise concurrent cold lookups for the SAME company behind one fetch.
    with _key_lock(key):
        # Re-check: another thread may have filled the cache while we waited.
        fresh = _cache_get(key)
        if fresh is not None:
            return fresh

        article_count, sample, ok = _fetch_articles(name)
        # Only spend the second (tone) call when enabled AND there's coverage to weigh.
        avg_tone = _fetch_tone(name) if (FETCH_TONE and article_count > 0) else None
        payload = {
            "company": name,
            "articleCount": int(article_count),
            "avgTone": avg_tone,                   # number|null — negative = adverse
            "sample": sample,
            "available": article_count > 0,
            "source": "GDELT DOC 2.0",
            "window": TIMESPAN,
        }
        if not ok and article_count == 0:
            # Transient upstream failure (e.g. 429). Prefer the last real answer for
            # this company if we have one; otherwise serve a brief, honest degrade.
            last_good = _cache_get_last_good(key)
            if last_good is not None:
                stale = dict(last_good)
                stale["stale"] = True
                stale["reason"] = "GDELT rate-limited; showing last cached result"
                payload, ttl = stale, FAIL_CACHE_TTL
            else:
                payload["reason"] = ("GDELT temporarily unavailable (rate-limited); "
                                     "try again shortly")
                ttl = FAIL_CACHE_TTL
        else:
            ttl = CACHE_TTL
        # Stamp TTL from completion time so a slow cold fetch still has a warm window.
        with _cache_lock:
            _cache[key] = (time.time() + ttl, payload)
        return payload


def empty(name, reason="no global media coverage found"):
    """Uniform graceful-degrade payload — always HTTP 200, never a crash."""
    return {
        "company": name,
        "articleCount": 0,
        "avgTone": None,
        "sample": [],
        "available": False,
        "reason": reason,
        "source": "GDELT DOC 2.0",
        "window": TIMESPAN,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "base": GDELT_BASE,
                                    "cached": len(_cache)})
        if u.path != "/gdelt":
            return self._send(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()[:120]
        if not name:
            return self._send(200, empty("", reason="q is required"))
        try:
            return self._send(200, assess(name))
        except Exception as e:  # noqa: BLE001 — never break the router
            return self._send(200, empty(name, reason=f"upstream error: {e}"))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"GDELT global media + sentiment -> http://{HOST}:{PORT}/gdelt?q=BARCLAYS%20BANK%20PLC")
    print(f"  upstream: {GDELT_BASE}  window={TIMESPAN}  spacing>={MIN_SPACING}s  cacheTTL={CACHE_TTL}s")
    Server((HOST, PORT), Handler).serve_forever()
