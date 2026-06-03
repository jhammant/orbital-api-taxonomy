#!/usr/bin/env python3
"""THE GAZETTE — official UK insolvency / winding-up / appointment notices.

The Gazette (https://www.thegazette.co.uk) is the UK's official public record of
record. Every corporate insolvency event a company is legally obliged to publish
— a winding-up petition, an administration appointment, the appointment of
liquidators, a notice to creditors, a striking-off — lands here, by law, dated
and citable. That makes it the authoritative "has this counterparty publicly
failed?" lens for the Orbital Risk demo: it complements the corpus-backed risk /
insolvency engines with the *official published notices themselves*, each with a
real, stable thegazette.co.uk URL anyone can open.

    python3 demo/services/gazette_service.py        # binds 0.0.0.0:8926

    GET /gazette?q=CARILLION
    ->  { company, noticeCount, latest:{type,date,title,url},
          notices:[{type,date,title,url}] }

HOW IT WORKS (and why it's honest): The Gazette's documented JSON endpoint
(/all-notices/notice/data.json) is currently broken upstream (returns HTTP 500),
but its sibling Atom-feed endpoint (/all-notices/notice/data.feed) works and is
the same official search. We query that feed for the company name, keep only the
*granular published notices* (dropping the Gazette's aggregated "Supplement N,
Page N" index pages), classify each by its official notice wording, and return
the insolvency / appointment notices with their canonical URLs. A name with no
such notices returns noticeCount 0 gracefully — a hit is only ever a real,
published Gazette notice, never fabricated.

Pure Python stdlib — no pip. Read-only (it only reads the public Gazette).
Designed for the Orbital gazette.taxi adapter (@HttpService host.docker.internal:8926).
"""
import json
import os
import re
import socketserver
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("GAZETTE_HOST", "0.0.0.0")
PORT = int(os.environ.get("GAZETTE_PORT", "8926"))
GAZETTE_BASE = os.environ.get(
    "GAZETTE_BASE", "https://www.thegazette.co.uk")
# The Gazette scopes search by URL path. "/insolvency/notice/..." returns ONLY
# insolvency notices (winding-up, administration, liquidator appointments, notices
# to creditors, striking-off) — so granular notices surface immediately instead of
# being buried under aggregated index pages, which is what an all-notices search
# returns for a bare company name. This is the official insolvency-notice search.
GAZETTE_FEED_PATH = os.environ.get(
    "GAZETTE_FEED_PATH", "/insolvency/notice/data.feed")
HTTP_TIMEOUT = int(os.environ.get("GAZETTE_HTTP_TIMEOUT", "20"))
# How many granular notices the feed page is scanned for / how many we return.
# 30 is the largest page size the CloudFront-fronted feed serves reliably for a
# single request; larger bursts of requests (not larger pages) are what trip it.
FEED_PAGE_SIZE = int(os.environ.get("GAZETTE_FEED_PAGE_SIZE", "30"))
MAX_NOTICES = int(os.environ.get("GAZETTE_MAX_NOTICES", "10"))
# The Gazette is fronted by CloudFront, which rate-limits *bursts* aggressively
# (HTTP 429/500/403) — a single spaced request of any page size succeeds, but
# several in quick succession trip it. So we keep volume LOW: at most 2 attempts
# with a long gap between them, plus a short-lived in-process cache so repeat/demo
# queries don't re-hit upstream at all.
HTTP_RETRIES = int(os.environ.get("GAZETTE_HTTP_RETRIES", "2"))
RETRY_BACKOFF = float(os.environ.get("GAZETTE_RETRY_BACKOFF", "8"))  # seconds between attempts
CACHE_TTL = int(os.environ.get("GAZETTE_CACHE_TTL", "900"))  # seconds (15 min)
_CACHE = {}  # name(lower) -> (epoch, result_dict)

