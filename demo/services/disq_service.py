#!/usr/bin/env python3
"""DIRECTOR DISQUALIFICATIONS — checks a company's controllers against the REAL
UK Companies House Disqualified Officers register.

Given a company name or number, this service:
  1. resolves the company number from the local corpus (companies_house_bulk),
  2. reads its active PSC controllers (psc_records — beneficial owners),
  3. for EACH named individual controller, searches the official Companies House
     Disqualified Officers register (GET /search/disqualified-officers?q=NAME) and,
     where a result closely matches the controller's name, pulls the natural
     officer record (GET /disqualified-officers/natural/{officer_id}) to confirm a
     currently-ACTIVE disqualification (disqualified_until in the future).

This is the "is anyone who controls this company barred from being a director"
lens for the Orbital Risk demo — it complements the corpus-backed risk / PSC
network engines with the authoritative live disqualification source.

Companies House auth is HTTP Basic where the username is the API key and the
password is EMPTY. The free key is read from env COMPANIES_HOUSE_KEY (the same
key the ch_live_service uses).

    COMPANIES_HOUSE_KEY=xxxx python3 demo/services/disq_service.py   # binds 0.0.0.0:8925

    GET /disq?q=<company name or number>
    ->  { company, companyNumber, checked:int, disqualifiedCount:int,
          hits:[{name, reason, from, to}], available:bool }

Pure Python stdlib — no pip. Read-only. Designed for the Orbital
disqualifications.taxi adapter (@HttpService host.docker.internal:8925).

KEY-LESS BY DESIGN: if COMPANIES_HOUSE_KEY is unset/empty, every lookup returns
HTTP 200 with {available:false, reason:"set COMPANIES_HOUSE_KEY", ...} — it never
crashes and never blocks the router. The corpus (companies_house_bulk +
psc_records) is used ONLY to resolve a name->number and to list the controllers
to check; all disqualification facts come from the CH API. The 30GB corpus is
opened strictly read-only (immutable=1) and is never copied.
"""
import base64
import datetime
import json
import os
import re
import socketserver
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("DISQ_HOST", "0.0.0.0")
PORT = int(os.environ.get("DISQ_PORT", "8925"))
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "").strip()
CH_BASE = os.environ.get(
    "DISQ_CH_BASE", "https://api.company-information.service.gov.uk")
HTTP_TIMEOUT = int(os.environ.get("DISQ_HTTP_TIMEOUT", "20"))
# Corpus is used ONLY to resolve name->number and list controllers (never for live facts).
DB_PATH = os.environ.get(
    "GCLOUD_INTEL_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")

# Companies House numbers: 8 digits, or 2-char prefix (SC/NI/OC/...) + 6 chars.
CH_NUMBER_RE = re.compile(r"^[A-Z0-9]{2}[0-9]{6}$|^[0-9]{8}$")
MAX_CONTROLLERS = 8          # cap how many controllers we check (rate-friendly)
MAX_SEARCH_RESULTS = 20      # how many register hits to scan per controller name
NAME_MATCH_THRESHOLD = 0.82  # token-overlap score above which two names "closely match"

# Honorifics / suffixes that are noise when comparing a PSC name to a register name.
_NAME_NOISE = {
    "mr", "mrs", "miss", "ms", "dr", "sir", "dame", "lord", "lady", "prof",
    "professor", "rev", "hon", "the", "mx",
}


