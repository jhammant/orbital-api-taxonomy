#!/usr/bin/env python3
"""ASK IN ENGLISH — natural-language -> TaxiQL -> a live answer from Orbital.

The Company Brain already exposes three live verticals through Orbital's router:
a company lens (Companies House / GLEIF / the UK distress corpus / a local LLM),
a markets lens (Yahoo quotes), and a coding lens (GitHub). Each is reachable with
a tiny TaxiQL `find { ... }`. This service puts a PLAIN-ENGLISH front door on all
three: you ask a question, it works out WHICH lens you meant and WHAT entity you
named, writes the matching TaxiQL, runs it against Orbital, and hands back the
answer plus the query it generated.

    python3 demo/services/nl_query_service.py        # binds 0.0.0.0:8910

    GET  /ask?q=is%20Tesco%20in%20trouble%3F
    POST /ask            {"q": "price of AAPL"}
        -> 200 {
             "question": "...",
             "intent":   "company" | "ticker" | "repo",
             "target":   { ...the resolved entity... },
             "taxiql":   "given { ... } find { ... }",
             "answer":   { ...the result Orbital returned... },
             "classifier": "llm" | "heuristic",
             "summary":  "one human-readable sentence"
           }

Classification is LLM-first: it asks the LOCAL model (either the brain's own
/narrative service on :8903, which itself fronts LM Studio, or LM Studio direct
on :1234) to label the question as {kind: company|ticker|repo, value}. If the LLM
is unreachable or replies with junk, it falls back to deterministic heuristics
(ticker symbols, `owner/repo` slugs, "in trouble / price of / issues on" cues) so
the endpoint ALWAYS answers. Pure Python stdlib — no dependencies.

Examples it handles:
    "is Tesco in trouble?"          -> company  TESCO PLC      (distress profile + narrative)
    "price of AAPL"                 -> ticker   AAPL           (live Yahoo quote)
    "issues on orbitalapi/orbital"  -> repo     orbitalapi/orbital (open issues + repo facts)
"""
import json
import os
import re
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("NLQ_HOST", "0.0.0.0")
PORT = int(os.environ.get("NLQ_PORT", "8910"))

ORBITAL = os.environ.get("ORBITAL_URL", "http://localhost:9022") + "/api/taxiql"
# The brain's own LLM micro-service (typed /narrative on :8903) — we (ab)use it as a
# generic local-LLM classifier by feeding it a "facts" line that asks for a label.
LLM_NARRATIVE = os.environ.get("LLM_URL", "http://localhost:8903") + "/narrative"
# LM Studio direct (OpenAI-compatible) — second choice if /narrative is down.
LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO_CHAT = LMSTUDIO + "/v1/chat/completions"
LMSTUDIO_MODELS = LMSTUDIO + "/v1/models"
PREFERRED_MODEL = os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3-coder-next")

# --------------------------------------------------------------------------- #
# Input hygiene
# --------------------------------------------------------------------------- #
# Whitelist for values that get spliced into a TaxiQL string literal. We keep the
# slash so an `owner/repo` slug survives; everything is bounded in length.
_LITERAL_RE = re.compile(r"[^A-Za-z0-9 &.,()\-_/]")


def clean_literal(s: str, n: int = 80) -> str:
    return _LITERAL_RE.sub("", s or "").strip()[:n]


def clean_question(s: str, n: int = 240) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()[:n]


# --------------------------------------------------------------------------- #
# Heuristic classifier — the always-available fallback (and a sanity net for the LLM)
# --------------------------------------------------------------------------- #
# owner/repo : two path-ish segments around a single slash (github-style).
_REPO_RE = re.compile(r"\b([A-Za-z0-9][\w.\-]{0,38})/([A-Za-z0-9][\w.\-]{0,99})\b")
# A bare ticker: 1-5 upper-case letters, optionally with a .EXCHANGE suffix.
_TICKER_RE = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z]{1,4})?)\b")
# A UK Companies House number: 8 digits, or a 2-letter prefix (SC/NI/OC/...) + 6 digits.
_CH_NUMBER_RE = re.compile(r"\b([0-9]{8}|[A-Z]{2}[0-9]{6})\b")

# Cue words that pull a question towards one vertical.
_PRICE_CUES = ("price", "quote", "stock", "ticker", "share price", "shares",
               "trading at", "how much is", "worth", "market cap")
_REPO_CUES = ("repo", "repository", "issue", "issues", "github", "pull request",
              "pr ", "prs", "open issues", "bugs on", "stars")
