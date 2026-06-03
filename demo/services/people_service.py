#!/usr/bin/env python3
"""PEOPLE SCREENING — screen the humans (and corporate controllers) BEHIND a company.

A single-company lookup tells you about the company. This tells you about the people
who actually control it. Given a company name or number it:

  1. resolves the company number in the gcloud-intel corpus (same logic as risk_service),
  2. pulls its ACTIVE PSC controllers from psc_records (name, dob, natures-of-control),
  3. for EACH controller (capped at 6) screens the NAME — in parallel, with short
     timeouts — against the live sanctions service (US OFAC / UK OFSI / EU) and the
     adverse-media / social scan service (recent news + social mentions),

then returns a flat per-person verdict: who they are, what they control, whether they
hit a sanctions list, how much adverse media surrounds them, and a combined `flagged`.
Read-only over the corpus; the two screening calls are the only outbound traffic, and
every one is best-effort (a failed call degrades that person's signal, never the whole
response).

    python3 demo/services/people_service.py        # binds 0.0.0.0:8920

    GET /people?q=<company name or number>   (also ?name=, ?company_number=)
    GET /health
"""
import concurrent.futures
import json
import os
import re
import socketserver
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

DB_PATH = os.environ.get("PEOPLE_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")
HOST = os.environ.get("PEOPLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PEOPLE_PORT", "8920"))

# Downstream screening services (already running on the host).
SANCTIONS_URL = os.environ.get("PEOPLE_SANCTIONS_URL", "http://127.0.0.1:8918/sanctions")
NEWS_URL = os.environ.get("PEOPLE_NEWS_URL", "http://127.0.0.1:8906/scan")
SCREEN_TIMEOUT = float(os.environ.get("PEOPLE_SCREEN_TIMEOUT", "7"))  # per outbound call
MAX_PEOPLE = int(os.environ.get("PEOPLE_MAX", "6"))                   # controllers to screen
# Adverse-media count at/above this is itself a flag (sanctions hit always flags).
ADVERSE_FLAG_AT = int(os.environ.get("PEOPLE_ADVERSE_FLAG_AT", "5"))

CH_RE = re.compile(r"^[A-Z0-9]{6,10}$")
UA = "company-brain-demo/1.0 (people-screening; jhammant@gmail.com)"

import sqlite3