def open_db():
    """Open the corpus strictly read-only (immutable=1: no locks, no -wal writes)."""
    uri = f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_company(name):
    """A company name OR number -> (company_number, company_name) via the corpus.

    If the input already looks like a company number, it is looked up directly so
    we can echo the registered name; if not found as a number we still return it.
    """
    bare = (name or "").upper().replace(" ", "")
    looks_like_number = bool(CH_NUMBER_RE.match(bare)) and not any(
        ch.isspace() for ch in (name or ""))
    try:
        conn = open_db()
    except Exception:
        # No corpus: best-effort echo the input if it is a number.
        return (bare, None) if looks_like_number else (None, None)
    try:
        if looks_like_number:
            row = conn.execute(
                "SELECT company_number, company_name FROM companies_house_bulk "
                "WHERE company_number=? LIMIT 1", (bare,)).fetchone()
            if row:
                return row["company_number"], row["company_name"]
            return bare, None
        for pattern in (name + "%", "%" + name + "%"):
            row = conn.execute(
                "SELECT company_number, company_name FROM companies_house_bulk "
                "WHERE company_name LIKE ? "
                "ORDER BY (company_status='Active') DESC, length(company_name) ASC "
                "LIMIT 1", (pattern,)).fetchone()
            if row:
                return row["company_number"], row["company_name"]
    except Exception:
        return None, None
    finally:
        conn.close()
    return None, None


def active_individual_controllers(company_number):
    """Names of the active, individual PSC controllers of a company (deduped)."""
    try:
        conn = open_db()
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT name FROM psc_records "
            "WHERE company_number=? AND ceased_on IS NULL AND name IS NOT NULL "
            "AND kind LIKE 'individual%' "
            "LIMIT 25", (company_number,)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    seen, out = set(), []
    for r in rows:
        nm = (r["name"] or "").strip()
        key = _name_tokens_key(nm)
        if nm and key and key not in seen:
            seen.add(key)
            out.append(nm)
        if len(out) >= MAX_CONTROLLERS:
            break
    return out


def _name_tokens(name):
    """Lower-cased alphabetic word tokens of a name, honorifics/suffixes removed."""
    raw = re.findall(r"[a-zA-Z]+", (name or "").lower())
    return [t for t in raw if t not in _NAME_NOISE and len(t) > 1]


def _name_tokens_key(name):
    return " ".join(sorted(_name_tokens(name)))


def names_match(psc_name, register_name):
    """True if two person names "closely match".

    Symmetric token-overlap (Jaccard-style but normalised by the smaller name so a
    PSC "John A Smith" still matches a register "John Smith"). Requires the surname
    (last meaningful PSC token) to be present, to avoid first-name-only collisions.
    """
    a, b = set(_name_tokens(psc_name)), set(_name_tokens(register_name))
    if not a or not b:
        return False
    inter = a & b
    if not inter:
        return False
    # surname guard: the PSC's last token must appear in the register name
    psc_seq = _name_tokens(psc_name)
    if psc_seq and psc_seq[-1] not in b:
        return False
    score = len(inter) / min(len(a), len(b))
    return score >= NAME_MATCH_THRESHOLD


