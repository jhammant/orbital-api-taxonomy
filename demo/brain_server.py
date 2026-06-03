#!/usr/bin/env python3
"""The Counterparty Brain — demo backend.

Serves the single-page demo and assembles a decision-grade dossier for any UK company
by fanning out through Orbital (http://localhost:9022) across independent TYPED sources
with no integration code — identity (GLEIF), counterparty risk (gcloud-intel corpus),
adverse media (news + social), and (when wired) sanctions — then enriches with the full
risk network for the ownership graph. Returns the dossier + the lineage receipts that
show how Orbital composed it. Pure stdlib.

    python3 demo/brain_server.py        # http://localhost:8848  (set GATE_PASS to gate it)
"""
import base64
import json
import os
import re
import bisect
import socketserver
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

ORBITAL = os.environ.get("ORBITAL_URL", "http://localhost:9022") + "/api/taxiql"
RISK_URL = os.environ.get("RISK_URL", "http://127.0.0.1:8917")  # full risk detail (network graph)
SANCTIONS_URL = os.environ.get("SANCTIONS_URL", "http://127.0.0.1:8918")  # full sanctions detail (all hits/lists)
NLQ_URL = os.environ.get("NLQ_URL", "http://127.0.0.1:8910")  # ask-in-English (NL -> TaxiQL) service
PEOPLE_URL = os.environ.get("PEOPLE_URL", "http://127.0.0.1:8920")        # screen the controllers
CHLIVE_URL = os.environ.get("CHLIVE_URL", "http://127.0.0.1:8921")        # Companies House live (officers/charges)
UBO_URL = os.environ.get("UBO_URL", "http://127.0.0.1:8922")              # GLEIF upward ownership
CONTRACTS_URL = os.environ.get("CONTRACTS_URL", "http://127.0.0.1:8923")  # public-sector contract exposure
NARRATIVE_URL = os.environ.get("NARRATIVE_URL", "http://127.0.0.1:8924")  # AI risk narrative (local LLM)
DISQ_URL = os.environ.get("DISQ_URL", "http://127.0.0.1:8925")            # director disqualifications (CH register)
GAZETTE_URL = os.environ.get("GAZETTE_URL", "http://127.0.0.1:8926")      # The Gazette official insolvency notices
FIN_URL = os.environ.get("FIN_URL", "http://127.0.0.1:8927")             # financial health (CH accounts)
GDELT_URL = os.environ.get("GDELT_URL", "http://127.0.0.1:8928")         # GDELT global media + sentiment
CORPUS_DB = os.environ.get("RISK_DB", "/Users/jhammant/dev/gcloud-intel/data/db.sqlite")  # for /api/suggest autocomplete
PORT = int(os.environ.get("BRAIN_PORT", "8848"))

# In-memory company-name index for instant /api/suggest type-ahead. Warm-loaded once in a
# background thread (active companies only, ~220MB); until ready, /api/suggest returns [].
# Each entry is "COMPANY NAME\tNUMBER" (CH names are upper-case) so we bisect by name prefix.
_NAMES = []
_NAMES_READY = [False]
def _build_name_index():
    try:
        c = sqlite3.connect(f"file:{urllib.parse.quote(CORPUS_DB)}?mode=ro&immutable=1", uri=True, timeout=120)
        data = [f"{(r[0] or '').upper()}\t{r[1]}" for r in c.execute(
            "SELECT company_name, company_number FROM companies_house_bulk "
            "WHERE company_status='Active' AND company_name IS NOT NULL")]
        c.close()
        data.sort()
        _NAMES.extend(data)
        _NAMES_READY[0] = True
    except Exception:
        _NAMES_READY[0] = False
GATE_USER = os.environ.get("GATE_USER", "demo")
GATE_PASS = os.environ.get("GATE_PASS", "")  # set this to require a basic-auth password at the edge
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.environ.get("INDEX_FILE", os.path.join(HERE, "index.html"))  # override to preview a variant

