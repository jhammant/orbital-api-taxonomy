#!/usr/bin/env python3
"""UPWARD OWNERSHIP / UBO microservice — real GLEIF Level-2 relationships, no auth.

Answers the question a single company lookup never does: who ULTIMATELY owns this
company? Resolves a UK/global company NAME to its GLEIF Legal Entity Identifier
(LEI), then walks the free, public GLEIF relationship graph to surface its direct
parent, ultimate parent (the UBO entity at the top of the group), and how many
subsidiaries it controls. Everything returned is a genuine GLEIF record — if a
relationship does not exist GLEIF returns 404 and we report it honestly as null.

All data comes from https://api.gleif.org (the official Global LEI Foundation API,
free, unauthenticated). No corpus is touched; nothing is invented.

    python3 demo/services/ubo_service.py        # binds 0.0.0.0:8922

    GET /ubo?q=NAME    (also ?lei=LEI)  -> JSON ownership profile
    GET /health                          -> {"ok": true}

Resolution note: GLEIF's exact legalName filter can return several entities of the
same name in different jurisdictions (e.g. nine "BARCLAYS BANK PLC" records). We
fetch a handful of candidates and PREFER the one that actually has a parent
relationship (and a GB registration), so the demo surfaces the real group chain
(BARCLAYS BANK PLC -> BARCLAYS PLC) rather than an unconnected branch. A company
that is itself the top of its group (e.g. TESCO PLC) simply has no parent and is
reported with hasParent=false.
"""
import json, os, socketserver, ssl, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("UBO_HOST", "0.0.0.0")
PORT = int(os.environ.get("UBO_PORT", "8922"))
HTTP_TIMEOUT = int(os.environ.get("UBO_HTTP_TIMEOUT", "30"))
GLEIF = os.environ.get("UBO_GLEIF_BASE", "https://api.gleif.org/api/v1")
# How many same-name candidates to probe when resolving a name to the best LEI.
MAX_CANDIDATES = int(os.environ.get("UBO_MAX_CANDIDATES", "8"))
MAX_SAMPLE_CHILDREN = 5
UA = "Mozilla/5.0 (orbital-ubo-demo; +stdlib-urllib)"

_CTX = ssl.create_default_context()


def _get(url):
    """GET a GLEIF JSON:API URL. Returns parsed dict, or None on 404 (no relationship)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/vnd.api+json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _legal_name(entity):
    """GLEIF legalName is {"name","language"}; be defensive about shape."""
    ln = (entity or {}).get("legalName")
    if isinstance(ln, dict):
        return ln.get("name")
    return ln


def _country(entity):
    return ((entity or {}).get("legalAddress") or {}).get("country")


def _entity_of(record):
    return ((record or {}).get("attributes") or {}).get("entity") or {}


def _parent(lei, rel):
    """Fetch direct-parent / ultimate-parent for an LEI. Returns {lei,name,country} or None.

    GLEIF returns HTTP 404 when no such relationship is recorded — handled as None.
    """
    d = _get(f"{GLEIF}/lei-records/{lei}/{rel}")
    if not d or not d.get("data"):
        return None
    rec = d["data"]
    ent = _entity_of(rec)
    return {"lei": rec.get("id"), "name": _legal_name(ent), "country": _country(ent)}


def _children(lei):
    """Direct children: (total_count, [sample names...]). Empty on 404/none."""
    d = _get(f"{GLEIF}/lei-records/{lei}/direct-children"
             f"?page%5Bsize%5D={MAX_SAMPLE_CHILDREN}")
    if not d:
        return 0, []
    total = (((d.get("meta") or {}).get("pagination") or {}).get("total")) or 0
    sample = []
    for rec in (d.get("data") or [])[:MAX_SAMPLE_CHILDREN]:
        nm = _legal_name(_entity_of(rec))
        if nm:
            sample.append(nm)
    return int(total), sample


def resolve_lei(name):
    """Resolve a company name to the best LEI record.

    Fetch up to MAX_CANDIDATES exact-legalName matches, then prefer the candidate
    that actually has a parent relationship (and GB registration) so the group
    chain shows. Falls back to the first match (e.g. a true top-of-group like
    TESCO that legitimately has no parent). Returns (record, has_parent_flag) or
    (None, False).
    """
    url = (f"{GLEIF}/lei-records?filter%5Bentity.legalName%5D="
           + urllib.parse.quote(name) + f"&page%5Bsize%5D={MAX_CANDIDATES}")
    d = _get(url)
    cands = (d or {}).get("data") or []
    if not cands:
        return None, False
    want = (name or "").strip().casefold()
    best = None  # (score, record, has_parent)
    for rec in cands:
        lei = rec.get("id")
        ent = _entity_of(rec)
        exact = ((_legal_name(ent) or "").strip().casefold() == want)
        has_parent = _parent(lei, "direct-parent") is not None \
            or _parent(lei, "ultimate-parent") is not None
        gb = (_country(ent) == "GB")
        # Exact legalName match dominates (so "TESCO PLC" beats the fuzzy-matched
        # "TESCO PROPERTY FINANCE 1 PLC"); among equals, prefer the one that
        # actually has a parent, then a GB registration. This keeps the real group
        # chain (BARCLAYS BANK PLC -> BARCLAYS PLC, an exact GB match with a parent).
        score = (4 if exact else 0) + (2 if has_parent else 0) + (1 if gb else 0)
        if best is None or score > best[0]:
            best = (score, rec, has_parent)
        if exact and has_parent and gb:
            break  # ideal match, stop early
    _, rec, has_parent = best
    return rec, has_parent


def profile(name=None, lei=None):
    """Build the upward-ownership profile for a name or an explicit LEI."""
    if lei:
        rec = _get(f"{GLEIF}/lei-records/{lei}")
        rec = (rec or {}).get("data")
        if not rec:
            return {"query": lei, "found": False, "lei": None, "name": None,
                    "hasParent": False, "directParent": None, "ultimateParent": None,
                    "childCount": 0, "sampleChildren": []}
    else:
        rec, _ = resolve_lei(name or "")
        if not rec:
            return {"query": name, "found": False, "lei": None, "name": None,
                    "hasParent": False, "directParent": None, "ultimateParent": None,
                    "childCount": 0, "sampleChildren": []}
    lei = rec.get("id")
    ent = _entity_of(rec)
    direct = _parent(lei, "direct-parent")
    ultimate = _parent(lei, "ultimate-parent")
    child_count, sample = _children(lei)
    return {
        "query": name or lei,
        "found": True,
        "lei": lei,
        "name": _legal_name(ent),
        "country": _country(ent),
        "hasParent": bool(direct or ultimate),
        "directParent": direct,
        "ultimateParent": ultimate,
        "childCount": child_count,
        "sampleChildren": sample,
    }


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
            return self._json(200, {"ok": True})
        if u.path != "/ubo":
            return self._json(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()
        lei = (q.get("lei", [""])[0] or "").strip().upper()
        if not name and not lei:
            return self._json(200, {"query": "", "found": False, "lei": None, "name": None,
                                    "hasParent": False, "directParent": None,
                                    "ultimateParent": None, "childCount": 0, "sampleChildren": []})
        try:
            return self._json(200, profile(name=name or None, lei=lei or None))
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"ubo (upward ownership) -> http://{HOST}:{PORT}/ubo?q=TESCO%20PLC")
    S((HOST, PORT), H).serve_forever()
