#!/usr/bin/env python3
"""Counterparty RISK engine — the 100x core.

Given a company number, traverses the PSC ownership network in the gcloud-intel corpus
to surface the risk a single-company lookup never shows: who really controls it, what
ELSE those people run, how many of those have failed, distress on the company itself,
and mass-registration addresses. Returns a scored RED/AMBER/GREEN verdict + the network
graph + concrete red flags.  Read-only over the corpus.

    python3 demo/services/risk_service.py        # binds 0.0.0.0:8907
"""
import json, os, re, socketserver, urllib.parse
from http.server import BaseHTTPRequestHandler

DB_PATH = os.environ.get("RISK_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")
HOST = os.environ.get("RISK_HOST", "0.0.0.0")
PORT = int(os.environ.get("RISK_PORT", "8907"))
CH_RE = re.compile(r"^[A-Z0-9]{6,10}$")
# A linked company counts as "failed" on these statuses (real failure, not plain dissolution)
FAIL_SQL = ("(company_status LIKE '%iquidation%' OR company_status LIKE '%dministration%' "
            "OR company_status LIKE '%eceiver%' OR company_status = 'Voluntary Arrangement')")

import base64
GATE_USER = os.environ.get("GATE_USER", "demo")
GATE_PASS = os.environ.get("GATE_PASS", "")  # set this to require a basic-auth password over the tunnel