# The maximal decision-grade fan-out: one company name -> N typed sources, composed by
# Orbital with NO integration code. (sanctions is appended once its adapter is wired.)
SOURCES = [
    "identity : uk.gov.apis.gleif.LeiRecord",
    "risk : brain.risk.RiskAssessment",
    "adverseMedia : social.AdverseMediaList",
]

# The actual Taxi behind each typed source — surfaced so the demo can SHOW the
# schema Orbital routes through (the "taxi being used"), not just the result.
TAXI_SOURCES = {
    "identity": {
        "taxiFile": "gleif.taxi", "operation": "GleifService.getLeiRecord",
        "schema": 'model LeiRecord {\n'
                  '  lei : Lei by jsonPath("$.data[0].attributes.lei")\n'
                  '  companyNumber : CompanyRegistrationNumber by jsonPath("$..entity.registeredAs")\n'
                  '  companyStatus : CompanyStatus by jsonPath("$..entity.status")\n'
                  '}\n'
                  '@HttpService(baseUrl = "https://api.gleif.org")   // auth: none\n'
                  'service GleifService {\n'
                  '  operation getLeiRecord(legalName : CompanyName) : LeiRecord\n'
                  '}',
    },
    "risk": {
        "taxiFile": "risk.taxi", "operation": "RiskService.assessByNumber",
        "schema": 'model RiskAssessment {\n'
                  '  band : RiskBand by jsonPath("$.band")        // RED / AMBER / GREEN\n'
                  '  score : RiskScore by jsonPath("$.score")\n'
                  '  verdict : RiskVerdict by jsonPath("$.verdict")\n'
                  '}\n'
                  '@HttpService(baseUrl = "http://host.docker.internal:8917")  // gcloud-intel corpus\n'
                  'service RiskService {\n'
                  '  operation assessByNumber(companyNumber : CompanyRegistrationNumber) : RiskAssessment\n'
                  '  operation assessByName(q : CompanyName) : RiskAssessment\n'
                  '}',
    },
    "adverseMedia": {
        "taxiFile": "social-news.taxi", "operation": "SocialNewsService.getAdverseMedia",
        "schema": 'model AdverseMediaItem {\n'
                  '  title : MediaTitle by jsonPath("$.title")\n'
                  '  channel : MediaChannel by jsonPath("$.channel")\n'
                  '}\n'
                  'model AdverseMediaList { items : AdverseMediaItem[] }\n'
                  '@HttpService(baseUrl = "http://host.docker.internal:8906")  // news + social\n'
                  'service SocialNewsService {\n'
                  '  operation getAdverseMedia(q : CompanyName) : AdverseMediaList\n'
                  '}',
    },
    "sanctions": {
        "taxiFile": "sanctions.taxi", "operation": "SanctionsService.screen",
        "schema": 'model SanctionsResult {\n'
                  '  status : SanctionsStatus by jsonPath("$.status")   // clear / hit\n'
                  '  hitCount : SanctionsHitCount by jsonPath("$.hitCount")\n'
                  '  topHitName : SanctionsHitName by jsonPath("$.hits[0].name")\n'
                  '}\n'
                  '@HttpService(baseUrl = "http://host.docker.internal:8918")  // OFAC + OFSI + EU lists\n'
                  'service SanctionsService {\n'
                  '  operation screen(q : CompanyName) : SanctionsResult\n'
                  '}',
    },
}


