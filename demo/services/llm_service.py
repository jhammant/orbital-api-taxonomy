#!/usr/bin/env python3
"""LLM-as-a-typed-source — the brain's *narrative* layer for The Company Brain.

Orbital routes assembled facts (a company name + a bundle of facts) INTO a local
LLM and gets a *typed* risk/business narrative back out. To Orbital this is just
another HTTP source emitting a `brain.NarrativeText` — so the generated prose can
be composed into any TaxiQL `find { ... }` alongside GLEIF, Police.uk, markets, etc.

    GET  /narrative?name=TESCO%20PLC&facts=status:Active;crimes:12
    POST /narrative            { "name": "...", "facts": "..." }
        -> 200 { "summary": "...", "model": "...", "source": "llm|fallback" }

Calls a LOCAL LM Studio (OpenAI-compatible) at http://localhost:1234. If that is
unreachable / errors, it FALLS BACK to a deterministic templated summary so the
endpoint always returns 200 and the demo never breaks. Pure Python stdlib.

    python3 demo/services/llm_service.py     # 127.0.0.1:8903
"""
import json
import os
import re
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

PORT = int(os.environ.get("LLM_PORT", "8903"))
LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234") + "/v1/chat/completions"
MODELS_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234") + "/v1/models"
# Preferred model; if not loaded we auto-detect the first non-embedding model.
PREFERRED_MODEL = os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3-coder-next")


def clean(s: str, n: int = 600) -> str:
    """Whitelist input — these strings end up in an LLM prompt and a JSON body."""
    return re.sub(r"[^\w \t&.,:;()\-_/%'\"]", "", s or "").strip()[:n]


def pick_model() -> str:
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


def call_llm(name: str, facts: str):
    """Return (summary, model_id) from LM Studio, or raise on any failure."""
    model = pick_model()
    facts_line = facts if facts else "(no extra facts supplied)"
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 220,
        "messages": [
            {"role": "system", "content": (
                "You are a risk analyst. Given a company and a few assembled facts, "
                "write ONE tight paragraph (max 70 words) summarising the company and "
                "any risk signals. Plain prose, no preamble, no markdown, no lists.")},
            {"role": "user", "content": (
                f"Company: {name}\nAssembled facts: {facts_line}\n\nWrite the narrative.")},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LMSTUDIO, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        body = json.loads(r.read().decode("utf-8"))
    summary = body["choices"][0]["message"]["content"].strip()
    summary = re.sub(r"\s+", " ", summary).strip()
    if not summary:
        raise ValueError("empty LLM completion")
    return summary, model


def fallback(name: str, facts: str) -> str:
    """Deterministic templated narrative — always available, no LLM required."""
    if facts:
        return (f"{name}: assembled risk snapshot — {facts}. No live LLM was reachable, "
                f"so this is a deterministic summary of the routed facts; review the "
                f"underlying sources before acting.")
    return (f"{name}: no adverse facts were routed in for this entity. No live LLM was "
            f"reachable, so this is a deterministic placeholder narrative.")


def narrative(name: str, facts: str):
    """Return a JSON-able dict; never raises — falls back so callers always get 200."""
    name = clean(name, 120) or "Unknown company"
    facts = clean(facts, 600)
    try:
        summary, model = call_llm(name, facts)
        return {"summary": summary, "model": model, "source": "llm"}
    except Exception as e:  # noqa: BLE001
        return {"summary": fallback(name, facts), "model": "template",
                "source": "fallback", "note": f"LM Studio unreachable: {e}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {"ok": True, "model": pick_model()})
        if u.path == "/narrative":
            name = q.get("name", [""])[0]
            facts = q.get("facts", [""])[0]
            return self._send(200, narrative(name, facts))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/narrative":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            body = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            body = {}
        # Accept JSON body, or form-encoded as a fallback.
        if not isinstance(body, dict):
            body = {}
        name = body.get("name") or ""
        facts = body.get("facts") or ""
        if not name and raw:
            form = urllib.parse.parse_qs(raw)
            name = form.get("name", [""])[0]
            facts = form.get("facts", [""])[0]
        self._send(200, narrative(name, facts))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"LLM typed-source -> http://127.0.0.1:{PORT}/narrative  (LM Studio: {LMSTUDIO})")
    Server(("127.0.0.1", PORT), Handler).serve_forever()
