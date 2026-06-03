#!/usr/bin/env python3
"""Insolvency-intelligence micro-service for The Company Brain.

Routes a UK Companies House company number into the real distress corpus held at
/Users/jhammant/dev/gcloud-intel (SQLite, ~21.4M records: Gazette insolvency
notices + Companies House bulk + PSC ownership + G-Cloud/DOS government awards).

Read-only. Pure Python stdlib — no dependencies.

    python3 demo/services/insolvency_service.py     # binds 127.0.0.1:8901

    GET /insolvency?company_number=07021926
    ->  { companyNumber, companyName, inDistress, severity, stage, latestEvent,
          eventDate, controllerRisk, liveContractsAtRisk:[{buyer,contract,valueGbp}] }

Designed to be consumed by Orbital via the insolvency.taxi adapter
(@HttpService baseUrl = http://host.docker.internal:8901). Unknown / clean
company numbers return HTTP 200 with inDistress:false so the router never breaks.
"""
import json
import os
import re
import socketserver
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

DB_PATH = os.environ.get(
    "GCLOUD_INTEL_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")
PORT = int(os.environ.get("INSOLVENCY_PORT", "8901"))
HOST = os.environ.get("INSOLVENCY_HOST", "127.0.0.1")

# Companies House numbers: 8 chars, digits or 2-letter prefix (SC/NI/OC/...) + 6 digits.
CH_NUMBER_RE = re.compile(r"^[A-Z0-9]{2}[0-9]{6}$|^[0-9]{8}$")

# Gazette distress ladder (insolvency_events.severity 1..6) -> plain-English stage.
SEVERITY_STAGE = {
    1: "Early warning / notice",
    2: "Winding-up petition",
    3: "Creditors' notice / meeting",
    4: "Resolution to wind up / administration",
    5: "Liquidation / winding-up order",
    6: "Dissolution / final meeting",
}

# CH bulk statuses that count as "distressed" for the controller serial-failure
# network (mirrors v_psc_distress_network in views.sql).
CH_DISTRESS_STATUSES = (
    "Liquidation",
    "In Administration",
    "Voluntary Arrangement",
    "Live but Receiver Manager on at least one charge",
    "Active - Proposal to Strike off",
)


def open_db():
    """Open the corpus strictly read-only (immutable=1: no locks, no -wal writes)."""
    uri = f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def latest_event(conn, company_number):
    """Most-severe-then-most-recent corporate insolvency event for a company.

    Ordered by severity desc, then date_event/published_date desc, so a company
    that has both a winding-up resolution (sev 4) and a liquidator appointment
    (sev 5) surfaces the liquidation as its headline stage.
    """
    row = conn.execute(
        """
        SELECT company_name, event_type, severity,
               COALESCE(date_event, published_date) AS event_date,
               summary
        FROM insolvency_events
        WHERE company_number = ? AND notice_class = 'corporate'
        ORDER BY COALESCE(severity, 0) DESC,
                 COALESCE(date_event, published_date, '') DESC
        LIMIT 1
        """,
        (company_number,),
    ).fetchone()
    return row


def company_name_any(conn, company_number):
    """Best available display name: insolvency notice first, else CH bulk."""
    row = conn.execute(
        "SELECT company_name FROM insolvency_events "
        "WHERE company_number = ? AND company_name IS NOT NULL LIMIT 1",
        (company_number,),
    ).fetchone()
    if row and row["company_name"]:
        return row["company_name"]
    row = conn.execute(
        "SELECT company_name FROM companies_house_bulk WHERE company_number = ? LIMIT 1",
        (company_number,),
    ).fetchone()
    return row["company_name"] if row else None