# Attach the FULL .taxi file content + the real call endpoint to each source (read once at startup).
_TAXI_DIR = os.path.join(os.path.dirname(HERE), "build", "gov-uk", "taxi")
_TAXI_FILES = {"identity": "gleif.taxi", "risk": "risk.taxi", "adverseMedia": "social-news.taxi", "sanctions": "sanctions.taxi"}
_ENDPOINTS = {
    "identity": "GET https://api.gleif.org/api/v1/lei-records  ·  auth: none",
    "risk": "GET http://host.docker.internal:8917/risk  ·  the gcloud-intel corpus (read-only)",
    "adverseMedia": "GET http://host.docker.internal:8906/scan  ·  Google News · HN · Reddit · Bluesky",
    "sanctions": "GET http://host.docker.internal:8918/sanctions  ·  OFAC SDN · UK OFSI · EU lists",
}
for _n, _f in _TAXI_FILES.items():
    try:
        with open(os.path.join(_TAXI_DIR, _f)) as _fh:
            TAXI_SOURCES[_n]["schemaFull"] = _fh.read().strip()
    except Exception:
        TAXI_SOURCES[_n]["schemaFull"] = TAXI_SOURCES[_n].get("schema", "")
    TAXI_SOURCES[_n]["endpoint"] = _ENDPOINTS.get(_n, "")
# GLEIF's service is declared in services.taxi — append it so 'identity' shows the full picture.
TAXI_SOURCES["identity"]["schemaFull"] += (
    '\n\n// service — services.taxi\n'
    '@HttpService(baseUrl = "https://api.gleif.org")\n'
    'service GleifService {\n'
    '  @HttpOperation(method = "GET", url = "/api/v1/lei-records?page[size]=1")\n'
    '  operation getLeiRecord( @QueryVariable("filter[entity.legalName]") legalName : uk.gov.CompanyName ) : uk.gov.apis.gleif.LeiRecord\n'
    '}'
)


# --- the 5 additional decision-grade sources, orchestrated directly + shown as typed .taxi ---
EXTRA_SOURCES = {
    "people": {"label": "People screening", "detail": "controllers vs sanctions + adverse media",
               "type": "PeopleScreen", "operation": "PeopleService.screen", "taxiFile": "people.taxi",
               "endpoint": "GET http://host.docker.internal:8920/people  ·  screens each controller",
               "url": PEOPLE_URL + "/people", "key": "peopleScreening"},
    "chlive": {"label": "Companies House (live)", "detail": "active officers · charges/mortgages · filings",
               "type": "ChLiveProfile", "operation": "ChLiveService.getLiveProfile", "taxiFile": "companies-house-live.taxi",
               "endpoint": "GET https://api.company-information.service.gov.uk  ·  auth: api-key",
               "url": CHLIVE_URL + "/chlive", "key": "companiesHouseLive"},
    "ubo": {"label": "Ultimate ownership", "detail": "GLEIF parent / child relationships",
            "type": "UboProfile", "operation": "UboService.trace", "taxiFile": "gleif-ubo.taxi",
            "endpoint": "GET https://api.gleif.org/.../ultimate-parent  ·  auth: none",
            "url": UBO_URL + "/ubo", "key": "ubo"},
    "contracts": {"label": "Public contracts", "detail": "G-Cloud / DOS awards (gcloud-intel corpus)",
                  "type": "ContractExposure", "operation": "ContractsService.exposure", "taxiFile": "contracts.taxi",
                  "endpoint": "GET http://host.docker.internal:8923/contracts  ·  the corpus",
                  "url": CONTRACTS_URL + "/contracts", "key": "contracts"},
    "narrative": {"label": "AI risk narrative", "detail": "local LLM, grounded in the risk facts",
                  "type": "RiskNarrative", "operation": "NarrativeService.write", "taxiFile": "risk-narrative.taxi",
                  "endpoint": "GET http://host.docker.internal:8924/narrative  ·  local LM Studio",
                  "url": NARRATIVE_URL + "/narrative", "key": "narrative"},
    "disq": {"label": "Director disqualifications", "detail": "controllers vs the CH disqualified-officers register",
             "type": "DisqualificationCheck", "operation": "DisqService.checkByName", "taxiFile": "disqualifications.taxi",
             "endpoint": "GET https://api.company-information.service.gov.uk/search/disqualified-officers  ·  auth: api-key",
             "url": DISQ_URL + "/disq", "key": "disqualifications"},
    "financials": {"label": "Financial health", "detail": "accounts overdue · last accounts · category",
                   "type": "FinancialsProfile", "operation": "FinancialsService.getFinancials", "taxiFile": "financials.taxi",
                   "endpoint": "GET https://api.company-information.service.gov.uk/company/{number}  ·  auth: api-key",
                   "url": FIN_URL + "/financials", "key": "financials"},
    "gazette": {"label": "Official insolvency notices", "detail": "The Gazette — the official public record",
                "type": "GazetteNotices", "operation": "GazetteService.notices", "taxiFile": "gazette.taxi",
                "endpoint": "GET https://www.thegazette.co.uk/all-notices/notice/data.json  ·  auth: none",
                "url": GAZETTE_URL + "/gazette", "key": "gazette"},
    "gdelt": {"label": "Global media + sentiment", "detail": "worldwide coverage + average tone (GDELT)",
              "type": "GlobalMediaTone", "operation": "GdeltService.coverage", "taxiFile": "gdelt.taxi",
              "endpoint": "GET https://api.gdeltproject.org/api/v2/doc/doc  ·  auth: none",
              "url": GDELT_URL + "/gdelt", "key": "gdelt"},
}
for _n, _meta in EXTRA_SOURCES.items():
    try:
        with open(os.path.join(_TAXI_DIR, _meta["taxiFile"])) as _fh:
            _meta["schemaFull"] = _fh.read().strip()
    except Exception:
        _meta["schemaFull"] = ""