_COMPANY_CUES = ("in trouble", "trouble", "insolven", "distress", "going bust",
                 "bankrupt", "administration", "liquidation", "winding up",
                 "winding-up", "risk", "company", "ltd", "limited", "plc",
                 "about ", "tell me about", "how is", "status of", "healthy",
                 "going under", "failing")

# Common words that look like tickers but almost never are — don't misfire on these.
_TICKER_STOPWORDS = {
    "I", "A", "AN", "THE", "IS", "IN", "ON", "OF", "TO", "IT", "AT", "OR", "AS",
    "BE", "DO", "GO", "ME", "MY", "WE", "US", "PLC", "LTD", "LLC", "INC", "CO",
    "AND", "FOR", "ARE", "WAS", "HOW", "WHO", "WHY", "PRICE", "STOCK", "REPO",
    "OK", "PR", "PRS", "API", "URL", "OK", "UK", "USA", "CEO", "CFO", "IPO",
}

# A few well-known names -> their canonical Companies-House-style display name, so
# "is tesco in trouble" types as "TESCO PLC" (GLEIF/Companies House resolve from there).
_COMPANY_CANON = {
    "tesco": "TESCO PLC",
    "monzo": "MONZO BANK LIMITED",
    "barclays": "BARCLAYS PLC",
    "vodafone": "VODAFONE GROUP PUBLIC LIMITED COMPANY",
    "bp": "BP P.L.C.",
    "shell": "SHELL PLC",
    "rolls royce": "ROLLS-ROYCE HOLDINGS PLC",
    "rolls-royce": "ROLLS-ROYCE HOLDINGS PLC",
    "sainsbury": "SAINSBURY'S SUPERMARKETS LTD",
    "sainsburys": "SAINSBURY'S SUPERMARKETS LTD",
    "marks and spencer": "MARKS AND SPENCER PLC",
    "greggs": "GREGGS PLC",
    "ocado": "OCADO GROUP PLC",
    "asos": "ASOS PLC",
    "boohoo": "BOOHOO GROUP PLC",
}

# Phrase fragments to peel off the front/back when extracting a company name.
_STRIP_PHRASES = [
    "is ", "are ", "how is ", "how are ", "what is the status of ",
    "what's the status of ", "status of ", "tell me about ", "tell me ",
    "about ", "what about ", "how's ", "whats up with ", "what's up with ",
    "is the company ", "the company ",
]
_STRIP_TAIL = [
    " in trouble", " in any trouble", " in distress", " distressed",
    " going bust", " going under", " bankrupt", " insolvent", " ok",
    " okay", " doing", " doing ok", " healthy", " at risk", " failing",
    " safe", " in administration", " a safe bet",
]


def _looks_like_ticker(tok: str) -> bool:
    return tok.upper() not in _TICKER_STOPWORDS


def _canon_company(name: str) -> str:
    """Map a known short alias to its Companies-House-style display name.

    "Tesco" / "tesco plc" -> "TESCO PLC" so GLEIF resolves a registration number
    and the distress lens populates. Unknown names are upper-cased and returned
    unchanged (GLEIF still does a fuzzy match on many of them).
    """
    low = (name or "").lower().strip()
    if not low:
        return "TESCO PLC"
    for alias, canon in _COMPANY_CANON.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return canon
    return name.upper().strip()


