#!/usr/bin/env python3
"""AI RISK NARRATIVE — the LLM-grounded prose layer for Orbital Risk.

Routes a company name into the counterparty RISK engine (demo/services/risk_service.py,
:8917), then hands ONLY those hard facts (band, score, verdict, red flags, controllers)
to a LOCAL LM Studio model and gets back a TIGHT, plain-English counterparty-risk
narrative plus a one-line recommendation. The model is instructed to stay strictly
grounded in the supplied facts and invent nothing; if the LLM is unreachable the
endpoint still returns 200 with a clean deterministic narrative built from the facts.

To Orbital this is just another typed HTTP source emitting String/Decimal fields, so
the generated narrative composes into any TaxiQL find { ... } on the company spine.

Pure Python stdlib — no dependencies. Read-only (calls the risk service + the LLM).

    python3 demo/services/narrative_service.py        # binds 0.0.0.0:8924

    GET /narrative?q=PROMOAT%20LIMITED
    ->  { company, band, score, narrative, recommendation, model, source }

Mirrors llm_service.py's LM Studio call (OpenAI-compatible /v1/chat/completions with
model auto-detect) and risk_service.py's server shape (ThreadingTCPServer, CORS,
quiet logging, 0.0.0.0 bind, port from env).
"""
import json
import os
import re
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("NARRATIVE_HOST", "0.0.0.0")
PORT = int(os.environ.get("NARRATIVE_PORT", "8924"))

# Upstream RISK engine (already running) — source of the hard facts we ground on.
RISK_URL = os.environ.get("RISK_URL", "http://127.0.0.1:8917") + "/risk"
RISK_TIMEOUT = int(os.environ.get("NARRATIVE_RISK_TIMEOUT", "30"))

# LOCAL LM Studio (OpenAI-compatible), reusing llm_service.py's mechanism.
LMSTUDIO_BASE = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
LMSTUDIO = LMSTUDIO_BASE + "/v1/chat/completions"
MODELS_URL = LMSTUDIO_BASE + "/v1/models"
PREFERRED_MODEL = os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3-coder-next")
LLM_TIMEOUT = int(os.environ.get("NARRATIVE_LLM_TIMEOUT", "60"))
LLM_MAX_TOKENS = int(os.environ.get("NARRATIVE_MAX_TOKENS", "180"))


def clean(s, n=2000):
    """Whitelist text destined for an LLM prompt and a JSON body."""
    return re.sub(r"[^\w \t&.,:;()\-_/%'\"]", "", s or "").strip()[:n]


def pick_model():
    """Use PREFERRED_MODEL if LM Studio has it loaded; else first chat-capable model."""
    try:
        with urllib.request.urlopen(MODELS_URL, timeout=4) as r:
            ids = [m.get("id", "") for m in json.loads(r.read()).get("data", [])]
        if PREFERRED_MODEL in ids:
            return PREFERRED_MODEL
        for mid in ids:
            if "embed" in mid.lower():
                continue
            return mid
    except Exception:  # noqa: BLE001
        pass
    return PREFERRED_MODEL


def fetch_risk(name):
    """Call the running RISK engine for a company name; return its JSON dict (or {})."""
    qs = urllib.parse.urlencode({"q": name})
    req = urllib.request.Request(f"{RISK_URL}?{qs}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=RISK_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def facts_block(risk):
    """Render the risk facts into a compact, unambiguous block for the LLM prompt.

    Only fields the RISK engine actually returned are included, so the model has
    nothing to hallucinate from. Flags/controllers are flattened to short lines.
    """
    band = risk.get("band") or "UNKNOWN"
    score = risk.get("score")
    lines = [
        f"Company: {risk.get('companyName') or risk.get('companyNumber') or 'Unknown'}",
        f"Risk band: {band}",
        f"Risk score: {score}/100" if score is not None else "Risk score: n/a",
    ]
    verdict = (risk.get("verdict") or "").strip()
    if verdict:
        lines.append(f"Engine verdict: {verdict}")

    flags = risk.get("flags") or []
    if flags:
        lines.append("Red flags:")
        for f in flags[:6]:
            label = (f.get("label") or "").strip()
            detail = (f.get("detail") or "").strip()
            sev = (f.get("sev") or "").strip()
            bits = " - ".join(b for b in (label, detail) if b)
            lines.append(f"  - [{sev or 'info'}] {bits}" if bits else f"  - [{sev or 'info'}]")
    else:
        lines.append("Red flags: none recorded")

    ctrls = risk.get("controllers") or []
    if ctrls:
        names = [c.get("name") for c in ctrls if c.get("name")]
        if names:
            lines.append("Controllers: " + ", ".join(names[:6]))
    else:
        lines.append("Controllers: none on record")

    return clean("\n".join(lines), 1800)


def call_llm(company, band, facts):
    """Return (narrative, recommendation, model) from LM Studio, or raise on failure."""
    model = pick_model()
    payload = {
        "model": model,
        "temperature": 0.15,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": (
                "You are a counterparty-risk analyst writing for a credit/onboarding "
                "decision-maker. You are given a company and a block of ALREADY-VERIFIED "
                "risk facts. Write a TIGHT plain-English narrative of 2-3 sentences that "
                "explains the counterparty risk, then a separate one-line recommendation. "
                "Rules: use ONLY the supplied facts; do NOT invent numbers, names, dates, "
                "regulators, events or sources; do not speculate beyond the facts; no "
                "markdown, no headings, no bullet points, no preamble. "
                "Respond as STRICT JSON only, exactly: "
                '{"narrative": "<2-3 sentences>", "recommendation": "<one line>"}')},
            {"role": "user", "content": (
                f"Risk band for {company}: {band}.\n\n"
                f"Verified facts:\n{facts}\n\n"
                "Write the grounded counterparty-risk narrative and recommendation now "
                "as the strict JSON object.")},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LMSTUDIO, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    raw = body["choices"][0]["message"]["content"].strip()
    narrative, recommendation = _parse_llm_text(raw)
    if not narrative:
        raise ValueError("empty LLM narrative")
    return narrative, recommendation, model


