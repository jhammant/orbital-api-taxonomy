#!/usr/bin/env python3
"""HONEST sanctions / PEP screening microservice — real free public lists, no fakes.

Screens a name against REAL, FREE, unauthenticated public sanctions lists loaded
into memory: the US OFAC SDN list (+ its alias file), and best-effort the UK OFSI
consolidated list and the EU consolidated list. Nothing is ever invented — if a
list cannot be fetched it is reported as unavailable and simply not screened.

Matching is deliberately CONSERVATIVE so legitimate companies do not false-positive:
a hit needs an exact normalised match, or a difflib ratio >= 0.90 against a listed
name/alias, after stripping punctuation and company suffixes (LTD/PLC/INC/...).

    python3 demo/services/sanctions_service.py        # binds 0.0.0.0:8918

    GET /sanctions?q=NAME   (also ?name=NAME)  -> JSON screening result
    GET /health                                -> {"ok": true, "warm": <bool>, ...}
"""
import csv, datetime, difflib, io, json, os, re, socketserver, ssl, threading, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("SANCTIONS_HOST", "0.0.0.0")
PORT = int(os.environ.get("SANCTIONS_PORT", "8918"))
HTTP_TIMEOUT = int(os.environ.get("SANCTIONS_HTTP_TIMEOUT", "90"))
MATCH_RATIO = float(os.environ.get("SANCTIONS_MATCH_RATIO", "0.90"))
MAX_HITS = 10
UA = "Mozilla/5.0 (orbital-sanctions-demo; +stdlib-urllib)"

# --- Real, free, unauthenticated public source URLs (verified to download CSV) ---
# treasury.gov 302-redirects to a signed S3 object; urllib follows the redirect.
OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
OFAC_ALT_URL = "https://www.treasury.gov/ofac/downloads/alt.csv"
# UK OFSI consolidated list (HM Treasury) — public Azure blob, "2022 format" CSV.
OFSI_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"
# EU consolidated financial sanctions list — public FSF distribution (public token).
EU_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
          "csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw")

# Company suffixes / corporate noise stripped during normalisation to cut false hits.
_SUFFIXES = (r"LIMITED", r"LTD", r"PLC", r"LLP", r"LLC", r"INC", r"INCORPORATED",
             r"CORP", r"CORPORATION", r"COMPANY", r"CO", r"GMBH", r"SA", r"AG",
             r"NV", r"BV", r"PTE", r"PTY", r"SARL", r"SRL", r"SPA", r"OOO", r"AO",
             r"PAO", r"ZAO", r"JSC", r"OJSC", r"CJSC", r"GROUP", r"HOLDINGS", r"HOLDING")
