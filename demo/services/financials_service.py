#!/usr/bin/env python3
"""FINANCIAL HEALTH — the accounts/filing-currency lens from the Companies House profile.

Calls the REAL UK Companies House public REST API
(https://api.company-information.service.gov.uk) and surfaces the *accounts* block of a
company profile that a risk/ownership view never shows: are the annual accounts OVERDUE
or current, when were the last accounts made up to, when are the next ones due, the
accounts category/type, whether the confirmation statement is overdue, plus the headline
identity facts (status, incorporation date, type, SIC codes). This is the "is this company
filing on time / financially current" lens for the Orbital Risk demo — it complements the
corpus-backed risk / insolvency / ownership engines and the live officers/charges lens
with the authoritative *financial-currency* signal.

Companies House auth is HTTP Basic where the username is the API key and the password is
EMPTY. The free key is read from env COMPANIES_HOUSE_KEY.

    COMPANIES_HOUSE_KEY=xxxx python3 demo/services/financials_service.py   # binds 0.0.0.0:8927

    GET /financials?company_number=00445790      (also ?q=NAME -> resolved via corpus)
    ->  { companyNumber, available, accountsStatus:"overdue"|"current"|"unknown",
          accountsOverdue:"true"|"false", lastAccounts, nextDue, nextMadeUpTo,
          accountsCategory, accountsType, confirmationOverdue:"true"|"false",
          status, incorporated, type, sic:[...] }

Pure Python stdlib — no pip. Read-only. Designed for the Orbital financials.taxi adapter
(@HttpService host.docker.internal:8927).

KEY-LESS BY DESIGN: if COMPANIES_HOUSE_KEY is unset/empty, every lookup returns HTTP 200
with {available:false, reason:"set COMPANIES_HOUSE_KEY"} — it never crashes and never
blocks the router. The corpus (companies_house_bulk) is used ONLY to resolve a ?q=name
into a company number; all financial facts come from the CH API. The corpus is opened
strictly read-only (immutable=1) and is never copied.
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

HOST = os.environ.get("FINANCIALS_HOST", "0.0.0.0")
PORT = int(os.environ.get("FINANCIALS_PORT", "8927"))
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "").strip()
CH_BASE = os.environ.get(
    "FINANCIALS_BASE", "https://api.company-information.service.gov.uk")
HTTP_TIMEOUT = int(os.environ.get("FINANCIALS_HTTP_TIMEOUT", "20"))
# Corpus is used ONLY to resolve a name -> company number (never for financial facts).
DB_PATH = os.environ.get(
    "GCLOUD_INTEL_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")

# Companies House numbers: 8 digits, or 2-char prefix (SC/NI/OC/...) + 6 digits.
CH_NUMBER_RE = re.compile(r"^[A-Z0-9]{2}[0-9]{6}$|^[0-9]{8}$")
MAX_SIC = 4


def open_db():
    """Open the corpus strictly read-only (immutable=1: no locks, no -wal writes)."""
    uri = f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_number(name):
    """A company name -> its number, via the local corpus (prefer Active, shortest match).

    If the input already looks like a company number, it is returned as-is. Used only to
    let callers pass ?q=NAME; the authoritative facts still come from CH.
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

    Returns (data_dict_or_None, http_status). A 404 returns (None, 404); network/parse
    errors return (None, 0/code).
    """
    url = CH_BASE.rstrip("/") + path
    token = base64.b64encode(f"{CH_KEY}:".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + token,
        "Accept": "application/json",
        "User-Agent": "orbital-financials-demo (+stdlib-urllib)",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0


def _bool_str(v):
    """A JSON bool (or None) -> the STRING "true"/"false" (None -> None).

    The .taxi side maps onto String semantic types only, so booleans are emitted as
    strings — nothing has to map onto a Taxi Boolean (which fails to map).
    """
    if v is None:
        return None
    return "true" if bool(v) else "false"


def build(num):
    """Assemble the financial-health profile for a (validated) company number."""
    data, status = _ch_get(f"/company/{num}")
    if not data:
        # 404 / other -> company not found on the live register, but never crash.
        return unavailable(
            num, reason=("company not found on the Companies House register"
                         if status == 404 else
                         f"Companies House lookup failed (status {status})"))

    accounts = data.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}
    next_accounts = accounts.get("next_accounts") or {}
    confirmation = data.get("confirmation_statement") or {}

    # accounts.overdue (bool) -> a status string AND a "true"/"false" string.
    overdue = accounts.get("overdue")
    if overdue is True:
        accounts_status = "overdue"
    elif overdue is False:
        accounts_status = "current"
    else:
        accounts_status = "unknown"

    # next due date: prefer the explicit next_accounts.due_on, fall back to the
    # profile-level accounts.next_due (older payloads), then next_made_up_to context.
    next_due = (next_accounts.get("due_on")
                or accounts.get("next_due")
                or None)
    # next_made_up_to lives under next_accounts in current payloads, with a
    # legacy accounts.next_made_up_to fallback.
    next_made_up_to = (next_accounts.get("period_end_on")
                       or accounts.get("next_made_up_to")
                       or None)

    sic = data.get("sic_codes") or []
    if isinstance(sic, list):
        sic = [s for s in sic if s][:MAX_SIC]
    else:
        sic = []

    return {
        "companyNumber": num,
        "available": True,
        "source": "Companies House public API",
        "accountsStatus": accounts_status,                 # overdue|current|unknown
        "accountsOverdue": _bool_str(overdue),             # "true"/"false"/None
        "lastAccounts": last_accounts.get("made_up_to"),   # accounts.last_accounts.made_up_to
        "nextDue": next_due,                               # accounts.next_accounts.due_on
        "nextMadeUpTo": next_made_up_to,                   # next_made_up_to
        "accountsCategory": (last_accounts.get("type")     # accounts_category/type
                             or accounts.get("accounts_category")),
        "accountsType": last_accounts.get("type"),
        "confirmationOverdue": _bool_str(confirmation.get("overdue")),  # "true"/"false"
        "status": data.get("company_status"),              # company_status
        "incorporated": data.get("date_of_creation"),      # date_of_creation
        "type": data.get("type"),                          # type
        "sic": sic,                                         # sic_codes
    }


def unavailable(num=None, reason="set COMPANIES_HOUSE_KEY"):
    """Uniform key-less / not-found response — always HTTP 200, never a crash.

    accountsStatus is "unknown" (not "current"/"overdue") so the live lens reads as
    genuinely unavailable rather than silently asserting the company is filing on time.
    """
    return {
        "companyNumber": num,
        "available": False,
        "reason": reason,
        "accountsStatus": "unknown",
        "accountsOverdue": None,
        "lastAccounts": None,
        "nextDue": None,
        "nextMadeUpTo": None,
        "accountsCategory": None,
        "accountsType": None,
        "confirmationOverdue": None,
        "status": None,
        "incorporated": None,
        "type": None,
        "sic": [],
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
        if u.path != "/financials":
            return self._send(404, {"error": "not found"})

        q = urllib.parse.parse_qs(u.query)
        num = (q.get("company_number", [""])[0] or "").strip().upper()
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()

        # Resolve a ?q=name into a company number via the local corpus (best-effort; the
        # authoritative facts still come from the CH API below). Done even in the no-key
        # path so the unavailable response still echoes the resolved number.
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
    print(f"financials -> http://{HOST}:{PORT}/financials?company_number=00445790")
    print(f"  Companies House key set: {bool(CH_KEY)}"
          + ("" if CH_KEY else "  (returns available:false until COMPANIES_HOUSE_KEY is set)"))
    Server((HOST, PORT), Handler).serve_forever()