def _parse_llm_text(raw):
    """Pull narrative + recommendation out of the model output.

    The model is asked for strict JSON, but local models sometimes wrap it in
    prose or code fences, so we degrade gracefully: try JSON first (even if
    embedded), else fall back to treating the whole cleaned text as the narrative.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # Try a direct or embedded JSON object.
    candidates = [text]
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            nar = _flatten(obj.get("narrative"))
            rec = _flatten(obj.get("recommendation"))
            if nar:
                return _norm(nar), _norm(rec)
    # No usable JSON — use the whole thing as the narrative.
    return _norm(text), ""


def _flatten(v):
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(_flatten(x) for x in v)
    if v is None:
        return ""
    return str(v)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def fallback_narrative(risk):
    """Deterministic, fact-grounded narrative when the LLM is unreachable.

    Built strictly from the risk engine's own fields — no invention — so the
    endpoint always returns something honest and useful.
    """
    company = risk.get("companyName") or risk.get("companyNumber") or "This company"
    band = (risk.get("band") or "UNKNOWN").upper()
    score = risk.get("score")
    flags = risk.get("flags") or []
    details = [(_norm(f.get("detail")) or _norm(f.get("label"))) for f in flags]
    details = [d for d in details if d]

    score_phrase = f" (risk score {score}/100)" if score is not None else ""
    if band == "RED":
        opening = (f"{company} screens RED for counterparty risk{score_phrase}.")
    elif band == "AMBER":
        opening = (f"{company} screens AMBER for counterparty risk{score_phrase}, "
                   "indicating signals that warrant review.")
    elif band == "GREEN":
        opening = (f"{company} screens GREEN for counterparty risk{score_phrase}, "
                   "with no material risk signals on the automated checks.")
    else:
        opening = f"{company} could not be conclusively scored for counterparty risk."

    if details:
        drivers = "; ".join(details[:2])
        body = f" The key driver{'s' if len(details[:2]) > 1 else ''}: {drivers}."
    elif band == "GREEN":
        body = " It is clean on distress, director-network and registered-address checks."
    else:
        body = ""

    narrative = _norm(opening + body)

    rec = _norm(risk.get("recommendation") or "")
    if not rec:
        rec = {
            "RED": "Do not proceed without senior sign-off / enhanced due diligence.",
            "AMBER": "Manual review recommended before proceeding.",
            "GREEN": "No blockers found on automated checks.",
        }.get(band, "Insufficient data — verify the counterparty before proceeding.")
    return narrative, rec


def build(name):
    """Assemble the grounded narrative response; never raises (always returns 200-able dict)."""
    company_in = clean(name, 160) or "Unknown company"
    try:
        risk = fetch_risk(name)
    except Exception as e:  # noqa: BLE001
        # Risk engine unreachable — return an honest minimal payload, no invention.
        return {
            "company": company_in,
            "band": "UNKNOWN",
            "score": None,
            "narrative": (f"Counterparty risk for {company_in} could not be assessed: "
                          "the risk engine was unreachable."),
            "recommendation": "Retry once the risk service is available before proceeding.",
            "model": "none",
            "source": "error",
            "note": f"risk service unreachable: {e}",
        }

    company = risk.get("companyName") or company_in
    band = risk.get("band") or "UNKNOWN"
    score = risk.get("score")
    facts = facts_block(risk)

    try:
        narrative, recommendation, model = call_llm(company, band, facts)
        if not recommendation:
            recommendation = _norm(risk.get("recommendation") or "") or \
                "Review the underlying risk facts before proceeding."
        source = "llm"
    except Exception as e:  # noqa: BLE001
        narrative, recommendation = fallback_narrative(risk)
        model = "template"
        source = "fallback"
        return {
            "company": company, "band": band, "score": score,
            "narrative": narrative, "recommendation": recommendation,
            "model": model, "source": source,
            "note": f"LM Studio unreachable: {e}",
        }

    return {
        "company": company, "band": band, "score": score,
        "narrative": narrative, "recommendation": recommendation,
        "model": model, "source": source,
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
            return self._send(200, {"ok": True, "model": pick_model(), "risk": RISK_URL})
        if u.path != "/narrative":
            return self._send(404, {"error": "not found"})
        q = urllib.parse.parse_qs(u.query)
        name = (q.get("q", [""])[0] or q.get("name", [""])[0] or "").strip()
        if not name:
            return self._send(200, {
                "company": "", "band": "UNKNOWN", "score": None,
                "narrative": "No company supplied.",
                "recommendation": "Provide a company name (?q=NAME).",
                "model": "none", "source": "empty",
            })
        try:
            return self._send(200, build(name))
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"AI risk narrative -> http://{HOST}:{PORT}/narrative?q=PROMOAT%20LIMITED")
    print(f"  risk facts: {RISK_URL}   LM Studio: {LMSTUDIO}")
    Server((HOST, PORT), Handler).serve_forever()