def _extract_company_name(q: str) -> str:
    """Best-effort plain-English -> a company display name."""
    low = q.lower().strip().rstrip("?.! ")
    # Known aliases win outright.
    for alias in _COMPANY_CANON:
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return _COMPANY_CANON[alias]
    s = low
    for p in _STRIP_PHRASES:
        if s.startswith(p):
            s = s[len(p):]
            break
    for t in _STRIP_TAIL:
        if s.endswith(t):
            s = s[: -len(t)]
            break
    # Drop trailing question cue words.
    s = re.sub(r"\b(in trouble|trouble|distress|distressed|ok|okay|healthy|"
               r"bankrupt|insolvent|failing|going (bust|under))\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip(" ?.!,")
    if not s:
        s = re.sub(r"\b(is|are|the|in|trouble|about)\b", "", low).strip(" ?.!,")
    return s.upper().strip() or "TESCO PLC"


def heuristic_classify(q: str) -> dict:
    """Deterministic fallback. Returns {kind, value}; never fails."""
    low = q.lower()

    # 0) A bare Companies House number => company lens BY NUMBER (skips GLEIF, goes
    #    straight into the distress corpus — so "is 08810995 in trouble?" works even
    #    for small private companies GLEIF has no LEI for).
    chm = _CH_NUMBER_RE.search(q.upper())
    if chm and not any(c in low for c in _REPO_CUES):
        num = chm.group(1)
        return {"kind": "company", "value": num, "companyNumber": num}

    # 1) An explicit owner/repo slug, or a repo cue word, => coding lens.
    m = _REPO_RE.search(q)
    repo_cue = any(c in low for c in _REPO_CUES)
    if m and (repo_cue or "/" in q):
        owner, repo = m.group(1), m.group(2)
        # Guard against false positives like "and/or", "TCP/IP" with no repo cue.
        if repo_cue or ("." not in owner and len(owner) > 1):
            return {"kind": "repo", "value": f"{owner}/{repo}",
                    "owner": owner, "repo": repo}

    # 2) Markets cues + a plausible bare ticker => ticker lens.
    price_cue = any(c in low for c in _PRICE_CUES)
    company_cue = any(c in low for c in _COMPANY_CUES)
    tickers = [t for t in _TICKER_RE.findall(q) if _looks_like_ticker(t)]
    if price_cue and tickers:
        return {"kind": "ticker", "value": tickers[0]}
    # A lone all-caps token with no company-ish wording, e.g. "AAPL" or "MSFT?".
    if tickers and not company_cue and not repo_cue:
        stripped = re.sub(r"[^A-Za-z0-9. ]", "", q).strip()
        if stripped.upper() == stripped and len(stripped.split()) <= 2:
            return {"kind": "ticker", "value": tickers[0]}
    if price_cue:
        # Price intent but no clean symbol — take the last upper-ish token.
        cand = tickers[0] if tickers else None
        if cand:
            return {"kind": "ticker", "value": cand}

    # 3) Default: a company question.
    return {"kind": "company", "value": _extract_company_name(q)}


# --------------------------------------------------------------------------- #
# LLM classifier — preferred path
# --------------------------------------------------------------------------- #
_CLASSIFY_INSTRUCTION = (
    "Classify the user's question into exactly one of three kinds and extract the "
    "entity. Reply with ONLY a single minified JSON object, no prose, no markdown:\n"
    '{"kind":"company|ticker|repo","value":"..."}\n'
    "Rules: kind=ticker for stock price / quote questions (value = the symbol in "
    "UPPER CASE, e.g. AAPL). kind=repo for GitHub repository / issues questions "
    '(value = "owner/repo"). kind=company for anything about a company\'s health, '
    "trouble, insolvency, risk, or general info (value = the company name). "
    'Examples: "price of AAPL" -> {"kind":"ticker","value":"AAPL"}; '
    '"is Tesco in trouble?" -> {"kind":"company","value":"Tesco"}; '
    '"issues on orbitalapi/orbital" -> {"kind":"repo","value":"orbitalapi/orbital"}.'
)


def _extract_json_obj(text: str):
    """Pull the first {...} JSON object out of a possibly-chatty completion."""
    if not text:
        return None
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def _classify_via_narrative(q: str):
    """Use the brain's own :8903 /narrative source as a generic classifier.

    /narrative emits {summary}; we pack the classify instruction into `facts` and
    ask for the label as the 'narrative'. Works whenever LM Studio is up behind it.
    """
    payload = {
        "name": "QUESTION-ROUTER",
        "facts": f"{_CLASSIFY_INSTRUCTION}\n\nUser question: {q}\n\nJSON:",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLM_NARRATIVE, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode("utf-8"))
    # Only trust it if LM Studio actually answered (source == llm); the templated
    # fallback can't classify, so let our own heuristics handle that case instead.
    if body.get("source") != "llm":
        raise ValueError("narrative service returned its template fallback")
    return _extract_json_obj(body.get("summary", ""))


def _pick_lmstudio_model() -> str:
    try:
        with urllib.request.urlopen(LMSTUDIO_MODELS, timeout=4) as r:
            ids = [m.get("id", "") for m in json.loads(r.read()).get("data", [])]
        if PREFERRED_MODEL in ids:
            return PREFERRED_MODEL
        for mid in ids:
            if "embed" not in mid.lower():
                return mid
    except Exception:  # noqa: BLE001
        pass
    return PREFERRED_MODEL


def _classify_via_lmstudio(q: str):
    """Second choice: hit LM Studio's chat endpoint directly."""
    payload = {
        "model": _pick_lmstudio_model(),
        "temperature": 0,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": _CLASSIFY_INSTRUCTION},
            {"role": "user", "content": q},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LMSTUDIO_CHAT, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode("utf-8"))
    return _extract_json_obj(body["choices"][0]["message"]["content"])


def _normalise_label(obj) -> dict | None:
    """Validate an LLM label and reshape it into our internal {kind,value,...}."""
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("kind", "")).strip().lower()
    value = str(obj.get("value", "")).strip()
    if kind not in ("company", "ticker", "repo") or not value:
        return None
    if kind == "ticker":
        sym = clean_literal(value, 12).upper().replace(" ", "")
        return {"kind": "ticker", "value": sym} if sym else None
    if kind == "repo":
        slug = clean_literal(value, 120)
        m = _REPO_RE.search(slug) or _REPO_RE.search(slug.replace(" ", "/"))
        if not m:
            return None
        return {"kind": "repo", "value": f"{m.group(1)}/{m.group(2)}",
                "owner": m.group(1), "repo": m.group(2)}
    # Company: canonicalise a known short alias (e.g. the LLM says "Tesco") to its
    # Companies-House-style name so GLEIF can resolve a registration number and the
    # distress lens populates — otherwise pass the cleaned name through as-is.
    canon = _canon_company(clean_literal(value, 80))
    return {"kind": "company", "value": canon}