def controller_risk(conn, company_number):
    """Serial-failure signal for this company's active individual controllers.

    For each non-ceased individual PSC of the queried company, count the OTHER
    companies that same person controls (matched on canonical name + DOB) which
    are themselves distressed — either carrying a corporate Gazette insolvency
    event or holding a Companies House distress status. Returns a one-line
    human string, e.g. "controller Mr Neville Taylor runs 145 other distressed
    companies".
    """
    pscs = conn.execute(
        """
        SELECT name, name_canon, dob_year, dob_month
        FROM psc_records
        WHERE company_number = ?
          AND kind LIKE 'individual-person%'
          AND (ceased_on IS NULL OR ceased_on = '')
        """,
        (company_number,),
    ).fetchall()

    placeholders = ",".join("?" for _ in CH_DISTRESS_STATUSES)
    worst = None  # (count, name)
    for p in pscs:
        if not p["name_canon"]:
            continue
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT p.company_number) AS n_other
            FROM psc_records p
            LEFT JOIN companies_house_bulk chb
                   ON chb.company_number = p.company_number
            WHERE p.name_canon = ?
              AND IFNULL(p.dob_year, -1)  = IFNULL(?, -1)
              AND IFNULL(p.dob_month, -1) = IFNULL(?, -1)
              AND p.kind LIKE 'individual-person%'
              AND (p.ceased_on IS NULL OR p.ceased_on = '')
              AND p.company_number <> ?
              AND (
                    EXISTS (SELECT 1 FROM insolvency_events ie
                             WHERE ie.company_number = p.company_number
                               AND ie.notice_class = 'corporate')
                 OR chb.company_status IN ({placeholders})
                  )
            """,
            (p["name_canon"], p["dob_year"], p["dob_month"], company_number,
             *CH_DISTRESS_STATUSES),
        ).fetchone()
        n = row["n_other"] if row else 0
        if worst is None or n > worst[0]:
            worst = (n, p["name"])

    if worst is None:
        return None  # no individual controllers on record
    n, name = worst
    nm = (name or "controller").strip()
    if n <= 0:
        return f"controller {nm} runs no other distressed companies on record"
    plural = "company" if n == 1 else "companies"
    return f"controller {nm} runs {n} other distressed {plural}"


def live_contracts_at_risk(conn, company_number, limit=25):
    """Live government awards held by this company — work that may re-compete.

    Bridges the insolvent company to the awards table via the same join Orbital's
    corpus uses (matched_supplier_id or matched_supplier name on the company's
    insolvency events), keeping only awards with no end date or an end date in the
    future. value_amount_gbp is preferred, falling back to value_amount.
    """
    # The corpus ingests the same award from several feeds (regional OCDS, FTS,
    # combined), so collapse to one row per (buyer, contract, value); the same
    # underlying contract is only at risk once.
    rows = conn.execute(
        """
        SELECT a.buyer, a.title,
               COALESCE(a.value_amount_gbp, a.value_amount) AS value_gbp
        FROM insolvency_events ie
        JOIN awards a
          ON (a.matched_supplier_id = ie.matched_supplier_id
              AND ie.matched_supplier_id IS NOT NULL)
          OR (a.supplier = ie.matched_supplier
              AND ie.matched_supplier IS NOT NULL)
        WHERE ie.company_number = ?
          AND ie.notice_class = 'corporate'
          AND (a.framework IS NULL OR a.framework NOT LIKE 'SUSPECT%')
          AND (a.end_date IS NULL OR a.end_date >= date('now'))
        GROUP BY a.buyer, a.title,
                 COALESCE(a.value_amount_gbp, a.value_amount)
        ORDER BY COALESCE(a.value_amount_gbp, a.value_amount) DESC
        LIMIT ?
        """,
        (company_number, limit),
    ).fetchall()
    out = []
    for r in rows:
        v = r["value_gbp"]
        out.append({
            "buyer": r["buyer"],
            "contract": r["title"],
            "valueGbp": round(v, 2) if isinstance(v, (int, float)) else None,
        })
    return out


def build_profile(conn, company_number):
    ev = latest_event(conn, company_number)
    name = company_name_any(conn, company_number)

    if ev is None:
        # No corporate insolvency notice on record -> treat as not distressed.
        return {
            "companyNumber": company_number,
            "companyName": name,
            "inDistress": False,
            "severity": 0,
            "stage": "No insolvency on record",
            "latestEvent": None,
            "eventDate": None,
            "controllerRisk": controller_risk(conn, company_number),
            "liveContractsAtRisk": [],
        }

    sev = ev["severity"] or 0
    return {
        "companyNumber": company_number,
        "companyName": name or ev["company_name"],
        "inDistress": True,
        "severity": sev,
        "stage": SEVERITY_STAGE.get(sev, "Insolvency event"),
        "latestEvent": ev["event_type"],
        "eventDate": ev["event_date"],
        "controllerRisk": controller_risk(conn, company_number),
        "liveContractsAtRisk": live_contracts_at_risk(conn, company_number),
    }


# In-memory cache (controller-risk recomputation is slow on a 15.5M-row corpus, so
# we cache built profiles and warm the demo companies on boot for instant responses).
_CACHE = {}
WARM_SEED = ["07021926", "08810995", "09446231", "10976115",
             "04151059", "04088165", "16262215", "00445790"]


def warm():
    try:
        conn = open_db()
    except Exception:
        return
    try:
        for cn in WARM_SEED:
            try:
                _CACHE[cn] = build_profile(conn, cn)
            except Exception:
                pass
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "db": DB_PATH})
        if u.path != "/insolvency":
            return self._send(404, {"error": "not found"})

        q = urllib.parse.parse_qs(u.query)
        raw = (q.get("company_number", [""])[0] or "").strip().upper()
        if not raw:
            return self._send(400, {"error": "company_number is required"})
        if not CH_NUMBER_RE.match(raw):
            # Invalid format -> behave like "not found": never break the router.
            return self._send(200, {
                "companyNumber": raw, "companyName": None, "inDistress": False,
                "severity": 0, "stage": "Invalid company number", "latestEvent": None,
                "eventDate": None, "controllerRisk": None, "liveContractsAtRisk": [],
            })

        if raw in _CACHE:
            return self._send(200, _CACHE[raw])
        try:
            conn = open_db()
            try:
                prof = build_profile(conn, raw)
                _CACHE[raw] = prof
                return self._send(200, prof)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"Insolvency intel -> http://{HOST}:{PORT}/insolvency?company_number=07021926")
    print(f"  reading (ro) {DB_PATH}")
    threading.Thread(target=warm, daemon=True).start()
    Server((HOST, PORT), Handler).serve_forever()