_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(_SUFFIXES) + r")\b")
_NONALNUM_RE = re.compile(r"[^A-Z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize(name):
    """Uppercase, drop punctuation + company suffixes, collapse whitespace."""
    s = (name or "").upper()
    s = s.replace("&", " AND ")
    s = _NONALNUM_RE.sub(" ", s)
    s = _SUFFIX_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _fetch(url):
    """GET a URL with stdlib urllib, following redirects; returns decoded text."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
        raw = r.read()
    # OFAC/OFSI are latin-1-ish; EU is UTF-8 (BOM). latin-1 never raises and keeps bytes 1:1.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# ---------------------------------------------------------------------------
# Each loader returns (entries, as_of) where entries is a list of dicts:
#   {"name": <listed name/alias>, "norm": <normalised>, "program": <str>}
# and as_of is the list's own date/version string when discoverable.
# A loader that fails returns ([], None) — the list is then marked unavailable.
# ---------------------------------------------------------------------------

def load_ofac():
    """US OFAC SDN primary names (col 1) + programs (col 3), plus aliases from alt.csv.

    SDN.CSV  : ent_num, NAME, type, program, ...
    ALT.CSV  : ent_num, alt_num, alt_type("aka"), alt_name, remarks
    Aliases inherit their entity's program/primary-name via ent_num.
    """
    sdn = _fetch(OFAC_SDN_URL)
    entries, by_ent = [], {}
    for row in csv.reader(io.StringIO(sdn)):
        if len(row) < 4:
            continue
        ent, name, prog = row[0].strip(), row[1].strip(), row[3].strip()
        if not name or name == "-0-":
            continue
        prog = "" if prog == "-0-" else prog
        nrm = normalize(name)
        if nrm:
            entries.append({"name": name, "norm": nrm, "program": prog})
            by_ent[ent] = prog
    # Aliases (best-effort; SDN alone is acceptable if this fails).
    try:
        alt = _fetch(OFAC_ALT_URL)
        for row in csv.reader(io.StringIO(alt)):
            if len(row) < 4:
                continue
            ent, alt_name = row[0].strip(), row[3].strip()
            if not alt_name or alt_name == "-0-":
                continue
            nrm = normalize(alt_name)
            if nrm:
                entries.append({"name": alt_name, "norm": nrm, "program": by_ent.get(ent, "")})
    except Exception as e:
        print(f"[sanctions] OFAC alt.csv unavailable ({e}); using primary SDN names only")
    return entries, _today()


def load_ofsi():
    """UK OFSI consolidated list. Line 1 is 'Last Updated,<date>'; header on line 2.

    Name is built from 'Name 1'..'Name 6' (Name 1 first, Name 6 is the family name).
    Regime (col 'Regime') is used as the program; each row is one name/alias variant.
    """
    text = _fetch(OFSI_URL)
    lines = text.splitlines()
    as_of = _today()
    if lines and lines[0].lower().startswith("last updated"):
        parts = next(csv.reader([lines[0]]))
        if len(parts) > 1 and parts[1].strip():
            as_of = parts[1].strip()
        lines = lines[1:]
    rdr = csv.DictReader(io.StringIO("\n".join(lines)))
    entries = []
    for d in rdr:
        parts = [(d.get(f"Name {i}") or "").strip() for i in (1, 2, 3, 4, 5, 6)]
        name = " ".join(p for p in parts if p)
        if not name:
            continue
        nrm = normalize(name)
        if nrm:
            entries.append({"name": name, "norm": nrm, "program": (d.get("Regime") or "").strip()})
    return entries, as_of


def load_eu():
    """EU consolidated financial sanctions list (semicolon-delimited).

    One row per name-alias; 'NameAlias_WholeName' is the display name and
    'Entity_Regulation_Programme' is the programme code.
    """
    text = _fetch(EU_URL)
    rdr = csv.DictReader(io.StringIO(text), delimiter=";")
    entries, seen = [], set()
    for d in rdr:
        name = (d.get("NameAlias_WholeName") or "").strip()
        if not name:
            continue
        prog = (d.get("Entity_Regulation_Programme") or "").strip()
        nrm = normalize(name)
        key = (nrm, prog)
        if nrm and key not in seen:
            seen.add(key)
            entries.append({"name": name, "norm": nrm, "program": prog})
    return entries, _today()


def _today():
    return datetime.date.today().isoformat()


# (display label, loader) — order defines listsChecked order. OFAC first (must-have).
SOURCES = [
    ("US OFAC SDN", load_ofac),
    ("UK OFSI Consolidated", load_ofsi),
    ("EU Consolidated", load_eu),
]


class Lists:
    """In-memory store of loaded lists, built once (lazily or by warm thread)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.loaded = False
        self.available = []      # display labels of lists that loaded OK
        self.unavailable = []    # display labels that failed (reported, never faked)
        self.as_of = {}          # label -> as-of string
        # norm -> {"name","program","list"} for exact lookups
        self.exact = {}
        # parallel arrays for fuzzy matching
        self.norms = []
        self.meta = []           # {"name","program","list"}

    def build(self):
        with self.lock:
            if self.loaded:
                return
            available, unavailable, as_of = [], [], {}
            exact, norms, meta = {}, [], []
            for label, loader in SOURCES:
                try:
                    entries, ts = loader()
                    if not entries:
                        raise RuntimeError("no entries parsed")
                    available.append(label)
                    as_of[label] = ts
                    for e in entries:
                        m = {"name": e["name"], "program": e["program"], "list": label}
                        exact.setdefault(e["norm"], m)
                        norms.append(e["norm"])
                        meta.append(m)
                    print(f"[sanctions] loaded {label}: {len(entries):,} names (asOf {ts})")
                except Exception as ex:
                    unavailable.append(label)
                    print(f"[sanctions] {label} UNAVAILABLE — skipped, not faked ({ex})")
            self.available, self.unavailable, self.as_of = available, unavailable, as_of
            self.exact, self.norms, self.meta = exact, norms, meta
            self.loaded = True
            print(f"[sanctions] warm: {len(norms):,} total names across {len(available)} list(s)")

    def screen(self, query):
        if not self.loaded:
            self.build()
        nq = normalize(query)
        hits, seen = [], set()
        # 1) exact normalised match (fast, score 1.0)
        if nq and nq in self.exact:
            m = self.exact[nq]
            key = (m["name"], m["list"])
            seen.add(key)
            hits.append({"name": m["name"], "list": m["list"],
                         "program": m["program"], "score": 1.0})
        # 2) conservative fuzzy match (>= MATCH_RATIO) against every listed name/alias
        if nq:
            sm = difflib.SequenceMatcher()
            sm.set_seq2(nq)
            qlen = len(nq)
            for norm, m in zip(self.norms, self.meta):
                # cheap length prefilter: ratio can't reach threshold if lengths diverge a lot
                if abs(len(norm) - qlen) > qlen * 0.5 + 4:
                    continue
                sm.set_seq1(norm)
                if sm.real_quick_ratio() < MATCH_RATIO or sm.quick_ratio() < MATCH_RATIO:
                    continue
                r = sm.ratio()
                if r >= MATCH_RATIO:
                    key = (m["name"], m["list"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({"name": m["name"], "list": m["list"],
                                 "program": m["program"], "score": round(r, 4)})
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:MAX_HITS]
        as_of = self.as_of.get(self.available[0]) if self.available else _today()
        is_hit = bool(hits)
        return {
            "query": query,
            "checked": True,
            "listsChecked": list(self.available),
            "listsUnavailable": list(self.unavailable),
            "totalNames": len(self.norms),
            "status": "hit" if is_hit else "clear",
            "clear": not is_hit,
            "hitCount": len(hits),
            "hits": hits,
            "asOf": as_of or _today(),
        }


LISTS = Lists()


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._json(200, {"ok": True, "warm": LISTS.loaded,
                                    "listsChecked": list(LISTS.available),
                                    "listsUnavailable": list(LISTS.unavailable),
                                    "totalNames": len(LISTS.norms)})
        if u.path != "/sanctions":
            return self._json(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()
        if not name:
            return self._json(200, {"query": "", "checked": False, "listsChecked": list(LISTS.available),
                                    "listsUnavailable": list(LISTS.unavailable), "totalNames": len(LISTS.norms),
                                    "status": "clear", "clear": True, "hitCount": 0, "hits": [],
                                    "asOf": _today()})
        try:
            return self._json(200, LISTS.screen(name))
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _warm():
    try:
        LISTS.build()
    except Exception as e:
        print(f"[sanctions] warm thread error: {e}")


if __name__ == "__main__":
    print(f"sanctions screening -> http://{HOST}:{PORT}/sanctions?q=NAME")
    # Background warm so the first real request is fast; requests still build lazily if needed.
    threading.Thread(target=_warm, daemon=True).start()
    S((HOST, PORT), H).serve_forever()
