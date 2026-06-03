#!/usr/bin/env python3
"""PUBLIC-SECTOR CONTRACT EXPOSURE engine — free, from the gcloud-intel corpus.

Given a company name or Companies House number, resolves the CH number, maps it to
its government-supplier identity (supplier_ch_map), and returns that company's
public-sector award footprint: how many UK government contracts it holds, their
total value, how many are still live, and the largest few (buyer / title / value /
status / dates). Read-only over the corpus — nothing is written, copied or uploaded.

The join key is uk.gov.CompanyRegistrationNumber, so this plugs straight onto the
company spine: a company number (or name) flows in, Orbital routes it here, and the
company-360 gains a "where does it earn its public money" lens.

    python3 demo/services/contracts_service.py        # binds 0.0.0.0:8923

    GET /contracts?q=NAME            (also ?name=NAME or a CH number directly)
    GET /contracts?company_number=NUM
    GET /health
"""
import datetime, json, os, re, socketserver, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler

DB_PATH = os.environ.get("CONTRACTS_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")
HOST = os.environ.get("CONTRACTS_HOST", "0.0.0.0")
PORT = int(os.environ.get("CONTRACTS_PORT", "8923"))
CH_RE = re.compile(r"^[A-Z0-9]{6,10}$")
# A *real* Companies House number is 8 digits, or a 2-letter jurisdiction prefix
# (SC/NI/OC/SO/NC/IP/RC/FC/...) followed by 6 alphanumerics. Used to decide whether a
# spaceless query token is an actual number vs. a single-word company name (e.g.
# "Capgemini" must resolve as a NAME, not be mistaken for a number).
CH_NUM_RE = re.compile(r"^(?:\d{8}|[A-Z]{2}[A-Z0-9]{6})$")
TOP_N = int(os.environ.get("CONTRACTS_TOP_N", "8"))


def db():
    c = sqlite3.connect(f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def resolve_number(c, name):
    """A company name -> its CH number (prefer active, closest-length match).

    Mirrors risk_service.resolve_number so the two services agree on which company a
    name refers to. A bare token that already looks like a CH number is used as-is.
    """
    bare = name.upper().replace(" ", "")
    if CH_NUM_RE.match(bare) and not any(ch.isspace() for ch in name):
        return bare
    r = c.execute("SELECT company_number FROM companies_house_bulk WHERE company_name LIKE ? "
                  "ORDER BY (company_status='Active') DESC, length(company_name) ASC LIMIT 1",
                  (name + "%",)).fetchone()
    if r:
        return r["company_number"]
    r = c.execute("SELECT company_number FROM companies_house_bulk WHERE company_name LIKE ? "
                  "ORDER BY (company_status='Active') DESC, length(company_name) ASC LIMIT 1",
                  ("%" + name + "%",)).fetchone()
    return r["company_number"] if r else None


def company_name(c, num):
    r = c.execute("SELECT company_name FROM companies_house_bulk WHERE company_number=? LIMIT 1",
                  (num,)).fetchone()
    return r["company_name"] if r else None


def supplier_names(c, num):
    """Every government-supplier display-name string mapped to this CH number.

    One CH number can have several supplier_name spelling variants (e.g. SoftCat
    Plc / SOFTCAT LTD / Softcat plc); awards are keyed on those exact strings, so we
    gather them all and aggregate across the lot.
    """
    rows = c.execute("SELECT supplier_name FROM supplier_ch_map WHERE ch_number=? "
                     "AND supplier_name IS NOT NULL AND supplier_name<>''", (num,)).fetchall()
    return [r["supplier_name"] for r in rows]


def _today():
    return datetime.date.today().isoformat()


def _date(s):
    """First 10 chars of an ISO-ish timestamp -> YYYY-MM-DD, or '' if missing."""
    return (s or "")[:10] if s else ""


def contract_status(end_date, today):
    """Live if the contract's end_date is today or later; otherwise expired.

    end_date is an ISO timestamp so a lexical compare on the YYYY-MM-DD prefix is a
    correct date compare. Unknown end_date -> status reported as 'unknown'.
    """
    d = _date(end_date)
    if not d:
        return "unknown"
    return "live" if d >= today else "expired"


def exposure(num):
    """Full public-sector contract exposure for one CH number."""
    c = db()
    try:
        name = company_name(c, num)
        sup = supplier_names(c, num)
        if not sup:
            # Known company (or unknown number) but never seen as a UK gov supplier.
            return {"company": name or "", "companyNumber": num, "found": bool(name),
                    "isSupplier": False, "contractCount": 0, "totalValue": 0.0,
                    "liveCount": 0, "topBuyer": "", "supplierNames": [],
                    "summary": f"{name or num}: no UK public-sector contracts on record.",
                    "topContracts": []}

        today = _today()
        ph = ",".join("?" * len(sup))
        # Only GB (UK gov) awards; one row per distinct notice_id (the table PK), so
        # split-award line items stay distinct but nothing is double-counted by the join.
        rows = c.execute(
            f"SELECT notice_id, supplier, buyer, title, framework, "
            f"value_amount_gbp AS value, awarded_date, start_date, end_date "
            f"FROM awards WHERE country='GB' AND supplier IN ({ph}) "
            f"ORDER BY (value_amount_gbp IS NULL), value_amount_gbp DESC, awarded_date DESC",
            sup).fetchall()

        count = len(rows)
        total = 0.0
        live = 0
        buyers = {}
        top = []
        for r in rows:
            v = r["value"] or 0.0
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            total += v
            st = contract_status(r["end_date"], today)
            if st == "live":
                live += 1
            b = (r["buyer"] or "").strip()
            if b:
                buyers[b] = buyers.get(b, 0.0) + v
            if len(top) < TOP_N:
                top.append({
                    "buyer": b,
                    "title": (r["title"] or "").strip(),
                    "value": round(v, 2),
                    "status": st,
                    "date": _date(r["awarded_date"]) or _date(r["start_date"]),
                    "endDate": _date(r["end_date"]),
                    "framework": (r["framework"] or "").strip(),
                })

        top_buyer = max(buyers, key=buyers.get) if buyers else ""
        live_txt = f", {live} live" if live else ""
        summary = (f"{name or num}: {count} UK public-sector contract"
                   f"{'s' if count != 1 else ''} worth £{round(total):,}{live_txt}"
                   + (f"; biggest buyer {top_buyer}." if top_buyer else "."))
        return {
            "company": name or "",
            "companyNumber": num,
            "found": True,
            "isSupplier": True,
            "supplierNames": sup,
            "contractCount": count,
            "totalValue": round(total, 2),
            "liveCount": live,
            "topBuyer": top_buyer,
            "summary": summary,
            "topContracts": top,
        }
    finally:
        c.close()


def empty(num, name=""):
    # Reached only when no CH number could be resolved, so found is always False here.
    return {"company": name, "companyNumber": num, "found": False, "isSupplier": False,
            "contractCount": 0, "totalValue": 0.0, "liveCount": 0, "topBuyer": "",
            "supplierNames": [], "summary": "No UK company matched that input.", "topContracts": []}


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
        if u.path != "/contracts":
            return self._json(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()
        num = (q.get("company_number", [""])[0] or "").strip().upper()
        try:
            if name and not num:
                c = db()
                try:
                    num = resolve_number(c, name) or ""
                finally:
                    c.close()
            if not CH_RE.match(num):
                return self._json(200, empty(num, name))
            return self._json(200, exposure(num))
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"contract-exposure engine -> http://{HOST}:{PORT}/contracts?q=Capgemini")
    S((HOST, PORT), H).serve_forever()