def _ch_get(path):
    """GET a Companies House API path with Basic auth (key as username, blank password).

    Returns (data_dict_or_None, http_status). A 404 (e.g. no matches) is normal and
    returns (None, 404). Network/parse errors return (None, 0/code).
    """
    url = CH_BASE.rstrip("/") + path
    token = base64.b64encode(f"{CH_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + token,
        "Accept": "application/json",
        "User-Agent": "orbital-disq-demo (+stdlib-urllib)",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0


def _parse_iso(d):
    """Parse a CH ISO date 'YYYY-MM-DD' -> date, or None."""
    if not d or not isinstance(d, str):
        return None
    try:
        return datetime.date.fromisoformat(d[:10])
    except Exception:
        return None


def _reason_text(reason):
    """Turn a CH reason object into a short human string."""
    if not isinstance(reason, dict):
        return None
    parts = []
    desc = reason.get("description_identifier")
    if desc:
        parts.append(str(desc).replace("-", " "))
    act = reason.get("act")
    sec = reason.get("section")
    if act:
        leg = str(act).replace("-", " ")
        if sec:
            leg += f" s.{sec}"
        parts.append(leg)
    elif sec:
        parts.append(f"section {sec}")
    return " — ".join(parts) if parts else None


def _active_disqualifications(officer_id):
    """Fetch a natural officer's record; return list of currently-ACTIVE disqs.

    Active = disqualified_until is missing/blank OR in the future (>= today).
    Each entry: {reason, from, to}.
    """
    safe_id = urllib.parse.quote(officer_id, safe="")
    data, _ = _ch_get(f"/disqualified-officers/natural/{safe_id}")
    if not data:
        return []
    today = datetime.date.today()
    out = []
    for dq in data.get("disqualifications", []) or []:
        frm = dq.get("disqualified_from")
        to = dq.get("disqualified_until")
        to_d = _parse_iso(to)
        # Active if there is no end, or the end is today or later.
        if to_d is not None and to_d < today:
            continue
        out.append({
            "reason": _reason_text(dq.get("reason")) or dq.get("disqualification_type"),
            "from": frm,
            "to": to,
        })
    return out


def _full_name(it):
    """Best display name for a disqualified-officers search item."""
    return (it.get("title") or it.get("name") or "").strip()


def check_controller(psc_name):
    """Search the register for one controller; return list of active hits.

    Each hit: {name, reason, from, to}. Empty list means clean (or no match).
    """
    data, _ = _ch_get(
        "/search/disqualified-officers?items_per_page=%d&q=%s"
        % (MAX_SEARCH_RESULTS, urllib.parse.quote(psc_name)))
    if not data:
        return []
    hits = []
    for it in (data.get("items") or [])[:MAX_SEARCH_RESULTS]:
        reg_name = _full_name(it)
        if not names_match(psc_name, reg_name):
            continue
        self_link = ((it.get("links") or {}).get("self") or "")
        officer_id = self_link.rstrip("/").split("/")[-1] if self_link else None
        actives = _active_disqualifications(officer_id) if officer_id else []
        for dq in actives:
            hits.append({
                "name": reg_name or psc_name,
                "reason": dq["reason"],
                "from": dq["from"],
                "to": dq["to"],
            })
        if actives:
            # one matched person with active disqs is enough signal for this name
            break
    return hits


def build(query):
    """Assemble the disqualification check for a company name/number."""
    number, reg_name = resolve_company(query)
    if not number:
        return {
            "company": None,
            "companyNumber": None,
            "checked": 0,
            "disqualifiedCount": 0,
            "hits": [],
            "available": True,
            "reason": "no UK company resolved for that input",
        }
    controllers = active_individual_controllers(number)
    all_hits, checked = [], 0
    for nm in controllers:
        checked += 1
        all_hits.extend(check_controller(nm))
    return {
        "company": reg_name,
        "companyNumber": number,
        "checked": checked,
        "disqualifiedCount": len(all_hits),
        "hits": all_hits,
        "available": True,
        "source": "Companies House Disqualified Officers register",
    }


def unavailable(reason="set COMPANIES_HOUSE_KEY"):
    """Uniform key-less response — always HTTP 200, never a crash."""
    return {
        "company": None,
        "companyNumber": None,
        "checked": 0,
        "disqualifiedCount": 0,
        "hits": [],
        "available": False,
        "reason": reason,
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
            return self._send(200, {"ok": True, "keyed": bool(CH_KEY),
                                    "base": CH_BASE})
        if u.path != "/disq":
            return self._send(404, {"error": "not found"})

        q = urllib.parse.parse_qs(u.query)
        query = (q.get("q", [""])[0]
                 or q.get("company", [""])[0]
                 or q.get("company_number", [""])[0]
                 or q.get("name", [""])[0] or "").strip()

        # No key -> honest, stable, HTTP-200 unavailable. Never crash the router.
        if not CH_KEY:
            return self._send(200, unavailable())

        if not query:
            return self._send(200, unavailable(
                reason="no company name or number supplied"))

        try:
            return self._send(200, build(query))
        except Exception as e:  # noqa: BLE001 — never break the router
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"disq -> http://{HOST}:{PORT}/disq?q=ANGLIA%20SALADS%20LIMITED")
    print(f"  Companies House key set: {bool(CH_KEY)}"
          + ("" if CH_KEY else
             "  (returns available:false until COMPANIES_HOUSE_KEY is set)"))
    Server((HOST, PORT), Handler).serve_forever()