def db():
    c = sqlite3.connect(f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def resolve_number(c, name):
    """A company name -> its number (prefer active, closest-length match). Mirrors risk_service."""
    bare = name.upper().replace(" ", "")
    if CH_RE.match(bare) and not any(ch.isspace() for ch in name):
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


def controllers(c, num):
    """Active PSC controllers for a company number — individuals and corporate entities,
    excluding pure '...-statement' rows (which carry no controlling person)."""
    rows = c.execute(
        "SELECT name, dob_year, dob_month, kind, natures_of_control FROM psc_records "
        "WHERE company_number=? AND ceased_on IS NULL AND name IS NOT NULL "
        "AND kind NOT LIKE '%statement%' "
        "ORDER BY (kind LIKE 'individual%') DESC LIMIT ?",
        (num, MAX_PEOPLE)).fetchall()
    return [dict(r) for r in rows]


_CONTROL_LABEL = {
    "ownership-of-shares-75-to-100-percent": "75-100% shares",
    "ownership-of-shares-50-to-75-percent": "50-75% shares",
    "ownership-of-shares-25-to-50-percent": "25-50% shares",
    "voting-rights-75-to-100-percent": "75-100% votes",
    "voting-rights-50-to-75-percent": "50-75% votes",
    "voting-rights-25-to-50-percent": "25-50% votes",
    "right-to-appoint-and-remove-directors": "appoints directors",
}


def control_summary(natures_json):
    """Turn the natures_of_control JSON array into a short human label."""
    try:
        items = json.loads(natures_json) if natures_json else []
    except Exception:
        items = [natures_json] if natures_json else []
    labels = [_CONTROL_LABEL.get(i, i.replace("-", " ")) for i in items if i]
    return ", ".join(labels[:3])


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=SCREEN_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def screen_sanctions(name):
    """-> (status, hitCount). Best-effort; on failure returns ('unknown', 0)."""
    try:
        d = _get_json(f"{SANCTIONS_URL}?q={urllib.parse.quote(name)}")
        status = d.get("status") or ("hit" if d.get("hitCount") else "clear")
        return status, int(d.get("hitCount") or 0)
    except Exception:
        return "unknown", 0


def screen_news(name):
    """-> adverse-media item count. Best-effort; on failure returns 0."""
    try:
        d = _get_json(f"{NEWS_URL}?q={urllib.parse.quote(name)}")
        items = d.get("items")
        if isinstance(items, list):
            return len(items)
        # fall back to a counts block if present
        counts = d.get("counts") or {}
        if isinstance(counts, dict):
            return int(counts.get("total") or sum(v for v in counts.values() if isinstance(v, int)))
        return 0
    except Exception:
        return 0


def screen_person(ctrl):
    """Screen one controller's NAME against sanctions + news, in parallel."""
    name = ctrl["name"]
    is_person = (ctrl.get("kind") or "").startswith("individual")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_sanc = ex.submit(screen_sanctions, name)
        f_news = ex.submit(screen_news, name)
        status, hits = f_sanc.result()
        adverse = f_news.result()
    flagged = bool(hits) or (status == "hit") or (adverse >= ADVERSE_FLAG_AT)
    person = {
        "name": name,
        "kind": "individual" if is_person else "entity",
        "control": control_summary(ctrl.get("natures_of_control")),
        "sanctionsStatus": status,
        "sanctionsHits": hits,
        "adverseMediaCount": adverse,
        "flagged": flagged,
    }
    if is_person and ctrl.get("dob_year"):
        # birth month/year only (never a full DOB) — matches what PSC exposes publicly.
        m = ctrl.get("dob_month")
        person["dob"] = f"{int(m):02d}/{ctrl['dob_year']}" if m else str(ctrl["dob_year"])
    return person


def assess(query):
    c = db()
    try:
        num = ""
        name_in = (query or "").strip()
        if name_in:
            num = resolve_number(c, name_in) or ""
        if not CH_RE.match(num or ""):
            return {"company": name_in, "companyNumber": num, "found": False,
                    "people": [], "flaggedCount": 0, "screened": 0,
                    "note": "No UK company matched that input."}
        cname = company_name(c, num) or name_in
        ctrls = controllers(c, num)
    finally:
        c.close()

    if not ctrls:
        return {"company": cname, "companyNumber": num, "found": True,
                "people": [], "flaggedCount": 0, "screened": 0,
                "note": "No active person-with-significant-control on record — beneficial "
                        "ownership may be unreported or via overseas entities."}

    # Screen every controller in parallel (each controller in turn fans out to its own
    # 2 outbound calls). One worker per person, capped — fast even for the 6-person max.
    people = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PEOPLE, len(ctrls))) as ex:
        for p in ex.map(screen_person, ctrls):
            people.append(p)

    flagged = [p for p in people if p["flagged"]]
    return {
        "company": cname,
        "companyNumber": num,
        "found": True,
        "people": people,
        "flaggedCount": len(flagged),
        "screened": len(people),
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
            return self._json(200, {"ok": True, "port": PORT,
                                    "sanctions": SANCTIONS_URL, "news": NEWS_URL})
        if u.path != "/people":
            return self._json(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        query = (q.get("q", [""])[0] or q.get("name", [""])[0]
                 or q.get("company_number", [""])[0] or "").strip()
        if not query:
            return self._json(200, {"company": "", "companyNumber": "", "found": False,
                                    "people": [], "flaggedCount": 0, "screened": 0,
                                    "note": "Provide ?q=<company name or number>."})
        try:
            return self._json(200, assess(query))
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"people screening -> http://{HOST}:{PORT}/people?q=PROMOAT%20LIMITED")
    S((HOST, PORT), H).serve_forever()