ATOM = "http://www.w3.org/2005/Atom"
FACETS = "https://www.thegazette.co.uk/facets"
NS = {"a": ATOM, "f": FACETS}

# A plain browser-style User-Agent. (The Gazette origin blocks some non-browser UAs
# such as "python-requests/*" with HTTP 403, so we present a browser one.) Overridable.
USER_AGENT = os.environ.get(
    "GAZETTE_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Each feed <entry> carries the Gazette's official notice code inline as
# <f:notice-code> — no extra request needed. The corporate-insolvency family is the
# 24xx range; personal insolvency / bankruptcy is 25xx. This map gives an
# authoritative notice type straight from the source, far better than guessing from
# the (often empty) feed snippet. Codes verified against live Gazette notices.
NOTICE_CODE_TYPES = {
    "2401": "Meeting of creditors",
    "2402": "Notice of disclaimer",
    "2403": "Liquidation notice",
    "2405": "Appointment of administrators",
    "2406": "Members' voluntary winding-up",
    "2407": "Winding-up order (High Court)",
    "2408": "Petition to wind up (companies)",
    "2410": "Appointment of administrators",
    "2411": "Notice of intention to appoint administrators",
    "2412": "Move from administration to dissolution",
    "2413": "End of administration",
    "2431": "Members' voluntary liquidation",
    "2432": "Members' voluntary liquidation",
    "2433": "Members' voluntary liquidation",
    "2434": "Members' voluntary liquidation",
    "2435": "Members' voluntary liquidation",
    "2436": "Members' voluntary liquidation",
    "2440": "Appointment of administrators",
    "2441": "Appointment of administrators",
    "2442": "Administrator's proposals",
    "2443": "Administrator's proposals",
    "2444": "Notice of creditors' meeting",
    "2445": "Notice of administrator's progress report",
    "2446": "End of administration",
    "2447": "Notice to creditors",
    "2448": "Notice of liquidator's progress report",
    "2449": "Final meeting of creditors",
    "2450": "Resolution for winding-up",
    "2451": "Appointment of liquidators",
    "2452": "Appointment of liquidators",
    "2453": "Members' voluntary winding-up",
    "2454": "Notice of liquidator's appointment",
    "2455": "Creditors' voluntary liquidation",
    "2456": "Notice to creditors",
    "2457": "Appointment of liquidators",
    "2460": "Receivership",
    "2470": "Dissolution / striking-off",
    # Personal insolvency / bankruptcy (25xx)
    "2501": "Bankruptcy order",
    "2502": "Bankruptcy petition",
    "2503": "Bankruptcy order",
    "2510": "Individual voluntary arrangement",
}

# The Gazette returns two kinds of <entry>: real published notices (descriptive
# titles like "BHS LIMITED" / "THOMAS COOK LIQUIDATION") and aggregated index
# pages whose title is always "The <City> Gazette, Supplement|Issue N, Page N".
# We keep only the former.
INDEX_TITLE_RE = re.compile(
    r"^the\s+.+\s+gazette,\s+(supplement|issue)\s+\d+,\s+page\s+\d+\s*$", re.I)

# Classify an official notice from its title + body wording. Ordered: the first
# match wins, most-specific first. Labels mirror the Gazette's own notice families
# (Insolvency Act / corporate-insolvency notice codes 24xx/25xx and personal 27xx).
NOTICE_TYPES = [
    ("Winding-up petition",
     r"winding[\s-]*up\s+petition|petition\s+to\s+wind\s+up|petition\s+for\s+winding[\s-]*up"),
    ("Winding-up order",
     r"winding[\s-]*up\s+order|order\s+to\s+wind\s+up|order\s+for\s+winding[\s-]*up"),
    ("Appointment of administrators",
     r"appointment\s+of\s+an?\s+administrator|administrator[s]?\s+(?:was|were|has\s+been|have\s+been)\s+appointed|notice\s+of\s+administrator"),
    ("Administration order",
     r"administration\s+order|in\s+administration|enter(?:ed|ing)?\s+administration"),
    ("Appointment of liquidators",
     r"appointment\s+of\s+(?:a\s+)?liquidator|liquidator[s]?\s+(?:was|were|has\s+been|have\s+been)\s+appointed"),
    ("Members' voluntary liquidation",
     r"members[''’]?\s+voluntary\s+(?:liquidation|winding[\s-]*up)|mvl\b"),
    ("Creditors' voluntary liquidation",
     r"creditors[''’]?\s+voluntary\s+(?:liquidation|winding[\s-]*up)|cvl\b"),
    ("Notice to creditors",
     r"notice\s+to\s+creditors|to\s+all\s+creditors|meeting\s+of\s+creditors|claim\s+against\b|prove\s+(?:their|your)\s+debt"),
    ("Company voluntary arrangement",
     r"voluntary\s+arrangement|\bcva\b"),
    ("Receivership",
     r"receiver(?:ship)?\s+appointed|appointment\s+of\s+(?:a\s+)?receiver|administrative\s+receiver"),
    ("Dissolution / striking-off",
     r"strik(?:e|ing)[\s-]*off|struck\s+off|dissolution|notice\s+to\s+be\s+struck\s+off|will\s+be\s+dissolved"),
    ("Final meeting / release of liquidator",
     r"final\s+meeting|release\s+of\s+(?:the\s+)?liquidator|completion\s+of\s+winding[\s-]*up"),
    ("Liquidation notice",
     r"liquidation|liquidat(?:ed|ing)|insolven"),
]
_COMPILED = [(label, re.compile(pat, re.I)) for label, pat in NOTICE_TYPES]

# A notice "counts" toward the insolvency lens only if its wording clearly relates
# to insolvency / failure / formal appointments. Plain trustee/probate notices that
# merely *mention* a company are excluded so noticeCount stays meaningful.
#
# Note: The Gazette's feed <content> is a short, often truncated snippet, so the
# explicit keyword may be cut off. We therefore ALSO treat the standard heading of
# a UK corporate-insolvency notice as a positive signal — in The Gazette, notices
# headed "In the High Court of Justice ... Companies Court" (or the Insolvency-Rules
# "No NNNNN of YYYY" case-number form) are winding-up / administration proceedings.
INSOLVENCY_SIGNAL = re.compile(
    r"insolven|liquidat|winding[\s-]*up|administrat|receiver|creditor|"
    r"voluntary\s+arrangement|strik(?:e|ing)[\s-]*off|struck\s+off|dissolv|"
    r"bankrupt|company\s+voluntary|\bcva\b|\bcvl\b|\bmvl\b|"
    r"companies\s+court|petition|sequestrat", re.I)

# Strong structural marker of a corporate-insolvency court notice when the snippet
# is too short to contain an explicit keyword: High Court / Companies Court + an
# insolvency case-number ("No 002220 of 2016" / "No NNNN - CR of YYYY").
COURT_INSOLVENCY_SIGNAL = re.compile(
    r"high\s+court\s+of\s+justice.*?(?:companies\s+court|chancery|"
    r"no\.?\s*\d{3,}\s*(?:-\s*cr)?\s+of\s+\d{4})", re.I | re.S)


def _classify(title, body, notice_code=None):
    """Return the official notice type. Prefer the Gazette's own notice code
    (authoritative, carried inline in the feed); fall back to text classification.
    """
    if notice_code and notice_code in NOTICE_CODE_TYPES:
        return NOTICE_CODE_TYPES[notice_code]
    blob = ((title or "") + " \n " + (body or "")).strip()
    for label, rx in _COMPILED:
        if rx.search(blob):
            return label
    # Snippet too short for an explicit keyword, but it's a Companies-Court matter.
    if COURT_INSOLVENCY_SIGNAL.search(blob):
        return "Insolvency court notice"
    # A 24xx code we don't have a precise label for is still corporate insolvency.
    if notice_code and notice_code.startswith("24"):
        return "Corporate insolvency notice"
    if notice_code and notice_code.startswith("25"):
        return "Personal insolvency notice"
    return "Insolvency notice"


def _is_insolvency(title, body):
    blob = (title or "") + " " + (body or "")
    return bool(INSOLVENCY_SIGNAL.search(blob)
                or COURT_INSOLVENCY_SIGNAL.search(blob))


def _abs_url(href):
    """Make a notice URL absolute and canonical (the public /notice/<id> page)."""
    if not href:
        return None
    if href.startswith("http"):
        url = href
    else:
        url = GAZETTE_BASE.rstrip("/") + "/" + href.lstrip("/")
    # Prefer the clean human page over /id/notice/ or per-format data.* variants.
    url = url.replace("/id/notice/", "/notice/")
    url = re.sub(r"/data\.[a-z]+(\?.*)?$", "", url)
    return url


def _feed_url(name, page_size):
    # Exact-phrase quoting focuses the search on the company name.
    qs = urllib.parse.urlencode({
        "text": '"%s"' % name.strip(),
        "results-page-size": str(page_size),
    })
    return GAZETTE_BASE.rstrip("/") + GAZETTE_FEED_PATH + "?" + qs


def _fetch_feed(name):
    """GET the Gazette Atom search feed for a company name. Returns raw XML bytes.

    CloudFront rate-limits bursts, so on a transient 429/5xx we wait a long fixed
    gap and try ONCE more (page size held constant — size isn't the trigger, burst
    rate is). Raises the last urllib.error.* / OSError only if every attempt fails.
    """
    import time
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(max(1, HTTP_RETRIES)):
        # NB: send NO restrictive Accept header. The .feed URL extension already
        # selects the Atom format, and the Gazette origin returns HTTP 500 if an
        # "application/atom+xml" Accept is sent (its content-negotiation path is
        # broken, the same way /data.json 500s). "*/*" keeps it on the .feed path.
        req = urllib.request.Request(_feed_url(name, FEED_PAGE_SIZE), headers={
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 = rate-limited, 5xx = transient CloudFront/ELB faults: retry once
            # after a long gap. Other 4xx (e.g. 403 UA-block) won't fix themselves.
            if e.code not in (429, 500, 502, 503, 504):
                raise
            ra = e.headers.get("Retry-After") if e.headers else None
            wait = int(ra) if (ra and str(ra).isdigit()) else RETRY_BACKOFF
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            wait = RETRY_BACKOFF
        if attempt < max(1, HTTP_RETRIES) - 1:
            time.sleep(min(15, wait))
    if last_err:
        raise last_err
    return b""


def _entry_url(entry):
    """Best canonical URL for an <entry>: the self link, else the id."""
    self_href = None
    plain_href = None
    for ln in entry.findall("a:link", NS):
        rel = ln.get("rel")
        href = ln.get("href")
        if rel == "self" and self_href is None:
            self_href = href
        elif rel is None and plain_href is None:
            plain_href = href
    nid = entry.findtext("a:id", default="", namespaces=NS)
    return _abs_url(plain_href or self_href or nid)


def _parse(xml_bytes, name):
    """Parse the feed into the granular insolvency notices for `name`."""
    notices = []
    if not xml_bytes:
        return notices
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return notices

    # Significant tokens of the searched name (drop common company suffixes/noise so
    # "BHS LIMITED" matches a "BHS LIMITED" notice and "CARILLION CONSTRUCTION LTD"
    # still matches "Carillion Construction Limited").
    STOP = {"LIMITED", "LTD", "PLC", "LLP", "LP", "COMPANY", "CO", "GROUP",
            "HOLDINGS", "THE", "UK", "AND"}
    raw_tokens = [t for t in re.split(r"\W+", (name or "").upper()) if len(t) > 2]
    name_tokens = [t for t in raw_tokens if t not in STOP] or raw_tokens
    name_norm = re.sub(r"[^A-Z0-9]", "", (name or "").upper())

    for entry in root.findall("a:entry", NS):
        title = (entry.findtext("a:title", default="", namespaces=NS) or "").strip()
        if not title or INDEX_TITLE_RE.match(title):
            continue  # drop the Gazette's aggregated index pages

        content_el = entry.find("a:content", NS)
        body = ("".join(content_el.itertext()) if content_el is not None else "").strip()
        body = re.sub(r"\s+", " ", body)

        hay = (title + " " + body).upper()
        hay_norm = re.sub(r"[^A-Z0-9]", "", hay)

        # This is an insolvency-scoped feed, so every result is already an insolvency
        # notice. The job here is precision: keep only notices that actually reference
        # the searched company. Accept if the full name appears, or all significant
        # name tokens appear (covers empty/truncated snippets where only the title —
        # itself the company name — carries the match).
        if name_tokens:
            full_match = name_norm and name_norm in hay_norm
            all_tokens = all(tok in hay for tok in name_tokens)
            if not (full_match or all_tokens):
                continue
        # Safety net: if the scoped feed is ever swapped for the all-notices path,
        # still require an insolvency signal so noticeCount stays meaningful.
        if "/insolvency/" not in GAZETTE_FEED_PATH and not _is_insolvency(title, body):
            continue

        published = (entry.findtext("a:published", default="", namespaces=NS) or "")
        date = published[:10] if published else (
            entry.findtext("a:updated", default="", namespaces=NS) or "")[:10]

        notice_code = (entry.findtext("f:notice-code", default="", namespaces=NS) or "").strip()

        notices.append({
            "type": _classify(title, body, notice_code),
            "date": date or None,
            "title": title,
            "url": _entry_url(entry),
        })

    # Newest first, then cap.
    notices.sort(key=lambda n: (n.get("date") or ""), reverse=True)
    return notices[:MAX_NOTICES]


def build(name):
    """Assemble the Gazette notice profile for a company name (best-effort).

    Successful results are cached for CACHE_TTL so the demo can be re-run without
    re-hitting CloudFront; the upstream-unavailable fallback is NOT cached so it
    self-heals on the next call.
    """
    import time
    key = name.strip().lower()
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return hit[1]

    try:
        xml_bytes = _fetch_feed(name)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # Upstream unreachable -> honest empty result, never crash the router.
        # If we have a (stale) cached success, prefer it over an empty answer.
        if hit:
            return hit[1]
        return empty(name, reason="gazette upstream unavailable")

    notices = _parse(xml_bytes, name)
    result = {
        "company": name,
        "source": "The Gazette (official public record) — thegazette.co.uk",
        "noticeCount": len(notices),
        "latest": notices[0] if notices else None,
        "notices": notices,
    }
    _CACHE[key] = (time.time(), result)
    return result


def empty(name, reason=None):
    """Uniform zero-notices response. Always HTTP 200, never a crash."""
    out = {
        "company": name,
        "source": "The Gazette (official public record) — thegazette.co.uk",
        "noticeCount": 0,
        "latest": None,
        "notices": [],
    }
    if reason:
        out["reason"] = reason
    return out


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
            return self._send(200, {"ok": True, "base": GAZETTE_BASE})
        if u.path != "/gazette":
            return self._send(404, {"error": "not found"})

        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0]
                or q.get("company", [""])[0] or "").strip()
        if not name:
            return self._send(200, empty("", reason="provide ?q=<company name>"))
        try:
            return self._send(200, build(name))
        except Exception as e:  # noqa: BLE001 — never break the router
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"Gazette notices -> http://{HOST}:{PORT}/gazette?q=CARILLION")
    print(f"  upstream: {GAZETTE_BASE}/all-notices/notice/data.feed")
    Server((HOST, PORT), Handler).serve_forever()
