#!/usr/bin/env python3
"""COMPANIES HOUSE LIVE — officers + charges/mortgages + filing history.

Calls the REAL UK Companies House public REST API
(https://api.company-information.service.gov.uk) and returns the *live* registry
facts a static bulk snapshot never has: who the currently-appointed officers are,
what charges/mortgages sit over the company (and who holds them), and the latest
statutory filings. This is the "official record, right now" lens for the Orbital
Risk demo — it complements the corpus-backed risk/insolvency engines with the
authoritative live source.

Companies House auth is HTTP Basic where the username is the API key and the
password is EMPTY. The free key is read from env COMPANIES_HOUSE_KEY.

    COMPANIES_HOUSE_KEY=xxxx python3 demo/services/ch_live_service.py   # binds 0.0.0.0:8921

    GET /chlive?company_number=00445790        (also ?q=NAME -> resolved via corpus)
    ->  { companyNumber, available, officers:[{name,role,appointedOn}],
          charges:{total,outstanding,satisfied,topHolder},
          latestFilings:[{date,category,description}] }

Pure Python stdlib — no pip. Read-only. Designed for the Orbital
companies-house-live.taxi adapter (@HttpService host.docker.internal:8921).

KEY-LESS BY DESIGN: if COMPANIES_HOUSE_KEY is unset/empty, every lookup returns
HTTP 200 with {available:false, reason:"set COMPANIES_HOUSE_KEY"} — it never
crashes and never blocks the router. The corpus (companies_house_bulk) is used
ONLY to resolve a ?q=name into a company number; all live facts come from the CH
API. The 30GB corpus is opened strictly read-only and is never copied.
"""
import base64
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

HOST = os.environ.get("CH_LIVE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CH_LIVE_PORT", "8921"))
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "").strip()
CH_BASE = os.environ.get(
    "CH_LIVE_BASE", "https://api.company-information.service.gov.uk")
HTTP_TIMEOUT = int(os.environ.get("CH_LIVE_HTTP_TIMEOUT", "20"))
# Corpus is used ONLY to resolve a name -> company number (never for live facts).
DB_PATH = os.environ.get(
    "GCLOUD_INTEL_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")

# Companies House numbers: 8 digits, or 2-letter prefix (SC/NI/OC/...) + 6 chars.
CH_NUMBER_RE = re.compile(r"^[A-Z0-9]{2}[0-9]{6}$|^[0-9]{8}$")
MAX_OFFICERS = 12
MAX_FILINGS = 5


def open_db():
    """Open the corpus strictly read-only (immutable=1: no locks, no -wal writes)."""
    uri = f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_number(name):
    """A company name -> its number, via the local corpus (prefer Active, shortest match).

    If the input already looks like a company number, it is returned as-is. Used
    only to let callers pass ?q=NAME; the authoritative facts still come from CH.
    """
    bare = (name or "").upper().replace(" ", "")
    if CH_NUMBER_RE.match(bare) and not any(ch.isspace() for ch in (name or "")):
        return bare
    try:
        conn = open_db()
    except Exception:
        return None
    try:
        for pattern in (name + "%", "%" + name + "%"):
            row = conn.execute(
                "SELECT company_number FROM companies_house_bulk "
                "WHERE company_name LIKE ? "
                "ORDER BY (company_status='Active') DESC, length(company_name) ASC "
                "LIMIT 1",
                (pattern,),
            ).fetchone()
            if row:
                return row["company_number"]
    except Exception:
        return None
    finally:
        conn.close()
    return None