def classify(q: str) -> tuple[dict, str]:
    """Return (label, source) where source is 'llm' or 'heuristic'.

    Tries the local LLM (narrative service, then LM Studio direct); validates its
    answer; on any failure or junk output, falls back to deterministic heuristics
    so we ALWAYS produce a usable label.
    """
    for fn in (_classify_via_narrative, _classify_via_lmstudio):
        try:
            label = _normalise_label(fn(q))
            if label:
                # If the question names a Companies House number and the LLM agreed
                # it's a company, carry the number through so we can query by id
                # (the distress corpus answers without needing a GLEIF/LEI hop).
                if label["kind"] == "company":
                    chm = _CH_NUMBER_RE.search(q.upper())
                    if chm:
                        label["companyNumber"] = chm.group(1)
                return label, "llm"
        except Exception:  # noqa: BLE001
            continue
    return heuristic_classify(q), "heuristic"


# --------------------------------------------------------------------------- #
# TaxiQL builders — one per vertical. These mirror examples/orbital/*.taxi exactly.
# --------------------------------------------------------------------------- #
def build_taxiql(label: dict) -> tuple[str, dict]:
    """Return (taxiql_query, resolved_target) for a classified label."""
    kind = label["kind"]
    if kind == "ticker":
        sym = clean_literal(label["value"], 12).upper() or "AAPL"
        q = (f'given {{ ticker : brain.StockTicker = "{sym}" }}\n'
             f'find {{ markets.StockQuote }}')
        return q, {"kind": "ticker", "ticker": sym}
    if kind == "repo":
        owner = clean_literal(label.get("owner") or label["value"].split("/")[0], 39)
        repo = clean_literal(label.get("repo") or label["value"].split("/")[-1], 100)
        owner = owner or "orbitalapi"
        repo = repo or "orbital"
        q = (f'given {{ owner : brain.RepoOwner = "{owner}", '
             f'repo : brain.RepoName = "{repo}" }}\n'
             f'find {{\n'
             f'  repo : coding.GithubRepo\n'
             f'  openIssues : coding.GithubIssue[]\n'
             f'}}')
        return q, {"kind": "repo", "owner": owner, "repo": repo,
                   "fullName": f"{owner}/{repo}"}
    # company: distress lens + LLM risk narrative. If we already have a Companies
    # House number, query straight off it (CompanyRegistrationNumber) — skips GLEIF
    # so the distress corpus answers even for entities that have no LEI. Otherwise
    # start from the name and let GLEIF/Companies House resolve the number.
    num = label.get("companyNumber")
    if num and _CH_NUMBER_RE.fullmatch(num.upper()):
        cn = num.upper()
        q = (f'given {{ id : uk.gov.CompanyRegistrationNumber = "{cn}" }}\n'
             f'find {{ insolvency : brain.insolvency.InsolvencyProfile }}')
        return q, {"kind": "company", "companyNumber": cn}
    name = clean_literal(label["value"], 80).upper() or "TESCO PLC"
    q = (f'given {{ name : uk.gov.CompanyName = "{name}" }}\n'
         f'find {{\n'
         f'  insolvency : brain.insolvency.InsolvencyProfile\n'
         f'  narrative : brain.RiskNarrative\n'
         f'}}')
    return q, {"kind": "company", "name": name}