def extra_rows(node, data):
    """How many rows a given extra source returned (for the lineage receipts)."""
    d = data or {}
    if node == "people":
        return d.get("screened") or len(d.get("people") or [])
    if node == "chlive":
        return len(d.get("officers") or []) if d.get("available") else 0
    if node == "ubo":
        return 1 if (d.get("ultimateParent") or d.get("directParent")) else 0
    if node == "contracts":
        return d.get("contractCount") or 0
    if node == "narrative":
        return 1 if d.get("narrative") else 0
    if node == "disq":
        return d.get("disqualifiedCount") or 0
    if node == "financials":
        return 1 if d.get("available") else 0
    if node == "gazette":
        return d.get("noticeCount") or 0
    if node == "gdelt":
        return d.get("articleCount") or 0
    return 0


def brain_query(name, with_sanctions=False):
    src = list(SOURCES)
    if with_sanctions:
        src.append("sanctions : brain.sanctions.SanctionsResult")
    body = "\n  ".join(src)
    return f'given {{ name : uk.gov.CompanyName = "{name}" }}\nfind {{\n  {body}\n}}'


def taxiql(query, timeout=45):
    req = urllib.request.Request(ORBITAL, data=query.encode("utf-8"),
                                 headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_json(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(s):
    """Whitelist input so it can't break out of the TaxiQL string literal."""
    return re.sub(r"[^A-Za-z0-9 &.,()\-_]", "", s or "").strip()[:80]


def build_lineage(name, fan, total_ms):
    """Honest receipts: which TYPED sources Orbital composed for this one question."""
    idn = fan.get("identity") or {}
    rk = fan.get("risk") or {}
    am = (fan.get("adverseMedia") or {}).get("items") or []
    sx = fan.get("sanctions") or {}
    has = lambda x: 1 if (isinstance(x, dict) and any(v not in (None, "", []) for v in x.values())) else 0
    sources = [
        {"node": "identity", "label": "GLEIF", "detail": "Global LEI registry",
         "type": "LeiRecord", "via": "name → GLEIF", "rows": has(idn), "auth": "none"},
        {"node": "risk", "label": "gcloud-intel corpus", "detail": "PSC ownership + insolvency (21M+ records)",
         "type": "RiskAssessment", "via": "name → GLEIF → company-number → RiskService", "rows": has(rk), "auth": "none"},
        {"node": "adverseMedia", "label": "News + Social", "detail": "Google News · Hacker News · Reddit · Bluesky",
         "type": "AdverseMediaList", "via": "name → SocialNews", "rows": len(am), "auth": "none"},
    ]
    if sx:
        sources.append({"node": "sanctions", "label": "Sanctions", "detail": "OFAC SDN / consolidated lists",
                        "type": "SanctionsResult", "via": "name → Sanctions",
                        "rows": int(sx.get("hitCount") or 0), "auth": "none"})
    for s in sources:
        s.update(TAXI_SOURCES.get(s["node"], {}))
    return {
        "question": name, "engine": "Orbital",
        "query": "given CompanyName find { identity, risk, adverseMedia%s }" % (", sanctions" if sx else ""),
        "taxiql": brain_query(name, bool(sx)),
        "sources": sources,
        "invented": "name → GLEIF → company-number → the people behind it  —  Orbital chose this hop; nobody wired it.",
        "why": [
            "Zero integration code — each source is described once in Taxi (~10 lines). Nobody wrote glue between GLEIF, the risk corpus, the news scan and sanctions.",
            "Composed by meaning — Orbital matched GLEIF's company-number OUTPUT to the risk engine's INPUT and chained them itself; that hop was never wired by hand.",
            "O(N), not O(N²) — add the next source in ~10 lines of Taxi and it instantly composes with every existing query. No per-pair connectors to build or maintain.",
            "Auditable — every fact carries its lineage: which typed source produced it, via which operation, in how long. Receipts for the whole answer.",
        ],
        "totalMs": total_ms,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        if not GATE_PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, p = base64.b64decode(h[6:]).decode().split(":", 1)
        except Exception:
            return False
        return u == GATE_USER and p == GATE_PASS

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Orbital Counterparty Brain"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(INDEX_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if u.path == "/api/brain":
                name = clean(q.get("name", ["TESCO PLC"])[0]) or "TESCO PLC"
                want_sanctions = os.environ.get("BRAIN_SANCTIONS", "0") == "1"
                t0 = time.time()
                # The 5 extra decision-grade sources are fetched only for a FULL dossier (the Assess
                # view passes ?full=1); the Portfolio omits them so its rows stay fast.
                want_full = q.get("full", ["0"])[0] == "1" or q.get("detail", ["0"])[0] == "1"
                with ThreadPoolExecutor(max_workers=8) as ex:
                    f_fan = ex.submit(taxiql, brain_query(name, want_sanctions))
                    f_risk = ex.submit(http_json, f"{RISK_URL}/risk?q={urllib.parse.quote(name)}")
                    f_sanc = ex.submit(http_json, f"{SANCTIONS_URL}/sanctions?q={urllib.parse.quote(name)}")
                    f_extra = ({n: ex.submit(http_json, f"{m['url']}?q={urllib.parse.quote(name)}", 60)
                                for n, m in EXTRA_SOURCES.items()} if want_full else {})
                    try:
                        fan = f_fan.result()
                    except Exception as e:
                        fan = {"_error": str(e)}
                    try:
                        risk_full = f_risk.result()
                    except Exception as e:
                        risk_full = {"_error": str(e)}
                    try:
                        sanc_full = f_sanc.result()
                    except Exception as e:
                        sanc_full = None
                    extra = {}
                    for n, fut in f_extra.items():
                        try:
                            extra[n] = fut.result()
                        except Exception:
                            extra[n] = None
                idn = fan.get("identity") or {}
                lineage = build_lineage(name, fan, int((time.time() - t0) * 1000))
                # append the extra typed .taxi sources to the lineage receipts (console + assess panel)
                if want_full:
                    for n, m in EXTRA_SOURCES.items():
                        lineage["sources"].append({
                            "node": n, "label": m["label"], "detail": m["detail"], "type": m["type"],
                            "via": "name → " + m["operation"].split(".")[0], "rows": extra_rows(n, extra.get(n)),
                            "auth": "none", "taxiFile": m["taxiFile"], "operation": m["operation"],
                            "endpoint": m["endpoint"], "schema": m.get("schemaFull", ""), "schemaFull": m.get("schemaFull", ""),
                        })
                # Property facet — HM Land Registry indicative area value for the registered-office
                # postcode, via deeptech-dd /property (:8930, non-blocking). Additive; postcode-keyed.
                prop = None
                if want_full:
                    _pc = (risk_full.get("postcode") or idn.get("postcode") or "").strip()
                    if _pc:
                        try:
                            prop = http_json("http://localhost:8930/property?postcode=" + urllib.parse.quote(_pc), 8)
                        except Exception:
                            prop = None
                        if prop:
                            _avg = prop.get("averageSoldPrice")
                            lineage["sources"].append({
                                "node": "property", "label": "Property value",
                                "detail": "HM Land Registry Price Paid · indicative area value",
                                "type": "valuation", "via": "postcode → realestate.propertyValue",
                                "rows": ("avg £%s · %s sales" % (int(_avg), prop.get("transactionCount"))) if _avg else "computing…",
                                "auth": "none", "taxiFile": "deeptech-dd-service.taxi",
                                "operation": "DeeptechDdService.propertyValue", "endpoint": "GET :8930/property",
                                "schema": "", "schemaFull": "",
                            })
                resp = {
                    "query": name,
                    "resolved": {
                        "companyName": risk_full.get("companyName") or idn.get("companyName") or name,
                        "companyNumber": risk_full.get("companyNumber") or idn.get("companyNumber"),
                        "lei": idn.get("lei"),
                        "status": risk_full.get("status") or idn.get("companyStatus"),
                        "incorporated": risk_full.get("incorporated"),
                        "postcode": risk_full.get("postcode") or idn.get("postcode"),
                    },
                    "risk": risk_full,
                    "adverseMedia": (fan.get("adverseMedia") or {}).get("items") or [],
                    "sanctions": sanc_full or fan.get("sanctions"),
                    "peopleScreening": extra.get("people"),
                    "companiesHouseLive": extra.get("chlive"),
                    "ubo": extra.get("ubo"),
                    "contracts": extra.get("contracts"),
                    "narrative": extra.get("narrative"),
                    "disqualifications": extra.get("disq"),
                    "financials": extra.get("financials"),
                    "gazette": extra.get("gazette"),
                    "gdelt": extra.get("gdelt"),
                    "property": prop,
                    "lineage": lineage,
                }
                return self._send(200, json.dumps(resp))

            if u.path == "/api/analysis":
                # Deep-tech analysis workspace. Company (or postcode) -> registered-office
                # coordinate -> deeptech-dd engine (:8930): real HM Land Registry property value,
                # an *illustrative* wave-energy site model at that coordinate, and the independent
                # deep-tech diligence verdict. Demonstrates the generic engine composing off the
                # same geo spine the dossier uses (postcode -> lat/lon via postcodes.io).
                name = clean(q.get("name", [""])[0])
                pc = (q.get("postcode", [""])[0] or "").strip().upper()
                if not pc and name:
                    try:
                        rk = http_json(f"{RISK_URL}/risk?q={urllib.parse.quote(name)}", 20)
                        pc = (rk.get("postcode") or "").strip().upper()
                        name = rk.get("companyName") or name
                    except Exception:
                        pass
                lat = lon = None
                if pc:
                    try:
                        geo = http_json("https://api.postcodes.io/postcodes/" + urllib.parse.quote(pc), 8)
                        res = geo.get("result") or {}
                        lat, lon = res.get("latitude"), res.get("longitude")
                    except Exception:
                        pass
                DD = "http://localhost:8930"
                prop = site = audit = None
                if pc:
                    try:
                        prop = http_json(f"{DD}/property?postcode={urllib.parse.quote(pc)}", 8)
                    except Exception:
                        prop = None
                if lat is not None and lon is not None:
                    try:
                        site = http_json(f"{DD}/site-economics?lat={lat}&lon={lon}", 25)
                    except Exception:
                        site = None
                try:
                    audit = http_json(f"{DD}/audit-verdict?technology=wave", 10)
                except Exception:
                    audit = None
                resp = {
                    "query": name or pc,
                    "resolved": {"companyName": name, "postcode": pc, "latitude": lat, "longitude": lon},
                    "property": prop,
                    "siteEconomics": site,
                    "auditVerdict": audit,
                }
                return self._send(200, json.dumps(resp))

            if u.path == "/api/suggest":
                term = (q.get("q", [""])[0] or "").strip().upper()
                if len(term) < 2 or not _NAMES_READY[0]:
                    return self._send(200, json.dumps([]))
                i = bisect.bisect_left(_NAMES, term)
                hits = []
                while i < len(_NAMES) and len(hits) < 3000 and _NAMES[i].startswith(term):
                    nm, _, num = _NAMES[i].partition("\t")
                    hits.append((nm, num)); i += 1
                # rank: exact name first, then word-boundary ("TESCO PLC" over "TESCOMETER"), then shortest
                tl = len(term)
                hits.sort(key=lambda h: (h[0] != term, not (len(h[0]) > tl and h[0][tl] == " "), len(h[0]), h[0]))
                return self._send(200, json.dumps([{"name": h[0], "number": h[1]} for h in hits[:8]]))

            if u.path == "/api/ask":
                question = (q.get("q", [""])[0] or "").strip()[:200]
                if not question:
                    return self._send(200, json.dumps({"question": "", "summary": "Ask a question, e.g. “is Tesco in trouble?” or “who controls Promoat?”", "taxiql": ""}))
                try:
                    r = http_json(f"{NLQ_URL}/ask?q={urllib.parse.quote(question)}", timeout=125)
                    return self._send(200, json.dumps({"question": r.get("question", question), "summary": r.get("summary", ""),
                                                       "taxiql": r.get("taxiql", ""), "intent": r.get("intent", "")}))
                except Exception as e:
                    return self._send(200, json.dumps({"question": question, "summary": "(ask service unavailable — try the search box)", "taxiql": "", "error": str(e)}))

            if u.path == "/api/trading":
                t = clean(q.get("ticker", ["AAPL"])[0]) or "AAPL"
                query = f'given {{ ticker : brain.StockTicker = "{t}" }}\nfind {{ markets.StockQuote }}'
                return self._send(200, json.dumps(taxiql(query, 30)))

            if u.path == "/api/coding":
                owner = clean(q.get("owner", ["orbitalapi"])[0]) or "orbitalapi"
                repo = clean(q.get("repo", ["orbital"])[0]) or "orbital"
                query = (f'given {{ owner : brain.RepoOwner = "{owner}", '
                         f'repo : brain.RepoName = "{repo}" }}\nfind {{ coding.GithubRepo }}')
                return self._send(200, json.dumps(taxiql(query, 30)))

            self._send(404, json.dumps({"error": "not found"}))
        except urllib.error.HTTPError as e:
            self._send(502, json.dumps({"error": f"Orbital {e.code}: {e.reason}"}))
        except Exception as e:  # noqa: BLE001
            self._send(502, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"The Counterparty Brain → http://localhost:{PORT}  (Orbital {ORBITAL}, risk {RISK_URL})")
    if os.environ.get("SUGGEST_INDEX", "1") == "1":
        threading.Thread(target=_build_name_index, daemon=True).start()  # warm the autocomplete index
    Server(("127.0.0.1", PORT), Handler).serve_forever()