def _ch_get(path):
    """GET a Companies House API path with Basic auth (key as username, blank password).

    Returns (data_dict_or_None, http_status). A 404 (e.g. company has no charges)
    is normal and returns (None, 404). Network/parse errors return (None, 0/code).
    """
    url = CH_BASE.rstrip("/") + path
    token = base64.b64encode(f"{CH_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + token,
        "Accept": "application/json",
        "User-Agent": "orbital-ch-live-demo (+stdlib-urllib)",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0


def fetch_officers(num):
    """Currently-appointed (active) officers: name, role, appointment date."""
    data, _ = _ch_get(f"/company/{num}/officers?register_view=false")
    out = []
    if not data:
        return out
    for it in data.get("items", []):
        # Active officer = no resignation date recorded.
        if it.get("resigned_on"):
            continue
        out.append({
            "name": it.get("name"),
            "role": it.get("officer_role"),
            "appointedOn": it.get("appointed_on"),
        })
        if len(out) >= MAX_OFFICERS:
            break
    return out


def fetch_charges(num):
    """Charges/mortgages summary: totals + the holder of the top outstanding charge."""
    data, status = _ch_get(f"/company/{num}/charges")
    summary = {"total": 0, "outstanding": 0, "satisfied": 0, "topHolder": None}
    if not data:
        # 404 simply means no charges register -> all zeros, which is correct.
        return summary
    items = data.get("items", []) or []
    summary["total"] = data.get("total_count", len(items))
    top_holder = None
    for it in items:
        st = (it.get("status") or "").lower()
        if st == "outstanding" or st == "part-satisfied":
            summary["outstanding"] += 1
            if top_holder is None:
                persons = it.get("persons_entitled") or []
                if persons:
                    top_holder = persons[0].get("name")
        elif st in ("satisfied", "fully-satisfied"):
            summary["satisfied"] += 1
    summary["topHolder"] = top_holder
    return summary


def fetch_filings(num):
    """The latest few statutory filings: date, category, plain description."""
    data, _ = _ch_get(f"/company/{num}/filing-history?items_per_page={MAX_FILINGS}")
    out = []
    if not data:
        return out
    for it in data.get("items", []):
        out.append({
            "date": it.get("date"),
            "category": it.get("category"),
            "description": it.get("description"),
        })
        if len(out) >= MAX_FILINGS:
            break
    return out


def build(num):
    """Assemble the live CH profile for a (validated) company number."""
    return {
        "companyNumber": num,
        "available": True,
        "source": "Companies House public API",
        "officers": fetch_officers(num),
        "charges": fetch_charges(num),
        "latestFilings": fetch_filings(num),
    }


def unavailable(num=None, reason="set COMPANIES_HOUSE_KEY"):
    """Uniform key-less / not-found response — always HTTP 200, never a crash."""
    return {
        "companyNumber": num,
        "available": False,
        "reason": reason,
        "officers": [],
        "charges": {"total": 0, "outstanding": 0, "satisfied": 0, "topHolder": None},
        "latestFilings": [],
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
        if u.path != "/chlive":
            return self._send(404, {"error": "not found"})

        q = urllib.parse.parse_qs(u.query)
        num = (q.get("company_number", [""])[0] or "").strip().upper()
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()

        # Resolve a ?q=name into a company number via the local corpus (best-effort;
        # the authoritative facts still come from the CH API below). Done even in the
        # no-key path so the unavailable response still echoes the resolved number.
        if name and not num:
            try:
                num = (resolve_number(name) or "").upper()
            except Exception:
                num = ""

        # No key -> honest, stable, HTTP-200 unavailable. Never crash the router.
        if not CH_KEY:
            return self._send(200, unavailable(num or None))

        try:
            if not CH_NUMBER_RE.match(num):
                return self._send(200, unavailable(
                    num or None, reason="no UK company number resolved for that input"))
            return self._send(200, build(num))
        except Exception as e:  # noqa: BLE001 — never break the router
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"CH live -> http://{HOST}:{PORT}/chlive?company_number=00445790")
    print(f"  Companies House key set: {bool(CH_KEY)}"
          + ("" if CH_KEY else "  (returns available:false until COMPANIES_HOUSE_KEY is set)"))
    Server((HOST, PORT), Handler).serve_forever()