# --------------------------------------------------------------------------- #
# Orbital call + answer shaping
# --------------------------------------------------------------------------- #
def run_taxiql(query: str, timeout: int = 120):
    req = urllib.request.Request(
        ORBITAL, data=query.encode("utf-8"),
        headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _summarise(intent: str, target: dict, answer) -> str:
    """One plain-English sentence describing what came back."""
    try:
        if intent == "ticker":
            if isinstance(answer, dict) and answer.get("price") is not None:
                cur = answer.get("currency", "")
                exch = answer.get("exchange", "")
                tail = f" on {exch}" if exch else ""
                return (f"{target.get('ticker')} is trading at "
                        f"{answer['price']} {cur}{tail}.").strip()
            return f"No live quote was returned for {target.get('ticker')}."
        if intent == "repo":
            repo = answer.get("repo") if isinstance(answer, dict) else None
            issues = answer.get("openIssues") if isinstance(answer, dict) else None
            full = target.get("fullName")
            if isinstance(repo, dict):
                n = repo.get("openIssues")
                shown = len(issues) if isinstance(issues, list) else 0
                stars = repo.get("stars")
                bits = []
                if stars is not None:
                    bits.append(f"{stars} stars")
                if n is not None:
                    bits.append(f"{n} open issues")
                meta = ", ".join(bits)
                lead = f"{full}: {meta}." if meta else f"{full}."
                if shown:
                    lead += f" Showing {shown} issue(s)."
                return lead
            return f"Fetched repository facts for {full}."
        # company
        inso = answer.get("insolvency") if isinstance(answer, dict) else None
        narr = answer.get("narrative") if isinstance(answer, dict) else None
        name = (inso or {}).get("companyName") or target.get("name")
        stage = (inso or {}).get("stage") or ""
        in_trouble = bool(stage) and stage.lower() not in (
            "no insolvency on record", "invalid company number")
        if in_trouble:
            ev = (inso or {}).get("latestEvent") or "an insolvency event"
            when = (inso or {}).get("eventDate") or ""
            when = f" ({when[:10]})" if when else ""
            return (f"{name} shows DISTRESS: {stage} — latest event "
                    f"{ev}{when}.")
        line = f"{name}: no insolvency on record (looks healthy)."
        if isinstance(narr, dict) and narr.get("summary"):
            line += " " + narr["summary"]
        return line
    except Exception:  # noqa: BLE001
        return "Query ran; see the answer object for the raw result."


def ask(question: str) -> dict:
    """End-to-end: classify -> build TaxiQL -> run on Orbital -> shape the answer.

    Never raises: a failed Orbital call is reported inside the JSON so the front
    door always returns 200 with the generated query for transparency.
    """
    q = clean_question(question)
    label, source = classify(q)
    taxiql, target = build_taxiql(label)
    intent = label["kind"]
    out = {
        "question": q,
        "intent": intent,
        "target": target,
        "taxiql": taxiql,
        "classifier": source,
    }
    try:
        answer = run_taxiql(taxiql, 120 if intent == "company" else 40)
        out["answer"] = answer
        out["summary"] = _summarise(intent, target, answer)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        out["answer"] = None
        out["error"] = f"Orbital HTTP {e.code}: {e.reason} {detail}".strip()
        out["summary"] = f"Generated the TaxiQL but Orbital returned {e.code}."
    except Exception as e:  # noqa: BLE001
        out["answer"] = None
        out["error"] = str(e)
        out["summary"] = "Generated the TaxiQL but the Orbital call failed."
    return out


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "orbital": ORBITAL})
        if u.path != "/ask":
            return self._send(404, {"error": "not found",
                                    "usage": "GET/POST /ask?q=<english question>"})
        q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
        if not q.strip():
            return self._send(400, {"error": "q is required",
                                    "example": "/ask?q=is Tesco in trouble?"})
        return self._send(200, ask(q))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/ask":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8") if n else ""
        except Exception:  # noqa: BLE001
            raw = ""
        q = ""
        if raw:
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    q = body.get("q") or body.get("question") or ""
            except Exception:  # noqa: BLE001
                q = urllib.parse.parse_qs(raw).get("q", [""])[0]
        if not q.strip():
            return self._send(400, {"error": "q is required (JSON {\"q\":...} or form q=)"})
        return self._send(200, ask(q))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"ASK IN ENGLISH -> http://{HOST}:{PORT}/ask?q=is%20Tesco%20in%20trouble%3F")
    print(f"  Orbital: {ORBITAL}")
    print(f"  classifier: LLM via {LLM_NARRATIVE} / {LMSTUDIO_CHAT}, heuristic fallback")
    Server((HOST, PORT), Handler).serve_forever()