import sqlite3
def db():
    c = sqlite3.connect(f"file:{urllib.parse.quote(DB_PATH)}?mode=ro&immutable=1", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def company(c, num):
    r = c.execute("SELECT company_number,company_name,company_status,company_category,"
                  "incorporation_date,post_town,post_code,sic_1 FROM companies_house_bulk "
                  "WHERE company_number=? LIMIT 1", (num,)).fetchone()
    return dict(r) if r else None

def controllers(c, num):
    return [dict(r) for r in c.execute(
        "SELECT name,name_canon,dob_year,dob_month,kind,natures_of_control FROM psc_records "
        "WHERE company_number=? AND ceased_on IS NULL AND name IS NOT NULL LIMIT 12", (num,))]

def footprint(c, ncanon, dy, dm, exclude):
    """Every other company this individual controls, with failure tally."""
    rows = c.execute(
        "SELECT c.company_number n, c.company_name nm, c.company_status st FROM psc_records p "
        "JOIN companies_house_bulk c ON p.company_number=c.company_number "
        "WHERE p.name_canon=? AND p.dob_year=? AND p.dob_month=? AND p.company_number!=? "
        "ORDER BY (" + FAIL_SQL.replace("company_status", "c.company_status") + ") DESC LIMIT 400",
        (ncanon, dy, dm, exclude)).fetchall()
    def failed(st): return bool(st) and bool(re.search(r"iquidation|dministration|eceiver|Voluntary Arrangement", st))
    fails = [dict(number=r["n"], name=r["nm"], status=r["st"]) for r in rows if failed(r["st"])]
    return {"total": len(rows), "failed": len(fails), "failedExamples": fails[:10]}

def address_load(c, postcode):
    if not postcode: return 0
    return c.execute("SELECT COUNT(*) FROM companies_house_bulk WHERE post_code=?", (postcode,)).fetchone()[0]

def subject_insolvency(c, num):
    r = c.execute("SELECT event_type,severity,COALESCE(date_event,published_date) d,summary "
                  "FROM insolvency_events WHERE company_number=? ORDER BY severity DESC, d DESC LIMIT 1",
                  (num,)).fetchone()
    return dict(r) if r else None

STAGE = {2: "Winding-up petition", 3: "Creditors' notice", 4: "Administration / resolution",
         5: "Liquidation / winding-up order", 6: "Dissolution"}

def assess(num):
    c = db()
    try:
        co = company(c, num)
        if not co:
            return {"companyNumber": num, "found": False, "band": "UNKNOWN", "score": 0,
                    "verdict": "No company found for that number.", "flags": [], "controllers": [], "network": {"nodes": [], "edges": []}}
        score, flags = 0, []
        nodes = [{"id": "co", "label": co["company_name"], "type": "company",
                  "status": co["company_status"], "subject": True}]
        edges = []

        # 1. distress on the company itself
        ev = subject_insolvency(c, num)
        co_failed = bool(re.search(r"iquidation|dministration|eceiver|Voluntary Arrangement", co["company_status"] or ""))
        if ev:
            score += 55
            flags.append({"sev": "high", "label": "Company in active distress",
                          "detail": f"{STAGE.get(ev['severity'], ev['event_type'])} on record"})
        elif co_failed:
            score += 45
            flags.append({"sev": "high", "label": "Company status: distress", "detail": co["company_status"]})

        # 2. ownership network — the hidden risk
        ctrls = controllers(c, num)
        people = [p for p in ctrls if p["dob_year"]]
        for p in people[:6]:
            fp = footprint(c, p["name_canon"], p["dob_year"], p["dob_month"], num)
            pid = "p_" + re.sub(r"\W", "", (p["name_canon"] or ""))[:24]
            nodes.append({"id": pid, "label": p["name"], "type": "person",
                          "controls": fp["total"], "failed": fp["failed"]})
            edges.append({"from": pid, "to": "co", "rel": "controls"})
            if fp["failed"] >= 3:
                sev = "high" if fp["failed"] >= 8 else "med"
                score += min(55, 8 + fp["failed"] * 4)
                flags.append({"sev": sev, "label": "Serial-failure director",
                              "detail": f"{p['name']} is linked to {fp['failed']} other failed/insolvent companies"
                                        f" (controls {fp['total']} in total)"})
            elif fp["total"] >= 25:
                score += 8
                flags.append({"sev": "low", "label": "High director footprint",
                              "detail": f"{p['name']} controls {fp['total']} companies"})
            for f in fp["failedExamples"][:6]:
                fid = "c_" + f["number"]
                nodes.append({"id": fid, "label": f["name"], "type": "company", "status": f["status"], "failed": True})
                edges.append({"from": pid, "to": fid, "rel": "controls"})

        # 3. mass-registration address (formation-agent / nominee signal)
        load = address_load(c, co["post_code"])
        if load >= 300:
            score += 12
            flags.append({"sev": "med", "label": "Mass-registration address",
                          "detail": f"{load:,} companies share the registered postcode {co['post_code']}"})

        # 4. very young company (shell risk) — only when combined with other flags
        yr = (co.get("incorporation_date") or "")[-4:] or (co.get("incorporation_date") or "")[:4]
        if not people:
            flags.append({"sev": "low", "label": "No individual controllers on record",
                          "detail": "Beneficial ownership is via corporate entities only — trace upward."})

        score = min(score, 100)
        band = "RED" if score >= 55 else "AMBER" if score >= 25 else "GREEN"
        if band == "GREEN" and not flags:
            flags.append({"sev": "low", "label": "No material risk signals", "detail": "Clean on distress, director-network and address checks."})
        rec = {"RED": "Do not proceed without senior sign-off / enhanced due diligence.",
               "AMBER": "Manual review recommended before proceeding.",
               "GREEN": "No blockers found on automated checks."}[band]
        verdict = f"{band} {score}/100 — " + ("; ".join(f["detail"] for f in flags[:2]) or "no material risk signals") + f". {rec}"
        return {"companyNumber": num, "found": True, "companyName": co["company_name"],
                "verdict": verdict, "recommendation": rec,
                "status": co["company_status"], "incorporated": co["incorporation_date"],
                "postcode": co["post_code"], "score": score, "band": band,
                "flags": sorted(flags, key=lambda f: {"high": 0, "med": 1, "low": 2}[f["sev"]]),
                "controllers": [{"name": p["name"], "control": p["natures_of_control"]} for p in ctrls],
                "network": {"nodes": nodes, "edges": edges}}
    finally:
        c.close()


def resolve_number(c, name):
    """A company name -> its number (prefer active, closest-length match)."""
    bare = name.upper().replace(" ", "")
    if CH_RE.match(bare) and not any(ch.isspace() for ch in name):
        return bare
    r = c.execute("SELECT company_number FROM companies_house_bulk WHERE company_name LIKE ? "
                  "ORDER BY (company_status='Active') DESC, length(company_name) ASC LIMIT 1",
                  (name + "%",)).fetchone()
    if r: return r["company_number"]
    r = c.execute("SELECT company_number FROM companies_house_bulk WHERE company_name LIKE ? "
                  "ORDER BY (company_status='Active') DESC, length(company_name) ASC LIMIT 1",
                  ("%" + name + "%",)).fetchone()
    return r["company_number"] if r else None


HERE = os.path.dirname(os.path.abspath(__file__))

class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _authed(self):
        if not GATE_PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            usr, pw = base64.b64decode(h[6:]).decode().split(":", 1)
        except Exception:
            return False
        return usr == GATE_USER and pw == GATE_PASS

    def do_GET(self):
        if not self._authed():
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Orbital Risk"')
            self.send_header("Content-Length", "0"); self.end_headers(); return
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "risk.html"), "rb") as f: b = f.read()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                return
            except Exception as e: return self._json(500, {"error": str(e)})
        if u.path == "/health": return self._json(200, {"ok": True})
        if u.path != "/risk": return self._json(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()
        num = (q.get("company_number", [""])[0] or "").strip().upper()
        try:
            if name and not num:
                c = db()
                try: num = resolve_number(c, name) or ""
                finally: c.close()
            if not CH_RE.match(num):
                return self._json(200, {"companyNumber": num, "found": False, "band": "UNKNOWN", "score": 0,
                                        "verdict": "No UK company matched that input.", "flags": [],
                                        "controllers": [], "network": {"nodes": [], "edges": []}})
            return self._json(200, assess(num))
        except Exception as e: return self._json(500, {"error": str(e)})
    def log_message(self, *a): pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True

if __name__ == "__main__":
    print(f"risk engine -> http://{HOST}:{PORT}/risk?company_number=10976115")
    S((HOST, PORT), H).serve_forever()
